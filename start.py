"""JAVIS Render/Docker entrypoint.

Keeps runtime settings in place before bot.py imports Argos/CTranslate2.
All application behavior remains in bot.py.
"""

import asyncio
import os

# Release version for this deploy. This is intentionally set here so the deployed process reports the same version.
os.environ["JAVIS_VERSION"] = "1.0.4"

# Preserve the memory-oriented runtime configuration before bot.py imports Argos.
os.environ.setdefault("ARGOS_DEVICE_TYPE", "cpu")
os.environ.setdefault("ARGOS_COMPUTE_TYPE", "int8")
os.environ.setdefault("ARGOS_INTER_THREADS", "1")
os.environ.setdefault("ARGOS_INTRA_THREADS", "1")
os.environ.setdefault("ARGOS_BATCH_SIZE", "8")
os.environ.setdefault("ARGOS_BEAM_SIZE", "2")
os.environ.setdefault("ARGOS_CHUNK_TYPE", "NONE")

# Discord token must remain a Render Environment Secret.
# Accept DISCORD_BOT_TOKEN as a compatibility alias without exposing the token in code.
if not os.getenv("DISCORD_TOKEN", "").strip() and os.getenv("DISCORD_BOT_TOKEN", "").strip():
    os.environ["DISCORD_TOKEN"] = os.getenv("DISCORD_BOT_TOKEN", "").strip()

from bot import main  # noqa: E402  (must load after environment defaults above)


if __name__ == "__main__":
    asyncio.run(main())
