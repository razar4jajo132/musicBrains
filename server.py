"""
server.py
=========

The HTTP surface Lidarr talks to. One FastAPI app exposing two faces:

  * ``/newznab/api``  — a Newznab (usenet) **indexer**. Lidarr searches it for
    "Artist - Album"; we answer with synthetic releases whose download link
    points back at us and carries the MusicBrainz release id.

  * ``/sabnzbd/api``  — a fake **SABnzbd** download client. When Lidarr grabs
    one of our releases it sends it here; we enqueue a real YouTube download
    (see jobs.py) and report queue/history back in SABnzbd's shape.

  * ``/getnzb``       — serves the tiny NZB file our indexer links to. The file
    is just a carrier for the release MBID; ``addfile`` parses it back out.

Configure in Lidarr:
  Indexer  -> Newznab (custom):  URL = http://<host>:<port>/newznab , API key = MB_API_KEY
  Download -> SABnzbd:           Host/Port = <host>/<port>, URL base = sabnzbd,
                                 API key = MB_API_KEY, Category = music
"""

from __future__ import annotations

import re
from xml.sax.saxutils import escape, quoteattr

from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse, PlainTextResponse, Response

import config
import core
from jobs import store, COMPLETED, FAILED

app = FastAPI(title="musicBrains → Lidarr bridge")

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                     r"[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


@app.on_event("startup")
def _startup() -> None:
    config.ensure_dirs()


# --- helpers ---------------------------------------------------------------

def _auth_ok(apikey: str | None) -> bool:
    if not config.REQUIRE_API_KEY:
        return True
    return apikey == config.API_KEY


def _base_url(request: Request) -> str:
    """The externally-reachable base URL, for building download links."""
    if config.PUBLIC_URL:
        return config.PUBLIC_URL
    return str(request.base_url).rstrip("/")


# === Newznab indexer =======================================================

CAPS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<caps>
  <server title="musicBrains" />
  <limits max="100" default="{default}" />
  <searching>
    <search available="yes" supportedParams="q" />
    <music-search available="yes" supportedParams="q,artist,album" />
    <audio-search available="yes" supportedParams="q,artist,album" />
  </searching>
  <categories>
    <category id="3000" name="Audio">
      <subcat id="3010" name="Audio/MP3" />
      <subcat id="3040" name="Audio/Lossless" />
    </category>
  </categories>
</caps>"""


def _caps_response() -> Response:
    return Response(content=CAPS_XML.format(default=config.MAX_SEARCH_RESULTS),
                    media_type="application/xml")


def _release_title(hit: core.ReleaseHit) -> str:
    year = f" ({hit.year})" if hit.year else ""
    return f"{hit.artist} - {hit.album}{year} [{config.QUALITY_TOKEN}]"


def _search_response(request: Request, hits: list[core.ReleaseHit]) -> Response:
    base = _base_url(request)
    items: list[str] = []
    for hit in hits:
        title = _release_title(hit)
        # Plausible fake size so Lidarr's min-size checks pass.
        size = max(1, hit.track_count) * int(8.0 * 1024 * 1024)
        dl = f"{base}/getnzb?id={hit.mbid}&apikey={config.API_KEY}"
        items.append(f"""    <item>
      <title>{escape(title)}</title>
      <guid isPermaLink="false">{escape(hit.mbid)}</guid>
      <link>{escape(dl)}</link>
      <comments>{escape(f'https://musicbrainz.org/release/{hit.mbid}')}</comments>
      <category>3010</category>
      <enclosure url={quoteattr(dl)} length="{size}" type="application/x-nzb" />
      <newznab:attr name="category" value="3000" />
      <newznab:attr name="category" value="3010" />
      <newznab:attr name="size" value="{size}" />
    </item>""")

    body = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">
  <channel>
    <title>musicBrains</title>
    <description>YouTube-backed Newznab indexer</description>
{chr(10).join(items)}
  </channel>
</rss>"""
    return Response(content=body, media_type="application/xml")


@app.get("/newznab/api")
@app.get("/newznab")
def newznab_api(request: Request):
    p = request.query_params
    if not _auth_ok(p.get("apikey")):
        return Response(
            content='<?xml version="1.0"?><error code="100" '
                    'description="Incorrect user credentials"/>',
            media_type="application/xml", status_code=401)

    t = (p.get("t") or "search").lower()
    if t == "caps":
        return _caps_response()

    if t in ("search", "music", "audio", "tvsearch", "movie"):
        try:
            hits = core.search_releases(
                artist=p.get("artist", ""),
                album=p.get("album", ""),
                free=p.get("q", ""),
            )
        except Exception as exc:  # noqa: BLE001
            return Response(
                content=f'<?xml version="1.0"?><error code="900" '
                        f'description={quoteattr(str(exc))}/>',
                media_type="application/xml", status_code=200)
        return _search_response(request, hits)

    # Unknown function — empty but valid feed.
    return _search_response(request, [])


@app.get("/getnzb")
def get_nzb(request: Request):
    p = request.query_params
    if not _auth_ok(p.get("apikey")):
        return PlainTextResponse("unauthorized", status_code=401)
    mbid = p.get("id", "")
    name = p.get("name", mbid)
    # A minimal, valid-enough NZB whose only job is to carry the MBID. Our
    # fake-SABnzbd parses the id straight back out of it on addfile.
    nzb = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE nzb PUBLIC "-//newzbin//DTD NZB 1.1//EN" "http://www.newzbin.com/DTD/nzb/nzb-1.1.dtd">
