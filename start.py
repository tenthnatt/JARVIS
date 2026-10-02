"""JAVIS Render/Docker entrypoint v1.0.7.

Sets all Argos/CTranslate2 runtime settings BEFORE bot.py imports anything that
needs those settings. The Discord bot itself remains in bot.py.
"""

import asyncio
import os

JAVIS_VERSION = "1.0.7"
os.environ["JAVIS_VERSION"] = JAVIS_VERSION

# Runtime quantization: CTranslate2 models are loaded as INT8 in RAM.
# Render Environment Variables may override these values, but these defaults
# ensure a correct configuration even when they are not manually added.
os.environ.setdefault("ARGOS_COMPUTE_TYPE", "int8")
os.environ.setdefault("ARGOS_DEVICE_TYPE", "cpu")
os.environ.setdefault("ARGOS_INTER_THREADS", "1")
os.environ.setdefault("ARGOS_INTRA_THREADS", "1")
os.environ.setdefault("ARGOS_BATCH_SIZE", "1")
os.environ.setdefault("ARGOS_BEAM_SIZE", "2")
os.environ.setdefault("ARGOS_CHUNK_TYPE", "MINISBD")
os.environ.setdefault(
    "ARGOS_PACKAGE_INDEX",
    "https://raw.githubusercontent.com/argosopentech/argospm-index/main",
)

if not os.getenv("DISCORD_TOKEN", "").strip() and os.getenv("DISCORD_BOT_TOKEN", "").strip():
    os.environ["DISCORD_TOKEN"] = os.getenv("DISCORD_BOT_TOKEN", "").strip()

from bot import main  # noqa: E402


if __name__ == "__main__":
    asyncio.run(main())
