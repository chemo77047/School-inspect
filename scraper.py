"""Scrape Houston school cafeteria health inspections. No GUI, no dependencies on tk.

Source: https://houston-tx.healthinspections.us/media/search.cfm (Tyler Technologies
Environmental Health, ColdFusion). Protocol notes, verified empirically:

  * search.cfm takes a POST of its search form, and a *fresh* session per search is
    required -- reusing one returns an empty help page with HTTP 200.
  * Both the combined dates (sd/ed) and the split sd_month/sd_day/sd_year must be sent.
  * Results cap at 500 rows with no pagination, so the crawl runs one day at a time.
  * The per-inspection detail page also needs a POST carrying the search form body.
  * Bursts get HTTP 503, so requests are serial with exponential backoff.
  * The results page never names the facility type, so each level is searched
    separately in order to label a row Elementary / Middle / High.
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
UA = "Mozilla/5.0 (compatible; HISD-inspection-monitor/1.0)"

LEVELS = {
    "030": "Elementary",
    "031": "Middle / Jr. High",
    "032": "High School",
}


@dataclass
class Inspection:
    facility_id: str
    inspection_id: str
    campus: str
    address: str
    zipcode: str
    site: str
    date: str
    status: str
    level: str
    violations: list[dict] = field(default_factory=list)


class Stopped(Exception):
    """Raised inside a crawl when the caller asks it to stop."""


def form_body(day: dt.date, types: list[str]) -> list[tuple[str, str]]:
    d = day.strftime("%m/%d/%Y")
    m, dd, y = d.split("/")
    body = [("q", "s"), ("e", ""), ("k", ""), ("r", "")]
    body += [("tp", t) for t in types]
    body += [
        ("sd_month", m), ("sd_day", dd), ("sd_year", y), ("sd", d),
        ("ed_month", m), ("ed_day", dd), ("ed_year", y), ("ed", d),
        ("z", "ALL"), ("m", "LIKE"), ("maxrows", "500"), ("Submit", "Search"),
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


def parse_results(html: str) -> list[Inspection]:
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
            campus=a.get_text(strip=True),
            address=addr,
            zipcode=zipcode,
            site=cells[1].get_text(strip=True),
            date=dt.datetime.strptime(cells[2].get_text(strip=True),
                                      "%m/%d/%Y").date().isoformat(),
            status=cells[3].get_text(strip=True),
            level="",
        ))
    return out


def fetch_violations(s: requests.Session, insp: Inspection, day: dt.date,
                     types: list[str], stop: threading.Event | None = None) -> list[dict]:
    d = day.strftime("%m/%d/%Y")
    url = (f"{SEARCH}?q=d&f={insp.facility_id}&i={insp.inspection_id}"
           f"&sd={d}&ed={d}&z=ALL&m=LIKE&maxrows=500&e=&tp={','.join(types)}")
    body = [(k, v) for k, v in form_body(day, types) if k not in {"q", "Submit"}]
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


def is_fog(insp: Inspection) -> bool:
    """FOG rows are grease-trap records rather than kitchen inspections."""
    return insp.site.upper().startswith("FOG")


def crawl_day(day: dt.date, types: list[str], details: bool = True, fog: bool = True,
              stop: threading.Event | None = None) -> list[Inspection]:
    out: list[Inspection] = []
    for code in types:
        s = fresh_session(stop)
        r = polite(s.post, SEARCH, data=form_body(day, [code]), timeout=60, stop=stop)
        rows = [i for i in parse_results(r.text) if fog or not is_fog(i)]
        for insp in rows:
            if stop is not None and stop.is_set():
                raise Stopped
            insp.level = LEVELS[code]
            if details:
                insp.violations = fetch_violations(s, insp, day, [code], stop)
                # a detail POST invalidates the search session
                s = fresh_session(stop)
        out += rows
    return out


def as_dict(insp: Inspection) -> dict:
    return {
        "inspection_id": insp.inspection_id,
        "record_type": "grease_trap" if is_fog(insp) else "inspection",
        "facility_id": insp.facility_id,
        "campus": insp.campus,
        "address": insp.address,
        "zip": insp.zipcode,
        "site": insp.site,
        "date": insp.date,
        "status": insp.status,
        "level": insp.level,
        "violations": insp.violations,
    }


def load_existing(path: Path) -> dict[str, dict]:
    """Existing inspections keyed by inspection_id, so re-scrapes merge, not duplicate."""
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    return {i["inspection_id"]: i for i in data.get("inspections", [])}


def write_json(path: Path, records: dict[str, dict]) -> list[dict]:
    rows = sorted(records.values(), key=lambda i: (i["date"], i["campus"]), reverse=True)
    for row in rows:
        # both stamped on every write, so records saved by an older version of this
        # scraper are corrected in place rather than needing a re-scrape
        row["record_type"] = ("grease_trap" if row["site"].upper().startswith("FOG")
                              else "inspection")
        row["violations"] = [v for v in row["violations"] if not is_placeholder(v)]
    kitchen = [i for i in rows if i["record_type"] == "inspection"]
    path.write_text(json.dumps({
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "source": SEARCH,
        "inspection_count": len(kitchen),
        "grease_trap_count": len(rows) - len(kitchen),
        "record_count": len(rows),
        "campus_count": len({i["campus"] for i in kitchen}),
        "violation_count": sum(len(i["violations"]) for i in rows),
        "inspections": rows,
    }, indent=2) + "\n")
    return rows


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "campus", "level", "record_type", "address", "zip",
                    "status", "violation_count", "violations"])
        for i in rows:
            w.writerow([i["date"], i["campus"], i["level"], i["record_type"],
                        i["address"], i["zip"], i["status"], len(i["violations"]),
                        " | ".join(f"{v['no']} {v['item']} ({v['status']})"
                                   for v in i["violations"])])
