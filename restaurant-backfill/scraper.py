"""Scrape Houston commercial food-establishment health inspections.

Source: https://houston-tx.healthinspections.us/media/search.cfm (Tyler Technologies
Environmental Health, ColdFusion). Protocol notes, verified empirically:

  * search.cfm takes a POST of its search form, and a *fresh* session per search is
    required -- reusing one returns an empty help page with HTTP 200.
  * Both the combined dates (sd/ed) and the split sd_month/sd_day/sd_year must be sent.
  * Results cap at 500 rows with no pagination, so a range that reaches the cap is cut
    in half and searched again.
  * The per-inspection detail page also needs a POST carrying the search form body.
  * Bursts get HTTP 503, so requests are serial with exponential backoff.
  * The results page never names the facility type, so each type is searched separately
    in order to label a row "Restaurant - Full Service" and so on.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = "https://houston-tx.healthinspections.us/media/"
SEARCH = BASE + "search.cfm"
UA = "Mozilla/5.0 (compatible; houston-inspection-monitor/1.0)"

# Every facility type the city publishes that has a commercial owner, i.e. someone who
# could be sold a service. Schools (028-038), hospitals (040), nursing homes (041) and
# the rest of the care/shelter/pantry codes are deliberately absent -- school cafeterias
# live in their own repo.
TYPES: dict[str, str] = {
    "001": "Restaurant - Full Service",
    "002": "Restaurant - Single Service",
    "004": "Pop Up Restaurant - Single Service",
    "060": "Bar - Restricted",
    "061": "Bar - Single Service",
    "062": "Bar - Full Service",
    "070": "Mobile - Conventional, Unrestricted, Motorized",
    "071": "Mobile - Conventional, Unrestricted, Non-motorized",
    "072": "Mobile - Conventional, Restricted, Motorized",
    "073": "Mobile - Conventional, Restricted, Non-motorized",
    "074": "Mobile - Ice Cream Only, Motorized",
    "075": "Mobile - Ice Cream Only, Non-motorized",
    "140": "Mobile - Park, Restricted",
    "141": "Mobile - Park, Unrestricted",
    "142": "Mobile - Fixed Location, Restricted",
    "143": "Mobile - Fixed Location, Unrestricted",
    "080": "Catering Establishment",
    "081": "Commissary - Mobile Food Unit",
    "090": "Retail Food Market with Meat Market",
    "091": "Retail Food Market - Multi Service",
    "092": "Retail Food Market - Seafood Only",
    "100": "Convenience Grocery - Packaged Food Only",
    "101": "Convenience Grocery - Open Food",
    "102": "Convenience Grocery - Self Service",
    "110": "Produce Establishment",
    "111": "Produce Peddler",
    "112": "Produce Certified Farmer's Market",
    "120": "Bakery - Retail",
    "121": "Bakery - Wholesale",
    "150": "Food Processing Plant",
    "151": "Warehouse - Retail TCS",
    "160": "Temporary Food Establishment - Open Food",
    "161": "Temporary Food Establishment - Packaged Food",
    "162": "Temporary Food Establishment - Community",
    "163": "Temporary Food Establishment - Combined",
    "182": "Salvage Store - Packaged Food Only",
    "183": "Salvage Store - Open Food",
    "003": "Bed and Breakfast",
    "190": "Other Food Establishment",
}

RESTAURANTS = ["001", "002", "004"]

# Rows the portal returns before it silently truncates.
MAXROWS = 500


@dataclass
class Inspection:
    facility_id: str
    inspection_id: str
    name: str
    address: str
    zipcode: str
    site: str
    date: str
    status: str
    facility_type: str
    violations: list[dict] = field(default_factory=list)


class Stopped(Exception):
    """Raised inside a crawl when the caller asks it to stop."""


def form_body(start: dt.date, end: dt.date,
              types: list[str]) -> list[tuple[str, str]]:
    sd = start.strftime("%m/%d/%Y")
    ed = end.strftime("%m/%d/%Y")
    sm, sdd, sy = sd.split("/")
    em, edd, ey = ed.split("/")
    body = [("q", "s"), ("e", ""), ("k", ""), ("r", "")]
    body += [("tp", t) for t in types]
    body += [
        ("sd_month", sm), ("sd_day", sdd), ("sd_year", sy), ("sd", sd),
        ("ed_month", em), ("ed_day", edd), ("ed_year", ey), ("ed", ed),
        ("z", "ALL"), ("m", "LIKE"), ("maxrows", str(MAXROWS)),
        ("Submit", "Search"),
    ]
    return body


def polite(fn, *args, stop: threading.Event | None = None, tries: int = 5, **kw):
    """The site throttles bursts with HTTP 503; back off and retry."""
    for attempt in range(tries):
        if stop is not None and stop.is_set():
            raise Stopped
        r = fn(*args, **kw)
        if r.status_code != 503:
            r.raise_for_status()
            return r
        time.sleep(5 * 2 ** attempt)
    raise RuntimeError("the site is still throttling after %d attempts" % tries)


def fresh_session(stop: threading.Event | None = None) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": SEARCH})
    polite(s.get, SEARCH, timeout=30, stop=stop)
    return s


ADDR_RE = re.compile(r"^(.*?)\s+(\d{5})\s*$")


def parse_results(html: str, facility_type: str) -> list[Inspection]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[Inspection] = []
    for a in soup.select('a[href*="q=d&"]'):
        f = re.search(r"[?&]f=([^&]+)", a["href"])
        i = re.search(r"[?&]i=([^&]+)", a["href"])
        if not (f and i):
            continue
        cells = a.find_parent("tr").find_all("td")
        raw = a.find_parent("td").get_text("\n", strip=True).split("\n")[-1]
        addr, zipcode = raw, ""
        mm = ADDR_RE.match(raw.replace(",", " ").strip())
        if mm:
            addr, zipcode = mm.group(1).strip(), mm.group(2)
        out.append(Inspection(
            facility_id=f.group(1),
            inspection_id=i.group(1),
            name=a.get_text(strip=True),
            address=addr,
            zipcode=zipcode,
            site=cells[1].get_text(strip=True),
            date=dt.datetime.strptime(cells[2].get_text(strip=True),
                                      "%m/%d/%Y").date().isoformat(),
            status=cells[3].get_text(strip=True),
            facility_type=facility_type,
        ))
    return out


def fetch_violations(s: requests.Session, insp: Inspection, start: dt.date,
                     end: dt.date, types: list[str],
                     stop: threading.Event | None = None) -> list[dict]:
    sd = start.strftime("%m/%d/%Y")
    ed = end.strftime("%m/%d/%Y")
    url = (f"{SEARCH}?q=d&f={insp.facility_id}&i={insp.inspection_id}"
           f"&sd={sd}&ed={ed}&z=ALL&m=LIKE&maxrows={MAXROWS}&e="
           f"&tp={','.join(types)}")
    body = [(k, v) for k, v in form_body(start, end, types)
            if k not in {"q", "Submit"}]
    body.insert(0, ("q", "d"))
    r = polite(s.post, url, data=body, timeout=60, stop=stop)
    soup = BeautifulSoup(r.text, "html.parser")
    panel = soup.select_one("td.ge_searchResultsPanel") or soup
    items: list[dict] = []
    activity = ""
    for tr in panel.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all("td")]
        if len(cells) == 4 and re.fullmatch(r"\d{2}/\d{2}/\d{4}", cells[0]):
            activity = cells[3]
        elif len(cells) == 3 and cells[0].isdigit():
            item = {"no": int(cells[0]), "item": cells[1],
                    "status": cells[2], "activity": activity}
            if not is_placeholder(item):
                items.append(item)
    return items


def is_placeholder(item: dict) -> bool:
    """Detail pages print one numbered line per checklist entry, so a clean visit still
    yields rows reading "Houston Ordinance Violation:" with no code and no status. Those
    are not findings and would otherwise make every inspection look like a violation."""
    return (not item["status"].strip()
            and not item["item"].split(":", 1)[-1].strip())


def is_grease_trap(site: str) -> bool:
    """Grease-trap rows are a plumbing record, not a food inspection. They show up either
    as "FOG <permit>" or as a bare permit number in the site column."""
    s = site.upper().strip()
    return s.startswith("FOG") or s.replace("-", "").isdigit()


def search_range(code: str, start: dt.date, end: dt.date,
                 stop: threading.Event | None = None) -> list[Inspection]:
    """Every inspection of one facility type between two dates, inclusive.

    A result that reaches MAXROWS was silently truncated, so the range is halved and
    searched again until each half fits.
    """
    s = fresh_session(stop)
    r = polite(s.post, SEARCH, data=form_body(start, end, [code]),
               timeout=90, stop=stop)
    rows = parse_results(r.text, TYPES.get(code, code))
    if len(rows) < MAXROWS or start == end:
        return rows
    mid = start + (end - start) // 2
    return (search_range(code, start, mid, stop)
            + search_range(code, mid + dt.timedelta(days=1), end, stop))


def crawl_range(start: dt.date, end: dt.date, types: list[str],
                details: bool = True, fog: bool = True,
                known: set[str] | None = None,
                stop: threading.Event | None = None) -> list[Inspection]:
    """A whole date range, one search per facility type.

    Searching a range instead of a day at a time is what keeps the request count down:
    ~40 types over a 14-day window costs 40 searches rather than 560.

    `known` holds inspection_ids whose violations are already on file; their detail page
    is not fetched again. Detail pages are the expensive half of the crawl and a filed
    inspection does not change.
    """
    out: list[Inspection] = []
    for code in types:
        rows = [i for i in search_range(code, start, end, stop)
                if fog or not is_grease_trap(i.site)]
        s: requests.Session | None = None
        for insp in rows:
            if stop is not None and stop.is_set():
                raise Stopped
            if details and (known is None or insp.inspection_id not in known):
                if s is None:
                    s = fresh_session(stop)
                insp.violations = fetch_violations(s, insp, start, end, [code], stop)
                # a detail POST invalidates the search session
                s = fresh_session(stop)
        out += rows
    return out


def as_dict(insp: Inspection) -> dict:
    return {
        "inspection_id": insp.inspection_id,
        "record_type": "grease_trap" if is_grease_trap(insp.site) else "inspection",
        "facility_id": insp.facility_id,
        "name": insp.name,
        "address": insp.address,
        "zip": insp.zipcode,
        "facility_type": insp.facility_type,
        "site": insp.site,
        "date": insp.date,
        "status": insp.status,
        "violations": insp.violations,
    }


def load_existing(path: Path) -> dict[str, dict]:
    """Existing inspections keyed by inspection_id, so re-scrapes merge, not duplicate."""
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    return {i["inspection_id"]: i for i in data.get("inspections", [])}


def write_json(path: Path, records: dict[str, dict]) -> list[dict]:
    rows = sorted(records.values(), key=lambda i: (i["date"], i["name"]), reverse=True)
    for row in rows:
        row["record_type"] = ("grease_trap" if is_grease_trap(row["site"])
                              else "inspection")
        row["violations"] = [v for v in row["violations"] if not is_placeholder(v)]
    food = [i for i in rows if i["record_type"] == "inspection"]
    path.write_text(json.dumps({
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "source": SEARCH,
        "inspection_count": len(food),
        "grease_trap_count": len(rows) - len(food),
        "record_count": len(rows),
        "facility_count": len({i["facility_id"] for i in food}),
        "violation_count": sum(len(i["violations"]) for i in rows),
        "inspections": rows,
    }, indent=2) + "\n")
    return rows


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "name", "facility_type", "record_type", "address", "zip",
                    "status", "violation_count", "violations"])
        for i in rows:
            w.writerow([i["date"], i["name"], i["facility_type"], i["record_type"],
                        i["address"], i["zip"], i["status"], len(i["violations"]),
                        " | ".join(f"{v['no']} {v['item']} ({v['status']})"
                                   for v in i["violations"])])
