#!/usr/bin/env python3
"""
One-time backfill of history/ from the git log.

Before there was an archive, host-tracker.json was the only record, and it only
ever holds the last TRAIL_HOURS. But the poller commits on every change, so
every revision of that file is a snapshot of the trailing 72 hours, and the
union of all of them is the complete position history. This walks them and
feeds the points to the same append_records the poller uses, so the result is
byte-identical to what a live poll would have written.

Safe to re-run: records are deduped on (call, time), so a second pass adds
nothing.

Needs the FULL history, not the shallow clone CI checks out by default:

    git fetch --unshallow          # or: actions/checkout with fetch-depth: 0
    python backfill_history.py

Options:
  --ref REF     which ref to walk (default: main)
  --dry-run     report what would be written, touch nothing
"""

import json
import subprocess
import sys
from collections import defaultdict

from aprs_poller import (ARCHIVE_DIR, HOSTS, append_records, month_of,
                         rebuild_index)

TRACKED = {h["id"] for h in HOSTS}


def resolve(call):
    """Map a host entry back to the person it belongs to.

    The first couple of days keyed hosts by SSID ("W8KNX-9") rather than by
    person ("W8KNX"), before the merge-the-radios rewrite. The prefix says
    unambiguously whose radio it was, so those points are recoverable; the
    entry's own key becomes the ssid, which the old schema did not record.
    Anything else -- the JIMCALL/RORYCALL placeholders from the template --
    is not one of ours and is dropped.

    Returns (person, ssid_or_None), or (None, None) to skip.
    """
    if call in TRACKED:
        return call, None
    base = call.rsplit("-", 1)[0]
    if "-" in call and base in TRACKED:
        return base, call
    return None, None


def commits(ref):
    out = subprocess.run(["git", "rev-list", "--reverse", ref],
                         capture_output=True, text=True, check=True)
    return out.stdout.split()


def snapshots(shas, path="host-tracker.json"):
    """Stream every revision of `path` through one cat-file process.

    `git show` per commit would be 23k process spawns; --batch is one, which
    turns a multi-minute walk into a few seconds.
    """
    proc = subprocess.Popen(["git", "cat-file", "--batch"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    missing = unparsed = 0
    try:
        for sha in shas:
            proc.stdin.write(f"{sha}:{path}\n".encode())
            proc.stdin.flush()
            header = proc.stdout.readline().decode().split()
            if len(header) < 3:          # "<sha> missing" -- file not in that commit
                missing += 1
                continue
            body = proc.stdout.read(int(header[2]))
            proc.stdout.read(1)          # trailing newline cat-file adds
            try:
                yield json.loads(body)
            except Exception:            # noqa: BLE001
                unparsed += 1
    finally:
        proc.stdin.close()
        proc.wait()
    print(f"  {missing} commits without the file, {unparsed} unparseable",
          file=sys.stderr)


def main():
    ref = "main"
    if "--ref" in sys.argv:
        ref = sys.argv[sys.argv.index("--ref") + 1]
    dry = "--dry-run" in sys.argv

    shas = commits(ref)
    print(f"walking {len(shas)} commits on {ref}", file=sys.stderr)

    points = {}                      # (call, time) -> record
    skipped = defaultdict(int)       # callsigns from older schema versions
    for doc in snapshots(shas):
        for host in doc.get("hosts", []):
            person, legacy_ssid = resolve(host.get("callsign") or "")
            if not person:
                if host.get("callsign"):
                    skipped[host["callsign"]] += 1
                continue
            for pt in host.get("track", []):
                ts = pt.get("time")
                if not ts:
                    continue
                rec = {"call": person}
                rec.update(pt)
                if legacy_ssid and not rec.get("ssid"):
                    rec["ssid"] = legacy_ssid
                # A person-keyed entry always wins over a legacy SSID one for
                # the same instant, since it is the schema we kept.
                if legacy_ssid and (person, ts) in points:
                    continue
                points[(person, ts)] = rec

    if skipped:
        print(f"  ignored non-roster callsigns: {dict(skipped)}", file=sys.stderr)

    per_host, per_month = defaultdict(int), defaultdict(int)
    for (call, ts) in points:
        per_host[call] += 1
        per_month[month_of(ts)] += 1
    print(f"\nrecovered {len(points)} unique points")
    for call, n in sorted(per_host.items()):
        print(f"  {call:8} {n:6}")
    print("  " + ", ".join(f"{m}:{n}" for m, n in sorted(per_month.items())))

    if dry:
        print("\n--dry-run: nothing written")
        return

    written = append_records(list(points.values()))
    index = rebuild_index()
    print(f"\nwrote {written} new records into {ARCHIVE_DIR}/")
    if index:
        tot = index["totals"]
        print(f"archive now holds {tot['points']} points "
              f"across {len(index['months'])} month(s)")


if __name__ == "__main__":
    main()
