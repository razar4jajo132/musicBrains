# album_bot / musicBrains

Given a MusicBrainz release URL, find a YouTube link for every track on the album and write them to a CSV — **and** serve that capability to Lidarr as a Newznab indexer + download client (see [Lidarr integration](#lidarr-integration)).

## Setup

```bash
pip install -r requirements.txt
```

That installs `requests` (for the MusicBrainz API) and `yt-dlp` (for YouTube search). No API keys needed.

## Usage

Three ways to run it.

**1. One album via the command line:**

```bash
python album_bot.py https://musicbrainz.org/release/9643849e-1d87-49eb-bbf0-53336942d1b5
```

You can also pass a bare release UUID. Output goes to `<Artist> - <Album>.csv` in the current directory. Use `--output` to override.

**2. One album, interactive:**

```bash
python album_bot.py
```

With no argument the script prompts you to paste a release URL.

**3. Batch — process a CSV of releases:**

```bash
python album_bot.py --from-csv albums.csv
```

`albums.csv` has the release URL in the first column of each row. A header row is optional (auto-skipped). Each release produces its own output CSV. A sample `albums.csv` is included.

## How it works

For each track on the album, the script runs up to three YouTube searches in cascade and stops at the first one that returns an acceptable result:

1. **Stage 1 (MV):** `<artist> "<track>" official MV`
2. **Stage 2 (audio):** `<artist> "<track>" official audio`
3. **Stage 3 (Topic):** `"<artist> - Topic" "<track>"` — only runs if the artist has a "Topic" auto-channel, which is detected with one probe query at startup.

A candidate is **acceptable** if both:
- The channel is the artist's own channel, an `<Artist> - Topic` channel, or a recognized label channel (HYBE LABELS, BANGTANTV, JYP Entertainment, Atlantic Records, …).
- The duration is within ±5 seconds of the MusicBrainz length.

If no stage produces an acceptable result, the script falls back to the best duration-matching candidate from any of the queries (even an unofficial reupload), and flags it in the CSV. As a last resort it accepts an official upload with a wider duration tolerance (±30s), which catches K-pop tracks where the only official YouTube version is an extended music-video cut.

## CSV columns

| column | meaning |
|---|---|
| `track_no` | Position on the album |
| `artist` | Artist name from MusicBrainz |
| `title` | Track title from MusicBrainz |
| `mb_duration` | MusicBrainz duration, `m:ss` |
| `youtube_url` | Chosen YouTube URL |
| `yt_channel` | Channel that posted the chosen video |
| `yt_title` | Video title |
| `yt_duration` | Video duration, `m:ss` |
| `stage` | Which cascade stage chose this pick: `1-mv`, `2-audio`, `3-topic`, `fallback`, or `none` |
| `duration_match` | `exact`, `+1s`, `-3s`, `+21s`, `no_match`, etc. |
| `alt_official_mv` | An official MV URL when the primary pick is an unofficial reupload — gives you a swap option |

## Tuning

Constants at the top of `album_bot.py` are easy to tweak:

- `DURATION_TOLERANCE_SECONDS` (default 5) — how strict the "acceptable" duration check is.
- `DURATION_LOOSE_TOLERANCE_SECONDS` (default 30) — wider window for the final official-channel fallback.
- `RESULTS_PER_QUERY` (default 5) — how many hits per search to look at.
- `GLOBAL_OFFICIAL_CHANNELS` — set of label/imprint channels to treat as official everywhere.

## Known limitations

- **Interlude tracks** with parenthetical titles (`(Guitar)`, `(Piano)`) often appear on YouTube under positional names (`Interlude 2`, `Interlude 3`). The cascade can still pick them via duration match, but they may show as `fallback` stage in the CSV even when the actual upload is from the artist's official channel.
- **Major-label K-pop releases** rarely have a `<Artist> - Topic` channel and rarely upload the exact album-cut audio. The cascade falls back to either the longer Official MV or a third-party reupload that matches the album length. Both are noted in the CSV — the `alt_official_mv` column gives you the official link when the primary pick is unofficial.
- **YouTube search results vary** by location and the freshness of YouTube's index, so the chosen video for the same album can differ across runs. Re-run with a different cascade ordering or tighter tolerances if you hit a bad pick.

---

## Lidarr integration

The same YouTube-finding cascade is exposed to **Lidarr** so it can fill missing
albums from YouTube automatically. The service (`server.py`) pretends to be two
things Lidarr already understands:

- a **Newznab indexer** — when Lidarr searches for "Artist – Album", we answer
  with a synthetic release backed by a MusicBrainz lookup;
- a **fake SABnzbd download client** — when Lidarr grabs that release, we run the
  cascade, download + tag every track from YouTube, and report the finished
  album back so Lidarr imports it.

No Lidarr API key is needed — Lidarr drives the whole flow.

### Run it

```bash
cp .env.example .env   # edit MB_API_KEY etc. (optional; compose has defaults)
docker compose up -d --build
```

The service listens on **:8787**. Finished albums are written under the mounted
`./downloads/completed/<category>/<Artist - Album>/`.

### Configure Lidarr

**Settings → Indexers → Add → Newznab (custom):**

| Field | Value |
|---|---|
| URL | `http://<host>:8787/newznab` |
| API Path | `/api` |
| API Key | the value of `MB_API_KEY` |
| Categories | `3000` (Audio) |

**Settings → Download Clients → Add → SABnzbd:**

| Field | Value |
|---|---|
| Host | `<host>` |
| Port | `8787` |
| URL Base | `sabnzbd` |
| API Key | the value of `MB_API_KEY` |
| Category | `music` |

Both "Test" buttons should go green.

### Important notes

- **File visibility.** Lidarr imports by *reading the finished files*, so the
  `downloads` volume must be storage Lidarr can also see. Easiest: run this on
  the **same host as Lidarr** and point both at the same path. Across hosts, use
  a shared mount (NFS/SMB) plus Lidarr **Remote Path Mapping**.
- **Quality profile.** YouTube audio is lossy. `MB_QUALITY_TOKEN` (default
  `MP3-256`) is what we put in release titles — your Lidarr quality profile must
  *accept* that quality or it will reject the grab.
- **YouTube bot-checks.** From datacenter/VPN IPs YouTube may block yt-dlp. Export
  a `cookies.txt` from a logged-in browser, mount it, and set
  `MB_YTDLP_COOKIES=/cookies.txt`.
- **Partial albums.** A track with no acceptable YouTube match is skipped; the
  album still completes with the rest. Check the container logs for skips.

### Configuration reference

| Env var | Default | Meaning |
|---|---|---|
| `MB_API_KEY` | `changeme` | Shared key for indexer + download client |
| `MB_AUDIO_FORMAT` | `native` | `native` (Opus/m4a, no re-encode) or `mp3` |
| `MB_MP3_BITRATE` | `320` | kbps, when `MB_AUDIO_FORMAT=mp3` |
| `MB_QUALITY_TOKEN` | `MP3-256` | Quality string in release titles |
| `MB_CATEGORY` | `music` | SABnzbd category Lidarr is configured with |
| `MB_COMPLETED_DIR` | `/downloads/completed` | Where finished albums land |
| `MB_YTDLP_COOKIES` | _(unset)_ | Path to cookies.txt for YouTube |
| `MB_PORT` | `8787` | Listen port |
