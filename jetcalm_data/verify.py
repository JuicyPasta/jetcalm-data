"""Score the published forecast against pilot reports (PIREPs and AIREPs).

usage: python -m jetcalm_data.verify --site site/v1 [--out verification] [--doc docs/verification.md] [--hours 14]

1. Pull recent pilot reports from the NOAA Aviation Weather Center Data API
   (`/api/data/pirep`), politely: one request at a time, a pause between them,
   time windows small enough to stay under the API's 400-result cap.
2. Keep reports with a turbulence intensity and a flight level from FL130 to
   FL450, the levels the feed covers.
3. Match each one to the step file the pipeline published for that time (the
   nearest `valid` within 90 minutes; WAFS before outlook; then the shortest
   lead) and read the value the app would show: cruise (FL285 and up) reads
   `cruise_mid` at the cell for "expected" and the 3x3 maximum of `cruise_max`
   for "could reach"; FL130-284 reads `low_max` (the app's climb and descent).
4. Store every match in `verification/matches/YYYY-MM-DD.jsonl` (UTC day of the
   report) and rewrite `verification/latest.json` and the daily table in
   `docs/verification.md` from all stored matches.

The site keeps only the two newest builds (about 12 hours of past steps), so
this has to run at least every 12 hours to see every report; the matches it
stores are the history.
"""
import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from . import PROJECT_URL, tiles

API = "https://aviationweather.gov/api/data/pirep"
USER_AGENT = f"jetcalm-data verification (+{PROJECT_URL})"
API_CAP = 400                       # the API returns at most this many reports per request
CHUNK = timedelta(hours=3)
MIN_CHUNK = timedelta(minutes=10)
PAUSE_S = 1.5
# Turbulence-bearing reports anywhere, and every report (including "smooth") over North America,
# where almost all negative reports come from. Unfiltered global requests are flooded by AIREP
# position reports that carry no turbulence group.
QUERIES = [{"bbox": "-90,-180,90,180", "inten": "lgt"}, {"bbox": "15,-170,72,-50"}]

MAX_STEP_GAP = timedelta(minutes=90)
FL_LOW, FL_CRUISE, FL_TOP = 130, 285, 450

# Reported intensity -> category, calmest first.
REPORTED = ["none", "light", "light-moderate", "moderate", "severe"]
INTENSITY = {"NEG": "none", "SMTH": "none", "SMOOTH": "none", "NIL": "none",
             "SMTH-LGT": "light", "LGT": "light",
             "LGT-MOD": "light-moderate",
             "MOD": "moderate",
             "MOD-SEV": "severe", "SEV": "severe", "SEV-EXTM": "severe", "EXTM": "severe"}

LEVELS = ["calm", "light", "moderate", "strong", "severe"]
# Which reported categories count as "the forecast matched". "strong" (EDR 0.34-0.44) is
# severe for a medium aircraft on the GTG scale and moderate on the ICAO Annex 3 scale
# (docs/thresholds.md), so either report agrees with it.
AGREES = {"calm": {"none"}, "light": {"light", "light-moderate"},
          "moderate": {"light-moderate", "moderate"}, "strong": {"moderate", "severe"},
          "severe": {"severe"}}

STEP_DIR = re.compile(r"^(\d{10})(-cal\d{10})?$")
STEP_FILE = re.compile(r"^f(\d{3})\.bin\.gz$")


# --- Reports ----------------------------------------------------------------

def reported_category(d):
    """The worst of a report's turbulence intensities, or None if it has none we know."""
    cats = [INTENSITY.get((d.get(k) or "").strip().upper()) for k in ("tbInt1", "tbInt2")]
    cats = [c for c in cats if c]
    return max(cats, key=REPORTED.index) if cats else None


def parse_report(d, fl_min=FL_LOW, fl_max=FL_TOP):
    """One API record -> a compact report, or (None, reason). Scoring keeps FL130-450 (the
    feed's levels); the live tracker (reports.py) passes a wider range."""
    cat = reported_category(d)
    if cat is None:
        return None, "no turbulence"
    fl = d.get("fltLvl")
    try:
        fl = int(fl)
        lat, lon, t = float(d["lat"]), float(d["lon"]), int(d["obsTime"])
    except (TypeError, ValueError, KeyError):
        return None, "no position, time or level"
    if not fl_min <= fl <= fl_max:
        return None, f"below FL{fl_min:03d} or above FL{fl_max}"
    when = datetime.fromtimestamp(t, timezone.utc)
    raw = " ".join(i for i in ((d.get("tbInt1") or "").strip(), (d.get("tbInt2") or "").strip()) if i)
    ac = (d.get("acType") or "").strip()
    return {"t": tiles.iso(when), "lat": round(lat, 3), "lon": round(lon, 3), "fl": fl,
            "int": raw, "rep": cat, "ac": ac, "type": (d.get("pirepType") or "").strip(),
            "key": f"{t}|{lat:.2f}|{lon:.2f}|{fl}|{ac}"}, None


