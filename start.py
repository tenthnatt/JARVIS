"""JAVIS Render/Docker entrypoint.

Keeps runtime settings in place before bot.py imports Argos/CTranslate2.
All application behavior remains in bot.py.
"""

import asyncio
import os

# Preserve the memory-oriented runtime configuration before bot.py imports Argos.
os.environ.setdefault("ARGOS_DEVICE_TYPE", "cpu")
os.environ.setdefault("ARGOS_COMPUTE_TYPE", "int8")
os.environ.setdefault("ARGOS_INTER_THREADS", "1")
os.environ.setdefault("ARGOS_INTRA_THREADS", "1")
os.environ.setdefault("ARGOS_BATCH_SIZE", "8")
os.environ.setdefault("ARGOS_BEAM_SIZE", "2")

from bot import main  # noqa: E402  (must load after environment defaults above)


if __name__ == "__main__":
    asyncio.run(main())
