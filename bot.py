import asyncio
import gc
import json
import logging
import os
import re
import time
import subprocess
import sys
from pathlib import Path

from aiohttp import web
import discord
from discord.ext import commands

BASE_DIR = Path(__file__).resolve().parent
DICTIONARY_PATH = BASE_DIR / "dictionary.json"
INDEX_PATH = BASE_DIR / "index.html"
JAVIS_VERSION = os.getenv("JAVIS_VERSION", "1.0.9")

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
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

# Render Free is a 512 MB RAM plan. If a Render Environment Variable
# overrides batch size above 1, clamp it back to 1 to reduce peak RAM.
try:
    _runtime_batch = int(os.getenv("ARGOS_BATCH_SIZE", "1"))
except ValueError:
    _runtime_batch = 1
os.environ["ARGOS_BATCH_SIZE"] = "1"

os.environ.setdefault("ARGOS_CHUNK_TYPE", "MINISBD")
os.environ.setdefault(
    "ARGOS_PACKAGES_DIR",
    os.path.join(os.path.expanduser("~"), ".local", "share", "argos-translate", "packages"),
)
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
# Dedicated Argos translation subprocess
# ---------------------------------------------------------------------------

# The subprocess is intentionally tiny: it imports Argos only for one target
# translation, returns the result, then exits. This avoids keeping CTranslate2
# allocations alive between TH->EN / EN->KO (or the reverse pivot route).
ARGOS_SUBPROCESS_SCRIPT = r"""
import json
import os
import sys
import traceback
from pathlib import Path

def _package_roots():
    roots = []
    env_root = os.getenv("ARGOS_PACKAGES_DIR", "").strip()
    if env_root:
        roots.append(Path(env_root))
    home = Path.home()
    roots.extend([
        home / ".local" / "share" / "argos-translate" / "packages",
        Path("/root/.local/share/argos-translate/packages"),
        Path("/app/.local/share/argos-translate/packages"),
        Path("/usr/local/share/argos-translate/packages"),
    ])
    seen = set()
    out = []
    for root in roots:
        try:
            key = str(root.resolve())
        except Exception:
            key = str(root)
        if key and key not in seen:
            seen.add(key)
            out.append(root)
    return out

def _find_package(source_code, target_code):
    wanted = (source_code, target_code)
    for root in _package_roots():
        if not root.exists():
            continue
        try:
            metadata_files = list(root.glob("*/metadata.json"))
        except Exception:
            metadata_files = []
        for meta_path in metadata_files:
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if meta.get("type", "translate") != "translate":
                continue
            if (meta.get("from_code"), meta.get("to_code")) == wanted:
                return meta_path.parent
    return None

def _translate_direct(package_dir, text):
    # Import only the lightweight runtime pieces needed for one model.
    # Do NOT import argostranslate.translate here: that pulls the full
    # Argos registry/SBD stack into the translation subprocess.
    import ctranslate2
    import sentencepiece as spm

    model_dir = package_dir / "model"
    sp_model = package_dir / "sentencepiece.model"
    bpe_model = package_dir / "bpe.model"

    if not model_dir.is_dir():
        raise RuntimeError(f"Argos model directory not found: {model_dir}")
    if not sp_model.exists() and not bpe_model.exists():
        raise RuntimeError(
            f"Tokenizer model not found in Argos package: {package_dir}"
        )
    if not sp_model.exists():
        raise RuntimeError(
            "This JAVIS runtime expects Argos SentencePiece packages; "
            f"no sentencepiece.model found in {package_dir}"
        )

    tokenizer = spm.SentencePieceProcessor(model_file=str(sp_model))
    source_tokens = tokenizer.encode(text, out_type=str)
    if not source_tokens:
        return ""

    compute_type = os.getenv("ARGOS_COMPUTE_TYPE", "int8")
    device = os.getenv("ARGOS_DEVICE_TYPE", "cpu")
    try:
        inter_threads = max(1, int(os.getenv("ARGOS_INTER_THREADS", "1")))
    except ValueError:
        inter_threads = 1
    try:
        intra_threads = max(1, int(os.getenv("ARGOS_INTRA_THREADS", "1")))
    except ValueError:
        intra_threads = 1
    try:
        beam_size = max(1, int(os.getenv("ARGOS_BEAM_SIZE", "2")))
    except ValueError:
        beam_size = 2

    # CTranslate2 is the actual inference engine used by Argos models.
    # Loading it directly avoids importing the larger Argos registry/SBD
    # runtime inside the short-lived process, reducing memory pressure.
    params = {
        "model_path": str(model_dir),
        "device": device,
        "inter_threads": inter_threads,
        "intra_threads": intra_threads,
    }
    if compute_type and compute_type != "auto":
        params["compute_type"] = compute_type

    translator = ctranslate2.Translator(**params)
    try:
        results = translator.translate_batch(
            [source_tokens],
            beam_size=beam_size,
            max_batch_size=1,
            return_scores=False,
        )
        if not results or not results[0].hypotheses:
            raise RuntimeError("CTranslate2 returned no hypothesis")
        target_tokens = results[0].hypotheses[0]
        return tokenizer.decode(target_tokens)
    finally:
        del translator
        del tokenizer

def main():
    payload = json.loads(sys.stdin.read())
    source_lang = payload["source"]
    target_lang = payload["target"]
    text = payload["text"]

    try:
        package_dir = _find_package(source_lang, target_lang)
        if package_dir is None:
            raise RuntimeError(
                f"Installed Argos model package not found: "
                f"{source_lang}->{target_lang}"
            )

        result = _translate_direct(package_dir, text)
        print(
            json.dumps(
                {
                    "ok": True,
                    "result": str(result),
                    "runner": "ct2-direct-from-argos-model",
                    "source": source_lang,
                    "target": target_lang,
                },
                ensure_ascii=False,
            )
        )
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        print(
            json.dumps(
                {"ok": False, "error": repr(exc)},
                ensure_ascii=False,
            )
        )
        raise

if __name__ == "__main__":
    main()
"""


