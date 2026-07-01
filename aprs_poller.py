#!/usr/bin/env python3
"""
The Everyday Ham - Host Tracker poller.

Queries the aprs.fi API for the host roster and maintains a rolling
24-hour position log in host-tracker.json, which the DakBoard display reads.

aprs.fi only returns each station's *current* position, so this script is
what grows the breadcrumb trail: every run it appends any new beacon and
prunes anything older than the trail window.

Env vars:
  APRS_API_KEY  (required)  your aprs.fi API key  -> store as a repo secret
  OUT_FILE      (optional)  output path, default host-tracker.json
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

# ------------------------------------------------------------------ config
API_KEY   = os.environ.get("APRS_API_KEY", "").strip()
OUT_FILE  = Path(os.environ.get("OUT_FILE", "host-tracker.json"))

TRAIL_HOURS = 24        # keep this many hours of trail
MAX_POINTS  = 800       # hard cap per host (safety against runaway growth)

# Exact APRS identifiers to track, INCLUDING SSID.
# People beacon per SSID: home = base call, mobile = -9, HT = -7, etc.
# Pick the one you want to follow (usually the mobile -9 for a movement map).
# These MUST match the `call` values in the HTML HOSTS config.
ROSTER = [
    "K8JKU-9",     # James  (replace SSID if needed)
    "N8JRD-1",   # Jim    <-- replace with Jim's real callsign-SSID
    "W8KNX-9",  # Rory   <-- replace with Rory's real callsign-SSID
]

APRS_URL   = "https://api.aprs.fi/api/get"
USER_AGENT = "EverydayHam-HostTracker/1.0 (github actions)"


# ------------------------------------------------------------------ helpers
def to_float(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def fetch_positions(calls):
    """One batched query for the whole roster (aprs.fi allows up to 20)."""
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


def fetch_with_retry(calls, tries=3):
    """Simple exponential backoff, per aprs.fi guidance."""
    delay = 2
    for attempt in range(tries):
        try:
            return fetch_positions(calls)
        except Exception as exc:  # noqa: BLE001
            if attempt == tries - 1:
                raise
            print(f"fetch attempt {attempt + 1} failed: {exc}; retrying in {delay}s",
                  file=sys.stderr)
            time.sleep(delay)
            delay *= 2
    return []


def load_existing():
    if OUT_FILE.exists():
        try:
            return json.loads(OUT_FILE.read_text())
        except Exception:  # noqa: BLE001
            print("existing file unreadable; starting fresh", file=sys.stderr)
    return {"generated": 0, "hosts": []}


# ------------------------------------------------------------------ main
def main():
    if not API_KEY:
        print("APRS_API_KEY is not set", file=sys.stderr)
        sys.exit(1)

    doc = load_existing()
    hosts = {h.get("callsign"): h for h in doc.get("hosts", [])}
    for call in ROSTER:
        hosts.setdefault(call, {"callsign": call, "track": [], "last_seen": 0})

    try:
        entries = fetch_with_retry(ROSTER)
    except Exception as exc:  # noqa: BLE001
        # Graceful: keep the existing file so the board shows last-known data.
        print(f"poll failed, leaving file untouched: {exc}", file=sys.stderr)
        sys.exit(0)

    now = int(time.time())
    by_call = {e.get("name", "").upper(): e for e in entries}

    appended = 0
    for call in ROSTER:
        host = hosts[call]
        host.setdefault("track", [])
        host.setdefault("last_seen", 0)

        entry = by_call.get(call.upper())
        if not entry:
            continue  # not heard recently; existing trail just ages out

        # `lasttime` = last time a packet was heard from this target.
        lasttime = int(to_float(entry.get("lasttime") or entry.get("time") or now))
        if lasttime <= host["last_seen"]:
            continue  # no new beacon since last poll -> no duplicate point

        lat = entry.get("lat")
        lng = entry.get("lng")
        if lat is None or lng is None:
            continue

        host["track"].append({
            "time":   lasttime,
            "lat":    round(to_float(lat), 6),
            "lng":    round(to_float(lng), 6),
            "speed":  round(to_float(entry.get("speed")), 1),  # km/h
            "course": int(to_float(entry.get("course"))),       # degrees
        })
        host["last_seen"] = lasttime
        appended += 1

    # prune to the trailing window + hard cap, preserve roster order
    cutoff = now - TRAIL_HOURS * 3600
    out_hosts = []
    for call in ROSTER:
        host = hosts.get(call, {"callsign": call, "track": [], "last_seen": 0})
        track = [p for p in host.get("track", []) if p["time"] >= cutoff]
        track.sort(key=lambda p: p["time"])
        if len(track) > MAX_POINTS:
            track = track[-MAX_POINTS:]
        out_hosts.append({
            "callsign":  call,
            "track":     track,
            "last_seen": host.get("last_seen", 0),
        })

    out = {"generated": now, "hosts": out_hosts}
    OUT_FILE.write_text(json.dumps(out, separators=(",", ":")))
    summary = ", ".join(f"{h['callsign']}:{len(h['track'])}" for h in out_hosts)
    print(f"wrote {OUT_FILE} ({appended} new) -> {summary}")


if __name__ == "__main__":
    main()
