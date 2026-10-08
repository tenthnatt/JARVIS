import asyncio
import contextvars
import gc
import json
import logging
import os
import re
import time
import subprocess
import sys
import signal
from pathlib import Path
from types import MethodType

import aiohttp
from aiohttp import web
import discord
from discord.ext import commands
from discord import app_commands

BASE_DIR = Path(__file__).resolve().parent
DICTIONARY_PATH = BASE_DIR / "dictionary.json"
INDEX_PATH = BASE_DIR / "index.html"
JAVIS_VERSION = os.getenv("JAVIS_VERSION", "1.0.28")

# Translation-room configuration. JAVIS translates messages ONLY in channels
# explicitly enabled with /setroom. The configuration is per Discord guild.
TRANSLATION_ROOMS_PATH = BASE_DIR / "translation_rooms.json"
TRANSLATION_ROOMS: dict[str, set[int]] = {}
TRANSLATION_ROOMS_IO_LOCK = asyncio.Lock()
TRANSLATION_ROOM_COMMAND_SYNC_STARTED = False
TRANSLATION_ROOM_COMMAND_SYNC_STATE = "pending"
TRANSLATION_ROOM_COMMAND_SYNC_RETRY_AFTER = 0.0

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
os.environ["ARGOS_KO_ENGINE"] = "ct2-direct"
os.environ["ARGOS_KO_BEAM_SIZE"] = "2"
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


def _load_translation_rooms_sync() -> None:
    """Load per-guild translation-room IDs from local JSON config."""
    TRANSLATION_ROOMS.clear()
    try:
        if not TRANSLATION_ROOMS_PATH.exists():
            return
        raw = json.loads(TRANSLATION_ROOMS_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("translation_rooms.json must contain an object")
        for guild_id, channel_ids in raw.items():
            if not isinstance(channel_ids, list):
                continue
            cleaned: set[int] = set()
            for channel_id in channel_ids:
                try:
                    parsed = int(channel_id)
                except (TypeError, ValueError):
                    continue
                if parsed > 0:
                    cleaned.add(parsed)
            if cleaned:
                TRANSLATION_ROOMS[str(guild_id)] = cleaned
        log.info(
            "Translation room config loaded | guilds=%d | rooms=%d",
            len(TRANSLATION_ROOMS),
            sum(len(v) for v in TRANSLATION_ROOMS.values()),
        )
    except Exception as exc:
        log.warning("Translation room config load failed safely | %r", exc)


def _save_translation_rooms_sync() -> None:
    """Atomically save the per-guild translation-room IDs."""
    payload = {
        str(guild_id): sorted(int(channel_id) for channel_id in channel_ids)
        for guild_id, channel_ids in TRANSLATION_ROOMS.items()
        if channel_ids
    }
    temp_path = TRANSLATION_ROOMS_PATH.with_suffix(".json.tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temp_path.replace(TRANSLATION_ROOMS_PATH)


def _translation_room_ids(guild_id: int) -> set[int]:
    return set(TRANSLATION_ROOMS.get(str(int(guild_id)), set()))


def _is_translation_room_enabled(guild_id: int, channel_id: int) -> bool:
    return int(channel_id) in TRANSLATION_ROOMS.get(str(int(guild_id)), set())


def _translation_room_count() -> int:
    return sum(len(channel_ids) for channel_ids in TRANSLATION_ROOMS.values())


_load_translation_rooms_sync()

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


# URLs and emoji are non-translatable content.  Do NOT replace these
# fragments with placeholder tokens before translation: some SentencePiece /
# CTranslate2 tokenizers can alter placeholder text (e.g. JAVISX1QZ). Instead,
# v1.0.13 splits the message into translatable and non-translatable spans,
# translates only the language spans, and rejoins the untouched URLs/emoji.
URL_PATTERN = r"(?:https?://|www\.)[^\s<>]+"
CUSTOM_DISCORD_EMOJI_PATTERN = r"<a?:[A-Za-z0-9_]+:\d+>"
MENTION_PATTERN = r"<@!?\d+>"
# Common Unicode emoji blocks plus variation selectors / ZWJ / skin tones.
UNICODE_EMOJI_PATTERN = (
    r"(?:[\U0001F1E6-\U0001F1FF]"
    r"|[\U0001F300-\U0001F5FF]"
    r"|[\U0001F600-\U0001F64F]"
    r"|[\U0001F680-\U0001F6FF]"
    r"|[\U0001F700-\U0001F77F]"
    r"|[\U0001F780-\U0001F7FF]"
    r"|[\U0001F800-\U0001F8FF]"
    r"|[\U0001F900-\U0001F9FF]"
    r"|[\U0001FA00-\U0001FAFF]"
    r"|[\u2300-\u23FF]"
    r"|[\u2600-\u27BF])"
    r"(?:[\uFE0E\uFE0F]|[\U0001F3FB-\U0001F3FF])?"
    r"(?:\u200D"
    r"(?:[\U0001F1E6-\U0001F1FF]|[\U0001F300-\U0001FAFF]|[\u2600-\u27BF])"
    r"(?:[\uFE0E\uFE0F]|[\U0001F3FB-\U0001F3FF])?)*"
    r"(?:[0-9#*]\uFE0F?\u20E3)?"
)
NON_TRANSLATABLE_RE = re.compile(
    rf"(?:{URL_PATTERN}|{MENTION_PATTERN}|{CUSTOM_DISCORD_EMOJI_PATTERN}|{UNICODE_EMOJI_PATTERN})",
    re.IGNORECASE,
)


def split_non_translatable_spans(text: str) -> list[tuple[bool, str]]:
    """Split text into (is_protected, fragment) spans without placeholder tokens."""
    spans: list[tuple[bool, str]] = []
    last = 0
    for match in NON_TRANSLATABLE_RE.finditer(text):
        if match.start() > last:
            spans.append((False, text[last:match.start()]))
        spans.append((True, match.group(0)))
        last = match.end()
    if last < len(text):
        spans.append((False, text[last:]))
    if not spans:
        spans.append((False, text))
    return spans


def has_translatable_text_after_removing_nontext(text: str) -> bool:
    """Return True when any normal language text remains."""
    remaining = NON_TRANSLATABLE_RE.sub("", text)
    # Remove common zero-width/variation characters left by emoji sequences.
    remaining = remaining.replace("\u200d", "").replace("\ufe0f", "").replace("\ufe0e", "")
    return bool(remaining.strip())


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


def protect_common_content(text: str, protector: Protector) -> str:
    """Defensively protect common Discord/non-translatable content.

    The message is normally split by split_non_translatable_spans() before
    this function runs, so URL/emoji-only spans never reach the translator.
    This function is kept as a defensive second layer for any content that
    remains inside a translatable fragment.
    """
    text = protector.protect_regex(text, URL_PATTERN, re.IGNORECASE)
    text = protector.protect_regex(text, MENTION_PATTERN)
    text = protector.protect_regex(text, CUSTOM_DISCORD_EMOJI_PATTERN)
    text = protector.protect_regex(text, UNICODE_EMOJI_PATTERN)
    return text


def prepare_for_translation(text: str, source_lang: str) -> tuple[str, Protector]:
    """Prepare only ordinary text for the model.

    TWOM terms are intentionally NOT replaced with placeholder tokens here.
    In v1.0.16 those placeholders were sent through SentencePiece/CTranslate2,
    which can make the model copy/drop the source term and produce mixed-language
    output. TWOM terms are now handled as explicit glossary spans in
    split_translation_spans() before the model is called.
    """
    protector = Protector()
    prepared = protect_common_content(text, protector)
    return prepared, protector


def _twom_glossary_for(source_lang: str, target_lang: str) -> list[tuple[str, str]]:
    """Return source aliases and their target-language replacements.

    Matching is source-language aware and longest-first. Empty/missing target
    values are ignored so an incomplete dictionary entry never removes text.
    """
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in TWOM_DICTIONARY:
        if not isinstance(item, dict):
            continue
        aliases = item.get(source_lang, [])
        if isinstance(aliases, str):
            aliases = [aliases]
        target_value = item.get(target_lang)
        if isinstance(target_value, str):
            target = target_value.strip()
        elif isinstance(target_value, list):
            target = next((str(v).strip() for v in target_value if isinstance(v, str) and v.strip()), "")
        else:
            target = ""
        if not target:
            continue
        for alias in aliases:
            if not isinstance(alias, str):
                continue
            alias = alias.strip()
            if not alias:
                continue
            key = alias.casefold() if re.search(r"[A-Za-z]", alias) else alias
            if key in seen:
                continue
            seen.add(key)
            pairs.append((alias, target))
    pairs.sort(key=lambda item: len(item[0]), reverse=True)
    return pairs


def split_translation_spans(
    text: str, source_lang: str, target_lang: str
) -> list[tuple[str, str, str | None]]:
    """Split text into model text, non-translatable text, and TWOM glossary spans.

    URLs/mentions/emoji remain byte-for-byte unchanged. TWOM terms are replaced
    directly with their target-language glossary value instead of placeholder
    tokens, so the translation model never receives artificial JAVISX... tokens.
    """
    glossary = _twom_glossary_for(source_lang, target_lang)
    if not glossary:
        return [("translate", frag, None) if not protected else ("protected", frag, None)
                for protected, frag in split_non_translatable_spans(text)]

    latin_patterns: list[str] = []
    plain_patterns: list[str] = []
    target_by_alias: dict[str, str] = {}
    for alias, target in glossary:
        key = alias.casefold() if re.search(r"[A-Za-z]", alias) else alias
        target_by_alias[key] = target
        if re.search(r"[A-Za-z]", alias):
            latin_patterns.append(
                rf"(?<![A-Za-z0-9_]){re.escape(alias)}(?![A-Za-z0-9_])"
            )
        else:
            plain_patterns.append(re.escape(alias))

    alternatives = latin_patterns + plain_patterns
    if not alternatives:
        return [("translate", frag, None) if not protected else ("protected", frag, None)
                for protected, frag in split_non_translatable_spans(text)]

    glossary_re = re.compile("|".join(alternatives), re.IGNORECASE)
    result: list[tuple[str, str, str | None]] = []

    for protected, fragment in split_non_translatable_spans(text):
        if protected or not fragment:
            result.append(("protected", fragment, None))
            continue

        last = 0
        for match in glossary_re.finditer(fragment):
            if match.start() > last:
                result.append(("translate", fragment[last:match.start()], None))
            alias = match.group(0)
            key = alias.casefold() if re.search(r"[A-Za-z]", alias) else alias
            target = target_by_alias.get(key)
            if target is not None:
                result.append(("twom", alias, target))
            else:
                result.append(("translate", alias, None))
            last = match.end()
        if last < len(fragment):
            result.append(("translate", fragment[last:], None))

    return result or [("translate", text, None)]


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
                return meta_path.parent, meta
    return None, None


def _translate_direct(package_dir, metadata, texts):
    # This follows the important decoding behavior used by Argos Translate's
    # PackageTranslation while keeping the subprocess lightweight: package
    # tokenizer + optional target_prefix + replace_unknowns + token batching +
    # length_penalty=0.2 are preserved, but the full Argos registry/SBD stack is
    # not imported into the worker. See Argos PackageTranslation source.
    import ctranslate2
    import sentencepiece as spm

    model_dir = package_dir / "model"
    sp_model = package_dir / "sentencepiece.model"
    if not model_dir.is_dir():
        raise RuntimeError(f"Argos model directory not found: {model_dir}")
    if not sp_model.exists():
        raise RuntimeError(
            "This JAVIS runtime expects Argos SentencePiece packages; "
            f"no sentencepiece.model found in {package_dir}"
        )

    tokenizer = spm.SentencePieceProcessor(model_file=str(sp_model))
    tokenized = [tokenizer.encode(text, out_type=str) for text in texts]
    if not tokenized:
        return []

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
        batch_size = max(1, int(os.getenv("ARGOS_BATCH_SIZE", "1")))
    except ValueError:
        batch_size = 1
    try:
        beam_size = max(1, int(os.getenv("ARGOS_BEAM_SIZE", "2")))
    except ValueError:
        beam_size = 2

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
        target_prefix = None
        prefix = str(metadata.get("target_prefix", "") or "")
        if prefix:
            # Argos passes target_prefix as the raw target vocabulary token.
            # Do not SentencePiece-encode it; that changes the prefix semantics.
            target_prefix = [[prefix]] * len(tokenized)

        results = translator.translate_batch(
            tokenized,
            target_prefix=target_prefix,
            replace_unknowns=True,
            max_batch_size=batch_size,
            batch_type="tokens",
            beam_size=beam_size,
            num_hypotheses=1,
            length_penalty=0.2,
            return_scores=False,
        )
        if len(results) != len(tokenized):
            raise RuntimeError(
                f"CTranslate2 returned {len(results)} results for {len(tokenized)} inputs"
            )

        output = []
        for result in results:
            if not result.hypotheses:
                raise RuntimeError("CTranslate2 returned no hypothesis")
            # Match Argos Translate's current SentencePiece detokenization path.
            # Using SentencePiece.decode() here can leave literal U+2581 markers
            # in the Discord output; Argos uses decode_pieces() instead.
            value = tokenizer.decode_pieces(result.hypotheses[0])
            value = value.replace("▁", " ")
            if prefix and value.startswith(prefix):
                value = value[len(prefix):]
            if value.startswith(" "):
                value = value[1:]
            output.append(value.strip())
        return output
    finally:
        del translator
        del tokenizer


def main():
    payload = json.loads(sys.stdin.read())
    source_lang = payload["source"]
    target_lang = payload["target"]
    texts = payload.get("texts")
    if texts is None:
        # Backward-compatible single-text payload.
        texts = [payload.get("text", "")]
    if not isinstance(texts, list):
        raise ValueError("texts must be a list")
    texts = [str(x) for x in texts]

    try:
        package_dir, metadata = _find_package(source_lang, target_lang)
        if package_dir is None:
            raise RuntimeError(
                f"Installed Argos model package not found: {source_lang}->{target_lang}"
            )
        result = _translate_direct(package_dir, metadata, texts)
        print(
            json.dumps(
                {
                    "ok": True,
                    "results": result,
                    "runner": "ct2-direct-argos-compatible-v1.0.20",
                    "source": source_lang,
                    "target": target_lang,
                },
                ensure_ascii=False,
            )
        )
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"ok": False, "error": repr(exc)}, ensure_ascii=False))
        raise


