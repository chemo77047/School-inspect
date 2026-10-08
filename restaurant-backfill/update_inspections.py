"""Refresh inspections.json -- what the GitHub Actions schedule runs.

Re-scrapes a rolling window (default: the last 14 days) because the health department
enters inspections several days late. Records merge on inspection_id, so a day can be
scraped any number of times without creating duplicates, and a late entry still lands.
Violation details are only fetched for inspections not already on file.

    python update_inspections.py                  # last 14 days, all commercial types
    python update_inspections.py --days 30
    python update_inspections.py --types 001,002,004                   # restaurants only
    python update_inspections.py --start 2025-07-01 --end 2025-12-31   # backfill
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from pathlib import Path

from scraper import (TYPES, as_dict, crawl_range, load_existing, write_csv,
                     write_json)

HERE = Path(__file__).resolve().parent


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", type=Path, default=HERE / "inspections.json")
    ap.add_argument("--csv", type=Path, default=HERE / "inspections.csv")
    ap.add_argument("--days", type=int, default=14,
                    help="size of the rolling re-scrape window (default 14)")
    ap.add_argument("--start", type=dt.date.fromisoformat)
    ap.add_argument("--end", type=dt.date.fromisoformat, default=dt.date.today())
    ap.add_argument("--types", default=",".join(TYPES),
                    help="facility type codes (default: every commercial type)")
    ap.add_argument("--no-details", action="store_true",
                    help="skip the per-inspection violation list")
    ap.add_argument("--no-fog", action="store_true",
                    help="drop grease-trap (FOG) records")
    ap.add_argument("--refetch-details", action="store_true",
                    help="re-fetch violations for inspections already on file")
    ap.add_argument("--chunk", type=int, default=14,
                    help="days searched per request, and the unit of progress saved "
                         "to disk (default 14)")
    ap.add_argument("--sleep", type=float, default=0.8)
    a = ap.parse_args()

    start = a.start or a.end - dt.timedelta(days=a.days - 1)
    types = [t.strip() for t in a.types.split(",") if t.strip()]
    unknown = set(types) - set(TYPES)
    if unknown:
        ap.error(f"unknown facility type code(s): {', '.join(sorted(unknown))}")

    records = load_existing(a.json)
    before = len(records)
    print(f"{before} inspections on file; scraping {start} to {a.end} "
          f"across {len(types)} facility type(s)", flush=True)

    failed: list[str] = []
    day = start
    while day <= a.end:
        last = min(day + dt.timedelta(days=a.chunk - 1), a.end)
        span = f"{day} to {last}" if last != day else f"{day}"
        known = None if a.refetch_details else set(records)
        rows = []
        for attempt in range(3):
            try:
                rows = crawl_range(day, last, types, not a.no_details,
                                   not a.no_fog, known)
                break
            except Exception as exc:  # a bad chunk must not abort the whole window
                if attempt == 2:
                    failed.append(span)
                    print(f"  {span}: giving up after 3 tries ({exc})", flush=True)
                else:
                    print(f"  {span}: {exc}; retrying", flush=True)
                    time.sleep(10 * (attempt + 1))
        new = 0
        for insp in rows:
            old = records.get(insp.inspection_id)
            if old is None:
                new += 1
            elif not insp.violations:
                # detail page was skipped because this one is already on file
                insp.violations = old.get("violations", [])
            records[insp.inspection_id] = as_dict(insp)
        print(f"  {span}: {len(rows)} found, {new} new", flush=True)
        # written every chunk rather than once at the end, so a run that is killed on a
        # timeout still leaves the days it did finish on disk
        write_csv(a.csv, write_json(a.json, records))
        day = last + dt.timedelta(days=1)
        time.sleep(a.sleep)

    rows = write_json(a.json, records)
    write_csv(a.csv, rows)
    print(f"{len(records)} inspections total ({len(records) - before} added)")
    if failed:
        print("failed ranges:", "; ".join(failed))
        # the next scheduled run covers the same window, so this is not fatal
    return 0


if __name__ == "__main__":
    sys.exit(main())
