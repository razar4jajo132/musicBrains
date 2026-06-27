"""
core.py
=======

The bridge between the Lidarr-facing service and the YouTube-finding logic that
already lives in ``album_bot.py`` / ``Downloader.py``.

Two responsibilities:

  1. ``resolve_album(release_mbid)`` — given a MusicBrainz *release* ID, fetch
     the tracklist (with the IDs Lidarr cares about) and run the existing
     cascade to pick a YouTube URL for every track.

  2. ``download_album(album, dest_dir)`` — download each chosen URL as audio and
     write *MusicBrainz-derived* tags (artist / album / track no / title / MB
     IDs). This is deliberately NOT the YouTube-title tagging that
     ``Downloader.py`` does — Lidarr imports far more reliably from correct
     metadata.

Nothing here prints or writes CSVs; it returns plain data objects the service
layer turns into Newznab/SABnzbd responses.
"""

from __future__ import annotations

import glob
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests
import yt_dlp

import config
# Reuse the battle-tested cascade + helpers verbatim.
import album_bot
from album_bot import Track, MatchResult, normalize, find_youtube_match, detect_topic_channel


# --- Resolved data types ---------------------------------------------------

@dataclass
class ResolvedTrack:
    number: int
    title: str
    duration_seconds: Optional[int]
    recording_mbid: str = ""        # MusicBrainz recording id
    track_mbid: str = ""            # MusicBrainz release-track id
    youtube_url: str = ""
    yt_channel: str = ""
    yt_title: str = ""
    stage: str = "none"
    duration_match: str = "no_match"

    @property
    def has_pick(self) -> bool:
        return bool(self.youtube_url)


@dataclass
class ResolvedAlbum:
    release_mbid: str
    artist: str
    album: str
    year: str = ""
    release_group_mbid: str = ""
    tracks: list[ResolvedTrack] = field(default_factory=list)

    @property
    def pick_count(self) -> int:
        return sum(1 for t in self.tracks if t.has_pick)

    def folder_name(self) -> str:
        """`Artist - Album` sanitized for use as a directory name."""
        raw = f"{self.artist} - {self.album}"
        return re.sub(r'[\\/:*?"<>|]+', "_", raw).strip() or self.release_mbid


@dataclass
class DownloadReport:
    ok: int = 0
    fail: int = 0
    files: list[str] = field(default_factory=list)

    @property
    def any_success(self) -> bool:
        return self.ok > 0


# --- MusicBrainz fetch (richer than album_bot's, for tagging) --------------

