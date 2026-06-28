"""
album_bot.py
============

Given a MusicBrainz release URL or ID, find a YouTube link for every track
on the album and write them to a CSV in the current working directory.

Usage:
    python album_bot.py <release_url_or_id>
    python album_bot.py https://musicbrainz.org/release/9643849e-1d87-49eb-bbf0-53336942d1b5

How it works (the "cascade"):
    For each track, run up to three YouTube searches in order and stop at
    the first one that returns an acceptable result:

      Stage 1 (MV query):       <artist> "<track>" official MV
      Stage 2 (audio query):    <artist> "<track>" official audio
      Stage 3 (Topic trick):    "<artist> - Topic" "<track>"
      Fallback:                 best duration-matching result from any stage

    "Acceptable" means: the channel looks official (the artist's name,
    "<Artist> - Topic", or a known label channel) AND the duration is
    within +/-5 seconds of the MusicBrainz length.

    Lessons baked in from manual testing:
      - K-pop labels often upload "Official MVs" that are 30-90s longer
        than the album cut. The duration check catches those.
      - K-pop labels typically don't generate "Artist - Topic" auto
        channels, so we detect that upfront and skip Stage 3 for them.
      - MusicBrainz interlude tracks (e.g. "(Guitar)") often appear on
        YouTube under different names ("Interlude 2"). We fall through
        to duration-only matching for those.
      - Curly quotes (', ") in titles confuse YouTube search; we normalize.
      - In the fallback we de-prioritize lyric/fancam/cover videos so an
        unofficial-but-clean audio reupload beats a lyric video even when
        both match duration exactly.

Dependencies:
    pip install -r requirements.txt
        requests >= 2.31
        yt-dlp >= 2024.01.01
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import requests
import yt_dlp


# --- Configuration ---------------------------------------------------------

USER_AGENT = "album_bot/1.0 (https://example.com - educational/personal use)"

# How close a candidate's duration must be to the MusicBrainz length to
# count as a confident match.
DURATION_TOLERANCE_SECONDS = 5

# When no perfect-match exists, we still accept a hit within this larger
# window and flag it.
DURATION_LOOSE_TOLERANCE_SECONDS = 30

# Number of YouTube results to fetch per query.
RESULTS_PER_QUERY = 5

# Polite throttle so we don't hammer MusicBrainz / YouTube.
SLEEP_BETWEEN_QUERIES_SECONDS = 0.5

# Output CSVs go into this subdirectory next to the script (created on
# demand). Override per-run by passing --output PATH.
OUTPUT_SUBDIR = "Album"

# When falling back to unofficial uploads we'd rather skip these obvious
# "wrong kind of result" titles (lyric videos, fancams, covers, etc.) even
# when their duration matches the album cut.
BAD_TITLE_FRAGMENTS = (
    "color coded",
    "lyrics video",
    "lyric video",
    "[lyrics]",
    "(lyrics)",
    " lyrics ",
    "easy lyrics",
    "lyrics)",
    "fmv",
    "fan made",
    " cover ",
    "cover by",
    "reaction",
    "live at",
    "live in",
    "live on",
    "live from",
    "fancam",
    "직캠",   # Korean "fancam"
    "remix",
    "slowed",
    "sped up",
)

# Channels we treat as "official" for any artist. Add label channels here.
GLOBAL_OFFICIAL_CHANNELS = {
    "hybe labels",
    "bighit music",
    "bangtantv",
    "jyp entertainment",
    "sm town",
    "smtown",
    "yg entertainment",
    "atlantic records",
    "atlanticrecords",
    "infectious music",
    "warner records",
    "columbia records",
    "republic records",
    "interscope",
    "rcarecords",
    "vevo",  # substring match — covers altJVEVO, BTSVEVO, etc.
}


# --- Data types ------------------------------------------------------------

@dataclass
class Track:
    """A track as it appears on the MusicBrainz release."""
    number: int
    title: str
    duration_seconds: Optional[int]


@dataclass
class Candidate:
    """A YouTube search result."""
    video_id: str
    title: str
    channel: str
    duration_seconds: Optional[int]
    query: str

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"


@dataclass
class MatchResult:
    """The final pick for a track plus diagnostic info."""
    track: Track
    chosen: Optional[Candidate]
    stage: str           # "1-mv", "2-audio", "3-topic", "fallback", "none"
    duration_match: str  # "exact", "+1s", "-3s", "+21s", or "no_match"
    alt_official_mv: str = ""


# --- MusicBrainz ----------------------------------------------------------

MB_RELEASE_RE = re.compile(
    r"musicbrainz\.org/release/([0-9a-f-]{36})", re.IGNORECASE
)


def extract_release_id(arg: str) -> str:
    """Accept either a full MusicBrainz release URL or a bare UUID."""
    arg = arg.strip()
    match = MB_RELEASE_RE.search(arg)
    if match:
        return match.group(1)
    if re.fullmatch(r"[0-9a-f-]{36}", arg, re.IGNORECASE):
        return arg
    raise SystemExit(
        f"Could not parse a MusicBrainz release ID from {arg!r}.\n"
        "Pass either the full release URL or the UUID."
    )


def fetch_release(release_id: str) -> tuple[str, str, list[Track]]:
    """
    Hit the MusicBrainz API and return (artist_name, release_title, tracks).
    """
    url = f"https://musicbrainz.org/ws/2/release/{release_id}"
    params = {"inc": "recordings+artist-credits", "fmt": "json"}
    headers = {"User-Agent": USER_AGENT}

    resp = requests.get(url, params=params, headers=headers, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    # MusicBrainz returns artist info under "artist-credit" (a list).
    artist_credits = data.get("artist-credit", [])
    artist = "".join(
        (c["name"] if isinstance(c, dict) else c) +
        (c.get("joinphrase", "") if isinstance(c, dict) else "")
        for c in artist_credits
    ).strip() or data.get("artist-credit-phrase", "Unknown Artist")

    title = data.get("title", "Unknown Release")

    tracks: list[Track] = []
    for medium in data.get("media", []):
        for t in medium.get("tracks", []):
            length_ms = t.get("length")
            duration = round(length_ms / 1000) if length_ms else None
            tracks.append(Track(
                number=int(t["position"]),
                title=t.get("title", ""),
                duration_seconds=duration,
            ))

    if not tracks:
        raise SystemExit("MusicBrainz returned no tracks for this release.")

    return artist, title, tracks


# --- Title normalization ---------------------------------------------------

CURLY_TO_STRAIGHT = {
    "‘": "'",  # left single quote
    "’": "'",  # right single quote
    "“": '"',  # left double quote
    "”": '"',  # right double quote
    "‐": "-",  # hyphen
    "–": "-",  # en dash
    "—": "-",  # em dash
}


def normalize(text: str) -> str:
    """Strip stylization so YouTube search doesn't choke."""
    for src, dst in CURLY_TO_STRAIGHT.items():
        text = text.replace(src, dst)
    return unicodedata.normalize("NFKC", text).strip()


