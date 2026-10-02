FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    ARGOS_DEVICE_TYPE=cpu \
    ARGOS_COMPUTE_TYPE=int8 \
    ARGOS_INTER_THREADS=1 \
    ARGOS_INTRA_THREADS=1 \
    ARGOS_BATCH_SIZE=8 \
    ARGOS_BEAM_SIZE=2 \
    ARGOS_PACKAGE_INDEX=https://raw.githubusercontent.com/argosopentech/argospm-index/main

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

# Install only the four translation directions needed by JAVIS.
# For the Korean pair we intentionally pin the smaller 1.1 model files to
# reduce the chance of exceeding Render Free's 512 MB RAM limit at runtime.
RUN python - <<'PY'
from pathlib import Path
from urllib.request import urlopen, Request
import argostranslate.package

models = {
    "translate-en_th-1_9.argosmodel": "https://data.argosopentech.com/argospm/v1/translate-en_th-1_9.argosmodel",
    "translate-th_en-1_9.argosmodel": "https://data.argosopentech.com/argospm/v1/translate-th_en-1_9.argosmodel",
    "translate-en_ko-1_1.argosmodel": "https://data.argosopentech.com/argospm/v1/translate-en_ko-1_1.argosmodel",
    "translate-ko_en-1_1.argosmodel": "https://data.argosopentech.com/argospm/v1/translate-ko_en-1_1.argosmodel",
}

for filename, url in models.items():
    path = Path("/tmp") / filename
    print(f"Downloading {filename}")
    request = Request(url, headers={"User-Agent": "JAVIS-Docker/1.0"})
    with urlopen(request, timeout=120) as response, path.open("wb") as out:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
    print(f"Installing {filename}")
    argostranslate.package.install_from_path(str(path))
    path.unlink(missing_ok=True)
PY

COPY bot.py start.py index.html dictionary.json ./

EXPOSE 10000
CMD ["python", "start.py"]
