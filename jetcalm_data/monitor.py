"""Staleness check: fail if the published feed is older than 12 hours.

usage: python -m jetcalm_data.monitor [base URL]
"""
import json
import sys
from datetime import datetime, timezone

from . import http, tiles

BASE = "https://juicypasta.github.io/jetcalm-data/v1/"
MAX_AGE_HOURS = 12


def check(manifest, now, max_age_hours=MAX_AGE_HOURS):
    """Returns a list of problems (empty = healthy)."""
    try:
        tiles.validate_manifest(manifest)
    except (ValueError, KeyError, TypeError) as e:
        return [f"manifest is invalid: {e}"]
    out = []
    age = (now - tiles.parse_iso(manifest["generated"])).total_seconds() / 3600
    if age > max_age_hours:
        out.append(f"feed is stale: generated {manifest['generated']}, {age:.1f} h ago (limit {max_age_hours} h)")
    future = [s for s in manifest["steps"] if tiles.parse_iso(s["valid"]) > now]
    if len(future) < 8:
        out.append(f"only {len(future)} steps are still in the future")
    return out


def main(argv=None):
    base = (argv or sys.argv[1:] or [BASE])[0]
    now = datetime.now(timezone.utc)
    manifest = json.loads(http.get(base + f"manifest.json?t={int(now.timestamp())}"))
    problems = check(manifest, now)
    if not problems:
        step = next((s for s in manifest["steps"] if tiles.parse_iso(s["valid"]) > now), manifest["steps"][-1])
        try:
            tiles.decode(http.get(base + step["file"]), step["layers"])
        except Exception as e:
            problems.append(f"step file {step['file']} is not readable: {e}")
    for p in problems:
        print("PROBLEM:", p)
    if manifest.get("problems"):
        print("manifest reports:", manifest["problems"])
    print(f"generated {manifest.get('generated')}, wafs_run {manifest.get('wafs_run')}, "
          f"gfs_run {manifest.get('gfs_run')}, {len(manifest.get('steps', []))} steps")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