# --- YouTube search via yt-dlp --------------------------------------------

_YDL_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "extract_flat": "in_playlist",
    "noplaylist": True,
}


def yt_search(query: str, n: int = RESULTS_PER_QUERY) -> list[Candidate]:
    """Run a YouTube search and return up to n candidates."""
    search_url = f"ytsearch{n}:{query}"
    with yt_dlp.YoutubeDL(_YDL_OPTS) as ydl:
        try:
            info = ydl.extract_info(search_url, download=False)
        except Exception as exc:
            print(f"  ! yt-dlp error on {query!r}: {exc}", file=sys.stderr)
            return []

    entries = info.get("entries", []) if info else []
    candidates: list[Candidate] = []
    for entry in entries:
        if not entry:
            continue
        # yt-dlp can return duration as int or float — coerce to int.
        raw_duration = entry.get("duration")
        duration_seconds = int(raw_duration) if raw_duration is not None else None
        candidates.append(Candidate(
            video_id=entry.get("id", ""),
            title=entry.get("title", ""),
            channel=entry.get("channel") or entry.get("uploader") or "",
            duration_seconds=duration_seconds,
            query=query,
        ))
    time.sleep(SLEEP_BETWEEN_QUERIES_SECONDS)
    return candidates


# --- Cascade logic ---------------------------------------------------------

def _channel_is_official(channel: str, artist: str) -> bool:
    if not channel:
        return False
    # MusicBrainz uses Unicode hyphens (e.g. "alt‐J") while YouTube channel
    # names use ASCII hyphens (e.g. "alt-J"). Normalize both sides before
    # comparing — otherwise alt-J's actual channel doesn't register as "official".
    ch = normalize(channel).lower().strip()
    art = normalize(artist).lower().strip()

    if ch == art:
        return True
    if ch == f"{art} - topic":
        return True
    # alt-J often appears as "alt-j" with or without the hyphen.
    if art.replace("-", "") and ch.replace("-", "") == art.replace("-", ""):
        return True
    for known in GLOBAL_OFFICIAL_CHANNELS:
        if known in ch:
            return True
    return False


