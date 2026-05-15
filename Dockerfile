FROM python:3.12-slim

LABEL maintainer="Vito"
LABEL description="BunkrDownloader with WebUI - download media from Bunkr albums"

# Install system deps
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Create app user & dirs
RUN useradd -m -s /bin/bash bun && \
    mkdir -p /app /data/downloads /data/logs /data/config && \
    chown -R bun:bun /app /data

WORKDIR /app

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir flask

# Copy app code
COPY --chown=bun:bun webui/ webui/
COPY --chown=bun:bun scripts/ scripts/
COPY --chown=bun:bun src/ src/
COPY --chown=bun:bun downloader.py main.py .
COPY --chown=bun:bun config.default.json .

RUN chmod +x scripts/entrypoint.sh

USER bun

EXPOSE 8877

VOLUME ["/data/downloads", "/data/logs", "/data/config"]

ENTRYPOINT ["scripts/entrypoint.sh"]
