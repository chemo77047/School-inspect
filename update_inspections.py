"""Refresh inspections.json -- what the GitHub Actions schedule runs.

Re-scrapes a rolling window (default: the last 10 days) because the health department
enters inspections several days late. Records merge on inspection_id, so a day can be
scraped any number of times without creating duplicates, and a late entry still lands.

    python update_inspections.py                 # last 10 days
    python update_inspections.py --days 30
    python update_inspections.py --start 2025-07-01 --end 2025-12-31   # backfill
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from pathlib import Path

from scraper import LEVELS, as_dict, crawl_day, load_existing, write_csv, write_json

HERE = Path(__file__).resolve().parent


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", type=Path, default=HERE / "inspections.json")
    ap.add_argument("--csv", type=Path, default=HERE / "inspections.csv")
    ap.add_argument("--days", type=int, default=10,
                    help="size of the rolling re-scrape window (default 10)")
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
    print(f"{len(records)} inspections total ({len(records) - before} added)")
    if failed:
        print("failed days:", ", ".join(d.isoformat() for d in failed))
        # the next scheduled run covers the same window, so this is not fatal
    return 0


if __name__ == "__main__":
    sys.exit(main())
