from datetime import datetime, timedelta, timezone

import numpy as np

from jetcalm_data import monitor, sample, tiles

NOW = datetime(2026, 11, 23, 3, 0, tzinfo=timezone.utc)


def make_manifest(generated, first_valid, n=15):
    steps = [{"valid": tiles.iso(first_valid + timedelta(hours=3 * i)), "kind": "wafs",
              "file": f"2026112218/f{6 + 3 * i:03d}.bin.gz", "layers": tiles.LAYERS_WAFS} for i in range(n)]
    return tiles.build_manifest(generated=generated, wafs_run="2026112218", gfs_run=None, steps=steps)


def test_monitor_fresh_stale_and_invalid():
    fresh = make_manifest(NOW - timedelta(hours=11, minutes=59), NOW)
    assert monitor.check(fresh, NOW) == []
    stale = make_manifest(NOW - timedelta(hours=12, minutes=1), NOW)
    assert any("stale" in p for p in monitor.check(stale, NOW))
    used_up = make_manifest(NOW, NOW - timedelta(hours=40))
    assert any("future" in p for p in monitor.check(used_up, NOW))
    assert any("invalid" in p for p in monitor.check({"generated": "x"}, NOW))


def test_cell_lookup():
    assert sample.cell(75, -180) == (0, 0)
    assert sample.cell(-75, 179.5) == (300, 719)
    assert sample.cell(0, 0) == (150, 360)
    assert sample.cell(40.64, -73.78) == (69, 212)
    assert sample.cell(10, 179.9) == (130, 0)          # wraps across the date line
    assert sample.cell(80, 0) is None


def test_great_circle_and_route_sampling():
    pts, km = sample.great_circle(sample.AIRPORTS["JFK"], sample.AIRPORTS["LHR"], 40)
    assert 5530 < km < 5560 and max(p[0] for p in pts) > 52
    m = make_manifest(NOW, NOW)
    layers = {n: np.zeros((301, 720), np.uint8) for n in tiles.LAYERS_WAFS}
    layers["cruise_max"][:, 300:330] = 25             # a band over the mid-Atlantic (30W .. 15W)
    blob = tiles.encode(layers, tiles.LAYERS_WAFS)
    rows, km, minutes = sample.sample_route(lambda rel: blob, m, sample.AIRPORTS["JFK"], sample.AIRPORTS["LHR"],
                                            NOW + timedelta(hours=1))
    assert rows[0]["value"] == 0 and rows[0]["level"] == "smooth"
    hit = [r for r in rows if r["value"] == 25]
    assert hit and all(-30.25 <= r["lon"] < -14.75 and r["level"] == "moderate" for r in hit)
    assert rows[0]["step"] == "2026-11-23T03:00:00Z" and rows[-1]["step"] == "2026-11-23T09:00:00Z"
    assert sample.category(255, m["thresholds"]) == "no data" and sample.category(45, m["thresholds"]) == "severe"
