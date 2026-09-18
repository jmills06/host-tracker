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

host-tracker.json only ever holds the last TRAIL_HOURS, because that is all
the board draws. Everything older used to survive purely as an accident of the
git history -- every poll is a commit, so the repo happened to be an archive
nobody planned. That works until somebody squashes, and the board itself
cannot read it: it is a static page that can fetch files, not revisions. So
alongside the live window we also append every new beacon to history/, which
is the durable copy.

Env:
  APRS_API_KEY  (required)  your aprs.fi API key -> repo secret
  OUT_FILE      (optional)  output path, default host-tracker.json
  ARCHIVE_DIR   (optional)  append-only history, default history/
  PENDING_FILE  (optional)  this run's new points, default /tmp/aprs-pending.json

Usage:
  aprs_poller.py                   poll aprs.fi, rewrite the window, append history
  aprs_poller.py --append-pending   re-apply PENDING_FILE to history/ only (no network)

The second form exists for the commit retry loop in the workflow. Pushing races
another runner, so the loop resets onto the latest origin/main and rebuilds the
commit; the live window is a snapshot and can just be copied back over, but the
archive is append-only and a blind copy would drop whatever the other runner
appended. Re-running the append instead merges: it is deduped on (call, time),
so repeating it is free and nothing is ever clobbered.
"""

import datetime
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

API_KEY  = os.environ.get("APRS_API_KEY", "").strip()
OUT_FILE = Path(os.environ.get("OUT_FILE", "host-tracker.json"))
ARCHIVE_DIR  = Path(os.environ.get("ARCHIVE_DIR", "history"))
PENDING_FILE = Path(os.environ.get("PENDING_FILE", "/tmp/aprs-pending.json"))

# Fields aprs.fi hands back that the board has no use for but the archive keeps
# anyway, because they cost nothing now and cannot be recovered later. `path`
# is the one to care about: it names the digipeaters and igates that relayed
# the packet, which is the raw material for a coverage map. Every one of these
# is optional and simply left out of the record when the beacon did not carry it.
EXTRA_FIELDS = ("altitude", "path", "comment", "symbol")

TRAIL_HOURS = 72
MAX_POINTS  = 1500

# id    = the key the board matches on (use the base callsign).
# watch = the APRS SSIDs to follow and merge for that person (moving radios only).
HOSTS = [
    {"id": "K8JKU", "watch": ["K8JKU-9", "K8JKU-7", "K8JKU-3"]},            # James
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


# ---------------------------------------------------------------------------
# Append-only archive
#
# One NDJSON file per calendar month under ARCHIVE_DIR, plus an index.json
# naming what exists. A line per beacon, keyed by (call, time).
#
# NDJSON because it appends: a poll adds lines to the end and never rewrites
# what is already there, so a half-finished write can cost at worst the last
# line rather than the file. Split by month so the file being appended to stays
# small no matter how many years accumulate, and so anything reading it later
# can fetch a range instead of the lot.
#
# Lines are NOT globally sorted. Each host's own points go in in time order,
# but three hosts interleave, so sort by time when you read.
# ---------------------------------------------------------------------------

def month_of(ts):
    return datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m")


def month_file(mkey):
    return ARCHIVE_DIR / (mkey + ".ndjson")


def read_month(mkey):
    path = month_file(mkey)
    if not path.exists():
        return
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:  # noqa: BLE001
                print(f"skipping unparseable line in {path.name}", file=sys.stderr)


def archive_record(call, point, entry):
    """A track point plus whoever it belongs to and the fields the board drops."""
    rec = {"call": call}
    rec.update(point)
    for key in EXTRA_FIELDS:
        val = entry.get(key)
        if val is None or val == "":
            continue
        if key == "altitude":
            rec["alt"] = round(to_float(val), 1)
        else:
            rec[key] = str(val)[:300]
    return rec


def append_records(records):
    """Add records to their own month's file, skipping any already there.

    Filed by the point's own timestamp, not by today, so a late beacon lands in
    the month it happened and a run spanning midnight on the 1st writes both.
    """
    by_month = defaultdict(list)
    for rec in records:
        if rec.get("time") and rec.get("call"):
            by_month[month_of(rec["time"])].append(rec)

    written = 0
    for mkey, recs in sorted(by_month.items()):
        have = {(r.get("call"), r.get("time")) for r in read_month(mkey)}
        fresh = []
        for rec in sorted(recs, key=lambda r: (r["time"], r["call"])):
            key = (rec["call"], rec["time"])
            if key in have:
                continue
            have.add(key)
            fresh.append(json.dumps(rec, separators=(",", ":")))
        if not fresh:
            continue
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        path = month_file(mkey)
        # If a previous write was cut off mid-line, start on a clean one rather
        # than splicing this record onto the end of that one.
        if path.exists() and path.stat().st_size:
            with path.open("rb") as fh:
                fh.seek(-1, os.SEEK_END)
                needs_nl = fh.read(1) != b"\n"
        else:
            needs_nl = False
        with path.open("a", encoding="utf-8") as fh:
            fh.write(("\n" if needs_nl else "") + "\n".join(fresh) + "\n")
        written += len(fresh)
    return written


def rebuild_index():
    """Summarise every month file so a reader knows what exists without probing."""
    if not ARCHIVE_DIR.exists():
        return None
    months, totals = [], defaultdict(int)
    first = last = None
    for path in sorted(ARCHIVE_DIR.glob("*.ndjson")):
        mkey = path.stem
        per_host, count = defaultdict(int), 0
        lo = hi = None
        for rec in read_month(mkey):
            ts, call = rec.get("time"), rec.get("call")
            if not ts or not call:
                continue
            count += 1
            per_host[call] += 1
            totals[call] += 1
            lo = ts if lo is None else min(lo, ts)
            hi = ts if hi is None else max(hi, ts)
        if not count:
            continue
        months.append({"month": mkey, "points": count, "first": lo, "last": hi,
                       "hosts": dict(sorted(per_host.items()))})
        first = lo if first is None else min(first, lo)
        last = hi if last is None else max(last, hi)

    index = {
        "updated": int(time.time()),
        "format": "ndjson; one record per beacon; sort by time when reading",
        "months": months,
        "totals": {"points": sum(totals.values()),
                   "hosts": dict(sorted(totals.items())),
                   "first": first, "last": last},
    }
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    (ARCHIVE_DIR / "index.json").write_text(json.dumps(index, indent=1) + "\n")
    return index


def append_pending():
    """Re-apply this run's points onto whatever the tree now holds."""
    if not PENDING_FILE.exists():
        print("no pending file; nothing to append")
        return
    try:
        records = json.loads(PENDING_FILE.read_text())
    except Exception as exc:  # noqa: BLE001
        print(f"pending file unreadable: {exc}", file=sys.stderr)
        return
    written = append_records(records)
    rebuild_index()
    print(f"archive: {written} new of {len(records)} pending")