def _fetch_release_json(release_mbid: str) -> dict:
    url = f"https://musicbrainz.org/ws/2/release/{release_mbid}"
    params = {"inc": "recordings+artist-credits+release-groups", "fmt": "json"}
    resp = requests.get(
        url, params=params,
        headers={"User-Agent": config.USER_AGENT}, timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def _artist_from_credits(data: dict) -> str:
    credits = data.get("artist-credit", [])
    name = "".join(
        (c["name"] if isinstance(c, dict) else c)
        + (c.get("joinphrase", "") if isinstance(c, dict) else "")
        for c in credits
    ).strip()
    return name or data.get("artist-credit-phrase", "Unknown Artist")


@dataclass
class ReleaseHit:
    """A MusicBrainz release candidate returned to Lidarr as a search result."""
    mbid: str
    artist: str
    album: str
    year: str = ""
    track_count: int = 0


def _mb_query(artist: str, album: str, free: str) -> str:
    """Build a Lucene query for the MusicBrainz release search index."""
    parts: list[str] = []
    if album:
        parts.append(f'release:"{album}"')
    if artist:
        parts.append(f'artist:"{artist}"')
    if not parts and free:
        parts.append(free)
    return " AND ".join(parts) if parts else (free or "*")


def search_releases(artist: str = "", album: str = "", free: str = "",
                    limit: int = config.MAX_SEARCH_RESULTS) -> list[ReleaseHit]:
    """
    Search MusicBrainz for releases matching what Lidarr asked for. Returns a
    short list of candidates; the service turns each into a Newznab item whose
    download link carries the release MBID back to us.
    """
    query = _mb_query(normalize(artist), normalize(album), normalize(free))
    resp = requests.get(
        "https://musicbrainz.org/ws/2/release/",
        params={"query": query, "limit": limit, "fmt": "json"},
        headers={"User-Agent": config.USER_AGENT}, timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()

    hits: list[ReleaseHit] = []
    for rel in data.get("releases", []):
        credits = rel.get("artist-credit", [])
        artist_name = "".join(
            (c["name"] if isinstance(c, dict) else c)
            + (c.get("joinphrase", "") if isinstance(c, dict) else "")
            for c in credits
        ).strip() or "Unknown Artist"
        date = rel.get("date", "")
        hits.append(ReleaseHit(
            mbid=rel.get("id", ""),
            artist=artist_name,
            album=rel.get("title", "Unknown Release"),
            year=date[:4] if date else "",
            track_count=rel.get("track-count", 0) or 0,
        ))
    return [h for h in hits if h.mbid]


def resolve_album(release_mbid: str) -> ResolvedAlbum:
    """
    Fetch the release from MusicBrainz and run the YouTube cascade for every
    track. Returns a fully-populated ResolvedAlbum (tracks without a match keep
    empty youtube_url / stage="none").
    """
    data = _fetch_release_json(release_mbid)

    artist = _artist_from_credits(data)
    album_title = data.get("title", "Unknown Release")
    date = data.get("date", "") or data.get("release-group", {}).get("first-release-date", "")
    year = date[:4] if date else ""
    rg_mbid = data.get("release-group", {}).get("id", "")

    resolved = ResolvedAlbum(
        release_mbid=release_mbid,
        artist=artist,
        album=album_title,
        year=year,
        release_group_mbid=rg_mbid,
    )

    # Build the plain Track list the cascade understands, while stashing the
    # MB ids alongside so we can tag with them after download.
    pending: list[tuple[ResolvedTrack, Track]] = []
    for medium in data.get("media", []):
        for t in medium.get("tracks", []):
            length_ms = t.get("length")
            duration = round(length_ms / 1000) if length_ms else None
            rt = ResolvedTrack(
                number=int(t["position"]),
                title=t.get("title", ""),
                duration_seconds=duration,
                track_mbid=t.get("id", ""),
                recording_mbid=t.get("recording", {}).get("id", ""),
            )
            cascade_track = Track(rt.number, rt.title, rt.duration_seconds)
            pending.append((rt, cascade_track))
            resolved.tracks.append(rt)

    if not pending:
        return resolved

    # One upfront probe (same trick album_bot uses) to decide on Stage 3.
    use_topic = detect_topic_channel(artist)

    for rt, cascade_track in pending:
        match: MatchResult = find_youtube_match(artist, cascade_track, use_topic)
        rt.stage = match.stage
        rt.duration_match = match.duration_match
        if match.chosen:
            rt.youtube_url = match.chosen.url
            rt.yt_channel = match.chosen.channel
            rt.yt_title = match.chosen.title

    return resolved


# --- Download + tagging ----------------------------------------------------

def _ydl_opts(dest_no_ext: str) -> dict:
    postprocessors = []
    if config.AUDIO_FORMAT == "mp3":
        postprocessors.append({
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": config.MP3_BITRATE,
        })
    else:
        # Native: remux best audio into its own container, no re-encode.
        postprocessors.append({
            "key": "FFmpegExtractAudio",
            "preferredcodec": "best",
        })
    opts = {
        "format": "bestaudio/best",
        "outtmpl": dest_no_ext + ".%(ext)s",
        "postprocessors": postprocessors,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "ignoreerrors": False,
    }
    if config.YTDLP_COOKIES:
        opts["cookiefile"] = config.YTDLP_COOKIES
    return opts


def _safe(text: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", text).strip()


def _write_tags(path: Path, album: ResolvedAlbum, track: ResolvedTrack, total: int) -> None:
    """
    Write MusicBrainz-derived tags onto the downloaded file. Best-effort per
    format; a tagging failure must never sink an otherwise-good download, so
    everything is wrapped and swallowed with a note left to the caller's logs.
    """
    ext = path.suffix.lower()
    try:
        if ext == ".mp3":
            from mutagen.easyid3 import EasyID3
            from mutagen.id3 import ID3NoHeaderError
            try:
                tags = EasyID3(str(path))
            except ID3NoHeaderError:
                tags = EasyID3()
            tags["title"] = track.title
            tags["artist"] = album.artist
            tags["albumartist"] = album.artist
            tags["album"] = album.album
            tags["tracknumber"] = f"{track.number}/{total}"
            if album.year:
                tags["date"] = album.year
            if track.recording_mbid:
                tags["musicbrainz_trackid"] = track.recording_mbid
            if track.track_mbid:
                tags["musicbrainz_releasetrackid"] = track.track_mbid
            if album.release_mbid:
                tags["musicbrainz_albumid"] = album.release_mbid
            if album.release_group_mbid:
                tags["musicbrainz_releasegroupid"] = album.release_group_mbid
            tags.save(str(path))
            return

        if ext in (".m4a", ".mp4", ".aac"):
            from mutagen.mp4 import MP4
            mp4 = MP4(str(path))
            mp4["\xa9nam"] = track.title
            mp4["\xa9ART"] = album.artist
            mp4["aART"] = album.artist
            mp4["\xa9alb"] = album.album
            mp4["trkn"] = [(track.number, total)]
            if album.year:
                mp4["\xa9day"] = album.year
            # MusicBrainz freeform atoms Lidarr/Picard understand.
            def _ff(name: str, val: str):
                mp4[f"----:com.apple.iTunes:{name}"] = [val.encode("utf-8")]
            if track.recording_mbid:
                _ff("MusicBrainz Track Id", track.recording_mbid)
            if track.track_mbid:
                _ff("MusicBrainz Release Track Id", track.track_mbid)
            if album.release_mbid:
                _ff("MusicBrainz Album Id", album.release_mbid)
            if album.release_group_mbid:
                _ff("MusicBrainz Release Group Id", album.release_group_mbid)
            mp4.save()
            return

        # Opus / Ogg / FLAC — Vorbis-comment style key/values.
        if ext in (".opus", ".ogg", ".oga", ".flac"):
            if ext == ".flac":
                from mutagen.flac import FLAC as VC
            elif ext in (".opus",):
                from mutagen.oggopus import OggOpus as VC
            else:
                from mutagen.oggvorbis import OggVorbis as VC
            vc = VC(str(path))
            vc["title"] = track.title
            vc["artist"] = album.artist
            vc["albumartist"] = album.artist
            vc["album"] = album.album
            vc["tracknumber"] = str(track.number)
            vc["tracktotal"] = str(total)
            if album.year:
                vc["date"] = album.year
            if track.recording_mbid:
                vc["musicbrainz_trackid"] = track.recording_mbid
            if track.track_mbid:
                vc["musicbrainz_releasetrackid"] = track.track_mbid
            if album.release_mbid:
                vc["musicbrainz_albumid"] = album.release_mbid
            if album.release_group_mbid:
                vc["musicbrainz_releasegroupid"] = album.release_group_mbid
            vc.save()
            return
    except Exception as exc:  # noqa: BLE001 — tagging is best-effort
        print(f"  ! tag write failed for {path.name}: {exc}")


def download_album(album: ResolvedAlbum, dest_dir: Path,
                   progress=None) -> DownloadReport:
    """
    Download every matched track into ``dest_dir`` as ``NN - Title.ext`` and tag
    it from MusicBrainz metadata. ``progress(done, total)`` is invoked after
    each track if supplied (used to drive the SABnzbd queue percentage).
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    report = DownloadReport()
    total = len(album.tracks)
    matched = [t for t in album.tracks if t.has_pick]

    for i, track in enumerate(album.tracks, 1):
        if not track.has_pick:
            report.fail += 1
            if progress:
                progress(i, total)
            continue

        stem = f"{track.number:02d} - {_safe(track.title)}"
        dest_no_ext = str(dest_dir / stem)
        try:
            with yt_dlp.YoutubeDL(_ydl_opts(dest_no_ext)) as ydl:
                ydl.download([track.youtube_url])
        except Exception as exc:  # noqa: BLE001
            print(f"  ! download failed for track {track.number} "
                  f"({track.title}): {exc}")
            report.fail += 1
            if progress:
                progress(i, total)
            continue

        produced = glob.glob(glob.escape(dest_no_ext) + ".*")
        if not produced:
            report.fail += 1
        else:
            out = Path(produced[0])
            _write_tags(out, album, track, total)
            report.ok += 1
            report.files.append(str(out))

        if progress:
            progress(i, total)

    _ = matched  # (kept for readability of intent; counts derive from loop)
    return report
