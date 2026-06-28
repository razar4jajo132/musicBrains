"""
config.py
=========

Central, env-driven configuration for the Lidarr-facing service.

Everything the service needs to know is read from environment variables so the
same image behaves correctly whether it's launched by `docker compose`, by a
bare `uvicorn` invocation during development, or from a test harness. Defaults
are chosen so that `uvicorn server:app` with *no* env set still boots and does
something sensible on a developer laptop.
"""

from __future__ import annotations

import os
from pathlib import Path


def _env(name: str, default: str) -> str:
    val = os.environ.get(name)
    return val if val not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# --- Identity / auth -------------------------------------------------------

# A single shared key used for BOTH the Newznab indexer (`?apikey=`) and the
# fake-SABnzbd download client (`?apikey=`). Lidarr is configured with this
# same value in two places. Default is a obvious dev placeholder — override it.
API_KEY = _env("MB_API_KEY", "changeme")

# Whether to actually enforce the API key. Handy to disable while poking at the
# endpoints by hand with curl. Lidarr always sends it, so leave on in prod.
REQUIRE_API_KEY = _env_bool("MB_REQUIRE_API_KEY", True)


# --- Networking ------------------------------------------------------------

HOST = _env("MB_HOST", "0.0.0.0")
PORT = _env_int("MB_PORT", 8787)

# The base URL Lidarr (and the user's browser) uses to reach this service.
# It's baked into the download links we hand back inside Newznab results, so it
# MUST be reachable *from Lidarr's host*. When co-located that's the container
# name or localhost:PORT; across hosts it's the LAN IP. We fall back to a
# relative scheme if unset and rely on the incoming request host (see server).
PUBLIC_URL = _env("MB_PUBLIC_URL", "").rstrip("/")


# --- Storage ---------------------------------------------------------------

# Root under which finished albums are written, one folder per album. This is
# the path the fake-SABnzbd `history` reports to Lidarr as the download's
# `storage`, so it must be a path *Lidarr can read* to import from. When the
# service runs on Lidarr's own host this is just a shared local directory.
COMPLETED_DIR = Path(_env("MB_COMPLETED_DIR", "./downloads/completed")).resolve()

# Scratch space for in-progress downloads, moved into COMPLETED_DIR on success
# so Lidarr never sees a half-written album.
INCOMPLETE_DIR = Path(_env("MB_INCOMPLETE_DIR", "./downloads/incomplete")).resolve()

# The SABnzbd "category" we advertise. Lidarr's download-client config sets a
# category (default "lidarr" / "music"); files land in COMPLETED_DIR/<category>.
CATEGORY = _env("MB_CATEGORY", "music")


# --- Audio / download behaviour -------------------------------------------

# "native" keeps the source codec (Opus/m4a) with no re-encode — best fidelity
# from a lossy source. "mp3" transcodes to a fixed bitrate for universal
# playback / stricter Lidarr quality profiles.
AUDIO_FORMAT = _env("MB_AUDIO_FORMAT", "native").lower()  # "native" | "mp3"
MP3_BITRATE = _env("MB_MP3_BITRATE", "320")               # kbps, mp3 mode only

# Optional path to a Netscape-format cookies.txt exported from a logged-in
# browser. YouTube increasingly bot-checks datacenter IPs; supplying cookies is
# the usual fix. Empty == don't pass cookies to yt-dlp.
YTDLP_COOKIES = _env("MB_YTDLP_COOKIES", "")

# Quality token embedded in Newznab release titles so Lidarr's parser assigns a
# quality. Keep this consistent with what AUDIO_FORMAT actually produces and
# with what your Lidarr quality profile accepts.
QUALITY_TOKEN = _env("MB_QUALITY_TOKEN", "MP3-320" if AUDIO_FORMAT == "mp3" else "MP3-256")


# --- MusicBrainz search ----------------------------------------------------

USER_AGENT = _env(
    "MB_USER_AGENT",
    "musicBrains-lidarr/0.1 (https://github.com/razar4jajo132/musicBrains)",
)

# How many MusicBrainz release candidates to return per Lidarr search. Lidarr
# picks among them using its quality profile + the (estimated) sizes we report.
MAX_SEARCH_RESULTS = _env_int("MB_MAX_SEARCH_RESULTS", 5)

# Estimated size per track (MB), used both for the size we advertise in Newznab
# results and the SABnzbd queue display. Lidarr rejects a release whose size is
# too large to plausibly be the advertised QUALITY_TOKEN bitrate (e.g. an
# MP3-256 release that works out to >256 kbps over the album runtime). Native
# YouTube audio (~160 kbps Opus) is roughly 3 MB for a typical track, so keep
# this low enough to stay under that cap. Tunable for unusual albums.
EST_MB_PER_TRACK = float(_env("MB_EST_MB_PER_TRACK", "3"))


def ensure_dirs() -> None:
    """Create the storage directories if they don't exist. Called at startup."""
    COMPLETED_DIR.mkdir(parents=True, exist_ok=True)
    INCOMPLETE_DIR.mkdir(parents=True, exist_ok=True)