if __name__ == "__main__":
    main()
"""


class ArgosWorkerClient:
    """One-shot CTranslate2 subprocess per target route.

    A route can contain multiple translatable spans, but they are sent in a
    single subprocess invocation so the model is loaded once per target. This
    reduces latency while preserving the memory behavior that motivated the
    isolated runner.
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

    def translate_many(self, texts: list[str], source_lang: str, target_lang: str) -> list[str]:
        self.ensure_alive()
        if not texts:
            return []

        started_at = time.monotonic()

        payload = json.dumps(
            {"texts": texts, "source": source_lang, "target": target_lang},
            ensure_ascii=False,
        )
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"

        log.info(
            "Argos target start | %s->%s | fragments=%d | chars=%d | batch=%s | compute=%s",
            source_lang,
            target_lang,
            len(texts),
            sum(len(x) for x in texts),
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
                f"Argos target timed out after {TRANSLATION_TIMEOUT_SECONDS}s "
                f"({source_lang}->{target_lang})"
            ) from exc

        stderr = (completed.stderr or "").strip()
        stdout = (completed.stdout or "").strip()
        if stderr:
            for line in stderr.splitlines()[-40:]:
                logging.getLogger("JAVIS.argos-worker").info(line)

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
            error_detail = response.get("error") if isinstance(response, dict) else None
            if not error_detail:
                error_detail = stderr[-4000:] or "unknown Argos subprocess error"
            raise RuntimeError(
                f"Argos subprocess failed ({source_lang}->{target_lang}): {error_detail}"
            )

        if not isinstance(response, dict) or not response.get("ok"):
            raise RuntimeError(
                f"Argos subprocess returned no successful result ({source_lang}->{target_lang})"
            )

        results = response.get("results")
        if not isinstance(results, list) or len(results) != len(texts):
            raise RuntimeError(
                f"Argos subprocess returned {len(results) if isinstance(results, list) else 0} "
                f"results for {len(texts)} fragments ({source_lang}->{target_lang})"
            )

        results = [str(x) for x in results]
        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        log.info(
            "Argos target complete | %s->%s | result_chars=%d | elapsed_ms=%d | subprocess_exit=%s",
            source_lang,
            target_lang,
            sum(len(x) for x in results),
            elapsed_ms,
            completed.returncode,
        )
        return results

    def translate(self, text: str, source_lang: str, target_lang: str) -> str:
        return self.translate_many([text], source_lang, target_lang)[0]

    def shutdown(self) -> None:
        self.started = False


ARGOS_WORKER: ArgosWorkerClient | None = None

# Discord startup state. A Discord HTTP 429 must not crash the Render process.
# Once a 429 is observed, the current process enters a hard quarantine: it records
# Retry-After/telemetry and makes no further Discord HTTP request attempts.
DISCORD_LOGIN_STATE = "starting"
DISCORD_LOGIN_RETRY_COUNT = 0
DISCORD_LOGIN_RETRY_AFTER = 0.0
DISCORD_LOGIN_SESSION_RECOVERY_COUNT = 0
DISCORD_LOGIN_HTTP_REBUILD_COUNT = 0
DISCORD_QUARANTINED = False
DISCORD_QUARANTINE_CONTEXT = ""
DISCORD_QUARANTINE_RETRY_AFTER = 0.0
DISCORD_QUARANTINE_CF_RAY = ""

# Render zero-downtime deploys start a new instance before SIGTERM-ing the old one.
# Delay only the FIRST Discord login attempt so the old Gateway has time to hand over.
# This does not probe Discord during an existing restriction; the 429 Retry-After
# value remains the authoritative retry timer.
DISCORD_STARTUP_HANDOVER_DELAY_SECONDS = max(
    0.0,
    float(os.getenv("DISCORD_STARTUP_HANDOVER_DELAY", "75")),
)
DISCORD_SHUTDOWN_EVENT = asyncio.Event()
DISCORD_SHUTDOWN_REQUESTED = False
DISCORD_LOGIN_LAST_STATUS = 0
DISCORD_LOGIN_LAST_SCOPE = ""
DISCORD_LOGIN_LAST_GLOBAL = False
DISCORD_LOGIN_LAST_RETRY_AFTER = 0.0
DISCORD_LOGIN_LAST_CF_RAY = ""
DISCORD_LOGIN_LAST_VIA = ""
DISCORD_LOGIN_LAST_SERVER = ""
DISCORD_LOGIN_LAST_MESSAGE = ""

# Central outbound Discord HTTP quarantine/cooldown. Any Discord REST 429 arms
# the process-local gate using Retry-After and permanently blocks further Discord
# HTTP attempts for this process, so the same blocked source is never retried.
DISCORD_OUTBOUND_BLOCKED_UNTIL_MONO = 0.0
DISCORD_OUTBOUND_LAST_429_RETRY_AFTER = 0.0
DISCORD_OUTBOUND_LAST_429_CONTEXT = ""
DISCORD_OUTBOUND_LAST_CF_RAY = ""