class ArgosWorkerClient:
    """Compatibility wrapper around an isolated one-shot CTranslate2 subprocess using an Argos model artifact.

    The previous v1.0.7 long-lived worker kept the Python/Argos runtime resident
    under the same Render cgroup. v1.0.9 exits the subprocess after every target,
    so its CTranslate2 memory is returned to the OS before the next target.
    """

    def __init__(self) -> None:
        self.started = False

    def start(self) -> None:
        self.started = True
        log.info(
            "Argos isolated runner ready | mode=subprocess-per-target | "
            "compute=%s | device=%s | batch=%s | beam=%s",
            os.getenv("ARGOS_COMPUTE_TYPE", "int8"),
            os.getenv("ARGOS_DEVICE_TYPE", "cpu"),
            os.getenv("ARGOS_BATCH_SIZE", "1"),
            os.getenv("ARGOS_BEAM_SIZE", "2"),
        )

    def ensure_alive(self) -> None:
        if not self.started:
            raise RuntimeError("Argos isolated runner is not started")

    def translate(self, text: str, source_lang: str, target_lang: str) -> str:
        self.ensure_alive()

        payload = json.dumps(
            {
                "text": text,
                "source": source_lang,
                "target": target_lang,
            },
            ensure_ascii=False,
        )

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"

        log.info(
            "Argos target start | %s->%s | chars=%d | batch=%s | compute=%s",
            source_lang,
            target_lang,
            len(text),
            env.get("ARGOS_BATCH_SIZE", "1"),
            env.get("ARGOS_COMPUTE_TYPE", "int8"),
        )

        try:
            completed = subprocess.run(
                [sys.executable, "-c", ARGOS_SUBPROCESS_SCRIPT],
                input=payload,
                text=True,
                capture_output=True,
                env=env,
                timeout=TRANSLATION_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(
                f"Argos target timed out after "
                f"{TRANSLATION_TIMEOUT_SECONDS}s ({source_lang}->{target_lang})"
            ) from exc

        stderr = (completed.stderr or "").strip()
        stdout = (completed.stdout or "").strip()

        if stderr:
            # Keep the useful Argos log context without allowing an enormous
            # stderr buffer to flood the Render log.
            for line in stderr.splitlines()[-40:]:
                logging.getLogger("JAVIS.argos-worker").info(line)

        # The worker emits exactly one JSON result line on stdout.
        response = None
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                candidate = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict) and "ok" in candidate:
                response = candidate
                break

        if completed.returncode != 0:
            error_detail = (
                response.get("error")
                if isinstance(response, dict)
                else None
            )
            if not error_detail:
                error_detail = stderr[-4000:] or "unknown Argos subprocess error"
            raise RuntimeError(
                f"Argos subprocess failed ({source_lang}->{target_lang}): "
                f"{error_detail}"
            )

        if not isinstance(response, dict) or not response.get("ok"):
            raise RuntimeError(
                f"Argos subprocess returned no successful result "
                f"({source_lang}->{target_lang})"
            )

        result = str(response.get("result", ""))
        log.info(
            "Argos target complete | %s->%s | result_chars=%d | subprocess_exit=%s",
            source_lang,
            target_lang,
            len(result),
            completed.returncode,
        )
        return result

    def shutdown(self) -> None:
        self.started = False


