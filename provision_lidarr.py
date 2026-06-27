#!/usr/bin/env python3
"""
provision_lidarr.py
===================

Attach the musicBrains service to a Lidarr instance automatically: add the
Newznab indexer and the SABnzbd-style download client, with the right field
values pulled from Lidarr's own schema (so they're always correct for your
Lidarr version).

This is optional — you can do the same thing by hand in the Lidarr UI (see the
README). It's just faster and less error-prone.

Idempotent: matching entries (by name) are updated, not duplicated. Safe to
re-run.

Examples
--------
Basic attach (indexer + download client):

    python provision_lidarr.py \\
        --lidarr-url http://localhost:8686 \\
        --lidarr-key <LIDARR_API_KEY> \\
        --service-url http://192.168.1.50:8787 \\
        --service-key <MB_API_KEY>

Make musicBrains the *preferred* download client (priority 1) and demote any
existing usenet clients, AND make it the only active music indexer:

    python provision_lidarr.py ... --prefer --solo-indexer

Flags
-----
--category        SABnzbd category to use (default: music).
--prefer          Set our client to priority 1 and bump every other usenet
                  download client to priority 50, so music grabs prefer ours.
--solo-indexer    Turn OFF automatic + interactive search on all OTHER indexers
                  so only musicBrains searches for music. (Note: Prowlarr-managed
                  indexers may be re-enabled on the next Prowlarr sync — disable
                  them in Prowlarr for a durable change.)
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

INDEXER_NAME = "musicBrains (YouTube)"
CLIENT_NAME = "musicBrains (YouTube)"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lidarr-url", required=True, help="e.g. http://localhost:8686")
    ap.add_argument("--lidarr-key", required=True, help="Lidarr API key (Settings > General)")
    ap.add_argument("--service-url", required=True, help="musicBrains base URL, e.g. http://HOST:8787")
    ap.add_argument("--service-key", required=True, help="musicBrains API key (MB_API_KEY)")
    ap.add_argument("--category", default="music")
    ap.add_argument("--prefer", action="store_true")
    ap.add_argument("--solo-indexer", action="store_true")
    args = ap.parse_args()

    api = args.lidarr_url.rstrip("/") + "/api/v1"
    svc = urllib.parse.urlparse(args.service_url)
    svc_host = svc.hostname or ""
    svc_port = svc.port or (443 if svc.scheme == "https" else 80)
    svc_ssl = svc.scheme == "https"
    indexer_base = args.service_url.rstrip("/") + "/newznab"

    def req(method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(api + path, data=data, method=method,
                                   headers={"X-Api-Key": args.lidarr_key,
                                            "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                raw = resp.read().decode()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def field_set(fields, name, value):
        for f in fields:
            if f.get("name") == name:
                f["value"] = value
                return

    # --- download client ---------------------------------------------------
    _, schema = req("GET", "/downloadclient/schema")
    sab = next((s for s in schema if s.get("implementation") == "Sabnzbd"), None)
    if not sab:
        print("ERROR: this Lidarr has no SABnzbd download client schema", file=sys.stderr)
        return 1
    fields = sab["fields"]
    field_set(fields, "host", svc_host)
    field_set(fields, "port", svc_port)
    field_set(fields, "useSsl", svc_ssl)
    field_set(fields, "urlBase", "sabnzbd")
    field_set(fields, "apiKey", args.service_key)
    for f in fields:
        if "category" in f.get("name", "").lower():
            f["value"] = args.category
    client = {
        "enable": True, "protocol": "usenet",
        "priority": 1 if args.prefer else 1,
        "name": CLIENT_NAME, "implementation": sab["implementation"],
        "implementationName": sab["implementationName"],
        "configContract": sab["configContract"], "fields": fields, "tags": [],
    }
    _, existing = req("GET", "/downloadclient")
    cur = next((c for c in existing if c["name"] == CLIENT_NAME), None)
    if cur:
        client["id"] = cur["id"]
        st, _ = req("PUT", f"/downloadclient/{cur['id']}", client)
        print(f"download client: updated (status {st})")
    else:
        st, _ = req("POST", "/downloadclient", client)
        print(f"download client: created (status {st})")

    if args.prefer:
        _, clients = req("GET", "/downloadclient")
        for c in clients:
            if c["name"] != CLIENT_NAME and c.get("protocol") == "usenet":
                c["priority"] = 50
                req("PUT", f"/downloadclient/{c['id']}", c)
                print(f"  demoted other usenet client '{c['name']}' -> priority 50")

    # --- indexer -----------------------------------------------------------
    _, schema = req("GET", "/indexer/schema")
    nz = next((s for s in schema if s.get("implementation") == "Newznab"), None)
    if not nz:
        print("ERROR: this Lidarr has no Newznab indexer schema", file=sys.stderr)
        return 1
    fields = nz["fields"]
    field_set(fields, "baseUrl", indexer_base)
    field_set(fields, "apiPath", "/api")
    field_set(fields, "apiKey", args.service_key)
    field_set(fields, "categories", [3000, 3010, 3040])
    indexer = {
        "enable": True, "enableRss": False,
        "enableAutomaticSearch": True, "enableInteractiveSearch": True,
        "protocol": "usenet", "priority": 1,
        "name": INDEXER_NAME, "implementation": nz["implementation"],
        "implementationName": nz["implementationName"],
        "configContract": nz["configContract"], "fields": fields, "tags": [],
    }
    _, existing = req("GET", "/indexer")
    cur = next((c for c in existing if c["name"] == INDEXER_NAME), None)
    if cur:
        indexer["id"] = cur["id"]
        st, _ = req("PUT", f"/indexer/{cur['id']}", indexer)
        print(f"indexer: updated (status {st})")
    else:
        st, _ = req("POST", "/indexer", indexer)
        print(f"indexer: created (status {st})")

    if args.solo_indexer:
        _, idxs = req("GET", "/indexer")
        for i in idxs:
            if i["name"] == INDEXER_NAME:
                continue
            i["enable"] = False
            i["enableRss"] = False
            i["enableAutomaticSearch"] = False
            i["enableInteractiveSearch"] = False
            req("PUT", f"/indexer/{i['id']}", i)
            print(f"  disabled other indexer '{i['name']}'")

    print("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