# Discord request telemetry / Render egress identity.
# Diagnostic metadata only; this does not change Discord request behavior.
DISCORD_HTTP_ATTEMPT = 0
DISCORD_INVALID_REQUEST_COUNT = 0
DISCORD_LAST_REQUEST_STATUS_CODE: int | str = "-"
DISCORD_LAST_REQUEST_429_SCOPE = "-"
DISCORD_LAST_REQUEST_RETRY_AFTER = 0.0
DISCORD_LAST_REQUEST_CF_RAY = "-"
DISCORD_LAST_REQUEST_CONTEXT = "-"
DISCORD_LAST_REQUEST_ROUTE = "-"
DISCORD_REQUEST_CONTEXT = contextvars.ContextVar("javis_discord_request_context", default="")

RENDER_SERVICE = (os.getenv("RENDER_SERVICE", "").strip() or os.getenv("RENDER_SERVICE_NAME", "").strip() or os.getenv("RENDER_SERVICE_ID", "").strip())
if not RENDER_SERVICE:
    external_hostname = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()
    RENDER_SERVICE = external_hostname.split(".", 1)[0] if external_hostname else "-"
RENDER_REGION = os.getenv("RENDER_REGION", "").strip() or os.getenv("RENDER_SERVICE_REGION", "").strip() or "-"
OUTBOUND_IP = os.getenv("OUTBOUND_IP_OVERRIDE", "").strip() or "-"
OUTBOUND_IP_LAST_CHECK_AT = 0.0
OUTBOUND_IP_LAST_ERROR = ""
OUTBOUND_IP_REFRESH_INTERVAL_SECONDS = max(900.0, float(os.getenv("OUTBOUND_IP_REFRESH_INTERVAL", "1800")))
OUTBOUND_IP_REFRESH_TASK: asyncio.Task | None = None


def _preserve_whitespace(fragment: str) -> tuple[str, str, str]:
    left = fragment[: len(fragment) - len(fragment.lstrip())]
    right = fragment[len(fragment.rstrip()):]
    core = fragment.strip()
    return left, core, right


def translate_sync(text: str, source_lang: str, target_lang: str) -> str:
    if ARGOS_WORKER is None:
        raise RuntimeError("Argos isolated runner is not started")

    spans = split_translation_spans(text, source_lang, target_lang)
    output: list[str] = []
    jobs: list[tuple[int, Protector, str, str, str]] = []
    glossary_count = 0

    for kind, fragment, glossary_target in spans:
        if kind == "protected":
            output.append(fragment)
            continue
        if kind == "twom":
            # Direct glossary replacement: the model never sees an artificial
            # placeholder, so it cannot leak the source-language term.
            output.append(glossary_target if glossary_target is not None else fragment)
            glossary_count += 1
            continue

        left, core, right = _preserve_whitespace(fragment)
        if not core:
            output.append(fragment)
            continue
        prepared, protector = prepare_for_translation(core, source_lang)
        output_index = len(output)
        output.append(None)  # type: ignore[arg-type]
        jobs.append((output_index, protector, left, right, prepared))

    if jobs:
        results = ARGOS_WORKER.translate_many(
            [job[4] for job in jobs], source_lang, target_lang
        )
        for job, result in zip(jobs, results):
            idx, protector, left, right, _prepared = job
            restored = protector.restore(result)
            output[idx] = left + restored + right

    combined = "".join(output)
    log.info(
        "Preserved non-translatable spans | source=%s | target=%s | protected_spans=%d | glossary_spans=%d | translated_spans=%d",
        source_lang,
        target_lang,
        sum(1 for kind, _, _ in spans if kind == "protected"),
        glossary_count,
        len(jobs),
    )
    return combined


def translate_all_sync(original: str) -> dict[str, str]:
    source_lang = detect_language(original)
    clean = strip_language_prefix(original).strip()

    if not clean:
        return {"source": source_lang, "en": "", "th": "", "ko": "", "errors": {}}

    log.info("Translating message | source=%s | chars=%d", source_lang, len(clean))
    targets: dict[str, str] = {}
    errors: dict[str, str] = {}

    def run_target(label: str, source: str, target: str, text: str) -> str | None:
        try:
            value = translate_sync(text, source, target)
            if not value.strip():
                raise RuntimeError(f"Empty translation result: {source}->{target}")
            targets[label] = value
            return value
        except Exception as exc:
            errors[f"{source}->{target}"] = str(exc)
            log.exception("Target translation failed | %s->%s", source, target)
            return None

    if source_lang == "en":
        run_target("th", "en", "th", clean)
        run_target("ko", "en", "ko", clean)
    elif source_lang == "th":
        # Correct pivot behavior: Thai -> English first, then English -> Korean
        # using the English result. v1.0.14 incorrectly sent raw Thai text into
        # the English->Korean model, which explains poor/empty Korean output.
        english = run_target("en", "th", "en", clean)
        if english:
            run_target("ko", "en", "ko", english)
    else:
        # Correct pivot behavior: Korean -> English first, then English -> Thai
        # using the English result.
        english = run_target("en", "ko", "en", clean)
        if english:
            run_target("th", "en", "th", english)

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
            # The HTTP service + translation worker are alive even while Discord
            # temporarily rate-limits the login request. Keep this 200 so a
            # Render health check does not turn a transient 429 into a restart loop.
            "ok": token_configured and worker_alive,
            "bot": "JAVIS",
            "version": JAVIS_VERSION,
            "engine": "Argos Translate + CTranslate2",
            "compute_type": os.getenv("ARGOS_COMPUTE_TYPE", "auto"),
            "device_type": os.getenv("ARGOS_DEVICE_TYPE", "cpu"),
            "chunk_type": os.getenv("ARGOS_CHUNK_TYPE", "MINISBD"),
            "batch_size": int(os.getenv("ARGOS_BATCH_SIZE", "1")),
            "beam_size": int(os.getenv("ARGOS_BEAM_SIZE", "2")),
            "korean_engine": os.getenv("ARGOS_KO_ENGINE", "argos-native"),
            "korean_beam_size": int(os.getenv("ARGOS_KO_BEAM_SIZE", "4")),
            "translation_worker_alive": worker_alive,
            "translation_runner_mode": "ct2-direct-argos-compatible-v1.0.20-per-target",
            "discord_session_recovery_mode": "fresh-httpclient-object-after-login-transport-failure",
            "quality_mode": "argos-compatible-decode-pieces+raw-target-prefix+pivot-fix+twom-span-glossary",
            "decode_mode": "SentencePiece.decode_pieces + U+2581 normalization + no-TWOM-placeholders",
            "pivot_mode": "th->en->ko and ko->en->th",
            "non_translatable_mode": "segment-preserve",
            "response_mode": "progressive-first-result-then-edit",
            "argos_packages_dir": os.getenv("ARGOS_PACKAGES_DIR", ""),
            "discord_token_configured": token_configured,
            "discord_ready": bot.is_ready(),
            "discord_login_state": DISCORD_LOGIN_STATE,
            "discord_login_retry_count": DISCORD_LOGIN_RETRY_COUNT,
            "discord_login_retry_after": DISCORD_LOGIN_RETRY_AFTER,
            "discord_login_session_recovery_count": DISCORD_LOGIN_SESSION_RECOVERY_COUNT,
            "discord_login_http_rebuild_count": DISCORD_LOGIN_HTTP_REBUILD_COUNT,
            "discord_quarantined": DISCORD_QUARANTINED,
            "discord_quarantine_context": DISCORD_QUARANTINE_CONTEXT,
            "discord_quarantine_retry_after": DISCORD_QUARANTINE_RETRY_AFTER,
            "discord_quarantine_cf_ray": DISCORD_QUARANTINE_CF_RAY,
            "discord_startup_handover_delay_seconds": DISCORD_STARTUP_HANDOVER_DELAY_SECONDS,
            "discord_shutdown_requested": DISCORD_SHUTDOWN_REQUESTED,
            "discord_login_last_status": DISCORD_LOGIN_LAST_STATUS,
            "discord_login_last_scope": DISCORD_LOGIN_LAST_SCOPE,
            "discord_login_last_global": DISCORD_LOGIN_LAST_GLOBAL,
            "discord_login_last_retry_after": DISCORD_LOGIN_LAST_RETRY_AFTER,
            "discord_login_last_cf_ray": DISCORD_LOGIN_LAST_CF_RAY,
            "discord_login_last_via": DISCORD_LOGIN_LAST_VIA,
            "discord_login_last_server": DISCORD_LOGIN_LAST_SERVER,
            "discord_login_last_message": DISCORD_LOGIN_LAST_MESSAGE,
            "discord_outbound_block_retry_after": max(0.0, DISCORD_OUTBOUND_BLOCKED_UNTIL_MONO - time.monotonic()),
            "discord_outbound_last_429_retry_after": DISCORD_OUTBOUND_LAST_429_RETRY_AFTER,
            "discord_outbound_last_429_context": DISCORD_OUTBOUND_LAST_429_CONTEXT,
            "discord_outbound_last_429_cf_ray": DISCORD_OUTBOUND_LAST_CF_RAY,
            "render_service": RENDER_SERVICE,
            "render_region": RENDER_REGION,
            "outbound_ip": OUTBOUND_IP,
            "outbound_ip_last_check_at": OUTBOUND_IP_LAST_CHECK_AT,
            "outbound_ip_last_error": OUTBOUND_IP_LAST_ERROR,
            "discord_http_attempt": DISCORD_HTTP_ATTEMPT,
            "discord_invalid_request_count": DISCORD_INVALID_REQUEST_COUNT,
            "discord_last_request_status_code": DISCORD_LAST_REQUEST_STATUS_CODE,
            "discord_last_request_429_scope": DISCORD_LAST_REQUEST_429_SCOPE,
            "discord_last_request_retry_after": DISCORD_LAST_REQUEST_RETRY_AFTER,
            "discord_last_request_cf_ray": DISCORD_LAST_REQUEST_CF_RAY,
            "discord_last_request_context": DISCORD_LAST_REQUEST_CONTEXT,
            "discord_last_request_route": DISCORD_LAST_REQUEST_ROUTE,
            "discord_request_telemetry": {
                "BOT_NAME": (os.getenv("BOT_NAME", "JARVIS").strip() or "JARVIS"),
                "BOT_USER_ID": str(getattr(getattr(bot, "user", None), "id", "") or "-"),
                "APPLICATION_ID": str(getattr(bot, "application_id", None) or os.getenv("DISCORD_APPLICATION_ID", "-") or "-"),
                "RENDER_SERVICE": RENDER_SERVICE,
                "RENDER_REGION": RENDER_REGION,
                "OUTBOUND_IP": OUTBOUND_IP,
                "DISCORD_HTTP_ATTEMPT": DISCORD_HTTP_ATTEMPT,
                "STATUS_CODE": DISCORD_LAST_REQUEST_STATUS_CODE,
                "429_SCOPE": DISCORD_LAST_REQUEST_429_SCOPE,
                "RETRY_AFTER": DISCORD_LAST_REQUEST_RETRY_AFTER,
                "CF-RAY": DISCORD_LAST_REQUEST_CF_RAY,
                "INVALID_REQUEST_COUNT": DISCORD_INVALID_REQUEST_COUNT,
                "REQUEST_CONTEXT": DISCORD_LAST_REQUEST_CONTEXT,
            },
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