ARGOS_WORKER: ArgosWorkerClient | None = None


def translate_sync(text: str, source_lang: str, target_lang: str) -> str:
    if ARGOS_WORKER is None:
        raise RuntimeError("Argos isolated runner is not started")
    prepared, protector = prepare_for_translation(text, source_lang)
    result = ARGOS_WORKER.translate(prepared, source_lang, target_lang)
    return restore_twom_terms(result, protector, target_lang)


def translate_all_sync(original: str) -> dict[str, str]:
    source_lang = detect_language(original)
    clean = strip_language_prefix(original).strip()

    if not clean:
        return {"source": source_lang, "en": "", "th": "", "ko": "", "errors": {}}

    log.info("Translating message | source=%s | chars=%d", source_lang, len(clean))

    targets: dict[str, str] = {}
    errors: dict[str, str] = {}

    def run_target(label: str, source: str, target: str) -> None:
        try:
            targets[label] = translate_sync(clean, source, target)
        except Exception as exc:
            errors[f"{source}->{target}"] = str(exc)
            log.exception("Target translation failed | %s->%s", source, target)

    if source_lang == "en":
        run_target("th", "en", "th")
        run_target("ko", "en", "ko")
    elif source_lang == "th":
        run_target("en", "th", "en")
        # Important: always feed the original clean text to EN->KO.
        # This preserves the user's requested English pivot behavior.
        run_target("ko", "en", "ko")
    else:
        run_target("en", "ko", "en")
        run_target("th", "en", "th")

    return {"source": source_lang, **targets, "errors": errors}


def build_response(result: dict[str, str]) -> str:
    source = result["source"]
    errors = result.get("errors", {})
    lines = []

    if source == "th":
        if result.get("en"):
            lines.append(f"[EN] {result['en']}")
        if result.get("ko"):
            lines.append(f"[KO] {result['ko']}")
    elif source == "en":
        if result.get("th"):
            lines.append(f"[TH] {result['th']}")
        if result.get("ko"):
            lines.append(f"[KO] {result['ko']}")
    else:
        if result.get("en"):
            lines.append(f"[EN] {result['en']}")
        if result.get("th"):
            lines.append(f"[TH] {result['th']}")

    if errors and lines:
        log.warning("Partial translation response | errors=%s", errors)
    if not lines:
        raise RuntimeError(
            "No translation target completed successfully: "
            + (json.dumps(errors, ensure_ascii=False) if errors else "unknown error")
        )
    return "\n".join(lines)


async def index(request: web.Request) -> web.Response:
    return web.FileResponse(INDEX_PATH)


async def health(request: web.Request) -> web.Response:
    token_configured = bool(get_discord_token())
    worker_alive = ARGOS_WORKER is not None and ARGOS_WORKER.started
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
            "translation_runner_mode": "ct2-direct-from-argos-model-per-target",
            "argos_packages_dir": os.getenv("ARGOS_PACKAGES_DIR", ""),
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
    worker_alive = ARGOS_WORKER is not None and ARGOS_WORKER.started
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


async def send_safe_translation(
    message: discord.Message,
    content: str,
) -> None:
    try:
        await message.reply(
            content,
            mention_author=False,
            allowed_mentions=ALLOWED_MENTIONS,
        )
    except (discord.Forbidden, discord.HTTPException):
        # Fallback for channels where reply-to-message is unavailable.
        log.exception("Discord reply failed; falling back to channel.send")
        await message.channel.send(
            content,
            allowed_mentions=ALLOWED_MENTIONS,
        )


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
            await send_safe_translation(message, response)
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

            # First chunk uses reply for the normal UX; fallback to channel.send
            # is handled inside send_safe_translation.
            await send_safe_translation(message, chunks[0])
            for chunk in chunks[1:]:
                await message.channel.send(
                    chunk,
                    allowed_mentions=ALLOWED_MENTIONS,
                )
    except Exception as exc:
        log.exception("Translation failed")
        await send_safe_translation(
            message,
            "JAVIS แปลข้อความนี้ไม่สำเร็จ กรุณาลองใหม่อีกครั้ง",
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
