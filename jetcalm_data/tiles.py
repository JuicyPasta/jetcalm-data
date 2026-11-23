"""Step files (gzip of concatenated uint8 layers) and the manifest."""
import gzip
import json
from datetime import datetime, timezone

import numpy as np

from . import grid

LAYERS_WAFS = ["cruise_mid", "cruise_max", "low_max", "cb"]
LAYERS_OUTLOOK = ["cruise_mid", "cruise_max"]
THRESHOLDS = {"light": 15, "moderate": 22, "strong": 34, "severe": 45}
MIN_BUILD = 1
GRID = {
    "rows": grid.ROWS, "cols": grid.COLS,
    "lat0": 75.0, "lon0": -180.0, "dlat": -0.5, "dlon": 0.5,
    "dtype": "uint8", "order": "layer, row (north to south), column (west to east)",
    "scale": 100, "below_cutoff": 0, "nodata": grid.NODATA,
}
LAYER_BYTES = grid.ROWS * grid.COLS


def encode(layers, names):
    """{name: 301x720 uint8} -> gzip bytes, layers in `names` order."""
    parts = []
    for n in names:
        a = np.asarray(layers[n])
        if a.shape != (grid.ROWS, grid.COLS) or a.dtype != np.uint8:
            raise ValueError(f"layer {n}: shape {a.shape} dtype {a.dtype}")
        parts.append(a.tobytes())
    return gzip.compress(b"".join(parts), 9, mtime=0)


def decode(blob, names):
    """gzip bytes -> {name: 301x720 uint8}."""
    raw = gzip.decompress(blob)
    if len(raw) != LAYER_BYTES * len(names):
        raise ValueError(f"expected {LAYER_BYTES * len(names)} bytes, got {len(raw)}")
    a = np.frombuffer(raw, np.uint8).reshape(len(names), grid.ROWS, grid.COLS)
    return {n: a[i] for i, n in enumerate(names)}


def iso(t):
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def build_manifest(*, generated, wafs_run, gfs_run, steps, notice=None, problems=None):
    m = {
        "version": 1,
        "generated": iso(generated),
        "wafs_run": wafs_run,
        "gfs_run": gfs_run,
        "grid": GRID,
        "steps": steps,
        "thresholds": THRESHOLDS,
        "min_build": MIN_BUILD,
    }
    if notice:
        m["notice"] = notice
    if problems:
        m["problems"] = problems
    validate_manifest(m)
    return m


def validate_manifest(m):
    """Raise ValueError unless m follows FORMAT.md."""
    def need(cond, msg):
        if not cond:
            raise ValueError("manifest: " + msg)

    for k in ("version", "generated", "wafs_run", "gfs_run", "grid", "steps", "thresholds", "min_build"):
        need(k in m, f"missing {k}")
    need(m["version"] == 1, "version must be 1")
    parse_iso(m["generated"])
    for k in ("wafs_run", "gfs_run"):
        v = m[k]
        need(v is None or (isinstance(v, str) and len(v) == 10 and v.isdigit()), f"{k} must be YYYYMMDDHH or null")
    need(m["wafs_run"] or m["gfs_run"], "no source run")
    need(m["grid"] == GRID, "grid differs from v1")
    t = m["thresholds"]
    need(set(t) == {"light", "moderate", "strong", "severe"}, "threshold names")
    need(0 < t["light"] < t["moderate"] < t["strong"] < t["severe"] <= 100, "thresholds must rise")
    need(isinstance(m["min_build"], int) and m["min_build"] >= 1, "min_build")
    need(isinstance(m["steps"], list) and m["steps"], "no steps")
    last = None
    for s in m["steps"]:
        need(set(s) >= {"valid", "kind", "file", "layers"}, f"step keys {sorted(s)}")
        need(s["kind"] in ("wafs", "outlook"), f"kind {s['kind']}")
        need(s["layers"] == (LAYERS_WAFS if s["kind"] == "wafs" else LAYERS_OUTLOOK), f"layers {s['layers']}")
        need(s["file"].endswith(".bin.gz") and not s["file"].startswith("/") and ".." not in s["file"], f"file {s['file']}")
        v = parse_iso(s["valid"])
        need(last is None or v > last, "steps must be in rising valid order")
        last = v
    if "notice" in m:
        need(isinstance(m["notice"], dict) and "en" in m["notice"]
             and all(isinstance(x, str) for x in m["notice"].values()), "notice must map language -> text with en")
    return m


def dumps(m):
    return json.dumps(m, separators=(",", ":")) + "\n"
