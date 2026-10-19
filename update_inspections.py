"""Refresh inspections.json -- what the GitHub Actions schedule runs.

Re-scrapes a rolling window (default: the last 14 days) because the health department
enters inspections several days late. Records merge on inspection_id, so a day can be
scraped any number of times without creating duplicates, and a late entry still lands.

    python update_inspections.py                 # last 14 days
    python update_inspections.py --days 30
    python update_inspections.py --start 2025-07-01 --end 2025-12-31   # backfill
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

from scraper import LEVELS, as_dict, crawl_day, load_existing, write_csv, write_json

HERE = Path(__file__).resolve().parent


def build_latest(records: dict, out: Path) -> list[dict]:
    """Write latest.json: one bundled entry per school's most recent inspection
    DAY. The portal logs a separate record per unit/activity on the same day
    (kitchen + establishment + reinspection...), and ~1/3 of all records have a
    same-day sibling — often a clean one masking a violations one. Bundling
    here means the site never picks the wrong "latest"."""
    days: dict[tuple, list] = {}
    for r in records.values():
        if r.get("record_type") != "inspection" or not r.get("date"):
            continue
        days.setdefault((r["facility_id"], r["date"]), []).append(r)

    latest: dict[str, tuple] = {}
    for (fid, date), recs in days.items():
        if fid not in latest or date > latest[fid][0]:
            latest[fid] = (date, recs)

    out_rows = []
    for fid, (date, recs) in latest.items():
        rep = max(recs, key=lambda r: (len(r.get("violations") or []), r["inspection_id"]))
        out_rows.append({
            "facility_id": fid,
            "campus": rep.get("campus"),
            "address": rep.get("address"),
            "zip": rep.get("zip"),
            "site": rep.get("site"),
            "level": rep.get("level"),
            "date": date,
            "status": rep.get("status"),
            "violation_count": sum(len(x.get("violations") or []) for x in recs),
            "inspections_that_day": len(recs),
            "inspection_id": rep["inspection_id"],
        })
    out_rows.sort(key=lambda r: (r["date"] or "", r["campus"] or ""), reverse=True)

    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "source": "latest day per facility, bundled from inspections.json",
        "record_count": len(out_rows),
        "records": out_rows,
    }
    out.write_text(json.dumps(payload, indent=1) + "\n")
    return out_rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", type=Path, default=HERE / "inspections.json")
    ap.add_argument("--csv", type=Path, default=HERE / "inspections.csv")
    ap.add_argument("--latest", type=Path, default=HERE / "latest.json")
    ap.add_argument("--days", type=int, default=14,
                    help="size of the rolling re-scrape window (default 14)")
    ap.add_argument("--start", type=dt.date.fromisoformat)
    ap.add_argument("--end", type=dt.date.fromisoformat, default=dt.date.today())
    ap.add_argument("--types", default=",".join(LEVELS))
    ap.add_argument("--no-details", action="store_true",
                    help="skip the per-inspection violation list")
    ap.add_argument("--no-fog", action="store_true",
                    help="drop grease-trap (FOG) records")
    ap.add_argument("--sleep", type=float, default=0.8)
    a = ap.parse_args()

    start = a.start or a.end - dt.timedelta(days=a.days - 1)
    types = a.types.split(",")
    unknown = set(types) - set(LEVELS)
    if unknown:
        ap.error(f"unknown level code(s): {', '.join(sorted(unknown))}")

    records = load_existing(a.json)
    before = len(records)
    print(f"{before} inspections on file; scraping {start} to {a.end}", flush=True)

    failed: list[dt.date] = []
    day = start
    while day <= a.end:
        rows = []
        for attempt in range(3):
            try:
                rows = crawl_day(day, types, not a.no_details, not a.no_fog)
                break
            except Exception as exc:  # a bad day must not abort the whole window
                if attempt == 2:
                    failed.append(day)
                    print(f"  {day}: giving up after 3 tries ({exc})", flush=True)
                else:
                    print(f"  {day}: {exc}; retrying", flush=True)
                    time.sleep(10 * (attempt + 1))
        new = sum(1 for i in rows if i.inspection_id not in records)
        for insp in rows:
            records[insp.inspection_id] = as_dict(insp)
        print(f"  {day}: {len(rows)} found, {new} new", flush=True)
        day += dt.timedelta(days=1)
        time.sleep(a.sleep)

    rows = write_json(a.json, records)
    write_csv(a.csv, rows)
    latest_rows = build_latest(records, a.latest)
    print(f"{len(records)} inspections total ({len(records) - before} added), "
          f"{len(latest_rows)} latest-day rows")
    if failed:
        print("failed days:", ", ".join(d.isoformat() for d in failed))
        # the next scheduled run covers the same window, so this is not fatal
    return 0


if __name__ == "__main__":
    sys.exit(main())
