"""Reference reader: download the published feed and sample it along a route.

usage: python -m jetcalm_data.sample JFK LHR 2026-11-23T23:00:00Z [--base URL | --dir path] [--layer cruise_max]

This is the decoding the app has to reproduce (see FORMAT.md). The flight model
is deliberately crude: a great circle flown at a constant 850 km/h.
"""
import argparse
import json
import math
from datetime import timedelta
from pathlib import Path

from . import http, tiles
from .monitor import BASE

AIRPORTS = {"JFK": (40.6413, -73.7781), "LHR": (51.4700, -0.4543), "LAX": (33.9416, -118.4085),
            "NRT": (35.7720, 140.3929), "GRU": (-23.4356, -46.4731), "MIA": (25.7959, -80.2870),
            "SYD": (-33.9399, 151.1753), "SIN": (1.3644, 103.9915)}
SPEED_KMH = 850.0


def great_circle(a, b, n):
    """n+1 evenly spaced (lat, lon) points from a to b, and the distance in km."""
    p1, l1, p2, l2 = map(math.radians, (*a, *b))
    d = 2 * math.asin(math.sqrt(math.sin((p2 - p1) / 2) ** 2
                                + math.cos(p1) * math.cos(p2) * math.sin((l2 - l1) / 2) ** 2))
    pts = []
    for i in range(n + 1):
        f = i / n
        A, B = math.sin((1 - f) * d) / math.sin(d), math.sin(f * d) / math.sin(d)
        x = A * math.cos(p1) * math.cos(l1) + B * math.cos(p2) * math.cos(l2)
        y = A * math.cos(p1) * math.sin(l1) + B * math.cos(p2) * math.sin(l2)
        z = A * math.sin(p1) + B * math.sin(p2)
        pts.append((math.degrees(math.atan2(z, math.hypot(x, y))), math.degrees(math.atan2(y, x))))
    return pts, d * 6371.0


def cell(lat, lon):
    """(row, col) of the grid cell nearest to a point, or None outside 75S..75N."""
    if abs(lat) > 75.25:
        return None
    return min(300, max(0, round((75.0 - lat) / 0.5))), round((lon + 180.0) / 0.5) % 720


def nearest_step(manifest, when):
    return min(manifest["steps"], key=lambda s: abs((tiles.parse_iso(s["valid"]) - when).total_seconds()))


def category(value, thresholds):
    if value == 255:
        return "no data"
    name = "smooth"
    for k in ("light", "moderate", "strong", "severe"):
        if value >= thresholds[k]:
            name = k
    return name


def sample_route(read, manifest, origin, dest, departure, layer="cruise_max", every_min=20):
    pts, km = great_circle(origin, dest, 200)
    minutes = km / SPEED_KMH * 60
    cache, rows = {}, []
    t = 0.0
    while t <= minutes + 1e-6:
        lat, lon = pts[min(200, round(t / minutes * 200))]
        when = departure + timedelta(minutes=t)
        step = nearest_step(manifest, when)
        if step["file"] not in cache:
            cache[step["file"]] = tiles.decode(read(step["file"]), step["layers"])
        rc = cell(lat, lon)
        value = 255 if rc is None or layer not in cache[step["file"]] else int(cache[step["file"]][layer][rc])
        rows.append({"min": round(t), "lat": round(lat, 2), "lon": round(lon, 2), "step": step["valid"],
                     "kind": step["kind"], "file": step["file"], "value": value,
                     "level": category(value, manifest["thresholds"])})
        t += every_min
    return rows, km, minutes


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("origin"), ap.add_argument("dest"), ap.add_argument("departure")
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--dir", help="read a local v1 directory instead of the URL")
    ap.add_argument("--layer", default="cruise_max")
    a = ap.parse_args(argv)
    read = (lambda rel: (Path(a.dir) / rel).read_bytes()) if a.dir else (lambda rel: http.get(a.base + rel))
    manifest = tiles.validate_manifest(json.loads(read("manifest.json")))
    rows, km, minutes = sample_route(read, manifest, AIRPORTS[a.origin], AIRPORTS[a.dest],
                                     tiles.parse_iso(a.departure), a.layer)
    print(f"{a.origin}-{a.dest} {km:.0f} km, {minutes / 60:.1f} h, departs {a.departure}, layer {a.layer}; "
          f"feed generated {manifest['generated']} (WAFS {manifest['wafs_run']}, GFS {manifest['gfs_run']})")
    for r in rows:
        print(f"  +{r['min']:4d} min  {r['lat']:7.2f} {r['lon']:8.2f}  {r['kind']:7s} {r['step']}  "
              f"EDRx100 {r['value']:3d}  {r['level']}")
    print("  profile:", " ".join(str(r["value"]) for r in rows))


if __name__ == "__main__":
    main()
