"""Combine the per-slice JSON files produced by the parallel backfill jobs.

Each slice covers its own date range, so collisions are rare, but a record scraped
twice (an inspection re-entered under a later date) merges on inspection_id with the
richer violation list winning.

    python merge_slices.py slices/*/inspections.json -o inspections.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from scraper import write_csv, write_json


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("slices", nargs="+", type=Path)
    ap.add_argument("-o", "--out", type=Path, default=Path("inspections.json"))
    ap.add_argument("--csv", type=Path, default=Path("inspections.csv"))
    a = ap.parse_args()

    records: dict[str, dict] = {}
    for path in a.slices:
        rows = json.loads(path.read_text()).get("inspections", [])
        for row in rows:
            old = records.get(row["inspection_id"])
            if old is None or len(row.get("violations") or []) >= len(
                    old.get("violations") or []):
                records[row["inspection_id"]] = row
        print(f"{path}: {len(rows)} records -> {len(records)} unique", flush=True)

    write_csv(a.csv, write_json(a.out, records))
    print(f"{len(records)} records written to {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
