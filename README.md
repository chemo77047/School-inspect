# School Cafeteria Health Inspections — Houston

Public health-inspection records for Houston school cafeterias (Elementary,
Middle/Jr. High, High School), refreshed automatically and published as a static JSON
file for a dashboard to consume.

**Data URL for the dashboard**

```
https://raw.githubusercontent.com/chemo77047/School-inspect/main/inspections.json
```

`inspections.csv` holds the same records flattened, one row per inspection.

## How it updates

`.github/workflows/scrape.yml` runs at 11:00 and 17:00 America/Chicago and commits the
result when anything changed. GitHub often starts scheduled runs late — by minutes,
occasionally by hours — so treat those times as "twice a day", not as a guarantee.

Each run re-scrapes the **last 14 days**, not just today, because the health department
enters inspections several days late. Records are keyed on
`inspection_id`, so re-scraping the same day merges instead of duplicating, and a late
entry is picked up on the next run.

Run it by hand from the Actions tab ("Scrape inspections" → Run workflow); the `days`
input widens the window for a catch-up run.

## Files

| file | what it is |
| --- | --- |
| `inspections.json` | the dataset the dashboard reads |
| `inspections.csv` | same records, flattened for Excel |
| `scraper.py` | scraping and file-writing logic |
| `update_inspections.py` | what the schedule runs; also does backfills |

## Running locally

```bash
pip install -r requirements.txt
python update_inspections.py                                   # last 14 days
python update_inspections.py --days 30
python update_inspections.py --start 2025-07-01 --end 2026-06-30  # backfill
```

## JSON shape

```json
{
  "generated_at": "2026-08-21T13:54:02+00:00",
  "source": "https://houston-tx.healthinspections.us/media/search.cfm",
  "inspection_count": 1591,
  "grease_trap_count": 813,
  "record_count": 2404,
  "campus_count": 468,
  "violation_count": 2162,
  "inspections": [
    {
      "inspection_id": "070B35A1-0166-788E-5B36C8EB3A4C2589",
      "record_type": "inspection",
      "campus": "WHARTON ELEMENTARY SCHOOL",
      "level": "Elementary",
      "address": "900 W GRAY ST HOUSTON TX",
      "zip": "77019",
      "site": "ESTABLISHMENT",
      "date": "2026-08-19",
      "status": "OPEN",
      "violations": [
        {
          "no": 1,
          "item": "Houston Ordinance Violation: 8-304.11(K)",
          "status": "Violation",
          "activity": "Routine Inspection (001)"
        }
      ]
    }
  ]
}
```

`record_type` is `"inspection"` for kitchen inspections and `"grease_trap"` for FOG
(fats/oils/grease) records. Both can carry real citations, but a grease trap is plumbing
compliance, not food handling — keep them apart in any dashboard, or every campus looks
like it has twice as many findings as it does.

`violations` is empty when the visit found nothing, which is the common case: 715 of the
1,591 kitchen inspections and 739 of the 813 grease-trap records are clean. The detail
page prints a numbered line for every checklist entry, including a bare
`"Houston Ordinance Violation:"` with no code and no status when the entry passed, so
those lines are dropped on parse — every item left in `violations` is a real finding with
a `status` of `Violation`, `Violation Repeat`, `Violation Corrected On Site`, or
`Violation Corrected On Site Repeat`.

## Notes on the source

* The city publishes no numeric score or letter grade; severity would have to come from
  a mapping of the ordinance codes in `violations[].item`.
* Search results cap at 500 rows with no pagination, so the scraper queries one calendar
  day at a time.
* The site returns HTTP 503 on bursts; requests are serial with exponential backoff, and
  a day that fails three times is skipped and picked up by the next scheduled run.
