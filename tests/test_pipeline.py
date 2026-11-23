"""The whole build, offline: AWC images come from fixtures, GFS fields are synthetic."""
import json
import re
from datetime import datetime, timezone

import numpy as np
import pytest

from conftest import FIXTURES
from jetcalm_data import gfs, http, run, tiles

NOW = datetime(2026, 11, 22, 20, 30, tzinfo=timezone.utc)      # 12Z WAFS and 12Z GFS are the newest
AWC_RE = re.compile(r"/(\d{8})/(\d{2})/\d{8}_\d{2}_F(\d{2})_wafs_(\w+)_m\.png$")


def fake_fields(run_id, fh, fetch=None):
    rng = np.random.default_rng(int(run_id) % 1000 + fh)
    lat = np.radians(np.linspace(90, -90, gfs.NJ))[:, None]
    lon = np.radians(np.arange(gfs.NI) * 0.5)[None, :]
    f = {}
    for i, p in enumerate(gfs.PRESSURES):
        k = rng.integers(2, 6)
        f[("UGRD", p)] = (30 + 4 * i) * np.cos(lat) * (1 + 0.5 * np.sin(k * lon + fh)) + 3 * np.sin(7 * lat)
        f[("VGRD", p)] = (8 + i) * np.sin(k * lon + 0.3 * i) * np.cos(lat)
        f[("HGT", p)] = np.full((gfs.NJ, gfs.NI), 13500.0 - 1200.0 * i)
    return f


class FakeNet:
    def __init__(self, wafs_runs=("2026112212",), bad_product=None):
        self.wafs_runs, self.bad_product, self.calls = wafs_runs, bad_product, []

    def __call__(self, url, **kw):
        self.calls.append(url)
        m = AWC_RE.search(url)
        if not m:
            raise AssertionError("unexpected URL " + url)
        if m.group(1) + m.group(2) not in self.wafs_runs:
            raise http.NotFound(url)
        if self.bad_product and m.group(4) == self.bad_product and m.group(3) == "24":
            return (FIXTURES / "awc" / "20261122_12_F12_wafs_whole_cbhe_m.png").read_bytes()  # greys: wrong palette
        return (FIXTURES / "awc" / f"20261122_12_F12_wafs_{m.group(4)}_m.png").read_bytes()


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr(gfs, "fetch_fields", fake_fields)
    monkeypatch.setattr(gfs, "exists", lambda run_id, fh, fetch=None: run_id <= "2026112212")


def manifest(site):
    return tiles.validate_manifest(json.loads((site / "v1" / "manifest.json").read_text()))


def test_full_build_then_idempotent(tmp_path, offline):
    net = FakeNet()
    changed, problems = run.build(tmp_path, NOW, fetch=net)
    assert changed and not problems
    m = manifest(tmp_path)
    assert m["wafs_run"] == "2026112212" and m["gfs_run"] == "2026112212" and "problems" not in m
    kinds = [s["kind"] for s in m["steps"]]
    assert kinds == ["wafs"] * 15 + ["outlook"] * 12
    assert m["steps"][0]["valid"] == "2026-11-22T18:00:00Z"        # F06
    assert m["steps"][14]["valid"] == "2026-11-24T12:00:00Z"       # F48
    assert m["steps"][15]["valid"] == "2026-11-24T18:00:00Z"       # F54
    assert m["steps"][-1]["valid"] == "2026-11-27T12:00:00Z"       # F120
    assert m["thresholds"] == {"light": 15, "moderate": 22, "strong": 34, "severe": 45}
    for s in m["steps"]:
        blob = (tmp_path / "v1" / s["file"]).read_bytes()
        assert len(blob) == s["bytes"]
        layers = tiles.decode(blob, s["layers"])
        assert np.all(layers["cruise_mid"] <= layers["cruise_max"])
        assert layers["cruise_max"].max() <= 100
    out = tiles.decode((tmp_path / "v1" / m["steps"][-1]["file"]).read_bytes(), tiles.LAYERS_OUTLOOK)
    assert 0.02 < (out["cruise_max"] >= 15).mean() < 0.6           # calibrated to a WAFS-like share
    assert (tmp_path / ".nojekyll").exists() and (tmp_path / "v1" / "calibration.json").exists()
    # 15 steps x 8 images, each fetched once (the two probe files are reused), plus one 404 probe of 18Z
    assert len(net.calls) == 15 * 8 + 1 and len(set(net.calls)) == len(net.calls)

    net2 = FakeNet()
    changed, problems = run.build(tmp_path, NOW, fetch=net2)
    assert not changed and not problems
    assert len(net2.calls) <= 3


