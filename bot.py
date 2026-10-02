import asyncio
import json
import logging
import os
import re
from pathlib import Path

from aiohttp import web
import discord
from discord.ext import commands

BASE_DIR = Path(__file__).resolve().parent
DICTIONARY_PATH = BASE_DIR / "dictionary.json"
INDEX_PATH = BASE_DIR / "index.html"

# Release version for this deploy. Keep this value in sync with the copy-ready filenames.
JAVIS_VERSION = os.getenv("JAVIS_VERSION", "1.0.4")

def get_discord_token() -> str:
    """Read the Discord bot token only from environment variables.

    Primary name is DISCORD_TOKEN. DISCORD_BOT_TOKEN is accepted as a
    compatibility alias so the secret never needs to be placed in source.
    """
    return (
        os.getenv("DISCORD_TOKEN", "").strip()
        or os.getenv("DISCORD_BOT_TOKEN", "").strip()
    )

PORT = int(os.getenv("PORT", "10000"))
MAX_INPUT_CHARS = int(os.getenv("MAX_INPUT_CHARS", "1200"))
TRANSLATION_LOCK = asyncio.Lock()

# Argos/CTranslate2 memory-oriented defaults. These must be set BEFORE
# importing argostranslate because Argos reads them when its settings module loads.
os.environ.setdefault("ARGOS_DEVICE_TYPE", "cpu")
os.environ.setdefault("ARGOS_COMPUTE_TYPE", "int8")
os.environ.setdefault("ARGOS_INTER_THREADS", "1")
os.environ.setdefault("ARGOS_INTRA_THREADS", "1")
os.environ.setdefault("ARGOS_BATCH_SIZE", "8")
os.environ.setdefault("ARGOS_BEAM_SIZE", "2")
os.environ.setdefault("ARGOS_CHUNK_TYPE", "NONE")
os.environ.setdefault(
    "ARGOS_PACKAGE_INDEX",
    "https://raw.githubusercontent.com/argosopentech/argospm-index/main",
)

import argostranslate.translate as argos_translate

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

class SuppressUnusedVoiceDependencyWarning(logging.Filter):
    """Hide only optional voice-dependency warnings that are irrelevant to JAVIS.

    JAVIS is intentionally a text-only translation bot. PyNaCl and davey are
    optional Discord voice dependencies; installing them would add packages
    that the application never uses. All other discord.client log messages
    remain unchanged.
    """

    _IGNORED = {
        "PyNaCl is not installed, voice will NOT be supported",
        "davey is not installed, voice will NOT be supported",
    }

    def filter(self, record: logging.LogRecord) -> bool:
        return record.getMessage() not in self._IGNORED


discord_client_logger = logging.getLogger("discord.client")
discord_client_logger.addFilter(SuppressUnusedVoiceDependencyWarning())

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

# Cache translation objects so every Discord message does not repeatedly
# scan all installed Argos languages/packages. The actual CTranslate2 model
# remains lazy-loaded by Argos on first use.
TRANSLATOR_CACHE: dict[tuple[str, str], object] = {}
TRANSLATOR_CACHE_LOCK = asyncio.Lock()


class Protector:
    """Protects TWOM terms, URLs, mentions, and custom emojis from translation."""

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
        # Longest token first is harmless and deterministic.
        for token, original in sorted(self.replacements.items(), key=lambda x: -len(x[0])):
            text = text.replace(token, original)
        return text


def detect_language(text: str) -> str:
    """Lightweight EN/TH/KO detection to avoid another ML model in RAM."""
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
    # Protect URLs and Discord mention/custom emoji markup so translation does not corrupt them.
    text = protector.protect_regex(text, r"https?://[^\s<>]+", re.IGNORECASE)
    text = protector.protect_regex(text, r"<@!?\d+>")
    text = protector.protect_regex(text, r"<a?:[A-Za-z0-9_]+:\d+>")
    return text


def protect_twom_terms(text: str, source_lang: str, protector: Protector) -> str:
    """Replace source-language TWOM terms with stable tokens."""
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

    # Long phrases before short terms prevents partial replacement.
    entries.sort(key=len, reverse=True)
    for alias in entries:
        if re.search(r"[A-Za-z]", alias):
            pattern = rf"(?<![A-Za-z0-9_]){re.escape(alias)}(?![A-Za-z0-9_])"
            text = protector.protect_regex(text, pattern, re.IGNORECASE)
        else:
            text = text.replace(alias, protector.add(alias)) if alias in text else text
    return text


def twom_target_for_token(token: str, target_lang: str) -> str | None:
    original = token
    original_lower = original.lower()
    for item in TWOM_DICTIONARY:
        aliases = item.get("en", [])
        if isinstance(aliases, str):
            aliases = [aliases]
        aliases = [str(x).lower() for x in aliases]
        if original_lower in aliases:
            value = item.get(target_lang)
            if isinstance(value, str):
                return value
    return None


def restore_twom_terms(text: str, protector: Protector, target_lang: str) -> str:
    for token, original in list(protector.replacements.items()):
        # If this token was generated from a TWOM source alias, restore with the target-language term.
        target = None
        original_lower = original.lower()
        for item in TWOM_DICTIONARY:
            source_aliases = item.get("en", [])
            if isinstance(source_aliases, str):
                source_aliases = [source_aliases]
            source_aliases = [str(x).lower() for x in source_aliases]
            # Match against any known alias in the three supported languages.
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
        if target is not None:
            text = text.replace(token, target)
        else:
            text = text.replace(token, original)
    return text


def prepare_for_translation(text: str, source_lang: str, target_lang: str) -> tuple[str, Protector]:
    protector = Protector()
    text = protect_common_content(text, protector)
    text = protect_twom_terms(text, source_lang, protector)
    return text, protector


