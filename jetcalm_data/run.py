"""One pipeline run: build the feed in <site>/v1 if anything is new.

usage: python -m jetcalm_data.run --site site [--now 2026-11-23T03:00:00Z] [--force]

Exit codes: 0 = feed is current (built now or already up to date);
1 = a source failed (a degraded feed may still have been written; see the log).
Writes `changed=true|false` and `degraded=true|false` to $GITHUB_OUTPUT.
"""
import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from . import calibrate, gfs, http, tiles, wafs

REPO = Path(__file__).resolve().parent.parent
OUTLOOK_HOURS = tuple(range(54, 121, 6))        # F54 .. F120
FALLBACK_HOURS = wafs.STEPS                     # F06 .. F48 from GFS when WAFS failed
CAL_HOURS = (12, 24, 36)


def log(*a):
    print(*a, flush=True)


def run_time(run):
    return datetime.strptime(run, "%Y%m%d%H").replace(tzinfo=timezone.utc)


def hours_between(a, b):
    return int((run_time(a) - run_time(b)).total_seconds() // 3600)


class CachingFetch:
    """Wraps http.get so a file probed for existence is not downloaded twice."""

    def __init__(self, fetch=http.get):
        self.fetch, self.cache, self.count, self.bytes = fetch, {}, 0, 0

    def __call__(self, url, **kw):
        key = (url, kw.get("byte_range"))
        if key in self.cache:
            return self.cache.pop(key)
        data = self.fetch(url, **kw)
        self.count += 1
        self.bytes += len(data)
        return data

    def probe(self, url, **kw):
        data = self(url, **kw)
        self.cache[(url, kw.get("byte_range"))] = data
        return data


def find_gfs_run(now, base_for, fetch):
    """Newest GFS cycle that has every forecast hour the feed needs."""
    for run in wafs.candidate_runs(now, count=5):
        hours = needed_gfs_hours(run, base_for(run))
        if hours and gfs.exists(run, max(hours), fetch) and gfs.exists(run, min(hours), fetch):
            return run
    return None


def needed_gfs_hours(gfs_run, base_run, fallback=False):
    """GFS forecast hours for the outlook steps (valid base+54 .. base+120)."""
    off = hours_between(base_run, gfs_run)
    hours = [h + off for h in OUTLOOK_HOURS]
    if fallback:
        hours = [h + off for h in FALLBACK_HOURS] + hours
    return [h for h in hours if 0 <= h <= 384]


def fit_calibration(wafs_run, gfs_run, wafs_levels, fetch):
    """Quantile-map TI1 to WAFS EDR at the valid times of WAFS F12, F24, F36."""
    off = hours_between(wafs_run, gfs_run)
    ti = {p: [] for p in calibrate.LEVEL_PAIRS}
    edr = {p: [] for p in calibrate.LEVEL_PAIRS}
    used = []
    for h in CAL_HOURS:
        fh = h + off
        if fh < 0 or h not in wafs_levels:
            continue
        t = gfs.ti1_levels(gfs.fetch_fields(gfs_run, fh, fetch))
        for p, fl in calibrate.LEVEL_PAIRS.items():
            ti[p].append(t[p].ravel())
            edr[p].append(wafs_levels[h][fl].ravel())
        used.append(h)
    if not used:
        raise gfs.GfsError("no overlapping hours to calibrate on")
    cal = {"wafs_run": wafs_run, "gfs_run": gfs_run, "wafs_hours": used, "levels": {}}
    for p in calibrate.LEVEL_PAIRS:
        cal["levels"][str(p)] = calibrate.fit(np.concatenate(ti[p]), np.concatenate(edr[p]))
    return calibrate.validate(cal)


def outlook_layers(fields, cal):
    t = gfs.ti1_levels(fields)
    lv = {p: calibrate.apply(cal["levels"][str(p)], t[p]) for p in calibrate.LEVEL_PAIRS}
    return {"cruise_mid": lv[250], "cruise_max": np.max(list(lv.values()), axis=0)}


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def stored_calibration(v1):
    for path in (v1 / "calibration.json", REPO / "calibration.json"):
        try:
            return calibrate.load(path), str(path)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    raise gfs.GfsError("no usable stored calibration")


def build(site, now, force=False, fetch=http.get):
    """Returns (changed, problems)."""
    fetch = CachingFetch(fetch)
    v1 = Path(site) / "v1"
    v1.mkdir(parents=True, exist_ok=True)
    prev = load_json(v1 / "manifest.json")
    problems = []

    wafs_run = wafs.find_newest_run(now, fetch.probe)
    if wafs_run is None:
        problems.append({"source": "wafs", "run": None, "error": "no complete run in the last four cycles"})
    gfs_run = find_gfs_run(now, lambda g: wafs_run or g, fetch.probe)
    if gfs_run is None:
        problems.append({"source": "gfs", "run": None, "error": "no complete cycle found"})
    log(f"newest WAFS run: {wafs_run}; newest GFS cycle: {gfs_run}")
    if wafs_run is None and gfs_run is None:
        return False, problems

    if prev and not force:
        if prev.get("wafs_run") == wafs_run and prev.get("gfs_run") == gfs_run:
            log("manifest already names both; nothing to do")
            return False, problems
        failed = {(p.get("source"), p.get("run")) for p in prev.get("problems", [])}
        # A source that failed last time and has nothing newer is retried, but
        # the same degraded feed is not republished if it fails again.
        retry_only = ((prev.get("wafs_run") is None and ("wafs", wafs_run) in failed and prev.get("gfs_run") == gfs_run)
                      or (prev.get("gfs_run") is None and ("gfs", gfs_run) in failed and prev.get("wafs_run") == wafs_run))
    else:
        retry_only = False

    steps, files = [], {}

    # --- WAFS step -------------------------------------------------------
    wafs_levels = {}                    # fh -> {flight level: grid}, kept for calibration
    wafs_ok = False
    if wafs_run:
        wdir = v1 / wafs_run
        have_all = all((wdir / f"f{h:03d}.bin.gz").exists() for h in wafs.STEPS)
        try:
            for h in wafs.STEPS:
                if have_all and h not in CAL_HOURS:
                    continue            # tile already published; only calibration hours are re-read
                levels, cb = wafs.fetch_step(wafs_run, h, fetch)
                wafs_levels[h] = levels
                if not have_all:
                    files[f"{wafs_run}/f{h:03d}.bin.gz"] = tiles.encode(
                        wafs.layers_from_levels(levels, cb), tiles.LAYERS_WAFS)
                log(f"WAFS {wafs_run} F{h:02d} decoded")
            wafs_ok = True
        except wafs.WafsError as e:
            log(f"WAFS step FAILED: {e}")
            problems.append({"source": "wafs", "run": wafs_run, "error": str(e)[:300]})
            files.clear()
            wafs_levels.clear()
    if wafs_ok:
        for h in wafs.STEPS:
            steps.append({"valid": tiles.iso(wafs.valid_time(wafs_run, h)), "kind": "wafs",
                          "file": f"{wafs_run}/f{h:03d}.bin.gz", "layers": tiles.LAYERS_WAFS})

    # --- Outlook step ----------------------------------------------------
    new_cal = None
    gfs_ok = False
    if gfs_run:
        base = wafs_run if wafs_ok else gfs_run
        try:
            if wafs_ok:
                try:
                    cal = new_cal = fit_calibration(wafs_run, gfs_run, wafs_levels, fetch)
                    tag = f"cal{wafs_run}"
                except (gfs.GfsError, ValueError) as e:
                    log(f"calibration fit failed ({e}); using the stored mapping")
                    problems.append({"source": "calibration", "run": gfs_run, "error": str(e)[:300]})
                    (cal, src), tag = stored_calibration(v1), "calstored"
            else:
                (cal, src), tag = stored_calibration(v1), "calstored"
                log(f"using stored calibration from {src} (WAFS {cal.get('wafs_run')})")
            odir = f"{gfs_run}-{tag}"
            out_steps = []
            for fh in needed_gfs_hours(gfs_run, base, fallback=not wafs_ok):
                rel = f"{odir}/f{fh:03d}.bin.gz"
                files[rel] = tiles.encode(outlook_layers(gfs.fetch_fields(gfs_run, fh, fetch), cal),
                                          tiles.LAYERS_OUTLOOK)
                out_steps.append({"valid": tiles.iso(run_time(gfs_run) + timedelta(hours=fh)), "kind": "outlook",
                                  "file": rel, "layers": tiles.LAYERS_OUTLOOK})
                log(f"GFS {gfs_run} f{fh:03d} -> outlook")
            steps += out_steps
            gfs_ok = True
        except gfs.GfsError as e:
            log(f"Outlook step FAILED: {e}")
            problems.append({"source": "gfs", "run": gfs_run, "error": str(e)[:300]})
            files = {k: v for k, v in files.items() if k.startswith(f"{wafs_run}/")} if wafs_ok else {}

    log(f"fetched {fetch.count} files, {fetch.bytes / 1e6:.1f} MB")
    if not steps:
        log("nothing could be built; the published feed is left as it is")
        return False, problems
    if retry_only and not (wafs_ok and gfs_ok):
        log("same degraded feed as already published; not republishing")
        return False, problems

    # --- Write, then prune to the two newest builds ----------------------
    notice = load_json(REPO / "notice.json")
    manifest = tiles.build_manifest(
        generated=now, wafs_run=wafs_run if wafs_ok else None, gfs_run=gfs_run if gfs_ok else None,
        steps=steps, notice=notice, problems=problems)
    for rel, blob in files.items():
        p = v1 / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(blob)
    for s in steps:
        s["bytes"] = (v1 / s["file"]).stat().st_size
    if new_cal:
        new_cal["updated"] = tiles.iso(now)
        calibrate.dump(new_cal, v1 / "calibration.json")
    elif not (v1 / "calibration.json").exists():
        shutil.copy(REPO / "calibration.json", v1 / "calibration.json")
    (v1 / "manifest.json").write_text(tiles.dumps(manifest))
    (Path(site) / ".nojekyll").write_text("")

    keep = {s["file"].split("/")[0] for s in steps}
    keep |= {s["file"].split("/")[0] for s in (prev or {}).get("steps", [])}
    for d in v1.iterdir():
        if d.is_dir() and d.name not in keep:
            shutil.rmtree(d)
            log(f"pruned {d.name}")
    total = sum(f.stat().st_size for f in v1.rglob("*") if f.is_file())
    log(f"wrote {len(files)} step files; manifest has {len(steps)} steps; v1 is {total / 1e6:.2f} MB")
    return True, problems


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--site", required=True, help="checkout of the gh-pages branch (created if missing)")
    ap.add_argument("--now", help="pretend the time is this (ISO 8601, UTC)")
    ap.add_argument("--force", action="store_true", help="rebuild even if the manifest is current")
    a = ap.parse_args(argv)
    now = tiles.parse_iso(a.now) if a.now else datetime.now(timezone.utc).replace(microsecond=0)
    changed, problems = build(a.site, now, a.force)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"changed={'true' if changed else 'false'}\n")
            f.write(f"degraded={'true' if problems else 'false'}\n")
    for p in problems:
        log(f"PROBLEM: {p}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