def test_unknown_palette_falls_back_to_a_complete_gfs_feed(tmp_path, offline):
    changed, problems = run.build(tmp_path, NOW, fetch=FakeNet(bad_product="250_edr"))
    assert changed and [p["source"] for p in problems] == ["wafs"]
    assert "palette" in problems[0]["error"]
    m = manifest(tmp_path)
    assert m["wafs_run"] is None and m["gfs_run"] == "2026112212" and m["problems"]
    assert [s["kind"] for s in m["steps"]] == ["outlook"] * 27
    assert m["steps"][0]["valid"] == "2026-11-22T18:00:00Z" and m["steps"][-1]["valid"] == "2026-11-27T12:00:00Z"
    assert not (tmp_path / "v1" / "2026112212").exists()           # no half-written WAFS run
    # still failing an hour later: reported again, not republished
    before = (tmp_path / "v1" / "manifest.json").read_text()
    changed, problems = run.build(tmp_path, NOW, fetch=FakeNet(bad_product="250_edr"))
    assert not changed and problems
    assert (tmp_path / "v1" / "manifest.json").read_text() == before
    # WAFS works again: the full feed replaces the fallback
    changed, problems = run.build(tmp_path, NOW, fetch=FakeNet())
    assert changed and not problems and manifest(tmp_path)["wafs_run"] == "2026112212"


def test_wafs_gone_entirely_still_publishes(tmp_path, offline):
    changed, problems = run.build(tmp_path, NOW, fetch=FakeNet(wafs_runs=()))
    assert changed and problems[0]["source"] == "wafs"
    assert len(manifest(tmp_path)["steps"]) == 27


def test_gfs_failure_keeps_wafs_steps(tmp_path, offline, monkeypatch):
    def boom(run_id, fh, fetch=None):
        raise gfs.GfsError("S3 down")
    monkeypatch.setattr(gfs, "fetch_fields", boom)
    changed, problems = run.build(tmp_path, NOW, fetch=FakeNet())
    assert changed and {p["source"] for p in problems} == {"calibration", "gfs"}
    m = manifest(tmp_path)
    assert m["gfs_run"] is None and [s["kind"] for s in m["steps"]] == ["wafs"] * 15


def test_new_gfs_cycle_reuses_wafs_tiles_and_keeps_two_builds(tmp_path, offline, monkeypatch):
    run.build(tmp_path, NOW, fetch=FakeNet())
    first = manifest(tmp_path)
    monkeypatch.setattr(gfs, "exists", lambda run_id, fh, fetch=None: run_id <= "2026112218")
    later = datetime(2026, 11, 22, 23, 30, tzinfo=timezone.utc)
    net = FakeNet()
    changed, _ = run.build(tmp_path, later, fetch=net)
    assert changed
    m = manifest(tmp_path)
    assert m["wafs_run"] == "2026112212" and m["gfs_run"] == "2026112218"
    assert [s["valid"] for s in m["steps"]] == [s["valid"] for s in first["steps"]]
    assert m["steps"][15]["file"] == "2026112218-cal2026112212/f048.bin.gz"
    assert len(net.calls) <= 3 + 3 * 8                             # only the calibration hours are re-read
    dirs = sorted(d.name for d in (tmp_path / "v1").iterdir() if d.is_dir())
    assert dirs == ["2026112212", "2026112212-cal2026112212", "2026112218-cal2026112212"]
    # a third build drops the oldest outlook directory
    monkeypatch.setattr(gfs, "exists", lambda run_id, fh, fetch=None: run_id <= "2026112300")
    run.build(tmp_path, datetime(2026, 11, 23, 5, 30, tzinfo=timezone.utc), fetch=FakeNet(wafs_runs=("2026112212", "2026112218")))
    dirs = sorted(d.name for d in (tmp_path / "v1").iterdir() if d.is_dir())
    assert "2026112212-cal2026112212" not in dirs and "2026112218" in dirs and "2026112212" in dirs


