"""JAVIS Render/Docker entrypoint v1.0.25.

Sets all Argos/CTranslate2 runtime settings BEFORE bot.py imports anything that
needs those settings. The Discord bot itself remains in bot.py.
"""

import asyncio
import os

JAVIS_VERSION = "1.0.25"
os.environ["JAVIS_VERSION"] = JAVIS_VERSION

# Render zero-downtime deploy protection: Render starts a new instance before
# sending SIGTERM to the old instance. Keep the HTTP health server available, but
# delay the first Discord login so the old Gateway can shut down cleanly first.
# bot.py treats a pre-existing Discord 429 as a hard no-request window and does not
# send extra startup probes that could create more invalid requests.
os.environ.setdefault("DISCORD_STARTUP_HANDOVER_DELAY", "75")

# Optional: set DISCORD_GUILD_ID in Render to publish /setroom, /delroom and
# /roomlist to one guild immediately. Without it, global sync is used.


# Runtime quantization: CTranslate2 models are loaded as INT8 in RAM.
# Render Environment Variables may override these values, but these defaults
# ensure a correct configuration even when they are not manually added. Batch size is clamped to 1 on the Free/512 MB profile.
os.environ.setdefault("ARGOS_COMPUTE_TYPE", "int8")
os.environ.setdefault("ARGOS_DEVICE_TYPE", "cpu")
os.environ.setdefault("ARGOS_INTER_THREADS", "1")
os.environ.setdefault("ARGOS_INTRA_THREADS", "1")
# Render Environment Variables can override setdefault(). For the 512 MB
# Free plan, force batch size back to 1 when a larger value is configured.
try:
    _configured_batch = int(os.getenv("ARGOS_BATCH_SIZE", "1"))
except ValueError:
    _configured_batch = 1
os.environ["ARGOS_BATCH_SIZE"] = "1"
os.environ.setdefault("ARGOS_BEAM_SIZE", "2")
os.environ["ARGOS_KO_ENGINE"] = "ct2-direct"
os.environ["ARGOS_KO_BEAM_SIZE"] = "2"
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("ARGOS_CHUNK_TYPE", "MINISBD")
os.environ.setdefault(
    "ARGOS_PACKAGES_DIR",
    os.path.join(os.path.expanduser("~"), ".local", "share", "argos-translate", "packages"),
)
os.environ.setdefault(
    "ARGOS_PACKAGE_INDEX",
    "https://raw.githubusercontent.com/argosopentech/argospm-index/main",
)

if not os.getenv("DISCORD_TOKEN", "").strip() and os.getenv("DISCORD_BOT_TOKEN", "").strip():
    os.environ["DISCORD_TOKEN"] = os.getenv("DISCORD_BOT_TOKEN", "").strip()

from bot import main  # noqa: E402


if __name__ == "__main__":
    asyncio.run(main())
