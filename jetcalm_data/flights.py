"""Flight numbers: a daily table of airline flight number -> airports.

The app's and the web page's flight-number box read it: "BA 117" -> LHR -> JFK.
Format and rules: FLIGHTS.md.

Source: Virtual Radar Server standing data
(https://github.com/vradarserver/standing-data, CC0-1.0): routes stored by
ADS-B call sign, from ACARS messages and corrections sent by its users,
updated daily. Its git history tells live airlines from gone ones: an airline
that added no new call sign in two years is left out.

usage: python -m jetcalm_data.flights --site site [--source DIR] [--now ISO] [--force]
       python -m jetcalm_data.flights --site site --lookup "BA 117"

Exit codes: 0 = table is current (built now or already up to date);
1 = the source failed or the build looked wrong (the published table is kept).
Writes `changed=true|false` to $GITHUB_OUTPUT.
"""
import argparse
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import tiles

SOURCE_URL = "https://github.com/vradarserver/standing-data"
SOURCE_GIT = SOURCE_URL + ".git"
SOURCE_NAME = "Virtual Radar Server standing data"
SOURCE_LICENSE = "CC0-1.0"

# Bump when the rules below change, so the next run rebuilds even if the
# source has not moved.
BUILDER = 1

ACTIVE_DAYS = 730          # an airline must have added a call sign this recently
HISTORY_MARGIN_DAYS = 30   # extra history cloned before the window
MIN_FLIGHTS = 100_000      # fewer than this: the source looks broken, keep the old table
MIN_SHARE_OF_PREVIOUS = 0.5
REBUILD_HOURS = 20       # rebuild at most about once a day
MAX_AGE_HOURS = 72       # the staleness check fails past this

NUMBER = re.compile(r"\d{1,4}")
AIRLINE_IATA = re.compile(r"[A-Z][A-Z0-9]|[0-9][A-Z]")   # never two digits
IATA_INPUT = re.compile(r"([A-Z][A-Z0-9]|[0-9][A-Z])(\d{1,4})[A-Z]?")
ICAO_INPUT = re.compile(r"([A-Z]{3})(\d{1,4})[A-Z]?")

# Words that say nothing about which airline a name is.
GENERIC = {
    "air", "airline", "airlines", "airways", "aviation", "aero", "aerolineas",
    "lineas", "aereas", "cargo", "express", "international", "intl", "regional",
    "transport", "group", "the", "and", "of", "de", "del", "la", "le", "les",
    "jet", "jets", "connection", "link", "airlink", "airtours", "charter",
    "charters", "services", "company", "limited", "ltd", "inc", "llc",
}


def log(*a):
    print(*a, flush=True)


class SourceError(Exception):
    pass


# --- Source ----------------------------------------------------------------

def git(*args, cwd=None, retries=3):
    last = None
    for attempt in range(retries):
        try:
            return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                                  text=True, timeout=600).stdout
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            last = e
            if attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
    detail = getattr(last, "stderr", "") or str(last)
    raise SourceError(f"git {' '.join(args[:2])} failed: {detail.strip()[:300]}")


def remote_head():
    """The source's newest commit, without downloading it."""
    out = git("ls-remote", SOURCE_GIT, "refs/heads/main")
    sha = out.split()[0] if out.split() else ""
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise SourceError(f"ls-remote gave no commit: {out[:100]!r}")
    return sha


def clone(dest, now):
    since = (now - timedelta(days=ACTIVE_DAYS + HISTORY_MARGIN_DAYS)).strftime("%Y-%m-%d")
    git("clone", "--quiet", "--single-branch", "--branch", "main",
        f"--shallow-since={since}", SOURCE_GIT, str(dest))


def head_info(repo):
    sha, committed = git("log", "-1", "--format=%H %cI", cwd=repo).split()
    return sha, tiles.iso(datetime.fromisoformat(committed).astimezone(timezone.utc))