def _safe_bot_name() -> str:
    user = getattr(bot, "user", None)
    return str(getattr(user, "name", "") or os.getenv("BOT_NAME", "JARVIS") or "JARVIS")


def _safe_bot_user_id() -> str:
    user = getattr(bot, "user", None)
    return str(getattr(user, "id", "") or "-")


def _safe_application_id() -> str:
    application_id = getattr(bot, "application_id", None)
    if application_id:
        return str(application_id)
    return os.getenv("DISCORD_APPLICATION_ID", "").strip() or "-"


def _sanitize_discord_route(route: object) -> str:
    method = str(getattr(route, "method", "?") or "?")
    path = str(getattr(route, "path", "") or "")
    if not path:
        path = str(getattr(route, "url", "") or route)
    path = re.sub(r"(/webhooks/[^/]+/)[^/?]+", r"\1[redacted]", path)
    path = re.sub(r"(/interactions/[^/]+/)[^/?]+", r"\1[redacted]", path)
    return f"{method} {path}"[:500]


def _extract_discord_error_metadata(exc: Exception) -> tuple[int | str, str, float, str]:
    status = getattr(exc, "status", None)
    try:
        status_value: int | str = int(status) if status is not None else "-"
    except (TypeError, ValueError):
        status_value = str(status or "-")
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    scope = "-"
    if status_value == 429:
        try:
            scope = str(headers.get("X-RateLimit-Scope") or headers.get("x-ratelimit-scope") or "unknown")
        except Exception:
            scope = "-"
    retry_after = 0.0
    try:
        retry_after = float(getattr(exc, "retry_after", 0) or 0)
    except (TypeError, ValueError):
        retry_after = 0.0
    if retry_after <= 0:
        for key in ("Retry-After", "retry-after", "X-RateLimit-Reset-After", "x-ratelimit-reset-after"):
            try:
                raw = headers.get(key)
            except Exception:
                raw = None
            if raw is None:
                continue
            try:
                retry_after = max(0.0, float(raw))
                break
            except (TypeError, ValueError):
                continue
    try:
        cf_ray = str(headers.get("CF-Ray") or headers.get("cf-ray") or "-")
    except Exception:
        cf_ray = "-"
    return status_value, scope, retry_after, cf_ray


def _record_discord_request_telemetry(*, status_code: int | str, scope: str = "-", retry_after: float = 0.0, cf_ray: str = "-", context: str = "-", route: str = "-") -> None:
    global DISCORD_INVALID_REQUEST_COUNT
    global DISCORD_LAST_REQUEST_STATUS_CODE, DISCORD_LAST_REQUEST_429_SCOPE
    global DISCORD_LAST_REQUEST_RETRY_AFTER, DISCORD_LAST_REQUEST_CF_RAY
    global DISCORD_LAST_REQUEST_CONTEXT, DISCORD_LAST_REQUEST_ROUTE

    normalized_scope = str(scope or "-")
    if status_code in {401, 403, 429} and not (status_code == 429 and normalized_scope == "shared"):
        DISCORD_INVALID_REQUEST_COUNT += 1

    DISCORD_LAST_REQUEST_STATUS_CODE = status_code
    DISCORD_LAST_REQUEST_429_SCOPE = normalized_scope if status_code == 429 else "-"
    DISCORD_LAST_REQUEST_RETRY_AFTER = max(0.0, float(retry_after or 0.0))
    DISCORD_LAST_REQUEST_CF_RAY = str(cf_ray or "-")
    DISCORD_LAST_REQUEST_CONTEXT = str(context or "-")[:500]
    DISCORD_LAST_REQUEST_ROUTE = str(route or "-")[:500]

    log.info(
        "Discord request telemetry | BOT_NAME=%s | BOT_USER_ID=%s | APPLICATION_ID=%s | RENDER_SERVICE=%s | RENDER_REGION=%s | OUTBOUND_IP=%s | DISCORD_HTTP_ATTEMPT=%d | STATUS_CODE=%s | 429_SCOPE=%s | RETRY_AFTER=%.3f | CF-RAY=%s | INVALID_REQUEST_COUNT=%d | REQUEST_CONTEXT=%s | ROUTE=%s",
        _safe_bot_name(), _safe_bot_user_id(), _safe_application_id(), RENDER_SERVICE, RENDER_REGION,
        OUTBOUND_IP, DISCORD_HTTP_ATTEMPT, status_code, normalized_scope, max(0.0, float(retry_after or 0.0)),
        cf_ray or "-", DISCORD_INVALID_REQUEST_COUNT, str(context or "-")[:500], str(route or "-")[:500],
    )


async def _telemetry_wrapped_discord_http_request(original_request, route, *args, **kwargs):
    global DISCORD_HTTP_ATTEMPT
    DISCORD_HTTP_ATTEMPT += 1
    route_text = _sanitize_discord_route(route)
    context = DISCORD_REQUEST_CONTEXT.get() or f"discord-api:{route_text}"
    try:
        result = await original_request(route, *args, **kwargs)
        _record_discord_request_telemetry(status_code="2xx", context=context, route=route_text)
        return result
    except discord.HTTPException as exc:
        status_code, scope, retry_after, cf_ray = _extract_discord_error_metadata(exc)
        _record_discord_request_telemetry(status_code=status_code, scope=scope, retry_after=retry_after, cf_ray=cf_ray, context=context, route=route_text)
        raise
    except Exception:
        _record_discord_request_telemetry(status_code="transport-error", context=context, route=route_text)
        raise


def _install_discord_http_telemetry() -> None:
    http_client = getattr(bot, "http", None)
    if http_client is None or getattr(http_client, "_javis_telemetry_wrapped", False):
        return
    original_request = http_client.request

    async def _wrapped(self, route, *args, **kwargs):
        return await _telemetry_wrapped_discord_http_request(original_request, route, *args, **kwargs)

    http_client.request = MethodType(_wrapped, http_client)
    http_client._javis_telemetry_wrapped = True
    http_client._javis_telemetry_original_request = original_request
    log.info("Discord HTTP telemetry installed | client=%s", type(http_client).__name__)


async def _refresh_outbound_ip() -> None:
    global OUTBOUND_IP, OUTBOUND_IP_LAST_CHECK_AT, OUTBOUND_IP_LAST_ERROR
    override = os.getenv("OUTBOUND_IP_OVERRIDE", "").strip()
    if override:
        OUTBOUND_IP = override
        OUTBOUND_IP_LAST_CHECK_AT = time.time()
        OUTBOUND_IP_LAST_ERROR = ""
        log.info("Render outbound IP override configured | OUTBOUND_IP=%s", OUTBOUND_IP)
        return

    timeout = aiohttp.ClientTimeout(total=8)
    providers = ("https://api.ipify.org?format=json", "https://ifconfig.me/ip")
    last_error = ""
    for url in providers:
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": "JAVIS/1.0.27"}) as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        last_error = f"{url} -> HTTP {response.status}"
                        continue
                    if "ipify" in url:
                        data = await response.json(content_type=None)
                        ip_value = str(data.get("ip") or "").strip() if isinstance(data, dict) else ""
                    else:
                        ip_value = (await response.text()).strip()
                    if ip_value and len(ip_value) <= 64:
                        OUTBOUND_IP = ip_value
                        OUTBOUND_IP_LAST_CHECK_AT = time.time()
                        OUTBOUND_IP_LAST_ERROR = ""
                        log.info("Render outbound IP detected | OUTBOUND_IP=%s | provider=%s | RENDER_SERVICE=%s | RENDER_REGION=%s", OUTBOUND_IP, url, RENDER_SERVICE, RENDER_REGION)
                        return
                    last_error = f"{url} -> empty IP response"
        except Exception as exc:
            last_error = f"{url} -> {type(exc).__name__}: {exc}"
    OUTBOUND_IP_LAST_CHECK_AT = time.time()
    OUTBOUND_IP_LAST_ERROR = last_error[:500]
    log.warning("Render outbound IP detection failed safely | error=%s", OUTBOUND_IP_LAST_ERROR)


async def _outbound_ip_refresh_worker() -> None:
    while not DISCORD_SHUTDOWN_EVENT.is_set():
        try:
            await asyncio.wait_for(DISCORD_SHUTDOWN_EVENT.wait(), timeout=OUTBOUND_IP_REFRESH_INTERVAL_SECONDS)
            break
        except asyncio.TimeoutError:
            await _refresh_outbound_ip()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Outbound IP refresh worker failed safely | %r", exc)


