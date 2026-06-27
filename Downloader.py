#!/usr/bin/env python3
"""
Batch-download YouTube links from multiple CSV files as audio
(native source by default, with an optional 320 kbps MP3 mode),
cross-platform (Windows / macOS / Linux) with no system setup.

For every *.csv in the script's folder (sorted):
  1. create a subdirectory named after the CSV (its stem)
  2. read the youtube_url column
  3. download each link's audio into that subdirectory, in order
  4. embed metadata + cover art, then move to the next line / next CSV

Re-running is safe: a per-folder archive skips already-finished tracks.

One-time setup (any OS):
    python -m pip install --upgrade yt-dlp static-ffmpeg mutagen

Run:
    python download_batch.py

Note: static-ffmpeg downloads its ffmpeg binaries on first run, so the very
first execution needs an internet connection and a few extra seconds.
"""

import csv
import shutil
import sys
from pathlib import Path

# ------------------------------- CONFIG -------------------------------
SCRIPT_DIR  = Path(__file__).resolve().parent
CSV_DIR     = SCRIPT_DIR        # folder holding the .csv files
OUTPUT_ROOT = SCRIPT_DIR        # where the per-CSV subdirectories go
CSV_COLUMN  = "youtube_url"     # column holding the link (None = auto-detect)

# --- audio output mode ---------------------------------------------------
MP3_ONLY    = False             # False = keep native source audio (Opus/m4a),
                                #         no re-encode -> best fidelity from source
                                # True  = force 320 kbps MP3 for universal playback
MP3_BITRATE = "320"             # kbps, only used when MP3_ONLY is True
# -------------------------------------------------------------------------
EMBED_TAGS  = True              # embed metadata (title/artist/etc.) into each file
EMBED_ART   = False             # also embed cover art (needs the 'mutagen' package)
OUTPUT_TEMPLATE = "%(title)s [%(id)s].%(ext)s"
# ----------------------------------------------------------------------

CANDIDATE_COLUMNS = ("youtube_url", "url", "link", "URL", "Link", "video-url")


def ensure_ffmpeg():
    """Make ffmpeg/ffprobe available regardless of OS or prior install."""
    try:
        import static_ffmpeg
        static_ffmpeg.add_paths()          # downloads on first run, then PATHs them
        return
    except ImportError:
        pass
    # Fall back to a system ffmpeg if the helper package isn't installed.
    if shutil.which("ffmpeg") is None:
        sys.exit(
            "ffmpeg not available. Easiest fix (any OS):\n"
            "    python -m pip install static-ffmpeg\n"
            "or install ffmpeg system-wide and put it on PATH."
        )


def have_mutagen():
    """Cover-art embedding into Opus/Ogg/FLAC needs the mutagen package."""
    try:
        import mutagen  # noqa: F401
        return True
    except ImportError:
        return False


def read_links(csv_path: Path, column):
    # encoding='utf-8-sig' silently strips a Windows BOM if present
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    if not rows:
        return []

    header = [h.strip() for h in rows[0]]
    header_lower = [h.lower() for h in header]

    # Find the link column by NAME (deterministic) rather than guessing
    # whether a header exists with csv.Sniffer, which is unreliable.
    wanted = ([column] if column else []) + list(CANDIDATE_COLUMNS)
    col_idx = next(
        (header_lower.index(w.lower()) for w in wanted
         if w and w.lower() in header_lower),
        None,
    )

    if col_idx is not None:
        values = [r[col_idx] for r in rows[1:] if len(r) > col_idx]
    else:
        # No recognizable header: assume one bare link per row, first column
        values = [r[0] for r in rows if r]

    return [v.strip() for v in values if v.strip().lower().startswith("http")]


def make_ydl_opts(dest: Path, archive: Path):
    postprocessors = []
    if MP3_ONLY:
        postprocessors.append({
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": MP3_BITRATE,
        })
    else:
        # Native mode: copy the source audio stream into a proper audio
        # container (.opus / .m4a) with NO re-encode. YouTube delivers
        # bestaudio inside a .webm container, which ffmpeg cannot embed
        # artwork into; this remux fixes that while preserving fidelity.
        postprocessors.append({
            "key": "FFmpegExtractAudio",
            "preferredcodec": "best",
        })
    embed_art = EMBED_TAGS and EMBED_ART and have_mutagen()
    if EMBED_TAGS:
        postprocessors.append({"key": "FFmpegMetadata"})
        if embed_art:
            postprocessors.append({"key": "EmbedThumbnail"})
    return {
        "format": "bestaudio/best",
        "outtmpl": str(dest / OUTPUT_TEMPLATE),
        "download_archive": str(archive),
        "postprocessors": postprocessors,
        "writethumbnail": embed_art,
        "ignoreerrors": True,      # one bad link won't abort the batch
        "noplaylist": True,
        "quiet": False,
        "no_warnings": False,
    }


def process_csv(csv_file, YoutubeDL):
    """Download every link in one CSV into its own subdirectory.
    Returns (successes, failures). Never raises — failures are contained
    so they can't stop the rest of the batch."""
    subdir = Path(OUTPUT_ROOT) / csv_file.stem
    subdir.mkdir(parents=True, exist_ok=True)
    archive = subdir / ".yt-archive.txt"

    links = read_links(csv_file, CSV_COLUMN)
    print(f"  -> {subdir.name}/   ({len(links)} link(s))")
    if not links:
        print("     (no usable links)")
        return 0, 0

    ok = fail = 0
    with YoutubeDL(make_ydl_opts(subdir, archive)) as ydl:
        for i, url in enumerate(links, 1):
            print(f"  [{i}/{len(links)}] {url}")
            try:
                code = ydl.download([url])      # 0 == success
                if code == 0:
                    ok += 1
                else:
                    print("    ! download failed; continuing")
                    fail += 1
            except Exception as e:              # never let one link kill the loop
                print(f"    ! error: {e}; continuing")
                fail += 1
    return ok, fail


def main():
    ensure_ffmpeg()   # both modes run an ffmpeg post-processing step

    if EMBED_TAGS and EMBED_ART and not have_mutagen():
        print("NOTE: 'mutagen' isn't installed, so cover art won't be embedded.")
        print("      Metadata tags are still written. For artwork, install it:")
        print("      python -m pip install mutagen\n")
    try:
        from yt_dlp import YoutubeDL
    except ImportError:
        sys.exit("yt-dlp not installed.  python -m pip install --upgrade yt-dlp")

    csv_files = sorted(Path(CSV_DIR).glob("*.csv"))
    if not csv_files:
        sys.exit(f"No .csv files found in {Path(CSV_DIR).resolve()}")

    print(f"Found {len(csv_files)} CSV file(s) in {Path(CSV_DIR).resolve()}:")
    for cf in csv_files:
        print(f"  - {cf.name}")
    print()

    grand_ok = grand_fail = 0
    for n, csv_file in enumerate(csv_files, 1):
        print(f"===== CSV {n}/{len(csv_files)}: {csv_file.name} =====")
        try:
            ok, fail = process_csv(csv_file, YoutubeDL)
            grand_ok += ok
            grand_fail += fail
        except Exception as e:                  # never let one CSV kill the batch
            print(f"  !! problem with {csv_file.name}: {e}")
            print("     moving on to the next CSV")
        print()

    print(f"All done across {len(csv_files)} CSV(s). "
          f"Downloaded/verified: {grand_ok}, Failed: {grand_fail}")


if __name__ == "__main__":
    main()