<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">
  <head>
    <meta type="mbid">{escape(mbid)}</meta>
    <meta type="name">{escape(name)}</meta>
  </head>
  <file poster="musicBrains" date="0" subject="mbid:{escape(mbid)} - {escape(name)}">
    <groups><group>alt.binaries.music</group></groups>
    <segments><segment bytes="1" number="1">mbid-{escape(mbid)}@musicBrains</segment></segments>
  </file>
</nzb>"""
    return Response(
        content=nzb, media_type="application/x-nzb",
        headers={"Content-Disposition": f'attachment; filename="{mbid}.nzb"'})


# === Fake SABnzbd download client ==========================================

def _extract_mbid(*texts: str) -> str:
    for t in texts:
        if not t:
            continue
        m = UUID_RE.search(t)
        if m:
            return m.group(0)
    return ""


def _queue_json() -> dict:
    slots = []
    for j in store.active():
        slots.append({
            "status": j.status,
            "nzo_id": j.nzo_id,
            "filename": j.name,
            "cat": j.category,
            "percentage": str(j.percentage),
            "mb": f"{j.total_mb:.1f}",
            "mbleft": f"{j.mb_left:.1f}",
            "size": f"{j.total_mb:.1f} MB",
            "sizeleft": f"{j.mb_left:.1f} MB",
            "timeleft": "0:10:00",
            "priority": "Normal",
        })
    return {"queue": {
        "paused": False, "slots": slots,
        "speed": "1 M", "kbpersec": "1024.0",
        "mb": "0.0", "mbleft": "0.0",
        "diskspace1": "100.0", "diskspacetotal1": "1000.0",
    }}


def _history_json() -> dict:
    slots = []
    for j in store.finished():
        slots.append({
            "nzo_id": j.nzo_id,
            "name": j.name,
            "nzb_name": f"{j.name}.nzb",
            "category": j.category,
            "status": j.status,
            "storage": j.storage,
            "path": j.storage,
            "bytes": int(j.total_mb * 1024 * 1024),
            "fail_message": j.error if j.status == FAILED else "",
            "download_time": 1,
            "completeness": 100 if j.status == COMPLETED else 0,
        })
    return {"history": {"slots": slots, "noofslots": len(slots)}}


@app.api_route("/sabnzbd/api", methods=["GET", "POST"])
@app.api_route("/api", methods=["GET", "POST"])
async def sabnzbd_api(
    request: Request,
    name: UploadFile | None = File(default=None),
    nzbname: str | None = Form(default=None),
    cat: str | None = Form(default=None),
):
    p = request.query_params
    if not _auth_ok(p.get("apikey")):
        return JSONResponse({"status": False, "error": "API Key Incorrect"},
                            status_code=200)

    mode = (p.get("mode") or "").lower()

    if mode == "version":
        return JSONResponse({"version": "3.7.2"})

    if mode == "get_cats":
        return JSONResponse({"categories": ["*", config.CATEGORY]})

    if mode == "get_config":
        return JSONResponse({"config": {
            "misc": {
                "complete_dir": str(config.COMPLETED_DIR),
                "pre_check": 0, "enable_tv_sorting": 0,
                "enable_movie_sorting": 0, "enable_date_sorting": 0,
            },
            "categories": [
                {"name": "*", "dir": "", "pp": "3", "priority": 0},
                {"name": config.CATEGORY, "dir": "", "pp": "3", "priority": 0},
            ],
            "servers": [],
        }})

    if mode == "fullstatus":
        return JSONResponse({"status": {
            "completedir": str(config.COMPLETED_DIR), "paused": False,
        }})

    if mode == "queue":
        # Support delete from queue: mode=queue&name=delete&value=<nzo_id>
        if (p.get("name") or "").lower() == "delete":
            for v in (p.get("value") or "").split(","):
                store.remove(v.strip())
            return JSONResponse({"status": True})
        return JSONResponse(_queue_json())

    if mode == "history":
        if (p.get("name") or "").lower() == "delete":
            for v in (p.get("value") or "").split(","):
                store.remove(v.strip())
            return JSONResponse({"status": True})
        return JSONResponse(_history_json())

    if mode == "addfile":
        raw = await name.read() if name is not None else b""
        mbid = _extract_mbid(raw.decode("utf-8", "ignore"), nzbname or "")
        if not mbid:
            return JSONResponse({"status": False,
                                 "error": "no MusicBrainz id in nzb"})
        nzo = store.enqueue(mbid, nzbname or mbid, cat or p.get("cat"))
        return JSONResponse({"status": True, "nzo_ids": [nzo]})

    if mode == "addurl":
        url = p.get("name", "")
        mbid = _extract_mbid(url)
        if not mbid:
            return JSONResponse({"status": False,
                                 "error": "no MusicBrainz id in url"})
        nzo = store.enqueue(mbid, p.get("nzbname", mbid), p.get("cat"))
        return JSONResponse({"status": True, "nzo_ids": [nzo]})

    return JSONResponse({"status": True})


# === Health / human landing ================================================

@app.get("/")
def index():
    return PlainTextResponse(
        "musicBrains → Lidarr bridge is running.\n"
        f"  Newznab indexer:  /newznab/api   (apikey required)\n"
        f"  SABnzbd client:   /sabnzbd/api\n"
        f"  Completed dir:    {config.COMPLETED_DIR}\n"
        f"  Audio format:     {config.AUDIO_FORMAT}\n")


@app.get("/health")
def health():
    return {"ok": True, "active": len(store.active()),
            "finished": len(store.finished())}
