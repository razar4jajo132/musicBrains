# musicBrains → Lidarr bridge
#
# A small FastAPI service that presents a Newznab indexer + fake-SABnzbd
# download client to Lidarr and fulfils grabs by downloading from YouTube.

FROM python:3.12-slim

# ffmpeg is required by yt-dlp for audio extraction / remux. Installing the
# distro package is more reliable inside a container than static-ffmpeg.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.py core.py jobs.py server.py album_bot.py Downloader.py ./

# Storage defaults; override via env / compose. These live on a mounted volume
# in production so finished albums survive container restarts and are visible
# to Lidarr.
ENV MB_COMPLETED_DIR=/downloads/completed \
    MB_INCOMPLETE_DIR=/downloads/incomplete \
    MB_PORT=8787

EXPOSE 8787

# Single worker: the job store is in-process, so multiple uvicorn workers would
# each have their own queue and Lidarr's polls would hit the wrong one.
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8787", "--workers", "1"]