def _duration_delta(cand: Candidate, target: Optional[int]) -> Optional[int]:
    if cand.duration_seconds is None or target is None:
        return None
    return cand.duration_seconds - target


def _format_delta(delta: Optional[int]) -> str:
    if delta is None:
        return "unknown"
    delta = int(delta)
    if delta == 0:
        return "exact"
    return f"{delta:+d}s"


def _looks_like_lyric_or_fan_content(title: str) -> bool:
    lo = title.lower()
    return any(frag in lo for frag in BAD_TITLE_FRAGMENTS)


def _pick(
    candidates: list[Candidate],
    artist: str,
    target_duration: Optional[int],
    require_official: bool,
    tolerance: int,
    skip_lyric_videos: bool = False,
) -> Optional[Candidate]:
    """
    Pick the best candidate from a result list, given the rules.
    Returns None if nothing qualifies.
    """
    for cand in candidates:
        if require_official and not _channel_is_official(cand.channel, artist):
            continue
        if skip_lyric_videos and _looks_like_lyric_or_fan_content(cand.title):
            continue
        delta = _duration_delta(cand, target_duration)
        if delta is None:
            if target_duration is None:
                return cand
            continue
        if abs(delta) <= tolerance:
            return cand
    return None


def detect_topic_channel(artist: str) -> bool:
    """One upfront query to decide whether Stage 3 is worth running."""
    probe = yt_search(f'"{artist} - Topic"', n=3)
    target = f"{artist.lower()} - topic"
    for cand in probe:
        if cand.channel.lower() == target:
            return True
    return False


def find_youtube_match(
    artist: str,
    track: Track,
    use_topic_stage: bool,
) -> MatchResult:
    """Run the cascade for one track."""
    title = normalize(track.title)
    target = track.duration_seconds

    # ---- Stage 1: MV query ------------------------------------------------
    q1 = f'{artist} "{title}" official MV'
    r1 = yt_search(q1)
    pick = _pick(r1, artist, target, require_official=True,
                 tolerance=DURATION_TOLERANCE_SECONDS)
    if pick:
        return MatchResult(track, pick, "1-mv",
                           _format_delta(_duration_delta(pick, target)))

    # ---- Stage 2: audio query --------------------------------------------
    q2 = f'{artist} "{title}" official audio'
    r2 = yt_search(q2)
    pick = _pick(r2, artist, target, require_official=True,
                 tolerance=DURATION_TOLERANCE_SECONDS)
    if pick:
        return MatchResult(track, pick, "2-audio",
                           _format_delta(_duration_delta(pick, target)))

    # ---- Stage 2b: plain query -------------------------------------------
    # The "official MV"/"official audio" queries miss official uploads with
    # non-standard titles — e.g. "<Track> (OFFICIAL VISUALIZER)", lyric videos
    # posted on the artist's own channel, or plain "<Track>" uploads. A bare
    # query surfaces those. We still require an official channel + duration
    # match here, so this stays a *confident* pick, not a loose fallback; the
    # lyric/fan filter keeps an official lyric reupload from beating real audio.
    q2b = f'{artist} "{title}"'
    r2b = yt_search(q2b)
    pick = _pick(r2b, artist, target, require_official=True,
                 tolerance=DURATION_TOLERANCE_SECONDS, skip_lyric_videos=True)
    if not pick:
        # No non-lyric official hit — allow an official lyric upload before
        # falling through to Stage 3 / the unofficial fallback pool.
        pick = _pick(r2b, artist, target, require_official=True,
                     tolerance=DURATION_TOLERANCE_SECONDS)
    if pick:
        return MatchResult(track, pick, "2-plain",
                           _format_delta(_duration_delta(pick, target)))

    # ---- Stage 3: Topic trick --------------------------------------------
    r3: list[Candidate] = []
    if use_topic_stage:
        q3 = f'"{artist} - Topic" "{title}"'
        r3 = yt_search(q3)
        pick = _pick(r3, artist, target, require_official=True,
                     tolerance=DURATION_TOLERANCE_SECONDS)
        if pick:
            return MatchResult(track, pick, "3-topic",
                               _format_delta(_duration_delta(pick, target)))

    # ---- Fallback ---------------------------------------------------------
    # Pool order matters: results from the audio query are most likely to
    # be actual audio uploads, so they go first; the plain query next; Topic
    # results; MV query last (its top hits are usually the MV itself which
    # already failed the tolerance check).
    pool: list[Candidate] = [*r2, *r2b, *r3, *r1]

    # First try with the lyric-video filter on, so we don't pick a lyric
    # video over an unofficial-but-clean audio reupload.
    pick = _pick(pool, artist, target, require_official=False,
                 tolerance=DURATION_TOLERANCE_SECONDS,
                 skip_lyric_videos=True)
    if not pick:
        pick = _pick(pool, artist, target, require_official=False,
                     tolerance=DURATION_TOLERANCE_SECONDS)

    if pick:
        alt_mv = ""
        for c in pool:
            if _channel_is_official(c.channel, artist) and "mv" in c.title.lower():
                alt_mv = c.url
                break
        return MatchResult(
            track, pick, "fallback",
            _format_delta(_duration_delta(pick, target)),
            alt_official_mv=alt_mv,
        )

    # Last resort: accept an official upload with a wider duration window.
    # Catches K-pop tracks whose only official upload is an extended MV.
    pick = _pick(pool, artist, target, require_official=True,
                 tolerance=DURATION_LOOSE_TOLERANCE_SECONDS)
    if pick:
        return MatchResult(track, pick, "1-mv",
                           _format_delta(_duration_delta(pick, target)))

    # Give up — take the top result from any stage, flag it.
    if pool:
        return MatchResult(track, pool[0], "fallback",
                           _format_delta(_duration_delta(pool[0], target)))
    return MatchResult(track, None, "none", "no_match")


