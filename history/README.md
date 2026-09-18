# Position archive

Every beacon the poller has ever seen, kept forever.

`host-tracker.json` in the repo root is the *live window*: the last 72 hours,
rewritten on every poll, because that is all the board draws. This directory is
the durable copy. Nothing in here is ever rewritten, only appended to.

Before this existed, the only record of anything older than 72 hours was the git
history itself, since every poll is a commit and every revision of
`host-tracker.json` is a snapshot of its trailing window. That worked by
accident, not design, and it had two problems: one `git gc` or squash and 
the whole record is gone, and the board can't read it anyway, being a static
page that fetches files rather than revisions.

## Layout

    history/
      index.json        what exists, with per-month and per-host counts
      2026-07.ndjson    one file per calendar month
      2026-08.ndjson
      ...

## Format

NDJSON: one JSON object per line, one line per beacon.

```json
{"call":"W8KNX","time":1789736971,"lat":46.411,"lng":-86.649,"speed":104.0,"course":271,"ssid":"W8KNX-9","alt":188.4,"path":"WIDE1-1,WIDE2-1,K8UM-3*,qAR,W8TVC","comment":"Rory mobile","symbol":"/>"}
```

| field | | |
|---|---|---|
| `call` | always | the person, not the radio (`W8KNX`) |
| `time` | always | unix seconds, from the beacon's `lasttime` |
| `lat` `lng` | always | degrees |
| `speed` | always | **km/h**, as aprs.fi reports it. The board converts for display |
| `course` | always | degrees true |
| `ssid` | always | which radio it came from (`W8KNX-9`) |
| `alt` | when sent | metres |
| `path` | when sent | the digipeaters and igates that relayed the packet |
| `comment` | when sent | beacon comment text |
| `symbol` | when sent | APRS symbol code |

Records are keyed on `(call, time)`, which is what makes appends idempotent.

Two things to know when reading:

- **Lines are not globally sorted.** Each person's points go in in time order,
  but the hosts interleave. Sort by `time` yourself.
- **Files are chosen by the point's own timestamp**, not by when it was written,
  so a late-arriving beacon lands in the month it actually happened.

## Gotchas

**The data is bursty, not continuous.** APRS only transmits when a radio is on,
so you get dense runs during a drive (a point every 6-7 minutes) separated by
hours or days of nothing. Treat a gap as "radio off", not "stationary". Anything
that assumes an even sample rate will be wrong.

**Coverage is very uneven between people**, so don't compare totals naively.
Over the first 78 days Rory was heard on 95% of days, Jim 63%, James 33%.

**`speed` is km/h.** The board multiplies by `KM_TO_MI` before showing mph, and
filters anything above `SPEED_SANITY_KMH` as a bad decode. Do the same.

## Rebuilding

`backfill_history.py` reconstructs the whole archive from the git history. It's
how this directory was first populated, and it's safe to re-run at any time
since appends are deduplicated.

```sh
git fetch --unshallow       # it needs the full log, not CI's shallow clone
python backfill_history.py --dry-run
python backfill_history.py
```