def load_existing():
    if OUT_FILE.exists():
        try:
            return json.loads(OUT_FILE.read_text())
        except Exception:  # noqa: BLE001
            print("existing file unreadable; starting fresh", file=sys.stderr)
    return {"generated": 0, "hosts": []}


def main():
    if "--append-pending" in sys.argv:
        append_pending()
        return

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
    pending = []          # the same new points, for the archive

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
                point = {
                    "time":   lasttime,
                    "lat":    round(to_float(lat), 6),
                    "lng":    round(to_float(lng), 6),
                    "speed":  round(to_float(entry.get("speed")), 1),
                    "course": int(to_float(entry.get("course"))),
                    "ssid":   ssid,
                }
                track.append(point)
                pending.append(archive_record(hid, point, entry))
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

    # Hand this run's points to the archive, and leave them somewhere the
    # workflow's retry loop can re-apply them after it resets onto origin/main.
    try:
        PENDING_FILE.write_text(json.dumps(pending, separators=(",", ":")))
    except Exception as exc:  # noqa: BLE001
        print(f"could not write pending file: {exc}", file=sys.stderr)
    written = append_records(pending)
    index = rebuild_index()
    if index:
        tot = index["totals"]
        print(f"archive: {written} new -> {tot['points']} points "
              f"across {len(index['months'])} month(s)")


if __name__ == "__main__":
    main()
