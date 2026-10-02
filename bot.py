import asyncio
import gc
import json
import logging
import multiprocessing as mp
import os
import queue
import re
import time
from pathlib import Path

from aiohttp import web
import discord
from discord.ext import commands

BASE_DIR = Path(__file__).resolve().parent
DICTIONARY_PATH = BASE_DIR / "dictionary.json"
INDEX_PATH = BASE_DIR / "index.html"
JAVIS_VERSION = os.getenv("JAVIS_VERSION", "1.0.7")

PORT = int(os.getenv("PORT", "10000"))
MAX_INPUT_CHARS = int(os.getenv("MAX_INPUT_CHARS", "1200"))
TRANSLATION_TIMEOUT_SECONDS = int(os.getenv("TRANSLATION_TIMEOUT_SECONDS", "120"))
TRANSLATION_LOCK = asyncio.Lock()

# Keep runtime defaults here as a safety net. start.py sets these before this
# file is imported, and setdefault keeps explicit Render values intact.
os.environ.setdefault("ARGOS_DEVICE_TYPE", "cpu")
os.environ.setdefault("ARGOS_COMPUTE_TYPE", "int8")
os.environ.setdefault("ARGOS_INTER_THREADS", "1")
os.environ.setdefault("ARGOS_INTRA_THREADS", "1")
os.environ.setdefault("ARGOS_BATCH_SIZE", "1")
os.environ.setdefault("ARGOS_BEAM_SIZE", "2")
os.environ.setdefault("ARGOS_CHUNK_TYPE", "MINISBD")
os.environ.setdefault(
    "ARGOS_PACKAGE_INDEX",
    "https://raw.githubusercontent.com/argosopentech/argospm-index/main",
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)


class SuppressUnusedVoiceDependencyWarning(logging.Filter):
    """Hide only optional voice warnings; keep all other Discord logs."""

    _IGNORED = {
        "PyNaCl is not installed, voice will NOT be supported",
        "davey is not installed, voice will NOT be supported",
    }

    def filter(self, record: logging.LogRecord) -> bool:
        return record.getMessage() not in self._IGNORED


logging.getLogger("discord.client").addFilter(SuppressUnusedVoiceDependencyWarning())
log = logging.getLogger("JAVIS")

SUPPORTED = {"en", "th", "ko"}
LANG_NAMES = {"en": "EN", "th": "TH", "ko": "KO"}


def load_dictionary() -> list[dict]:
    with DICTIONARY_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("dictionary.json must contain a JSON array")
    return data


TWOM_DICTIONARY = load_dictionary()


class Protector:
    """Protect URLs, mentions, custom emoji, and TWOM terms from translation."""

    def __init__(self):
        self.replacements: dict[str, str] = {}
        self.counter = 0

    def add(self, original: str, prefix: str = "JAVISX") -> str:
        token = f"{prefix}{self.counter}QZ"
        self.counter += 1
        self.replacements[token] = original
        return token

    def protect_regex(self, text: str, pattern: str, flags: int = 0) -> str:
        regex = re.compile(pattern, flags)

        def repl(match: re.Match) -> str:
            return self.add(match.group(0))

        return regex.sub(repl, text)

    def restore(self, text: str) -> str:
        for token, original in sorted(self.replacements.items(), key=lambda x: -len(x[0])):
            text = text.replace(token, original)
        return text