# --- CSV output ------------------------------------------------------------

CSV_HEADER = [
    "track_no", "artist", "title", "mb_duration",
    "youtube_url", "yt_channel", "yt_title", "yt_duration",
    "stage", "duration_match", "alt_official_mv",
]


def _fmt_duration(seconds: Optional[int]) -> str:
    if seconds is None:
        return ""
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


def write_csv(path: Path, artist: str, matches: Iterable[MatchResult]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(CSV_HEADER)
        for m in matches:
            c = m.chosen
            w.writerow([
                m.track.number,
                artist,
                m.track.title,
                _fmt_duration(m.track.duration_seconds),
                c.url if c else "",
                c.channel if c else "",
                c.title if c else "",
                _fmt_duration(c.duration_seconds) if c else "",
                m.stage,
                m.duration_match,
                m.alt_official_mv,
            ])


# --- Main ------------------------------------------------------------------

def _safe_filename(text: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", text).strip()


def process_release(
    release_arg: str,
    output_path: Optional[Path],
    force_topic_stage: bool,
) -> int:
    """Run the cascade for one MusicBrainz release. Returns 0 on success."""
    release_id = extract_release_id(release_arg)

    print(f"Fetching tracklist from MusicBrainz ({release_id})...")
    artist, album, tracks = fetch_release(release_id)
    print(f"  {artist} - {album}: {len(tracks)} tracks\n")

    if force_topic_stage:
        topic_exists = True
        print("Stage 3 forced on (--no-topic-detect).\n")
    else:
        print(f"Probing for '{artist} - Topic' channel...")
        topic_exists = detect_topic_channel(artist)
        print(f"  {'found' if topic_exists else 'no Topic channel, skipping Stage 3'}\n")

    matches: list[MatchResult] = []
    for t in tracks:
        print(f"Track {t.number:>2}: {t.title} ({_fmt_duration(t.duration_seconds)})")
        result = find_youtube_match(artist, t, topic_exists)
        matches.append(result)
        if result.chosen:
            print(f"           -> [{result.stage}] {result.chosen.channel} | "
                  f"{result.chosen.title} | "
                  f"{_fmt_duration(result.chosen.duration_seconds)} "
                  f"({result.duration_match})")
        else:
            print(f"           -> no match found")

    if output_path:
        out = output_path
    else:
        out_dir = Path(__file__).resolve().parent / OUTPUT_SUBDIR
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{_safe_filename(artist)} - {_safe_filename(album)}.csv"
    write_csv(out, artist, matches)
    print(f"\nWrote {out}")

    by_stage: dict[str, int] = {}
    for m in matches:
        by_stage[m.stage] = by_stage.get(m.stage, 0) + 1
    print("Summary:")
    for stage in ("1-mv", "2-audio", "2-plain", "3-topic", "fallback", "none"):
        if stage in by_stage:
            print(f"  {stage:>10}: {by_stage[stage]}")
    return 0


def read_release_list(csv_path: Path) -> list[str]:
    """
    Read a CSV of release URLs. Treats the first column of each row as
    the URL/UUID. Skips blank rows. Auto-skips a header row if the first
    cell doesn't parse as a URL or UUID.
    """
    urls: list[str] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        for i, row in enumerate(csv.reader(f)):
            if not row:
                continue
            cell = row[0].strip()
            if not cell:
                continue
            # Auto-skip header on first row.
            if i == 0 and not (
                MB_RELEASE_RE.search(cell)
                or re.fullmatch(r"[0-9a-f-]{36}", cell, re.IGNORECASE)
            ):
                continue
            urls.append(cell)
    return urls


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Find YouTube links for every track on a MusicBrainz release."
    )
    p.add_argument(
        "release",
        nargs="?",
        help="MusicBrainz release URL or UUID. If omitted (and --from-csv "
             "isn't used), the script prompts for one.",
    )
    p.add_argument(
        "--from-csv",
        dest="from_csv",
        help="Path to a CSV whose first column is a list of MusicBrainz "
             "release URLs/UUIDs. Each one is processed in turn; each "
             "album gets its own output CSV.",
        default=None,
    )
    p.add_argument(
        "--output",
        help="CSV output path (default: <Artist> - <Album>.csv in cwd). "
             "Ignored in --from-csv mode.",
        default=None,
    )
    p.add_argument(
        "--no-topic-detect",
        action="store_true",
        help="Always run Stage 3 (Topic trick) even if no Topic channel exists.",
    )
    args = p.parse_args(argv)

    # If nothing was specified on the command line, fall back to an
    # `albums.csv` sitting next to this script. That way "hit Run in
    # VS Code" Just Works as a batch run.
    if not args.from_csv and not args.release:
        default_csv = Path(__file__).resolve().parent / "albums.csv"
        if default_csv.exists():
            print(f"No args given — defaulting to {default_csv.name}\n")
            args.from_csv = str(default_csv)

    # --- Batch mode: read URLs from a CSV ----------------------------------
    if args.from_csv:
        csv_path = Path(args.from_csv)
        if not csv_path.exists():
            print(f"Input CSV not found: {csv_path}", file=sys.stderr)
            return 1
        urls = read_release_list(csv_path)
        if not urls:
            print(f"No release URLs found in {csv_path}", file=sys.stderr)
            return 1
        print(f"Batch mode: processing {len(urls)} releases from {csv_path}\n")
        fails = 0
        for i, url in enumerate(urls, 1):
            print(f"\n{'=' * 60}")
            print(f"[{i}/{len(urls)}] {url}")
            print('=' * 60)
            try:
                process_release(url, None, args.no_topic_detect)
            except Exception as exc:
                print(f"  ! failed: {exc}", file=sys.stderr)
                fails += 1
        print(f"\nBatch done. {len(urls) - fails}/{len(urls)} succeeded.")
        return 0 if fails == 0 else 1

    # --- Single mode with command-line arg --------------------------------
    if args.release:
        output_path = Path(args.output) if args.output else None
        return process_release(args.release, output_path, args.no_topic_detect)

    # --- Interactive mode: prompt for URLs in a loop ----------------------
    # Nice for running from VS Code (just hit Run): paste URLs one after
    # another. Blank line or Ctrl-C exits.
    print("album_bot — interactive mode")
    print("Paste a MusicBrainz release URL (or UUID), then Enter.")
    print("Leave blank and hit Enter to quit.\n")

    while True:
        try:
            release_arg = input("Release URL > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            return 0
        if not release_arg:
            print("Goodbye.")
            return 0
        try:
            process_release(release_arg, None, args.no_topic_detect)
        except SystemExit as exc:
            # extract_release_id / fetch_release raise SystemExit on bad input;
            # we don't want one bad URL to kill the loop.
            print(f"  ! {exc}", file=sys.stderr)
        except Exception as exc:
            print(f"  ! failed: {exc}", file=sys.stderr)
        print()  # blank line between albums


if __name__ == "__main__":
    sys.exit(main())