def _get_json(params, session, tries=3, pause=time.sleep):
    for attempt in range(tries):
        try:
            r = session.get(API, params=params, timeout=(10, 60))
            if r.status_code == 204:
                return []
            r.raise_for_status()
            return r.json() if r.content.strip() else []
        except Exception:
            if attempt == tries - 1:
                raise
            pause(10 * (attempt + 1))


def fetch_reports(start, end, session=None, pause=time.sleep, log=print):
    """All API records observed in [start, end), deduplicated."""
    if session is None:
        import requests
        session = requests.Session()
        session.headers["User-Agent"] = USER_AGENT
    out, n_requests = {}, 0

    def window(q, a, b):
        nonlocal n_requests
        if n_requests:
            pause(PAUSE_S)
        n_requests += 1
        params = dict(q, format="json", date=tiles.iso(b), age=round((b - a).total_seconds() / 3600, 3))
        rows = _get_json(params, session)
        if len(rows) >= API_CAP and b - a > MIN_CHUNK:
            mid = a + (b - a) / 2
            window(q, a, mid)
            window(q, mid, b)
            return
        if len(rows) >= API_CAP:
            log(f"  capped at {API_CAP}: {q} {tiles.iso(a)}..{tiles.iso(b)}")
        for d in rows:
            k = (d.get("obsTime"), d.get("lat"), d.get("lon"), d.get("fltLvl"), d.get("acType"), d.get("rawOb"))
            out[k] = d

    for q in QUERIES:
        a = start
        while a < end:
            b = min(end, a + CHUNK)
            window(q, a, b)
            a = b
    log(f"{len(out)} reports from {n_requests} requests")
    return [d for d in out.values() if start.timestamp() <= (d.get("obsTime") or 0) < end.timestamp()]


# --- Forecast steps -----------------------------------------------------------

def inventory(v1):
    """Every step file under a local v1 directory -> list of step dicts."""
    steps = []
    for d in sorted(Path(v1).iterdir()):
        m = STEP_DIR.match(d.name)
        if not d.is_dir() or not m:
            continue
        run = m.group(1)
        kind = "outlook" if m.group(2) else "wafs"
        for f in sorted(d.iterdir()):
            fm = STEP_FILE.match(f.name)
            if not fm:
                continue
            fh = int(fm.group(1))
            valid = datetime.strptime(run, "%Y%m%d%H").replace(tzinfo=timezone.utc) + timedelta(hours=fh)
            steps.append({"file": f"{d.name}/{f.name}", "path": f, "run": run, "lead": fh,
                          "valid": valid, "kind": kind,
                          "layers": tiles.LAYERS_WAFS if kind == "wafs" else tiles.LAYERS_OUTLOOK})
    return steps


def choose_step(steps, when):
    """The step for a moment: nearest valid within 90 min, WAFS first, then shortest lead."""
    near = [s for s in steps if abs(s["valid"] - when) <= MAX_STEP_GAP]
    if not near:
        return None
    return min(near, key=lambda s: (s["kind"] != "wafs", abs((s["valid"] - when).total_seconds()), s["lead"]))


def cell(lat, lon):
    if abs(lat) > 75.25:
        return None
    return min(300, max(0, round((75.0 - lat) / 0.5))), round((lon + 180.0) / 0.5) % 720


def value_at(grid, rc):
    v = int(grid[rc])
    return None if v == 255 else v


def max_around(grid, rc):
    r, c = rc
    rows = grid[max(0, r - 1):r + 2]
    vals = [int(rows[i, (c + dc) % 720]) for i in range(rows.shape[0]) for dc in (-1, 0, 1)]
    vals = [v for v in vals if v != 255]
    return max(vals) if vals else None


def read_forecast(layers, fl, rc):
    """(band, expected, could reach) the way the app reads them."""
    if fl >= FL_CRUISE:
        band, exp_layer, reach_layer = "cruise", "cruise_mid", "cruise_max"
    elif "low_max" in layers:
        band, exp_layer, reach_layer = "low", "low_max", "low_max"
    else:
        band, exp_layer, reach_layer = "low", "cruise_mid", "cruise_max"
    exp = value_at(layers[exp_layer], rc)
    reach = max_around(layers[reach_layer], rc)
    if exp is not None:
        reach = max(reach if reach is not None else exp, exp)
    return band, exp, reach