def detect_language(text: str) -> str:
    explicit = re.match(r"^\s*\[(EN|TH|KO)\]\s*", text, re.IGNORECASE)
    if explicit:
        return explicit.group(1).lower()

    thai = len(re.findall(r"[\u0E00-\u0E7F]", text))
    hangul = len(re.findall(r"[\uAC00-\uD7AF\u1100-\u11FF]", text))
    latin = len(re.findall(r"[A-Za-z]", text))

    if thai > 0 and thai >= max(hangul, latin // 2):
        return "th"
    if hangul > 0 and hangul >= max(thai, latin // 2):
        return "ko"
    return "en"


def strip_language_prefix(text: str) -> str:
    return re.sub(r"^\s*\[(EN|TH|KO)\]\s*", "", text, count=1, flags=re.IGNORECASE)


def protect_common_content(text: str, protector: Protector) -> str:
    text = protector.protect_regex(text, r"https?://[^\s<>]+", re.IGNORECASE)
    text = protector.protect_regex(text, r"<@!?\d+>")
    text = protector.protect_regex(text, r"<a?:[A-Za-z0-9_]+:\d+>")
    return text


def protect_twom_terms(text: str, source_lang: str, protector: Protector) -> str:
    entries = []
    for item in TWOM_DICTIONARY:
        if not isinstance(item, dict):
            continue
        aliases = item.get(source_lang, [])
        if isinstance(aliases, str):
            aliases = [aliases]
        for alias in aliases:
            if isinstance(alias, str) and alias.strip():
                entries.append(alias.strip())

    entries.sort(key=len, reverse=True)
    for alias in entries:
        if re.search(r"[A-Za-z]", alias):
            pattern = rf"(?<![A-Za-z0-9_]){re.escape(alias)}(?![A-Za-z0-9_])"
            text = protector.protect_regex(text, pattern, re.IGNORECASE)
        elif alias in text:
            text = text.replace(alias, protector.add(alias))
    return text


def restore_twom_terms(text: str, protector: Protector, target_lang: str) -> str:
    for token, original in list(protector.replacements.items()):
        target = None
        original_lower = original.lower()
        for item in TWOM_DICTIONARY:
            if not isinstance(item, dict):
                continue
            all_aliases = []
            for code in SUPPORTED:
                vals = item.get(code, [])
                if isinstance(vals, str):
                    vals = [vals]
                all_aliases.extend(str(v).lower() for v in vals)
            if original_lower in all_aliases:
                value = item.get(target_lang)
                if isinstance(value, str):
                    target = value
                break
        text = text.replace(token, target if target is not None else original)
    return text


def prepare_for_translation(text: str, source_lang: str) -> tuple[str, Protector]:
    protector = Protector()
    prepared = protect_common_content(text, protector)
    prepared = protect_twom_terms(prepared, source_lang, protector)
    return prepared, protector


# ---------------------------------------------------------------------------
# Dedicated Argos worker process
# ---------------------------------------------------------------------------


def _release_worker_translation(translation: object) -> None:
    """Drop the heavy CTranslate2/SBD objects before the next target loads."""
    try:
        if hasattr(translation, "translator"):
            translation.translator = None
    except Exception:
        pass
    try:
        sentencizer = getattr(translation, "sentencizer", None)
        if sentencizer is not None and hasattr(sentencizer, "detector"):
            sentencizer.detector = None
    except Exception:
        pass
    gc.collect()


def translation_worker_main(request_q, response_q) -> None:
    """Run Argos/CTranslate2 outside the Discord process.

    This is the RAM-critical change in v1.0.7. Only this child process imports
    Argos. It handles one target at a time and releases the CTranslate2 model
    before accepting the next target, while the parent keeps Discord/HTTP alive.
    """
    import argostranslate.translate as argos_translate

    log_worker = logging.getLogger("JAVIS.argos-worker")
    route_cache: dict[tuple[str, str], object] = {}
    installed = None

    # Verify that the requested runtime compression settings reached the worker.
    try:
        from argostranslate import settings as argos_settings
        log_worker.info(
            "Argos worker ready | device=%s | compute_type=%s | batch=%s | beam=%s | chunk=%s",
            getattr(argos_settings, "device", os.getenv("ARGOS_DEVICE_TYPE")),
            getattr(argos_settings, "compute_type", os.getenv("ARGOS_COMPUTE_TYPE")),
            getattr(argos_settings, "batch_size", os.getenv("ARGOS_BATCH_SIZE")),
            getattr(argos_settings, "beam_size", os.getenv("ARGOS_BEAM_SIZE")),
            os.getenv("ARGOS_CHUNK_TYPE", "MINISBD"),
        )
    except Exception:
        log_worker.exception("Could not read Argos runtime settings")

    while True:
        job = request_q.get()
        if job is None:
            break

        request_id, text, source_lang, target_lang = job
        translation = None
        try:
            if installed is None:
                installed = argos_translate.get_installed_languages()

            cache_key = (source_lang, target_lang)
            translation = route_cache.get(cache_key)
            if translation is None:
                from_lang = next((lang for lang in installed if lang.code == source_lang), None)
                to_lang = next((lang for lang in installed if lang.code == target_lang), None)
                translation = from_lang.get_translation(to_lang) if from_lang and to_lang else None

                if translation is None:
                    # One refresh attempt handles stale Argos package/language state.
                    try:
                        argos_translate.get_installed_languages.cache_clear()
                    except Exception:
                        pass
                    installed = argos_translate.get_installed_languages()
                    from_lang = next((lang for lang in installed if lang.code == source_lang), None)
                    to_lang = next((lang for lang in installed if lang.code == target_lang), None)
                    translation = from_lang.get_translation(to_lang) if from_lang and to_lang else None

                if translation is None:
                    pairs = []
                    for lang in installed:
                        for candidate in getattr(lang, "translations_from", []):
                            pairs.append(f"{candidate.from_lang.code}->{candidate.to_lang.code}")
                    raise RuntimeError(
                        f"Argos translation route unavailable: {source_lang}->{target_lang}; "
                        f"installed routes: {', '.join(sorted(set(pairs))) or 'none'}"
                    )
                route_cache[cache_key] = translation

            result = translation.translate(text)
            response_q.put((request_id, True, result, ""))
        except Exception as exc:
            log_worker.exception(
                "Argos worker translation failed | %s->%s",
                source_lang,
                target_lang,
            )
            response_q.put((request_id, False, "", repr(exc)))
        finally:
            if translation is not None:
                _release_worker_translation(translation)
            # Keep route metadata but never a loaded CTranslate2 model resident.
            translation = None
            gc.collect()

    # Drop route objects on worker shutdown.
    route_cache.clear()
    gc.collect()


class ArgosWorkerClient:
    def __init__(self) -> None:
        ctx = mp.get_context("spawn")
        self.request_q = ctx.Queue(maxsize=2)
        self.response_q = ctx.Queue(maxsize=2)
        self.process = ctx.Process(
            target=translation_worker_main,
            args=(self.request_q, self.response_q),
            name="javis-argos-worker",
            daemon=True,
        )
        self.next_request_id = 0

    def start(self) -> None:
        self.process.start()
        log.info(
            "Argos worker process started | pid=%s | compute=%s | device=%s | batch=%s | beam=%s",
            self.process.pid,
            os.getenv("ARGOS_COMPUTE_TYPE", "auto"),
            os.getenv("ARGOS_DEVICE_TYPE", "cpu"),
            os.getenv("ARGOS_BATCH_SIZE", "1"),
            os.getenv("ARGOS_BEAM_SIZE", "2"),
        )

    def ensure_alive(self) -> None:
        if not self.process.is_alive():
            exitcode = self.process.exitcode
            raise RuntimeError(f"Argos worker is not running (exitcode={exitcode})")

    def translate(self, text: str, source_lang: str, target_lang: str) -> str:
        self.ensure_alive()
        request_id = self.next_request_id
        self.next_request_id += 1
        self.request_q.put((request_id, text, source_lang, target_lang), timeout=5)
        deadline = time.monotonic() + TRANSLATION_TIMEOUT_SECONDS
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"Argos worker timed out after {TRANSLATION_TIMEOUT_SECONDS}s "
                    f"({source_lang}->{target_lang})"
                )
            try:
                response_id, ok, result, error = self.response_q.get(timeout=min(2, remaining))
            except queue.Empty:
                self.ensure_alive()
                continue
            if response_id != request_id:
                # Only one Discord translation job is allowed at a time by
                # TRANSLATION_LOCK, so an unexpected ID indicates a worker-state bug.
                raise RuntimeError(
                    f"Argos worker response mismatch: expected={request_id}, got={response_id}"
                )
            if not ok:
                raise RuntimeError(error)
            return str(result)

    def shutdown(self) -> None:
        if self.process.is_alive():
            try:
                self.request_q.put(None, timeout=2)
            except Exception:
                pass
            self.process.join(timeout=5)
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(timeout=2)
        try:
            self.request_q.close()
            self.response_q.close()
        except Exception:
            pass


ARGOS_WORKER: ArgosWorkerClient | None = None


def translate_sync(text: str, source_lang: str, target_lang: str) -> str:
    if ARGOS_WORKER is None:
        raise RuntimeError("Argos worker is not started")
    prepared, protector = prepare_for_translation(text, source_lang)
    result = ARGOS_WORKER.translate(prepared, source_lang, target_lang)
    return restore_twom_terms(result, protector, target_lang)


def translate_all_sync(original: str) -> dict[str, str]:
    source_lang = detect_language(original)
    clean = strip_language_prefix(original).strip()

    if not clean:
        return {"source": source_lang, "en": "", "th": "", "ko": ""}

    log.info("Translating message | source=%s | chars=%d", source_lang, len(clean))

    if source_lang == "en":
        targets = {
            "th": translate_sync(clean, "en", "th"),
            "ko": translate_sync(clean, "en", "ko"),
        }
    elif source_lang == "th":
        targets = {
            "en": translate_sync(clean, "th", "en"),
            "ko": translate_sync(clean, "en", "ko"),
        }
    else:
        targets = {
            "en": translate_sync(clean, "ko", "en"),
            "th": translate_sync(clean, "en", "th"),
        }

    return {"source": source_lang, **targets}


def build_response(result: dict[str, str]) -> str:
    source = result["source"]
    if source == "th":
        return f"[EN] {result['en']}\n[KO] {result['ko']}"
    if source == "en":
        return f"[TH] {result['th']}\n[KO] {result['ko']}"
    return f"[EN] {result['en']}\n[TH] {result['th']}"


async def index(request: web.Request) -> web.Response:
    return web.FileResponse(INDEX_PATH)


async def health(request: web.Request) -> web.Response:
    token_configured = bool(get_discord_token())
    worker_alive = ARGOS_WORKER is not None and ARGOS_WORKER.process.is_alive()
    return web.json_response(
        {
            "ok": token_configured and worker_alive,
            "bot": "JAVIS",
            "version": JAVIS_VERSION,
            "engine": "Argos Translate + CTranslate2",
            "compute_type": os.getenv("ARGOS_COMPUTE_TYPE", "auto"),
            "device_type": os.getenv("ARGOS_DEVICE_TYPE", "cpu"),
            "chunk_type": os.getenv("ARGOS_CHUNK_TYPE", "MINISBD"),
            "batch_size": int(os.getenv("ARGOS_BATCH_SIZE", "1")),
            "beam_size": int(os.getenv("ARGOS_BEAM_SIZE", "2")),
            "translation_worker_alive": worker_alive,
            "discord_token_configured": token_configured,
            "discord_ready": bot.is_ready(),
        },
        status=200 if token_configured and worker_alive else 503,
    )


async def start_http_server() -> web.AppRunner:
    app = web.Application()
    app.add_routes([
        web.get("/", index),
        web.get("/health", health),
        web.get("/ping", health),
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("Health server listening on port %s", PORT)
    return runner


def get_discord_token() -> str:
    return (
        os.getenv("DISCORD_TOKEN", "").strip()
        or os.getenv("DISCORD_BOT_TOKEN", "").strip()
    )


intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!javis ", intents=intents, help_command=None)
ALLOWED_MENTIONS = discord.AllowedMentions.none()


@bot.event
async def on_ready():
    log.info(
        "Logged in as %s (%s) | JAVIS v%s",
        bot.user,
        bot.user.id if bot.user else "?",
        JAVIS_VERSION,
    )
    await bot.change_presence(activity=discord.Game(name="TH ↔ EN ↔ KO | TWOM"))


@bot.command(name="status")
async def status_command(ctx: commands.Context):
    worker_alive = ARGOS_WORKER is not None and ARGOS_WORKER.process.is_alive()
    await ctx.reply(
        "JAVIS พร้อมใช้งาน ✅\n"
        f"Version: v{JAVIS_VERSION}\n"
        "Engine: Argos Translate + CTranslate2\n"
        f"Quantization: {os.getenv('ARGOS_COMPUTE_TYPE', 'auto')}\n"
        f"Device: {os.getenv('ARGOS_DEVICE_TYPE', 'cpu')}\n"
        f"Batch: {os.getenv('ARGOS_BATCH_SIZE', '1')} | Beam: {os.getenv('ARGOS_BEAM_SIZE', '2')}\n"
        f"Chunking: {os.getenv('ARGOS_CHUNK_TYPE', 'MINISBD')}\n"
        f"Argos Worker: {'ONLINE ✅' if worker_alive else 'OFFLINE ❌'}\n"
        "Languages: TH ↔ EN ↔ KO",
        mention_author=False,
        allowed_mentions=ALLOWED_MENTIONS,
    )


@bot.command(name="help")
async def help_command(ctx: commands.Context):
    await ctx.reply(
        "JAVIS Help 📘\n"
        "• ส่งข้อความปกติ: TH→EN+KO / EN→TH+KO / KO→EN+TH\n"
        "• !javis status: ดูสถานะและ Version ของบอท\n"
        "• !javis reload: โหลด TWOM Dictionary ใหม่ (ต้องมี Manage Server)\n"
        "• !javis help: แสดงคำสั่งนี้",
        mention_author=False,
        allowed_mentions=ALLOWED_MENTIONS,
    )


@bot.command(name="reload")
@commands.has_guild_permissions(manage_guild=True)
async def reload_command(ctx: commands.Context):
    global TWOM_DICTIONARY
    try:
        TWOM_DICTIONARY = load_dictionary()
    except Exception as exc:
        log.exception("Dictionary reload failed")
        await ctx.reply(
            f"โหลด TWOM Dictionary ไม่สำเร็จ: `{exc}`",
            mention_author=False,
            allowed_mentions=ALLOWED_MENTIONS,
        )
        return
    await ctx.reply(
        f"โหลด TWOM Dictionary ใหม่แล้ว ✅ ({len(TWOM_DICTIONARY)} entries)",
        mention_author=False,
        allowed_mentions=ALLOWED_MENTIONS,
    )


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingPermissions):
        await ctx.reply(
            "คำสั่งนี้ต้องมีสิทธิ์ Manage Server",
            mention_author=False,
            allowed_mentions=ALLOWED_MENTIONS,
        )
        return
    log.exception("Discord command error", exc_info=error)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # Preserve command handling and do not translate commands.
    if message.content.startswith("!javis"):
        await bot.process_commands(message)
        return

    content = message.content.strip()
    if not content or len(content) > MAX_INPUT_CHARS:
        return
    if content.startswith(("/", "$", "?")):
        return
    if re.fullmatch(r"https?://[^\s]+", content, re.IGNORECASE):
        return

    try:
        async with TRANSLATION_LOCK:
            result = await asyncio.to_thread(translate_all_sync, content)
        response = build_response(result)
        log.info(
            "Translation completed | source=%s | response_chars=%d",
            result["source"],
            len(response),
        )

        if len(response) <= 1900:
            await message.reply(
                response,
                mention_author=False,
                allowed_mentions=ALLOWED_MENTIONS,
            )
        else:
            chunks = []
            current = ""
            for line in response.splitlines():
                candidate = f"{current}\n{line}" if current else line
                if len(candidate) > 1900:
                    if current:
                        chunks.append(current)
                    current = line
                else:
                    current = candidate
            if current:
                chunks.append(current)
            for index, chunk in enumerate(chunks):
                if index == 0:
                    await message.reply(
                        chunk,
                        mention_author=False,
                        allowed_mentions=ALLOWED_MENTIONS,
                    )
                else:
                    await message.channel.send(
                        chunk,
                        allowed_mentions=ALLOWED_MENTIONS,
                    )
    except Exception as exc:
        log.exception("Translation failed")
        await message.reply(
            "JAVIS แปลข้อความนี้ไม่สำเร็จ กรุณาลองใหม่อีกครั้ง",
            mention_author=False,
            allowed_mentions=ALLOWED_MENTIONS,
        )


async def main():
    global ARGOS_WORKER
    discord_token = get_discord_token()
    if not discord_token:
        raise RuntimeError(
            "Discord bot token is missing. Set DISCORD_TOKEN in Render Environment "
            "(or DISCORD_BOT_TOKEN as the compatibility name); never put the token in source code."
        )

    log.info(
        "Starting JAVIS v%s | Engine=Argos Translate + CTranslate2 | Compute=%s | Device=%s | Batch=%s | Beam=%s | Chunk=%s | Port=%s",
        JAVIS_VERSION,
        os.getenv("ARGOS_COMPUTE_TYPE", "auto"),
        os.getenv("ARGOS_DEVICE_TYPE", "cpu"),
        os.getenv("ARGOS_BATCH_SIZE", "1"),
        os.getenv("ARGOS_BEAM_SIZE", "2"),
        os.getenv("ARGOS_CHUNK_TYPE", "MINISBD"),
        PORT,
    )

    ARGOS_WORKER = ArgosWorkerClient()
    ARGOS_WORKER.start()
    runner = await start_http_server()
    try:
        await bot.start(discord_token)
    finally:
        await runner.cleanup()
        if ARGOS_WORKER is not None:
            ARGOS_WORKER.shutdown()
            ARGOS_WORKER = None
        await bot.close()


if __name__ == "__main__":
    asyncio.run(main())
