# musicBrains

**A YouTube-backed music source for [Lidarr](https://lidarr.audio/) — plus a standalone CLI.**

musicBrains gives Lidarr a way to fill albums from YouTube. It runs as a small
service that presents itself to Lidarr as two things Lidarr already understands:

- a **Newznab indexer** — when Lidarr searches for "Artist – Album", musicBrains
  answers with a synthetic release backed by a MusicBrainz lookup;
- a **SABnzbd-style download client** — when Lidarr grabs that release,
  musicBrains finds a YouTube link for every track, downloads the audio, tags it
  from MusicBrainz, and reports the finished album back so Lidarr imports it.

Because Lidarr drives the whole flow, **no Lidarr API key is needed at runtime**.
It works against a **stock Lidarr** — no plugins branch required.

It's also still a standalone command-line tool for dumping an album's YouTube
links to a CSV and batch-downloading them (see [Standalone CLI](#standalone-cli)).

---

## Contents

- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Quick start (Docker)](#quick-start-docker)
- [Attaching it to Lidarr](#attaching-it-to-lidarr)
  - [The one storage rule](#the-one-storage-rule)
  - [Option A — auto-provision script](#option-a--auto-provision-script)
  - [Option B — manual setup in the Lidarr UI](#option-b--manual-setup-in-the-lidarr-ui)
  - [If you have other download clients / indexers](#if-you-have-other-download-clients--indexers)
- [Configuration reference](#configuration-reference)
- [Audio quality & your Lidarr quality profile](#audio-quality--your-lidarr-quality-profile)
- [YouTube bot-checks (cookies)](#youtube-bot-checks-cookies)
- [Troubleshooting](#troubleshooting)
- [Standalone CLI](#standalone-cli)
- [How matching works (the cascade)](#how-matching-works-the-cascade)
- [Limitations](#limitations)

---

## How it works

```
        ┌──────────┐   1. search Artist/Album        ┌───────────────────────┐
        │          │ ──────────────────────────────▶ │  musicBrains           │
        │          │                                  │  /newznab  (indexer)   │
        │          │ ◀── synthetic release (MB-backed)│                        │
        │  Lidarr  │                                  │                        │
        │          │   2. grab release                │                        │
        │          │ ──────────────────────────────▶ │  /sabnzbd  (dl client) │
        │          │                                  │     │                  │
        │          │   3. poll queue/history          │     ▼ cascade + yt-dlp │
        │          │ ◀──────── "Completed" + path ─── │  download + tag audio  │
        └────┬─────┘                                  └──────────┬────────────┘
             │ 4. import from the completed folder               │ writes files
             ▼                                                    ▼
        /music library  ◀───────────────────────────────  shared completed dir
```

Track-to-video matching reuses the cascade described in
[How matching works](#how-matching-works-the-cascade). Files are tagged with the
MusicBrainz artist / album / track number / title **and** the MusicBrainz album &
track IDs, which lets Lidarr import them with high confidence.

---

## Requirements

- **Docker** + Docker Compose (the service ships ffmpeg inside the image).
- A running **Lidarr** instance.
- **Shared storage** Lidarr can read the finished files from — see
  [The one storage rule](#the-one-storage-rule).

---

## Quick start (Docker)

```bash
git clone https://github.com/razar4jajo132/musicBrains.git
cd musicBrains

cp .env.example .env
# edit .env: set MB_API_KEY (openssl rand -hex 16), pick audio format, set PUID/PGID
# and point MB_DOWNLOADS_HOST at storage Lidarr can see (see below)

docker compose up -d --build
```

Check it's alive:

```bash
curl http://localhost:8787/health        # {"ok":true,...}
```

Then attach it to Lidarr ↓.

---

## Attaching it to Lidarr

### The one storage rule

Lidarr imports an album by **reading the finished files**. So the folder
musicBrains writes to must be reachable by Lidarr at a path Lidarr can resolve.
Two ways:

1. **Same host as Lidarr (recommended, zero extra config).** Mount the *same*
   host folder Lidarr uses for completed downloads, at the *same container path*
   Lidarr sees it. Then the path musicBrains reports is already valid for Lidarr.

   Example — a LinuxServer stack where Lidarr mounts the host's
   `/media/Downloads` as `/config-complete`:

   ```yaml
   # docker-compose.yml
   volumes:
     - /media/Downloads:/config-complete
   environment:
     MB_COMPLETED_DIR: /config-complete/youtube/completed
     MB_INCOMPLETE_DIR: /config-complete/youtube/incomplete
   ```

   (Or set `MB_DOWNLOADS_HOST=/media/Downloads` and the matching `MB_*_DIR`
   values via `.env`.)

2. **Different host / different path.** Mount any shared storage (NFS/SMB) and
   add a **Lidarr → Settings → Download Clients → Remote Path Mapping** that
   translates the path musicBrains reports into the path Lidarr sees.

Also run the container as the **same uid/gid as Lidarr** (`PUID`/`PGID`, usually
`1000`) so imported files are owned by a user Lidarr can move and retag.

### Option A — auto-provision script

Adds the indexer and download client for you, reading Lidarr's own field schema
so values are always correct. Idempotent (safe to re-run).

```bash
python3 provision_lidarr.py \
  --lidarr-url  http://localhost:8686 \
  --lidarr-key  <LIDARR_API_KEY> \
  --service-url http://<MB_HOST>:8787 \
  --service-key <MB_API_KEY> \
  --prefer --solo-indexer
```

- `--prefer` — make musicBrains the preferred download client (priority 1) and
  demote other usenet clients, so music grabs go to it.
- `--solo-indexer` — disable automatic/interactive search on all other indexers,
  so only musicBrains searches for music.

`<LIDARR_API_KEY>` is in Lidarr → Settings → General. `<MB_API_KEY>` is your
`MB_API_KEY`. Only standard-library Python is needed.

### Option B — manual setup in the Lidarr UI

**Settings → Indexers → ➕ → Newznab (custom):**

| Field | Value |
|---|---|
| Name | `musicBrains (YouTube)` |
| URL | `http://<MB_HOST>:8787/newznab` |
| API Path | `/api` |
| API Key | your `MB_API_KEY` |
| Categories | `3000` (Audio) |

**Settings → Download Clients → ➕ → SABnzbd:**

| Field | Value |
|---|---|
| Name | `musicBrains (YouTube)` |
| Host | `<MB_HOST>` |
| Port | `8787` |
| URL Base | `sabnzbd` |
| API Key | your `MB_API_KEY` |
| Category | `music` (must equal `MB_CATEGORY`) |

Hit **Test** on each — both should go green.

### If you have other download clients / indexers

Lidarr picks a download client by **protocol + priority only** — there's no
"this indexer uses that client" binding. So if you already have another **usenet**
download client (e.g. a real SABnzbd/NZBGet), make musicBrains the preferred one
(priority 1) and demote the others, or grabs may misroute. The `--prefer` flag
does this. Likewise `--solo-indexer` stops other indexers from competing for
music. If your other indexers are **Prowlarr-managed**, disable them in Prowlarr
for a durable change (a Prowlarr sync can re-enable them in Lidarr).

---

## Configuration reference

All via environment variables (see `.env.example`):

| Env var | Default | Meaning |
|---|---|---|
| `MB_API_KEY` | `changeme` | Shared key for the indexer + download client. **Set this.** |
| `PUID` / `PGID` | `1000` | uid/gid the container runs as — match Lidarr. |
| `MB_PORT` | `8787` | Host port. |
| `MB_AUDIO_FORMAT` | `native` | `native` (Opus/m4a, no re-encode) or `mp3`. |
| `MB_MP3_BITRATE` | `320` | kbps, when `MB_AUDIO_FORMAT=mp3`. |
| `MB_QUALITY_TOKEN` | `MP3-256` | Quality string in release titles. |
| `MB_CATEGORY` | `music` | SABnzbd category; match the download client. |
| `MB_DOWNLOADS_HOST` | `./downloads` | Host path mounted to `/downloads`. |
| `MB_COMPLETED_DIR` | `/downloads/completed` | Where finished albums land (in-container). |
| `MB_INCOMPLETE_DIR` | `/downloads/incomplete` | In-progress scratch (in-container). |
| `MB_YTDLP_COOKIES` | _(unset)_ | Path to a cookies.txt for YouTube. |
| `MB_REQUIRE_API_KEY` | `true` | Enforce the API key (turn off only for curl testing). |
| `MB_PUBLIC_URL` | _(auto)_ | Override the base URL baked into download links. |

---

## Audio quality & your Lidarr quality profile

YouTube audio is **lossy**. `MB_QUALITY_TOKEN` (default `MP3-256`) is the quality
string musicBrains advertises in release titles — your Lidarr **quality profile
must accept that quality**, or Lidarr will reject the grab. If grabs never start,
this is the first thing to check.

`MB_AUDIO_FORMAT=native` keeps YouTube's source codec (usually Opus) with no
re-encode — best fidelity from a lossy source. Use `mp3` if you need universal
player compatibility or your profile is MP3-only.

---

## YouTube bot-checks (cookies)

From datacenter/VPN IPs, YouTube sometimes blocks `yt-dlp` ("Sign in to confirm
you're not a bot"). The fix is to supply cookies from a logged-in browser:

1. Export `cookies.txt` (Netscape format) using a browser extension.
2. Mount it and point the service at it:
   ```yaml
   volumes:
     - ./cookies.txt:/cookies.txt:ro
   environment:
     MB_YTDLP_COOKIES: /cookies.txt
   ```

---

## Troubleshooting

- **Indexer "Test" fails / won't add.** Confirm `http://<MB_HOST>:8787/health`
  is reachable *from the Lidarr container* (use the host LAN IP, not
  `localhost`, if they're separate containers/hosts).
- **Grabs never start.** Your quality profile probably rejects `MB_QUALITY_TOKEN`
  — accept it (see above).
- **Download completes but never imports.** A storage path problem — Lidarr can't
  see the files. Re-check [The one storage rule](#the-one-storage-rule) (path
  match or Remote Path Mapping) and that file ownership matches Lidarr's PUID.
- **Grabs from another indexer fail on musicBrains.** Routing collision — use
  `--prefer`/`--solo-indexer` (see above).
- **Logs:** `docker logs musicbrains`.

---

## Standalone CLI

The original tools still work without Lidarr.

```bash
pip install -r requirements.txt
```

**One album:**

```bash
python album_bot.py https://musicbrainz.org/release/<release-id>
```

Accepts a full release URL or a bare UUID. Writes `<Artist> - <Album>.csv`. Use
`--output` to override; run with no argument for an interactive prompt.

**Batch from a CSV of releases:**

```bash
python album_bot.py --from-csv albums.csv
```

First column of each row is a release URL/UUID (header auto-skipped).

**Download the links** a CSV contains:

```bash
python Downloader.py   # downloads audio for every *.csv in the folder
```

### CSV columns

| column | meaning |
|---|---|
| `track_no` | Position on the album |
| `artist` / `title` | From MusicBrainz |
| `mb_duration` | MusicBrainz duration, `m:ss` |
| `youtube_url` / `yt_channel` / `yt_title` / `yt_duration` | Chosen video |
| `stage` | `1-mv`, `2-audio`, `3-topic`, `fallback`, or `none` |
| `duration_match` | `exact`, `+1s`, `-3s`, `no_match`, … |
| `alt_official_mv` | An official MV URL when the primary pick is unofficial |

---

## How matching works (the cascade)

For each track, up to three YouTube searches run in order, stopping at the first
acceptable result:

1. **Stage 1 (MV):** `<artist> "<track>" official MV`
2. **Stage 2 (audio):** `<artist> "<track>" official audio`
3. **Stage 3 (Topic):** `"<artist> - Topic" "<track>"` — only if the artist has a
   "Topic" auto-channel (detected with one probe at startup).

A candidate is **acceptable** when both:
- the channel is the artist's own, an `<Artist> - Topic`, or a known label
  channel (HYBE, BANGTANTV, JYP, Atlantic, …), **and**
- the duration is within ±5s of the MusicBrainz length.

If no stage qualifies, it falls back to the best duration-matching candidate
(de-prioritizing lyric/fancam/cover videos), and as a last resort accepts an
official upload within a wider ±30s window (catches extended K-pop MV cuts).

Tuning constants live at the top of `album_bot.py`
(`DURATION_TOLERANCE_SECONDS`, `RESULTS_PER_QUERY`, `GLOBAL_OFFICIAL_CHANNELS`, …).

---

## Limitations

- **Lossy source.** YouTube audio isn't lossless; set quality profiles
  accordingly.
- **Partial albums complete.** A track with no acceptable match is skipped and
  the album still imports with the rest — check `docker logs` for skips.
- **Interlude / parenthetical tracks** sometimes match by duration only and show
  as `fallback`.
- **Major-label K-pop** rarely has Topic channels or exact album-cut audio, so it
  leans on the MV/fallback paths.
- **Results vary** by region and YouTube's index freshness; the same album can
  resolve differently across runs.
- **No per-indexer client routing in Lidarr** — see
  [If you have other download clients](#if-you-have-other-download-clients--indexers).
