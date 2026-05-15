FROM python:3.12-slim

LABEL maintainer="Vito"
LABEL description="BunkrDownloader with WebUI - download media from Bunkr albums"

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /app /data/downloads /data/logs /data/config

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir flask waitress

COPY webui/ webui/
COPY scripts/ scripts/
COPY src/ src/
COPY downloader.py main.py .
COPY config.default.json .

RUN chmod +x scripts/entrypoint.sh

EXPOSE 8877

VOLUME ["/data/downloads", "/data/logs", "/data/config"]

ENTRYPOINT ["scripts/entrypoint.sh"]