def translate_sync(text: str, source_lang: str, target_lang: str) -> str:
    prepared, protector = prepare_for_translation(text, source_lang, target_lang)

    # Argos exposes translation objects for installed language pairs. Reusing
    # the object avoids the repeated get_installed_languages() scan visible in
    # the failing Render log.
    cache_key = (source_lang, target_lang)
    translator = TRANSLATOR_CACHE.get(cache_key)
    if translator is None:
        # This function is called from a worker thread, so use a small
        # thread-safe critical section without blocking the Discord event loop.
        # Multiple calls are serialized by TRANSLATION_LOCK in on_message.
        translator = argos_translate.get_translation_from_codes(source_lang, target_lang)
        if translator is None:
            raise RuntimeError(f"Argos translation model unavailable: {source_lang}->{target_lang}")
        TRANSLATOR_CACHE[cache_key] = translator

    result = translator.translate(prepared)
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
    # Keep the browser landing page separate from the JSON health endpoint
    # so Render health checks remain machine-readable at /health.
    return web.FileResponse(INDEX_PATH)


async def health(request: web.Request) -> web.Response:
    token_configured = bool(get_discord_token())
    return web.json_response(
        {
            "ok": token_configured,
            "bot": "JAVIS",
            "version": JAVIS_VERSION,
            "engine": "Argos Translate + CTranslate2",
            "compute_type": os.getenv("ARGOS_COMPUTE_TYPE", "auto"),
            "chunk_type": os.getenv("ARGOS_CHUNK_TYPE", "DEFAULT"),
            "cached_translation_pairs": [f"{src}->{dst}" for src, dst in sorted(TRANSLATOR_CACHE)],
            "discord_token_configured": token_configured,
            "discord_ready": bot.is_ready(),
        },
        status=200 if token_configured else 503,
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


intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!javis ", intents=intents, help_command=None)
ALLOWED_MENTIONS = discord.AllowedMentions.none()


@bot.event
async def on_ready():
    log.info("Logged in as %s (%s) | JAVIS v%s", bot.user, bot.user.id if bot.user else "?", JAVIS_VERSION)
    await bot.change_presence(activity=discord.Game(name="TH ↔ EN ↔ KO | TWOM"))


@bot.command(name="status")
async def status_command(ctx: commands.Context):
    await ctx.reply(
        "JAVIS พร้อมใช้งาน ✅\n"
        f"Version: v{JAVIS_VERSION}\n"
        f"Engine: Argos Translate + CTranslate2\n"
        f"Quantization: {os.getenv('ARGOS_COMPUTE_TYPE', 'auto')}\n"
        f"Chunking: {os.getenv('ARGOS_CHUNK_TYPE', 'DEFAULT')}\n"
        f"Languages: TH ↔ EN ↔ KO",
        mention_author=False,
        allowed_mentions=ALLOWED_MENTIONS,
    )


@bot.command(name="help")
async def help_command(ctx: commands.Context):
    await ctx.reply(
        "JAVIS Help 📘\n"
        "• ส่งข้อความปกติ: บอทตอบ [EN] [TH] [KO] ในข้อความเดียว\n"
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
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # Commands must not be translated.
    if message.content.startswith("!javis "):
        await bot.process_commands(message)
        return

    content = message.content.strip()
    if not content or len(content) > MAX_INPUT_CHARS:
        return

    # Ignore obvious commands and empty URL-only messages.
    if content.startswith(("/", "$", "?")):
        return
    if re.fullmatch(r"https?://[^\s]+", content, re.IGNORECASE):
        return

    try:
        async with TRANSLATION_LOCK:
            result = await asyncio.to_thread(translate_all_sync, content)
        response = build_response(result)
        log.info("Translation completed | source=%s | response_chars=%d", result["source"], len(response))

        # Keep comfortably below Discord's 2000-character message limit.
        if len(response) <= 1900:
            await message.reply(response, mention_author=False, allowed_mentions=ALLOWED_MENTIONS)
        else:
            chunks = []
            current = ""
            for line in response.splitlines():
                candidate = f"{current}\n{line}" if current else line
                if len(candidate) > 1900:
                    chunks.append(current)
                    current = line
                else:
                    current = candidate
            if current:
                chunks.append(current)
            for index, chunk in enumerate(chunks):
                if index == 0:
                    await message.reply(chunk, mention_author=False, allowed_mentions=ALLOWED_MENTIONS)
                else:
                    await message.channel.send(chunk, allowed_mentions=ALLOWED_MENTIONS)
    except Exception:
        log.exception("Translation failed")
        await message.reply(
            "JAVIS แปลข้อความนี้ไม่สำเร็จ กรุณาลองใหม่อีกครั้งหรือเช็กว่า Argos models ติดตั้งครบแล้ว",
            mention_author=False,
            allowed_mentions=ALLOWED_MENTIONS,
        )


async def main():
    discord_token = get_discord_token()
    if not discord_token:
        raise RuntimeError(
            "Discord bot token is missing. Set DISCORD_TOKEN in Render Environment "
            "(or DISCORD_BOT_TOKEN as the compatibility name); never put the token in source code."
        )

    log.info(
        "Starting JAVIS v%s | Engine=Argos Translate + CTranslate2 | Compute=%s | Chunk=%s | Port=%s",
        JAVIS_VERSION,
        os.getenv("ARGOS_COMPUTE_TYPE", "auto"),
        os.getenv("ARGOS_CHUNK_TYPE", "DEFAULT"),
        PORT,
    )
    runner = await start_http_server()
    try:
        await bot.start(discord_token)
    finally:
        await runner.cleanup()
        await bot.close()


if __name__ == "__main__":
    asyncio.run(main())