def first_new_dates(lines):
    """Walks `git log --reverse -p --unified=0 --format='DATE %cI'` output.

    Returns {airline code: date (YYYY-MM-DD) of the newest commit that added a
    call sign not seen before}. The oldest commit in the (shallow) history is
    the starting state: what it holds counts as seen, not as new. Lines that
    only move (a file split) or change a route are not new call signs.
    """
    seen, newest, date, commits = set(), {}, None, 0
    for line in lines:
        if line.startswith("DATE "):
            date = line[5:15]
            commits += 1
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        fields = line[1:].lstrip("﻿").rstrip("\n").split(",")
        if len(fields) < 5 or fields[0] == "Callsign":
            continue
        callsign, airline = fields[0], fields[3]
        if callsign in seen:
            continue
        seen.add(callsign)
        if commits > 1:
            newest[airline] = date
    return newest


def activity(repo):
    proc = subprocess.Popen(
        ["git", "log", "--reverse", "-p", "--unified=0", "--format=DATE %cI", "--", "routes/schema-01"],
        cwd=repo, stdout=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    newest = first_new_dates(proc.stdout)
    if proc.wait() != 0:
        raise SourceError("git log failed")
    return newest


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def read_tables(repo):
    repo = Path(repo)
    airlines = {r["Code"]: r for r in read_csv(repo / "airlines/schema-01/airlines.csv")}
    airports = {}
    for p in sorted((repo / "airports/schema-01").rglob("*.csv")):
        for r in read_csv(p):
            airports[r["Code"]] = r
    routes = []
    for p in sorted((repo / "routes/schema-01").rglob("*.csv")):
        routes += read_csv(p)
    if not airlines or not airports or not routes:
        raise SourceError(f"empty tables: {len(airlines)} airlines, {len(airports)} airports, {len(routes)} routes")
    return airlines, airports, routes


# --- Rules -----------------------------------------------------------------

def words(name):
    return {w for w in re.findall(r"[a-z0-9]+", (name or "").lower()) if len(w) >= 4 and w not in GENERIC}


def same_brand(a, b):
    """'British Airways' / 'British Airways Shuttle', 'Qantas' / 'QantasLink'."""
    wa, wb = words(a), words(b)
    la, lb = (a or "").lower(), (b or "").lower()
    return bool(wa & wb) or any(w in lb for w in wa) or any(w in la for w in wb)


def full_match(pattern, number):
    if not pattern:
        return False
    try:
        return re.fullmatch(pattern.strip().lstrip("^").rstrip("$"), number) is not None
    except re.error:
        return False


def airport_iata(code, airports, known_iata):
    row = airports.get(code)
    if row is not None:
        iata = (row.get("IATA") or "").strip().upper()
        return iata if re.fullmatch(r"[A-Z]{3}", iata) else None
    return code if code in known_iata else None   # the source sometimes stores the IATA code itself


def build_tables(airlines, airports, routes, newest, now):
    """-> (airlines index {iata: entry}, files {iata: {number: "AAA-BBB"}}, stats)."""
    cutoff = (now - timedelta(days=ACTIVE_DAYS)).strftime("%Y-%m-%d")
    known_iata = {(r.get("IATA") or "").strip().upper() for r in airports.values()}
    stats = {"routes": len(routes), "kept": 0, "not_numeric": 0, "no_airline_iata": 0, "inactive": 0,
             "positioning": 0, "airport_without_iata": 0, "other_brand": 0, "same_number_lower_priority": 0}

    by_operator = {}            # operator code -> {number: route}
    for r in routes:
        number, op = r.get("Number", ""), r.get("AirlineCode", "")
        al = airlines.get(op)
        if not NUMBER.fullmatch(number):
            stats["not_numeric"] += 1
            continue
        iata = (al.get("IATA") or "").strip().upper() if al else ""
        if not AIRLINE_IATA.fullmatch(iata):
            stats["no_airline_iata"] += 1
            continue
        if newest.get(op, "") < cutoff:
            stats["inactive"] += 1
            continue
        if full_match(al.get("PositioningFlightPattern"), number):
            stats["positioning"] += 1
            continue
        legs = [airport_iata(c.strip().upper(), airports, known_iata) for c in r.get("AirportCodes", "").split("-")]
        if len(legs) < 2 or any(c is None for c in legs) or any(a == b for a, b in zip(legs, legs[1:])):
            stats["airport_without_iata"] += 1
            continue
        by_operator.setdefault(op, {})[number] = "-".join(legs)

    groups = {}
    for op, flights in by_operator.items():
        groups.setdefault(airlines[op]["IATA"].strip().upper(), []).append(op)

    index, files = {}, {}
    for iata, ops in sorted(groups.items()):
        ops.sort(key=lambda o: (-len(by_operator[o]), o))
        primary = ops[0]
        name = airlines[primary]["Name"].strip()
        members = [primary] + [o for o in ops[1:] if same_brand(name, airlines[o]["Name"])]
        for o in ops[1:]:
            if o not in members:
                stats["other_brand"] += len(by_operator[o])
        merged = {}
        for o in members:                      # larger operator first: it keeps a shared number
            for number, route in by_operator[o].items():
                if number in merged:
                    stats["same_number_lower_priority"] += 1
                else:
                    merged[number] = route
        files[iata] = dict(sorted(merged.items(), key=lambda kv: (int(kv[0]), kv[0])))
        index[iata] = {"name": name, "icao": members, "flights": len(merged)}
        stats["kept"] += len(merged)
    stats["airlines"] = len(index)
    return index, files, stats


# --- Output ----------------------------------------------------------------

def dumps(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def airline_file(iata, entry, flights, generated):
    return dumps({"version": 1, "airline": iata, "name": entry["name"], "generated": generated,
                  "flights": flights}).encode()


def write(out, index, files, source, now):
    """Writes <out>/index.json and one <IATA>.json per airline; removes airlines that left."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    generated = tiles.iso(now)
    entries = {}
    for iata, entry in index.items():
        blob = airline_file(iata, entry, files[iata], generated)
        (out / f"{iata}.json").write_bytes(blob)
        entries[iata] = {**entry, "file": f"{iata}.json",
                         "sha": hashlib.sha256(json.dumps(files[iata], separators=(",", ":")).encode()).hexdigest()[:12]}
    keep = {f"{iata}.json" for iata in index} | {"index.json"}
    for p in out.glob("*.json"):
        if p.name not in keep:
            p.unlink()
    doc = {"version": 1, "generated": generated, "builder": BUILDER, "source": source,
           "flights": sum(e["flights"] for e in entries.values()), "airlines": entries}
    (out / "index.json").write_text(dumps(doc))
    return doc


def load_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def current(prev, sha, now):
    """Built by these rules from this source commit, less than REBUILD_HOURS ago.

    The table is rebuilt once a day even when the source has not moved: the
    two-year activity window moves with the date, and `generated` then shows
    that the job runs (the staleness check reads it)."""
    if not prev or prev.get("builder") != BUILDER or prev.get("source", {}).get("commit") != sha:
        return False
    try:
        age = now - tiles.parse_iso(prev["generated"])
    except (KeyError, ValueError):
        return False
    return age < timedelta(hours=REBUILD_HOURS)


def build(site, now, force=False, source_dir=None, head=remote_head):
    """Returns (changed, problems)."""
    out = Path(site) / "flights" / "v1"
    prev = load_json(out / "index.json")
    try:
        if source_dir is None:
            sha = head()
            if not force and current(prev, sha, now):
                log(f"flight table already built from {sha[:8]} today; nothing to do")
                return False, []
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(source_dir) if source_dir else Path(tmp) / "standing-data"
            if source_dir is None:
                t = time.time()
                clone(repo, now)
                log(f"cloned the source in {time.time() - t:.1f} s")
            sha, committed = head_info(repo)
            newest = activity(repo)
            airlines, airports, routes = read_tables(repo)
            index, files, stats = build_tables(airlines, airports, routes, newest, now)
    except SourceError as e:
        log(f"flight numbers FAILED: {e}")
        return False, [{"source": "flights", "error": str(e)[:300]}]

    log("flight numbers:", json.dumps(stats))
    total = stats["kept"]
    if total < MIN_FLIGHTS:
        return False, [{"source": "flights", "error": f"only {total} flights; the table is left as it was"}]
    if prev and total < MIN_SHARE_OF_PREVIOUS * prev.get("flights", 0):
        return False, [{"source": "flights",
                        "error": f"{total} flights, under half of the {prev['flights']} published; left as it was"}]
    source = {"name": SOURCE_NAME, "url": SOURCE_URL, "license": SOURCE_LICENSE,
              "commit": sha, "committed": committed}
    doc = write(out, index, files, source, now)
    size = sum(p.stat().st_size for p in out.glob("*.json"))
    log(f"wrote {len(doc['airlines'])} airlines, {doc['flights']} flights, {size / 1e6:.2f} MB to {out}")
    return True, []


def check(index, now, max_age_hours=MAX_AGE_HOURS):
    """Problems with a published index.json (empty = healthy). Used by monitor.py."""
    try:
        if index.get("version") != 1:
            return [f"flights index has version {index.get('version')!r}"]
        age = (now - tiles.parse_iso(index["generated"])).total_seconds() / 3600
        count = int(index["flights"])
        airlines = len(index["airlines"])
    except (KeyError, ValueError, TypeError, AttributeError) as e:
        return [f"flights index is invalid: {e}"]
    out = []
    if age > max_age_hours:
        out.append(f"flight numbers are stale: generated {index['generated']}, {age:.0f} h ago (limit {max_age_hours} h)")
    if count < MIN_FLIGHTS or not airlines:
        out.append(f"flights index has only {count} flights in {airlines} airlines")
    return out


# --- Reading (the reference client) -----------------------------------------

def parse(text):
    """'BA 117' -> ('BA', '117', 'iata'); 'BAW0117' -> ('BAW', '117', 'icao'); else None."""
    s = re.sub(r"[\s\-.]", "", (text or "").upper())
    for pattern, kind in ((IATA_INPUT, "iata"), (ICAO_INPUT, "icao")):
        m = pattern.fullmatch(s)
        if m:
            return m.group(1), m.group(2).lstrip("0") or "0", kind
    return None


def lookup(index, read_airline, text):
    """-> (iata, name, number, [airports]) or None. read_airline(file) -> parsed JSON."""
    parsed = parse(text)
    if not parsed:
        return None
    code, number, kind = parsed
    airlines = index["airlines"]
    if kind == "icao":
        code = next((iata for iata, e in airlines.items() if code in e["icao"]), None)
    entry = airlines.get(code) if code else None
    if not entry:
        return None
    route = read_airline(entry["file"])["flights"].get(number)
    return (code, entry["name"], number, route.split("-")) if route else None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--site", required=True, help="checkout of the gh-pages branch")
    ap.add_argument("--source", help="an existing clone of the source (skips the download)")
    ap.add_argument("--now", help="pretend the time is this (ISO 8601, UTC)")
    ap.add_argument("--force", action="store_true", help="rebuild even if the source has not moved")
    ap.add_argument("--lookup", help="look a flight number up in <site>/flights/v1 and exit")
    a = ap.parse_args(argv)
    if a.lookup:
        out = Path(a.site) / "flights" / "v1"
        index = load_json(out / "index.json")
        hit = lookup(index, lambda f: load_json(out / f), a.lookup) if index else None
        print(hit or "not found")
        return 0 if hit else 1
    now = tiles.parse_iso(a.now) if a.now else datetime.now(timezone.utc).replace(microsecond=0)
    changed, problems = build(a.site, now, a.force, a.source)
    gh = os.environ.get("GITHUB_OUTPUT")
    if gh:
        with open(gh, "a") as f:
            f.write(f"changed={'true' if changed else 'false'}\n")
    for p in problems:
        log(f"PROBLEM: {p}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