def match(reports, steps, load=None):
    """Reports -> (matches, counts of reports not matched by reason)."""
    cache = {}
    if load is None:
        def load(step):
            return tiles.decode(step["path"].read_bytes(), step["layers"])
    out, skipped = [], {}
    for r in reports:
        step = choose_step(steps, tiles.parse_iso(r["t"]))
        rc = cell(r["lat"], r["lon"])
        reason = None
        if step is None:
            reason = "no forecast step published for that time"
        elif rc is None:
            reason = "outside 75S..75N"
        else:
            if step["file"] not in cache:
                cache[step["file"]] = load(step)
            band, exp, reach = read_forecast(cache[step["file"]], r["fl"], rc)
            if exp is None:
                reason = "no data in cell"
            else:
                out.append(dict(r, step=step["file"], kind=step["kind"], lead=step["lead"],
                                band=band, exp=exp, reach=reach))
        if reason:
            skipped[reason] = skipped.get(reason, 0) + 1
    return out, skipped


# --- Scoring ----------------------------------------------------------------------

def level_of(v, th):
    name = "calm"
    for k in ("light", "moderate", "strong", "severe"):
        if v >= th[k]:
            name = k
    return name


def _rate(a, b):
    return round(a / b, 3) if b else None


def score(matches, th):
    """Verification stats for a list of matches."""
    conf = {lv: {rp: 0 for rp in REPORTED} for lv in LEVELS}
    conf_reach = {lv: {rp: 0 for rp in REPORTED} for lv in LEVELS}
    per = {rp: {"n": 0, "agree": 0} for rp in REPORTED}
    bands, kinds = {}, {}
    agree = within_reach = 0
    mog = {"reports": 0, "expected_moderate_or_more": 0, "could_reach_moderate_or_more": 0}
    any_turb = {"reports": 0, "expected_light_or_more": 0}
    none = {"reports": 0, "expected_calm": 0, "expected_light_or_less": 0}
    leads = []
    for m in matches:
        lv, lv_reach, rp = level_of(m["exp"], th), level_of(m["reach"], th), m["rep"]
        conf[lv][rp] += 1
        conf_reach[lv_reach][rp] += 1
        per[rp]["n"] += 1
        ok = rp in AGREES[lv]
        per[rp]["agree"] += ok
        agree += ok
        # "Within range": the report lies between the expected level and the could-reach level.
        lo, hi = LEVELS.index(lv), LEVELS.index(lv_reach)
        within_reach += any(rp in AGREES[LEVELS[i]] for i in range(lo, hi + 1))
        bands[m["band"]] = bands.get(m["band"], 0) + 1
        kinds[m["kind"]] = kinds.get(m["kind"], 0) + 1
        leads.append(m["lead"])
        if rp in ("moderate", "severe"):
            mog["reports"] += 1
            mog["expected_moderate_or_more"] += m["exp"] >= th["moderate"]
            mog["could_reach_moderate_or_more"] += m["reach"] >= th["moderate"]
        if rp != "none":
            any_turb["reports"] += 1
            any_turb["expected_light_or_more"] += m["exp"] >= th["light"]
        else:
            none["reports"] += 1
            none["expected_calm"] += m["exp"] < th["light"]
            none["expected_light_or_less"] += m["exp"] < th["moderate"]
    for rp in per:
        per[rp]["rate"] = _rate(per[rp]["agree"], per[rp]["n"])
    mog["hit_rate_expected"] = _rate(mog["expected_moderate_or_more"], mog["reports"])
    mog["hit_rate_could_reach"] = _rate(mog["could_reach_moderate_or_more"], mog["reports"])
    any_turb["hit_rate"] = _rate(any_turb["expected_light_or_more"], any_turb["reports"])
    none["calm_rate"] = _rate(none["expected_calm"], none["reports"])
    none["light_or_less_rate"] = _rate(none["expected_light_or_less"], none["reports"])
    return {"n": len(matches), "agree": agree, "agree_rate": _rate(agree, len(matches)),
            "within_range": within_reach, "within_range_rate": _rate(within_reach, len(matches)),
            "per_reported": per, "moderate_or_greater": mog, "any_turbulence": any_turb, "smooth_reports": none,
            "confusion_expected": conf, "confusion_could_reach": conf_reach,
            "by_band": bands, "by_kind": kinds,
            "median_lead_hours": float(np.median(leads)) if leads else None}


# --- Storage -------------------------------------------------------------------------

