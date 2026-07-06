#!/usr/bin/env python3
"""
The Everyday Ham - Host Tracker poller  (multi-SSID, person-keyed).

Each host can beacon from several APRS SSIDs (mobile -9, HT -7, and so on).
This poller watches a curated list of SSIDs per person and merges them into
ONE trail that follows the person, whichever radio they happened to use.

IMPORTANT: only list SSIDs that MOVE WITH THE PERSON (mobile, handheld).
Do NOT list fixed stations (home digipeater / igate / weather). A fixed
station beacons constantly from one spot and would pin the trail at home,
hiding the person's actual movement.

Env:
  APRS_API_KEY  (required)  your aprs.fi API key -> repo secret
  OUT_FILE      (optional)  output path, default host-tracker.json
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

API_KEY  = os.environ.get("APRS_API_KEY", "").strip()
OUT_FILE = Path(os.environ.get("OUT_FILE", "host-tracker.json"))

TRAIL_HOURS = 72
MAX_POINTS  = 1500

# id    = the key the board matches on (use the base callsign).
# watch = the APRS SSIDs to follow and merge for that person (moving radios only).
HOSTS = [
    {"id": "K8JKU", "watch": ["K8JKU-9", "K8JKU-3"]},                       # James
    {"id": "N8JRD", "watch": ["N8JRD-1", "N8JRD-3", "N8JRD-7", "N8JRD-8",
                              "N8JRD-9", "N8JRD-11", "N8JRD-14"]},           # Jim (dropped -4, -15 home stations)
    {"id": "W8KNX", "watch": ["W8KNX-1", "W8KNX-3", "W8KNX-7", "W8KNX-9"]},  # Rory (dropped -B)
]

APRS_URL   = "https://api.aprs.fi/api/get"
USER_AGENT = "EverydayHam-HostTracker/2.0 (github actions)"
BATCH      = 20   # aprs.fi allows up to 20 targets per query


def to_float(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def fetch(calls):
    query = urllib.parse.urlencode({
        "name":   ",".join(calls),
        "what":   "loc",
        "apikey": API_KEY,
        "format": "json",
    })
    req = urllib.request.Request(APRS_URL + "?" + query,
                                 headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.load(resp)
    if str(data.get("result")) != "ok":
        raise RuntimeError("aprs.fi returned: " + json.dumps(data)[:300])
    return data.get("entries", [])


def fetch_all(all_calls, tries=3):
    entries = []
    for batch in chunks(all_calls, BATCH):
        delay = 2
        for attempt in range(tries):
            try:
                entries.extend(fetch(batch))
                break
            except Exception as exc:  # noqa: BLE001
                if attempt == tries - 1:
                    raise
                print(f"batch retry ({exc})", file=sys.stderr)
                time.sleep(delay)
                delay *= 2
    return entries


def load_existing():
    if OUT_FILE.exists():
        try:
            return json.loads(OUT_FILE.read_text())
        except Exception:  # noqa: BLE001
            print("existing file unreadable; starting fresh", file=sys.stderr)
    return {"generated": 0, "hosts": []}


def main():
    if not API_KEY:
        print("APRS_API_KEY is not set", file=sys.stderr)
        sys.exit(1)

    doc = load_existing()
    prev = {h.get("callsign"): h for h in doc.get("hosts", [])}

    # flatten the watch lists into one de-duped query set
    all_calls = []
    for host in HOSTS:
        for ssid in host["watch"]:
            if ssid not in all_calls:
                all_calls.append(ssid)

    try:
        entries = fetch_all(all_calls)
    except Exception as exc:  # noqa: BLE001
        print(f"poll failed, leaving file untouched: {exc}", file=sys.stderr)
        sys.exit(0)

    by_call = {e.get("name", "").upper(): e for e in entries}
    now = int(time.time())
    cutoff = now - TRAIL_HOURS * 3600
    out_hosts = []
    appended = 0

    for host in HOSTS:
        hid = host["id"]
        rec = prev.get(hid, {})
        track = list(rec.get("track", []))
        seen = dict(rec.get("seen", {}))   # per-SSID last-heard, to avoid duplicates

        # find the single most recent NEW beacon across this person's radios
        newest = None
        for ssid in host["watch"]:
            entry = by_call.get(ssid.upper())
            if not entry:
                continue
            lasttime = int(to_float(entry.get("lasttime") or entry.get("time") or now))
            if lasttime > seen.get(ssid, 0):
                if newest is None or lasttime > newest[1]:
                    newest = (ssid, lasttime, entry)
            seen[ssid] = max(seen.get(ssid, 0), lasttime)

        # one point per poll: the person's most recent position, from any radio
        if newest:
            ssid, lasttime, entry = newest
            lat, lng = entry.get("lat"), entry.get("lng")
            if lat is not None and lng is not None:
                track.append({
                    "time":   lasttime,
                    "lat":    round(to_float(lat), 6),
                    "lng":    round(to_float(lng), 6),
                    "speed":  round(to_float(entry.get("speed")), 1),
                    "course": int(to_float(entry.get("course"))),
                    "ssid":   ssid,
                })
                appended += 1

        track = [p for p in track if p.get("time", 0) >= cutoff]
        track.sort(key=lambda p: p["time"])
        if len(track) > MAX_POINTS:
            track = track[-MAX_POINTS:]

        out_hosts.append({"callsign": hid, "track": track, "seen": seen})

    OUT_FILE.write_text(json.dumps({"generated": now, "hosts": out_hosts},
                                   separators=(",", ":")))
    summary = ", ".join(f"{h['callsign']}:{len(h['track'])}" for h in out_hosts)
    print(f"wrote {OUT_FILE} ({appended} new) -> {summary}")


if __name__ == "__main__":
    main()