def get_discord_token() -> str:
    return (
        os.getenv("DISCORD_TOKEN", "").strip()
        or os.getenv("DISCORD_BOT_TOKEN", "").strip()
    )


intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!javis ", intents=intents, help_command=None)
ALLOWED_MENTIONS = discord.AllowedMentions.none()
_install_discord_http_telemetry()


@bot.event
async def on_ready():
    global DISCORD_LOGIN_STATE, DISCORD_LOGIN_RETRY_AFTER
    DISCORD_LOGIN_STATE = "ready"
    DISCORD_LOGIN_RETRY_AFTER = 0.0
    log.info(
        "Logged in as %s (%s) | JAVIS v%s",
        bot.user,
        bot.user.id if bot.user else "?",
        JAVIS_VERSION,
    )
    await bot.change_presence(activity=discord.Game(name="TH ↔ EN ↔ KO | TWOM"))
    _ensure_translation_room_command_sync_task()


@bot.command(name="status")
async def status_command(ctx: commands.Context):
    worker_alive = ARGOS_WORKER is not None and ARGOS_WORKER.started
    await safe_context_reply(ctx,
        "JAVIS พร้อมใช้งาน ✅\n"
        f"Version: v{JAVIS_VERSION}\n"
        "Engine: Argos Translate + CTranslate2\n"
        f"Quantization: {os.getenv('ARGOS_COMPUTE_TYPE', 'auto')}\n"
        f"Device: {os.getenv('ARGOS_DEVICE_TYPE', 'cpu')}\n"
        f"Batch: {os.getenv('ARGOS_BATCH_SIZE', '1')} | Beam: {os.getenv('ARGOS_BEAM_SIZE', '2')}\n"
        f"Korean engine: {os.getenv('ARGOS_KO_ENGINE', 'argos-native')} | Korean beam: {os.getenv('ARGOS_KO_BEAM_SIZE', '4')}\n"
        f"Chunking: {os.getenv('ARGOS_CHUNK_TYPE', 'MINISBD')}\n"
        f"Argos Worker: {'ONLINE ✅' if worker_alive else 'OFFLINE ❌'}\n"
        "Languages: TH ↔ EN ↔ KO",
        mention_author=False,
        allowed_mentions=ALLOWED_MENTIONS,
    )