def test_manifest_schema():
    step = {"valid": "2026-11-22T18:00:00Z", "kind": "wafs", "file": "2026112212/f006.bin.gz",
            "layers": tiles.LAYERS_WAFS}
    m = tiles.build_manifest(generated=NOW, wafs_run="2026112212", gfs_run=None, steps=[step],
                             notice={"en": "Forecasts may be delayed."})
    assert m["generated"] == "2026-11-22T20:30:00Z" and m["grid"]["rows"] == 301 and m["grid"]["cols"] == 720
    assert json.loads(tiles.dumps(m)) == m
    for breakage in (
        {"thresholds": {"light": 15, "moderate": 10, "strong": 34, "severe": 45}},
        {"steps": []},
        {"steps": [dict(step, kind="radar")]},
        {"steps": [dict(step, layers=["cruise_max"])]},
        {"steps": [dict(step, file="../x.bin.gz")]},
        {"steps": [step, step]},
        {"wafs_run": "20261122"},
        {"wafs_run": None},
        {"notice": {"de": "x"}},
        {"min_build": 0},
        {"generated": "yesterday"},
    ):
        with pytest.raises(ValueError):
            tiles.validate_manifest({**m, **breakage})


def test_http_retries_then_succeeds_and_404_is_immediate(monkeypatch):
    import requests
    calls, sleeps = [], []

    class Resp:
        def __init__(self, code, content=b""):
            self.status_code, self.content = code, content

    class Session:
        def __init__(self, script):
            self.script = script

        def get(self, url, headers=None, timeout=None):
            calls.append((url, headers, timeout))
            r = self.script(len(calls))
            if isinstance(r, Exception):
                raise r
            return r

    def use(script):
        calls.clear()
        monkeypatch.setattr(http, "_session", Session(script))

    use(lambda n: requests.ConnectionError("dropped") if n < 3 else Resp(200, b"ok"))
    assert http.get("https://example.invalid/x", sleep=sleeps.append) == b"ok"
    assert len(calls) == 3 and sleeps == [3.0, 6.0]

    use(lambda n: Resp(404))
    with pytest.raises(http.NotFound):
        http.get("https://example.invalid/x", sleep=sleeps.append)
    assert len(calls) == 1

    use(lambda n: Resp(503))
    with pytest.raises(IOError, match="after 5 tries"):
        http.get("https://example.invalid/x", sleep=sleeps.append)
    assert len(calls) == 5

    use(lambda n: Resp(206, b"abcd"))
    assert http.get("https://example.invalid/x", byte_range=(10, 13), sleep=sleeps.append) == b"abcd"
    assert calls[0][1] == {"Range": "bytes=10-13"}
    use(lambda n: Resp(206, b"ab"))
    with pytest.raises(IOError, match="short"):
        http.get("https://example.invalid/x", byte_range=(10, 13), retries=2, sleep=sleeps.append)


def test_user_agent_names_the_project():
    monkey = http.session()
    assert "jetcalm-data" in monkey.headers["User-Agent"]
    assert "https://github.com/JuicyPasta/jetcalm-data" in monkey.headers["User-Agent"]