def load_matches(mdir):
    out = []
    for f in sorted(Path(mdir).glob("*.jsonl")):
        out += [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
    return out


def store_matches(mdir, new):
    """Add matches to the per-day files; a report already stored keeps its first match. Returns # added."""
    mdir = Path(mdir)
    mdir.mkdir(parents=True, exist_ok=True)
    by_day = {}
    for m in new:
        by_day.setdefault(m["t"][:10], []).append(m)
    added = 0
    for day, ms in by_day.items():
        f = mdir / f"{day}.jsonl"
        old = [json.loads(x) for x in f.read_text().splitlines() if x.strip()] if f.exists() else []
        keys = {m["key"] for m in old}
        fresh = [m for m in ms if m["key"] not in keys]
        if not fresh:
            continue
        added += len(fresh)
        rows = sorted(old + fresh, key=lambda m: (m["t"], m["key"]))
        f.write_text("".join(json.dumps(m, separators=(",", ":"), sort_keys=True) + "\n" for m in rows))
    return added


def summary(all_matches, th, now):
    def since(days):
        cut = tiles.iso(now - timedelta(days=days))
        return [m for m in all_matches if m["t"] >= cut]
    days = {}
    for m in all_matches:
        days.setdefault(m["t"][:10], []).append(m)
    return {
        "generated": tiles.iso(now),
        "source": "NOAA/NWS Aviation Weather Center pilot reports (aviationweather.gov/api/data/pirep)",
        "method": "docs/verification.md",
        "thresholds": th,
        "first_report": min((m["t"] for m in all_matches), default=None),
        "last_report": max((m["t"] for m in all_matches), default=None),
        "last_7_days": score(since(7), th),
        "last_30_days": score(since(30), th),
        "all": score(all_matches, th),
        "daily": {d: score(ms, th) for d, ms in sorted(days.items())[-60:]},
    }


DOC_START, DOC_END = "<!-- daily-table:start -->", "<!-- daily-table:end -->"


def _pct(x):
    return "–" if x is None else f"{100 * x:.0f}%"


def daily_table(summ):
    lines = ["| Day (UTC) | Reports matched | Forecast agreed | Within expected–could-reach | "
             "Moderate+ reports: forecast moderate+ (expected / could reach) | Smooth reports: forecast calm |",
             "|---|---|---|---|---|---|"]
    rows = list(summ["daily"].items()) + [("**last 30 days**", summ["last_30_days"])]
    for day, s in rows:
        mog, sm = s["moderate_or_greater"], s["smooth_reports"]
        lines.append(f"| {day} | {s['n']} | {_pct(s['agree_rate'])} | {_pct(s['within_range_rate'])} | "
                     f"{_pct(mog['hit_rate_expected'])} / {_pct(mog['hit_rate_could_reach'])} of {mog['reports']} | "
                     f"{_pct(sm['calm_rate'])} of {sm['reports']} |")
    return "\n".join(lines)


def update_doc(doc, summ):
    doc = Path(doc)
    text = doc.read_text()
    a, b = text.index(DOC_START) + len(DOC_START), text.index(DOC_END)
    doc.write_text(text[:a] + "\n" + daily_table(summ) + "\n" + text[b:])


# --- Main -----------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", required=True, help="local copy of the published v1 directory")
    ap.add_argument("--out", default="verification")
    ap.add_argument("--doc", default="docs/verification.md")
    ap.add_argument("--hours", type=float, default=14.0, help="how far back to pull reports")
    a = ap.parse_args(argv)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    v1 = Path(a.site)
    try:
        th = tiles.validate_manifest(json.loads((v1 / "manifest.json").read_text()))["thresholds"]
    except (OSError, ValueError, KeyError):
        th = tiles.THRESHOLDS
    steps = inventory(v1)
    if not steps:
        print("no step files under", v1)
        return 1
    print(f"{len(steps)} step files, valid {tiles.iso(min(s['valid'] for s in steps))} .. "
          f"{tiles.iso(max(s['valid'] for s in steps))}")
    raw = fetch_reports(now - timedelta(hours=a.hours), now)
    reports, dropped = [], {}
    for d in raw:
        r, why = parse_report(d)
        if r:
            reports.append(r)
        else:
            dropped[why] = dropped.get(why, 0) + 1
    matches, skipped = match(reports, steps)
    out = Path(a.out)
    added = store_matches(out / "matches", matches)
    print(f"{len(raw)} reports: {len(reports)} usable, dropped {dropped}; matched {len(matches)}, "
          f"not matched {skipped}; {added} new")
    summ = summary(load_matches(out / "matches"), th, now)
    summ["last_run"] = {"at": tiles.iso(now), "reports": len(raw), "usable": len(reports), "dropped": dropped,
                        "matched": len(matches), "not_matched": skipped, "new": added}
    (out / "latest.json").write_text(json.dumps(summ, indent=1, sort_keys=True) + "\n")
    if Path(a.doc).exists():
        update_doc(a.doc, summ)
    s = summ["last_30_days"]
    print(f"last 30 days: {s['n']} matched, agreed {_pct(s['agree_rate'])}, "
          f"moderate+ hit {_pct(s['moderate_or_greater']['hit_rate_expected'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