@bot.command(name="help")
async def help_command(ctx: commands.Context):
    await safe_context_reply(ctx,
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
        await safe_context_reply(ctx,
            f"โหลด TWOM Dictionary ไม่สำเร็จ: `{exc}`",
            mention_author=False,
            allowed_mentions=ALLOWED_MENTIONS,
        )
        return
    await safe_context_reply(ctx,
        f"โหลด TWOM Dictionary ใหม่แล้ว ✅ ({len(TWOM_DICTIONARY)} entries)",
        mention_author=False,
        allowed_mentions=ALLOWED_MENTIONS,
    )


async def _sync_translation_room_commands_once() -> None:
    """Register room-management slash commands once, respecting Discord 429s."""
    global TRANSLATION_ROOM_COMMAND_SYNC_STATE, TRANSLATION_ROOM_COMMAND_SYNC_RETRY_AFTER
    configured_guild_id = os.getenv("DISCORD_GUILD_ID", "").strip()

    async def _do_sync() -> int:
        if configured_guild_id.isdigit() and int(configured_guild_id) > 0:
            guild = discord.Object(id=int(configured_guild_id))
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            TRANSLATION_ROOM_COMMAND_SYNC_STATE = "synced_guild"
        else:
            synced = await bot.tree.sync()
            TRANSLATION_ROOM_COMMAND_SYNC_STATE = "synced_global"
        TRANSLATION_ROOM_COMMAND_SYNC_RETRY_AFTER = 0.0
        return len(synced)

    if _discord_http_quarantined():
        TRANSLATION_ROOM_COMMAND_SYNC_STATE = "quarantined"
        log.warning("Translation room slash-command sync skipped during Discord 429 quarantine | no HTTP sent")
        return

    try:
        count = await _do_sync()
        log.info(
            "Translation room slash commands synced | commands=%d | scope=%s",
            count,
            "guild" if configured_guild_id.isdigit() and int(configured_guild_id) > 0 else "global",
        )
        return
    except discord.HTTPException as exc:
        if getattr(exc, "status", None) != 429:
            TRANSLATION_ROOM_COMMAND_SYNC_STATE = "sync_failed"
            log.warning(
                "Translation room slash-command sync failed safely | status=%s | %r",
                getattr(exc, "status", None),
                exc,
            )
            return

        retry_after, _ = _discord_retry_after_seconds(exc, 60.0)
        TRANSLATION_ROOM_COMMAND_SYNC_RETRY_AFTER = retry_after
        TRANSLATION_ROOM_COMMAND_SYNC_STATE = "quarantined"
        log.warning(
            "Translation room slash-command sync rate-limited | retry_after=%.1fs | quarantining | no retry will be sent",
            retry_after,
        )
        try:
            _record_discord_outbound_429(exc, context="startup:translation-room-command-sync")
        except Exception:
            pass
        return
    except Exception as exc:
        TRANSLATION_ROOM_COMMAND_SYNC_STATE = "sync_failed"
        TRANSLATION_ROOM_COMMAND_SYNC_RETRY_AFTER = 0.0
        log.warning("Translation room slash-command sync failed safely | %r", exc)


def _ensure_translation_room_command_sync_task() -> None:
    global TRANSLATION_ROOM_COMMAND_SYNC_STARTED
    if TRANSLATION_ROOM_COMMAND_SYNC_STARTED:
        return
    TRANSLATION_ROOM_COMMAND_SYNC_STARTED = True
    asyncio.create_task(
        _sync_translation_room_commands_once(),
        name="javis-translation-room-command-sync",
    )


async def _safe_interaction_message(
    interaction: discord.Interaction,
    content: str,
    *,
    ephemeral: bool = True,
) -> bool:
    """Best-effort interaction response for room-management commands."""
    if interaction.response.is_done():
        return False
    remaining = _discord_outbound_block_remaining()
    if _discord_http_quarantined() or remaining > 0:
        log.warning(
            "Discord room-management interaction skipped during outbound quarantine/cooldown | remaining=%.1fs | no HTTP sent",
            remaining,
        )
        return False
    try:
        await asyncio.wait_for(
            interaction.response.send_message(
                content,
                ephemeral=ephemeral,
                allowed_mentions=ALLOWED_MENTIONS,
            ),
            timeout=5,
        )
        return True
    except asyncio.TimeoutError:
        log.warning("Discord room-management interaction timed out safely")
    except discord.HTTPException as exc:
        if getattr(exc, "status", None) == 429:
            _record_discord_outbound_429(exc, context="translation-room:interaction")
        else:
            log.warning(
                "Discord room-management interaction failed | status=%s",
                getattr(exc, "status", None),
            )
    except Exception as exc:
        log.warning("Discord room-management interaction failed safely | %r", exc)
    return False


@bot.tree.command(name="setroom", description="กำหนดห้องที่ JAVIS จะอ่านข้อความและแปล")
@app_commands.describe(channel="เลือกห้องข้อความที่ต้องการเปิดการแปล")
@app_commands.checks.has_permissions(manage_channels=True)
async def setroom_command(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
    if interaction.guild is None:
        await _safe_interaction_message(
            interaction,
            "❌ คำสั่งนี้ใช้ได้เฉพาะในเซิร์ฟเวอร์ Discord",
        )
        return

    guild_key = str(interaction.guild.id)
    async with TRANSLATION_ROOMS_IO_LOCK:
        room_ids = TRANSLATION_ROOMS.setdefault(guild_key, set())
        already_enabled = channel.id in room_ids
        room_ids.add(channel.id)
        if not already_enabled:
            await asyncio.to_thread(_save_translation_rooms_sync)

    text = (
        f"ℹ️ ห้อง {channel.mention} เปิดการแปลของ JAVIS อยู่แล้ว"
        if already_enabled
        else f"✅ เปิดการแปลของ JAVIS สำหรับห้อง {channel.mention} แล้ว"
    )
    await _safe_interaction_message(interaction, text)
    log.info(
        "Translation room enabled | guild=%s | channel=%s | changed=%s",
        interaction.guild.id,
        channel.id,
        not already_enabled,
    )


@bot.tree.command(name="delroom", description="ลบห้องออกจากรายการห้องที่ JAVIS จะแปล")
@app_commands.describe(channel="เลือกห้องข้อความที่จะปิดการแปล")
@app_commands.checks.has_permissions(manage_channels=True)
async def delroom_command(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
    if interaction.guild is None:
        await _safe_interaction_message(
            interaction,
            "❌ คำสั่งนี้ใช้ได้เฉพาะในเซิร์ฟเวอร์ Discord",
        )
        return

    guild_key = str(interaction.guild.id)
    removed = False
    async with TRANSLATION_ROOMS_IO_LOCK:
        room_ids = TRANSLATION_ROOMS.get(guild_key, set())
        if channel.id in room_ids:
            room_ids.remove(channel.id)
            removed = True
            if room_ids:
                TRANSLATION_ROOMS[guild_key] = room_ids
            else:
                TRANSLATION_ROOMS.pop(guild_key, None)
            await asyncio.to_thread(_save_translation_rooms_sync)

    text = (
        f"✅ ลบห้อง {channel.mention} ออกจากรายการแปลแล้ว"
        if removed
        else f"ℹ️ ห้อง {channel.mention} ไม่ได้อยู่ในรายการแปล"
    )
    await _safe_interaction_message(interaction, text)
    log.info(
        "Translation room disabled | guild=%s | channel=%s | changed=%s",
        interaction.guild.id,
        channel.id,
        removed,
    )


@bot.tree.command(name="roomlist", description="ดูรายชื่อห้องที่ JAVIS เปิดการแปลไว้")
async def roomlist_command(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await _safe_interaction_message(
            interaction,
            "❌ คำสั่งนี้ใช้ได้เฉพาะในเซิร์ฟเวอร์ Discord",
        )
        return

    room_ids = sorted(_translation_room_ids(interaction.guild.id))
    if not room_ids:
        text = "ℹ️ ยังไม่มีห้องที่เปิดการแปลของ JAVIS ในเซิร์ฟเวอร์นี้\nใช้ `/setroom` เพื่อเลือกห้อง"
    else:
        mentions = []
        stale_ids = []
        for channel_id in room_ids:
            channel = interaction.guild.get_channel(channel_id)
            if isinstance(channel, discord.TextChannel):
                mentions.append(channel.mention)
            else:
                stale_ids.append(channel_id)
        if stale_ids:
            async with TRANSLATION_ROOMS_IO_LOCK:
                current = TRANSLATION_ROOMS.get(str(interaction.guild.id), set())
                current.difference_update(stale_ids)
                if current:
                    TRANSLATION_ROOMS[str(interaction.guild.id)] = current
                else:
                    TRANSLATION_ROOMS.pop(str(interaction.guild.id), None)
                await asyncio.to_thread(_save_translation_rooms_sync)
        if mentions:
            text = "📚 ห้องที่เปิดการแปลของ JAVIS:\n" + "\n".join(
                f"• {mention}" for mention in mentions
            )
        else:
            text = "ℹ️ ไม่มีห้องที่ยังใช้งานได้ในรายการแปล\nใช้ `/setroom` เพื่อเลือกห้อง"

    await _safe_interaction_message(interaction, text)


@bot.tree.error
async def _translation_room_tree_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    if isinstance(error, app_commands.MissingPermissions):
        await _safe_interaction_message(
            interaction,
            "❌ คำสั่งนี้ต้องมีสิทธิ์ Manage Channels",
        )
        return
    log.warning("Discord slash-command error | %r", error)
    await _safe_interaction_message(
        interaction,
        "❌ เกิดข้อผิดพลาดกับคำสั่ง Slash Command กรุณาลองใหม่",
    )


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingPermissions):
        await safe_context_reply(ctx,
            "คำสั่งนี้ต้องมีสิทธิ์ Manage Server",
            mention_author=False,
            allowed_mentions=ALLOWED_MENTIONS,
        )
        return
    log.exception("Discord command error", exc_info=error)


def _discord_outbound_block_remaining() -> float:
    return max(0.0, DISCORD_OUTBOUND_BLOCKED_UNTIL_MONO - time.monotonic())


def _discord_http_quarantined() -> bool:
    return bool(DISCORD_QUARANTINED)


def _enter_discord_429_quarantine(*, retry_after: float, context: str, cf_ray: str = "") -> None:
    """Hard-stop further Discord HTTP attempts in this process after a 429."""
    global DISCORD_QUARANTINED
    global DISCORD_QUARANTINE_CONTEXT, DISCORD_QUARANTINE_RETRY_AFTER, DISCORD_QUARANTINE_CF_RAY

    DISCORD_QUARANTINED = True
    DISCORD_QUARANTINE_CONTEXT = str(context or "-")[:500]
    DISCORD_QUARANTINE_RETRY_AFTER = max(0.0, float(retry_after or 0.0))
    DISCORD_QUARANTINE_CF_RAY = str(cf_ray or "-")[:200]
    log.warning(
        "Discord 429 quarantine engaged | context=%s | retry_after=%.3fs | cf_ray=%s | no further Discord HTTP requests will be attempted by this process",
        DISCORD_QUARANTINE_CONTEXT,
        DISCORD_QUARANTINE_RETRY_AFTER,
        DISCORD_QUARANTINE_CF_RAY or "-",
    )


def _record_discord_outbound_429(exc: Exception, *, context: str) -> float:
    """Arm the outbound cooldown using Discord's Retry-After metadata."""
    global DISCORD_OUTBOUND_BLOCKED_UNTIL_MONO
    global DISCORD_OUTBOUND_LAST_429_RETRY_AFTER, DISCORD_OUTBOUND_LAST_429_CONTEXT
    global DISCORD_OUTBOUND_LAST_CF_RAY

    retry_after = 0.0
    try:
        retry_after = float(getattr(exc, "retry_after", 0) or 0)
    except (TypeError, ValueError):
        retry_after = 0.0
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    if retry_after <= 0:
        for key in ("Retry-After", "retry-after", "X-RateLimit-Reset-After", "x-ratelimit-reset-after"):
            raw = headers.get(key) if hasattr(headers, "get") else None
            if raw is None:
                continue
            try:
                retry_after = max(0.0, float(raw))
                break
            except (TypeError, ValueError):
                pass
    retry_after = max(1.0, retry_after)

    try:
        cf_ray = str(headers.get("CF-Ray") or headers.get("cf-ray") or "")
    except Exception:
        cf_ray = ""

    DISCORD_OUTBOUND_BLOCKED_UNTIL_MONO = max(
        DISCORD_OUTBOUND_BLOCKED_UNTIL_MONO,
        time.monotonic() + retry_after,
    )
    DISCORD_OUTBOUND_LAST_429_RETRY_AFTER = retry_after
    DISCORD_OUTBOUND_LAST_429_CONTEXT = context
    DISCORD_OUTBOUND_LAST_CF_RAY = cf_ray
    log.warning(
        "Discord outbound REST cooldown armed | context=%s | retry_after=%.3fs | cf_ray=%s | no fallback HTTP will be sent",
        context,
        retry_after,
        cf_ray or "-",
    )
    _enter_discord_429_quarantine(
        retry_after=retry_after,
        context=context,
        cf_ray=cf_ray,
    )
    return retry_after


async def send_safe_translation(message: discord.Message, content: str) -> discord.Message | None:
    """Single-attempt translation send; never retry a failed Discord HTTP call."""
    remaining = _discord_outbound_block_remaining()
    if _discord_http_quarantined() or remaining > 0:
        log.warning(
            "Discord translation send skipped during outbound quarantine/cooldown | remaining=%.1fs | no HTTP sent",
            remaining,
        )
        return None
    try:
        log.info("Discord send start | channel=%s | chars=%d", getattr(message.channel, "id", "?"), len(content))
        _ctx_token = DISCORD_REQUEST_CONTEXT.set("translation:channel-send")
        try:
            sent = await asyncio.wait_for(
                message.channel.send(content, allowed_mentions=ALLOWED_MENTIONS),
                timeout=20,
            )
        finally:
            DISCORD_REQUEST_CONTEXT.reset(_ctx_token)
        log.info("Discord send complete | channel=%s | chars=%d", getattr(message.channel, "id", "?"), len(content))
        return sent
    except asyncio.TimeoutError:
        log.warning("Discord translation send timed out safely; no fallback HTTP request will be sent")
    except discord.HTTPException as exc:
        if getattr(exc, "status", None) == 429:
            _record_discord_outbound_429(exc, context="translation:channel-send")
        else:
            log.warning(
                "Discord translation send HTTP failed | status=%s | no fallback HTTP request will be sent",
                getattr(exc, "status", None),
            )
    except Exception as exc:
        log.warning("Discord translation send failed safely | %r | no fallback HTTP request will be sent", exc)
    return None


async def edit_safe_translation(sent_message: discord.Message, content: str) -> bool:
    """Single-attempt progressive edit; a failed HTTP call is never retried."""
    remaining = _discord_outbound_block_remaining()
    if _discord_http_quarantined() or remaining > 0:
        log.warning(
            "Discord translation edit skipped during outbound quarantine/cooldown | remaining=%.1fs | no HTTP sent",
            remaining,
        )
        return False
    try:
        _ctx_token = DISCORD_REQUEST_CONTEXT.set("translation:message-edit")
        try:
            await asyncio.wait_for(
                sent_message.edit(content=content, allowed_mentions=ALLOWED_MENTIONS),
                timeout=20,
            )
        finally:
            DISCORD_REQUEST_CONTEXT.reset(_ctx_token)
        return True
    except asyncio.TimeoutError:
        log.warning("Discord translation edit timed out safely; no fallback HTTP request will be sent")
    except discord.HTTPException as exc:
        if getattr(exc, "status", None) == 429:
            _record_discord_outbound_429(exc, context="translation:message-edit")
        else:
            log.warning(
                "Discord translation edit HTTP failed | status=%s | no fallback HTTP request will be sent",
                getattr(exc, "status", None),
            )
    except Exception as exc:
        log.warning("Discord translation edit failed safely | %r | no fallback HTTP request will be sent", exc)
    return False


async def safe_context_reply(ctx: commands.Context, content: str, **kwargs) -> bool:
    """Command reply helper that never retries after a Discord 429 quarantine."""
    if _discord_http_quarantined() or _discord_outbound_block_remaining() > 0:
        log.warning("Discord command reply skipped during outbound quarantine/cooldown | no HTTP sent")
        return False
    try:
        _ctx_token = DISCORD_REQUEST_CONTEXT.set("command:reply")
        try:
            await asyncio.wait_for(
                ctx.reply(
                    content,
                    **kwargs,
                ),
                timeout=20,
            )
        finally:
            DISCORD_REQUEST_CONTEXT.reset(_ctx_token)
        return True
    except asyncio.TimeoutError:
        log.warning("Discord command reply timed out safely; no retry HTTP request will be sent")
    except discord.HTTPException as exc:
        if getattr(exc, "status", None) == 429:
            _record_discord_outbound_429(exc, context="command:reply")
        else:
            log.warning("Discord command reply HTTP failed | status=%s | no retry HTTP request will be sent", getattr(exc, "status", None))
    except Exception as exc:
        log.warning("Discord command reply failed safely | %r | no retry HTTP request will be sent", exc)
    return False


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # Preserve command handling and do not translate commands.
    if message.content.startswith("!javis"):
        await bot.process_commands(message)
        return

    # Translation is opt-in per server/channel. DMs and unconfigured rooms
    # are ignored and do not trigger Argos or Discord output requests.
    if message.guild is None:
        return
    if not _is_translation_room_enabled(message.guild.id, message.channel.id):
        return

    content = message.content.strip()
    if not content or len(content) > MAX_INPUT_CHARS:
        return
    if content.startswith(("/", "$", "?")):
        return

    # URL/emoji-only messages are ignored. Mixed messages remain translatable;
    # their URL/emoji/mentions are preserved exactly and never sent through
    # the translation model.
    if not has_translatable_text_after_removing_nontext(content):
        log.info("Skipping non-translatable message | reason=url-or-emoji-only | chars=%d", len(content))
        return

    try:
        source_lang = detect_language(content)
        clean = strip_language_prefix(content).strip()
        if not clean:
            return

        # Keep the memory-safe one-target-at-a-time architecture, but expose
        # the first completed translation immediately and edit that same
        # Discord message after the second target finishes. This improves
        # perceived response time without loading two models concurrently.
        async with TRANSLATION_LOCK:
            log.info("Translating message | source=%s | chars=%d", source_lang, len(clean))

            result: dict[str, str] = {"source": source_lang, "en": "", "th": "", "ko": "", "errors": {}}
            sent_message: discord.Message | None = None

            async def run_target_async(label: str, source: str, target: str, text: str) -> str | None:
                try:
                    value = await asyncio.to_thread(translate_sync, text, source, target)
                    if not value.strip():
                        raise RuntimeError(f"Empty translation result: {source}->{target}")
                    result[label] = value
                    return value
                except Exception as exc:
                    result.setdefault("errors", {})[f"{source}->{target}"] = str(exc)
                    log.exception("Target translation failed | %s->%s", source, target)
                    return None

            def first_response(label: str) -> str:
                return f"[{LANG_NAMES[label]}] {result[label]}"

            if source_lang == "en":
                first = await run_target_async("th", "en", "th", clean)
                if first:
                    sent_message = await send_safe_translation(message, first_response("th"))
                second = await run_target_async("ko", "en", "ko", clean)
            elif source_lang == "th":
                first = await run_target_async("en", "th", "en", clean)
                if first:
                    sent_message = await send_safe_translation(message, first_response("en"))
                second = await run_target_async("ko", "en", "ko", first) if first else None
            else:
                first = await run_target_async("en", "ko", "en", clean)
                if first:
                    sent_message = await send_safe_translation(message, first_response("en"))
                second = await run_target_async("th", "en", "th", first) if first else None

            response = build_response(result)
            log.info(
                "Translation completed | source=%s | response_chars=%d | response_mode=progressive-edit",
                result["source"],
                len(response),
            )

            if sent_message is not None and len(response) <= 1900:
                if response != sent_message.content:
                    log.info(
                        "Discord edit start | channel=%s | chars=%d",
                        getattr(message.channel, "id", "?"),
                        len(response),
                    )
                    if await edit_safe_translation(sent_message, response):
                        log.info(
                            "Discord edit complete | channel=%s | chars=%d",
                            getattr(message.channel, "id", "?"),
                            len(response),
                        )
                    else:
                        return
                return

            # Fallback for unusually large output or when no first target
            # completed. This keeps the existing safe send/chunking behavior.
            if len(response) <= 1900:
                if sent_message is None:
                    await send_safe_translation(message, response)
                return

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

            if sent_message is not None:
                if not await edit_safe_translation(sent_message, chunks[0]):
                    return
                for chunk in chunks[1:]:
                    if await send_safe_translation(message, chunk) is None:
                        break
            else:
                await send_safe_translation(message, chunks[0])
                for chunk in chunks[1:]:
                    if await send_safe_translation(message, chunk) is None:
                        break
    except Exception as exc:
        log.exception("Translation failed")
        if _discord_outbound_block_remaining() > 0:
            log.warning("Translation error response suppressed during Discord outbound cooldown | no HTTP sent")
            return
        await send_safe_translation(
            message,
            "JAVIS แปลข้อความนี้ไม่สำเร็จ กรุณาลองใหม่อีกครั้ง",
        )


def _discord_retry_after_seconds(exc: object, fallback_seconds: float) -> tuple[float, str]:
    """Return Discord's requested wait time for rate limiting, with a safe fallback.

    Discord recommends honoring the retry-after / rate-limit reset values rather
    than retrying immediately. For discord.py's HTTPException we read the
    response headers; newer discord.py versions may also expose RateLimited with
    a retry_after attribute.
    """
    retry_after = getattr(exc, "retry_after", None)
    if retry_after is not None:
        try:
            value = float(retry_after)
            if value >= 0:
                return value, "discordpy-retry_after"
        except (TypeError, ValueError):
            pass

    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}

    for key in ("Retry-After", "X-RateLimit-Reset-After"):
        raw = headers.get(key) if hasattr(headers, "get") else None
        if raw is None:
            continue
        try:
            value = float(raw)
            if value >= 0:
                return value, key
        except (TypeError, ValueError):
            continue

    return max(30.0, fallback_seconds), "fallback-backoff"


async def _reset_discord_client_state_after_login_error(reason: str) -> None:
    """Reset discord.py client state after a failed login attempt."""
    try:
        bot.clear()
        log.info("Discord client state reset after login error | reason=%s", reason)
    except Exception:
        log.exception("Discord client state reset after login error failed | reason=%s", reason)


async def _rebuild_discord_http_client(reason: str) -> None:
    """Replace the discord.py HTTPClient object after a failed login transport.

    v1.0.22 closed bot.http and then reused the same HTTPClient object.  In the
    production log, the next static_login() still reached an already-closed
    aiohttp ClientSession and entered a permanent ``Session is closed`` loop.

    discord.py constructs HTTPClient lazily around a ClientSession. Replacing the
    HTTPClient object itself is safer than trying to resurrect its private session
    state. This function performs only local transport cleanup; it sends no Discord
    request.
    """
    global DISCORD_LOGIN_HTTP_REBUILD_COUNT

    old_http = getattr(bot, "http", None)
    loop = asyncio.get_running_loop()

    # Preserve supported HTTPClient configuration where available. Do not reuse
    # the old connector/session after a transport failure.
    proxy = getattr(old_http, "proxy", None)
    proxy_auth = getattr(old_http, "proxy_auth", None)
    http_trace = getattr(old_http, "http_trace", None)
    max_ratelimit_timeout = getattr(old_http, "max_ratelimit_timeout", None)
    use_clock = bool(getattr(old_http, "use_clock", False))

    if old_http is not None:
        try:
            await old_http.close()
        except Exception:
            log.exception("Failed to close old Discord HTTP client | reason=%s", reason)

    try:
        from discord.http import HTTPClient

        try:
            new_http = HTTPClient(
                loop,
                None,
                proxy=proxy,
                proxy_auth=proxy_auth,
                unsync_clock=not use_clock,
                http_trace=http_trace,
                max_ratelimit_timeout=max_ratelimit_timeout,
            )
        except TypeError:
            # Compatibility fallback for an older discord.py constructor.
            new_http = HTTPClient(loop, None, proxy=proxy, proxy_auth=proxy_auth)
        bot.http = new_http
        DISCORD_LOGIN_HTTP_REBUILD_COUNT += 1
        _install_discord_http_telemetry()
        log.info(
            "Discord HTTP client object rebuilt | rebuild_count=%d | reason=%s | fresh_session=deferred_until_login",
            DISCORD_LOGIN_HTTP_REBUILD_COUNT,
            reason,
        )
    except Exception:
        log.exception("Discord HTTP client rebuild failed | reason=%s", reason)
        raise


def _discord_http_429_diagnostics(exc: Exception) -> dict[str, object]:
    """Extract only safe 429 metadata needed to identify the blocking layer."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}

    def header(*names: str) -> str:
        for name in names:
            try:
                value = headers.get(name)
            except Exception:
                value = None
            if value is not None:
                return str(value)
        return ""

    text = str(getattr(exc, "text", "") or "")
    if len(text) > 500:
        text = text[:500]

    return {
        "scope": header("X-RateLimit-Scope", "x-ratelimit-scope"),
        "global": header("X-RateLimit-Global", "x-ratelimit-global").lower() == "true",
        "limit": header("X-RateLimit-Limit", "x-ratelimit-limit"),
        "remaining": header("X-RateLimit-Remaining", "x-ratelimit-remaining"),
        "reset_after": header("X-RateLimit-Reset-After", "x-ratelimit-reset-after"),
        "bucket": header("X-RateLimit-Bucket", "x-ratelimit-bucket"),
        "retry_after_header": header("Retry-After", "retry-after"),
        "cf_ray": header("CF-Ray", "cf-ray"),
        "via": header("Via", "via"),
        "server": header("Server", "server"),
        "content_type": header("Content-Type", "content-type"),
        "text": text,
    }


def _install_discord_shutdown_signal_handlers() -> None:
    """Let Render SIGTERM close the Gateway cleanly during zero-downtime handover."""
    global DISCORD_SHUTDOWN_REQUESTED
    DISCORD_SHUTDOWN_REQUESTED = False
    DISCORD_SHUTDOWN_EVENT.clear()
    loop = asyncio.get_running_loop()

    def _request_shutdown(signum):
        global DISCORD_SHUTDOWN_REQUESTED
        DISCORD_SHUTDOWN_REQUESTED = True
        try:
            signal_name = getattr(signal.Signals(signum), "name", str(signum))
        except Exception:
            signal_name = str(signum)
        log.warning("Discord shutdown requested by %s | graceful handover", signal_name)
        DISCORD_SHUTDOWN_EVENT.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _request_shutdown, sig)
        except (NotImplementedError, RuntimeError, ValueError):
            pass


async def _wait_for_discord_startup_handover() -> bool:
    """Hold only the first Gateway/REST login during a Render instance handover."""
    global DISCORD_LOGIN_STATE, DISCORD_LOGIN_RETRY_AFTER

    delay = DISCORD_STARTUP_HANDOVER_DELAY_SECONDS
    if delay <= 0:
        return True

    DISCORD_LOGIN_STATE = "handover_waiting"
    DISCORD_LOGIN_RETRY_AFTER = delay
    log.warning(
        "Discord startup handover hold | wait=%.1fs | reason=Render zero-downtime deploy protection | no Discord request sent",
        delay,
    )
    try:
        await asyncio.wait_for(DISCORD_SHUTDOWN_EVENT.wait(), timeout=delay)
        DISCORD_LOGIN_STATE = "shutdown_requested"
        DISCORD_LOGIN_RETRY_AFTER = 0.0
        return False
    except asyncio.TimeoutError:
        DISCORD_LOGIN_RETRY_AFTER = 0.0
        if DISCORD_SHUTDOWN_EVENT.is_set():
            DISCORD_LOGIN_STATE = "shutdown_requested"
            return False
        log.info("Discord startup handover hold complete | first Discord login may proceed")
        return True


async def _start_discord_with_rate_limit_retry(discord_token: str) -> None:
    """Start Discord; after any HTTP 429, record Retry-After and hard-quarantine this process without retrying."""
    global DISCORD_LOGIN_STATE, DISCORD_LOGIN_RETRY_COUNT, DISCORD_LOGIN_RETRY_AFTER
    global DISCORD_LOGIN_SESSION_RECOVERY_COUNT
    global DISCORD_LOGIN_LAST_STATUS, DISCORD_LOGIN_LAST_SCOPE, DISCORD_LOGIN_LAST_GLOBAL
    global DISCORD_LOGIN_LAST_RETRY_AFTER, DISCORD_LOGIN_LAST_CF_RAY, DISCORD_LOGIN_LAST_VIA
    global DISCORD_LOGIN_LAST_SERVER, DISCORD_LOGIN_LAST_MESSAGE

    session_recovery_backoff = 30.0
    handover_delay_done = False

    while True:
        if DISCORD_SHUTDOWN_EVENT.is_set():
            DISCORD_LOGIN_STATE = "shutdown_requested"
            DISCORD_LOGIN_RETRY_AFTER = 0.0
            return

        if not handover_delay_done:
            if not await _wait_for_discord_startup_handover():
                return
            handover_delay_done = True

        try:
            DISCORD_LOGIN_STATE = "connecting"
            DISCORD_LOGIN_RETRY_AFTER = 0.0
            await bot.start(discord_token, reconnect=True)
            DISCORD_LOGIN_STATE = "stopped"
            return
        except Exception as exc:
            is_http_429 = isinstance(exc, discord.HTTPException) and getattr(exc, "status", None) == 429
            rate_limited_type = getattr(discord, "RateLimited", None)
            is_library_ratelimited = bool(rate_limited_type) and isinstance(exc, rate_limited_type)

            if is_http_429 or is_library_ratelimited:
                DISCORD_LOGIN_RETRY_COUNT += 1
                wait_seconds, source = _discord_retry_after_seconds(exc, 0.0)
                DISCORD_LOGIN_RETRY_AFTER = wait_seconds

                diag = _discord_http_429_diagnostics(exc)
                scope = str(diag.get("scope") or "unknown")
                global_header = "true" if bool(diag.get("global")) else "false"
                DISCORD_LOGIN_LAST_STATUS = int(getattr(exc, "status", 429) or 429)
                DISCORD_LOGIN_LAST_SCOPE = scope
                DISCORD_LOGIN_LAST_GLOBAL = bool(diag.get("global"))
                DISCORD_LOGIN_LAST_RETRY_AFTER = wait_seconds
                DISCORD_LOGIN_LAST_CF_RAY = str(diag.get("cf_ray") or "")
                DISCORD_LOGIN_LAST_VIA = str(diag.get("via") or "")
                DISCORD_LOGIN_LAST_SERVER = str(diag.get("server") or "")
                DISCORD_LOGIN_LAST_MESSAGE = str(diag.get("text") or "")

                try:
                    _record_discord_outbound_429(
                        exc,
                        context="discord-api:GET /users/@me",
                    )
                except Exception:
                    pass

                DISCORD_LOGIN_STATE = "quarantined"
                log.warning(
                    "Discord login rate-limited | status=%s | scope=%s | global=%s | retry_after=%.3fs | source=%s | quarantine=yes | no retry",
                    getattr(exc, "status", 429),
                    scope,
                    global_header,
                    wait_seconds,
                    source,
                )

                log.warning(
                    "Discord 429 diagnostics | route=static_login:/users/@me | limit=%s | remaining=%s | reset_after=%ss | bucket=%s | cf_ray=%s | via=%s | server=%s | message=%s",
                    diag.get("limit") or "-",
                    diag.get("remaining") or "-",
                    diag.get("reset_after") or "-",
                    diag.get("bucket") or "-",
                    diag.get("cf_ray") or "-",
                    diag.get("via") or "-",
                    diag.get("server") or "-",
                    diag.get("text") or "-",
                )

                # Retry-After is recorded for telemetry and quarantine only.
                # Do not rebuild the HTTP client, do not wait, and do not retry
                # from the same process after Discord has returned HTTP 429.
                DISCORD_LOGIN_RETRY_AFTER = wait_seconds
                return

            if isinstance(exc, RuntimeError) and "Session is closed" in str(exc):
                DISCORD_LOGIN_SESSION_RECOVERY_COUNT += 1
                DISCORD_LOGIN_STATE = "session_recovery_waiting"
                wait_seconds = min(session_recovery_backoff, 300.0)
                DISCORD_LOGIN_RETRY_AFTER = wait_seconds
                log.warning(
                    "Discord login encountered closed HTTP session | recovery_count=%d | waiting=%.1fs | keeping service alive",
                    DISCORD_LOGIN_SESSION_RECOVERY_COUNT,
                    wait_seconds,
                )
                await _rebuild_discord_http_client("closed-session")
                await _reset_discord_client_state_after_login_error("closed-session-httpclient-rebuild")
                try:
                    await asyncio.wait_for(DISCORD_SHUTDOWN_EVENT.wait(), timeout=wait_seconds)
                    DISCORD_LOGIN_STATE = "shutdown_requested"
                    DISCORD_LOGIN_RETRY_AFTER = 0.0
                    return
                except asyncio.TimeoutError:
                    pass
                session_recovery_backoff = min(session_recovery_backoff * 2.0, 300.0)
                DISCORD_LOGIN_STATE = "retrying"
                DISCORD_LOGIN_RETRY_AFTER = 0.0
                continue

            DISCORD_LOGIN_STATE = "failed"
            raise


async def main():
    global ARGOS_WORKER
    discord_token = get_discord_token()
    if not discord_token:
        raise RuntimeError(
            "Discord bot token is missing. Set DISCORD_TOKEN in Render Environment "
            "(or DISCORD_BOT_TOKEN as the compatibility name); never put the token in source code."
        )

    log.info(
        "Starting JAVIS v%s | Engine=Argos Translate + CTranslate2 | Compute=%s | Device=%s | Batch=%s | Beam=%s | KO_Engine=%s | KO_Beam=%s | Chunk=%s | Port=%s",
        JAVIS_VERSION,
        os.getenv("ARGOS_COMPUTE_TYPE", "auto"),
        os.getenv("ARGOS_DEVICE_TYPE", "cpu"),
        os.getenv("ARGOS_BATCH_SIZE", "1"),
        os.getenv("ARGOS_BEAM_SIZE", "2"),
        os.getenv("ARGOS_KO_ENGINE", "argos-native"),
        os.getenv("ARGOS_KO_BEAM_SIZE", "4"),
        os.getenv("ARGOS_CHUNK_TYPE", "MINISBD"),
        PORT,
    )

    ARGOS_WORKER = ArgosWorkerClient()
    ARGOS_WORKER.start()
    runner = await start_http_server()
    _install_discord_shutdown_signal_handlers()
    await _refresh_outbound_ip()
    global OUTBOUND_IP_REFRESH_TASK
    OUTBOUND_IP_REFRESH_TASK = asyncio.create_task(
        _outbound_ip_refresh_worker(),
        name="javis-outbound-ip-refresh",
    )
    discord_task = asyncio.create_task(
        _start_discord_with_rate_limit_retry(discord_token),
        name="javis-discord-startup",
    )
    shutdown_task = asyncio.create_task(
        DISCORD_SHUTDOWN_EVENT.wait(),
        name="javis-discord-shutdown-wait",
    )
    try:
        done, _ = await asyncio.wait(
            {discord_task, shutdown_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if shutdown_task in done:
            DISCORD_LOGIN_STATE = "shutdown_requested"
            if not bot.is_closed():
                await bot.close()
            if not discord_task.done():
                discord_task.cancel()
            await asyncio.gather(discord_task, return_exceptions=True)
        else:
            await discord_task
    finally:
        if not shutdown_task.done():
            shutdown_task.cancel()
        await asyncio.gather(shutdown_task, return_exceptions=True)
        if OUTBOUND_IP_REFRESH_TASK is not None and not OUTBOUND_IP_REFRESH_TASK.done():
            OUTBOUND_IP_REFRESH_TASK.cancel()
            await asyncio.gather(OUTBOUND_IP_REFRESH_TASK, return_exceptions=True)
        await runner.cleanup()
        if ARGOS_WORKER is not None:
            ARGOS_WORKER.shutdown()
            ARGOS_WORKER = None
        await bot.close()


if __name__ == "__main__":
    asyncio.run(main())
