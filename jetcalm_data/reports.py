"""The latest pilot turbulence reports, for the free web tracker.

usage: python -m jetcalm_data.reports [--out reports/latest.json] [--hours 3]

Pulls the last few hours of PIREPs and AIREPs from the NOAA Aviation Weather
Center Data API with the same polite fetcher as the verification
(`verify.fetch_reports`), keeps every report that carries a turbulence group
(smooth ones included) at any altitude, and writes one small JSON file:

    {"generated": "...Z", "source": "...", "window_hours": 3,
     "levels": ["smooth", "light", ...],
     "reports": [{"t": "...Z", "lat": 40.1, "lon": -80.2, "fl": 350,
                  "ac": "B738", "type": "PIREP", "level": "moderate"}, ...]}

Newest first. Aircraft type only (PIREPs; an AIREP's field is the call sign, so it is dropped):
no tail numbers, call signs or raw text.
The AWC API sends no CORS header, so browsers read this file from
raw.githubusercontent.com instead (workflow `reports`, hourly).
"""
import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import tiles, verify

SOURCE = "NOAA/NWS Aviation Weather Center pilot reports (aviationweather.gov/api/data/pirep), public domain"
FL_MIN, FL_MAX = 0, 600

# Reported intensity -> level shown on the tracker, calmest first. Finer than the
# verification's categories: MOD-SEV and EXTM keep their own level.
LEVELS = ["smooth", "light", "light-moderate", "moderate", "moderate-severe", "severe", "extreme"]
INTENSITY = {"NEG": "smooth", "SMTH": "smooth", "SMOOTH": "smooth", "NIL": "smooth",
             "SMTH-LGT": "light", "LGT": "light",
             "LGT-MOD": "light-moderate",
             "MOD": "moderate",
             "MOD-SEV": "moderate-severe",
             "SEV": "severe",
             "SEV-EXTM": "extreme", "EXTM": "extreme"}


# An ICAO aircraft type designator (B738, A20N, CRJ9, C172). AIREPs put the flight's call sign
# (UAL877, BAW88C) in the same field, so for them, and for anything else, the type is left out.
AC_TYPE = re.compile(r"^[A-Z][A-Z0-9]{1,3}$")


def aircraft_type(r):
    ac = r["ac"].upper()
    return ac if r["type"] == "PIREP" and AC_TYPE.match(ac) else ""


def level(d):
    """The worst turbulence level in an API record, or None."""
    found = [INTENSITY.get((d.get(k) or "").strip().upper()) for k in ("tbInt1", "tbInt2")]
    found = [x for x in found if x]
    return max(found, key=LEVELS.index) if found else None


def compact(raw):
    """API records -> (tracker reports newest first, counts of dropped records by reason)."""
    out, dropped, seen = [], {}, set()
    for d in raw:
        r, why = verify.parse_report(d, FL_MIN, FL_MAX)
        lv = level(d) if r else None
        if r and lv is None:
            why = "no turbulence"
        if why:
            dropped[why] = dropped.get(why, 0) + 1
            continue
        if r["key"] in seen:
            continue
        seen.add(r["key"])
        out.append({"t": r["t"], "lat": round(r["lat"], 2), "lon": round(r["lon"], 2), "fl": r["fl"],
                    "ac": aircraft_type(r), "type": r["type"], "level": lv})
    out.sort(key=lambda r: (r["t"], r["lat"], r["lon"]), reverse=True)
    return out, dropped


def build(raw, now, hours):
    reports, dropped = compact(raw)
    doc = {"generated": tiles.iso(now), "source": SOURCE, "window_hours": hours,
           "levels": LEVELS, "reports": reports}
    return doc, dropped


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="reports/latest.json")
    ap.add_argument("--hours", type=float, default=3.0)
    a = ap.parse_args(argv)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    raw = verify.fetch_reports(now - timedelta(hours=a.hours), now)
    doc, dropped = build(raw, now, a.hours if a.hours % 1 else int(a.hours))
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, separators=(",", ":")) + "\n")
    counts = {lv: sum(r["level"] == lv for r in doc["reports"]) for lv in LEVELS}
    print(f"{len(raw)} records -> {len(doc['reports'])} turbulence reports {counts}; dropped {dropped}")
    if not doc["reports"]:
        print("no reports in the window")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
