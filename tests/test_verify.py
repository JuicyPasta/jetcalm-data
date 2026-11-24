import json
from datetime import datetime, timedelta, timezone

import numpy as np

from conftest import FIXTURES
from jetcalm_data import tiles, verify

TH = tiles.THRESHOLDS


def fixture_reports():
    raw = json.loads((FIXTURES / "pireps.json").read_text())
    reports, dropped = [], {}
    for d in raw:
        r, why = verify.parse_report(d)
        if r:
            reports.append(r)
        else:
            dropped[why] = dropped.get(why, 0) + 1
    return reports, dropped


def blank(names):
    return {n: np.zeros((301, 720), np.uint8) for n in names}


def write_step(v1, rel, layers, names):
    p = v1 / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(tiles.encode(layers, names))


def make_site(tmp_path):
    v1 = tmp_path / "v1"
    w = blank(tiles.LAYERS_WAFS)
    w["cruise_mid"][78, 651] = 18          # 36.13N 145.47E: light
    w["low_max"][70, 150] = 25             # 39.9N 105.1W: moderate (FL260 reads low_max)
    w["cruise_mid"][62, 103] = 15          # 43.75N 128.73W: light expected ...
    w["cruise_max"][63, 104] = 30          # ... a moderate cell next to it: could reach moderate
    w["cruise_mid"][70, 200] = 50          # 40N 80W: severe
    w["cruise_max"] = np.maximum(w["cruise_max"], w["cruise_mid"])
    write_step(v1, "2026112400/f009.bin.gz", w, tiles.LAYERS_WAFS)
    # Same valid time from an older run (longer lead) and other times: must not be chosen.
    loud = blank(tiles.LAYERS_WAFS)
    for n in ("cruise_mid", "cruise_max", "low_max"):
        loud[n][:] = 60
    write_step(v1, "2026112318/f015.bin.gz", loud, tiles.LAYERS_WAFS)
    write_step(v1, "2026112400/f012.bin.gz", loud, tiles.LAYERS_WAFS)
    write_step(v1, "2026112400-cal2026112400/f054.bin.gz", blank(tiles.LAYERS_OUTLOOK), tiles.LAYERS_OUTLOOK)
    (v1 / "manifest.json").write_text("{}")
    return v1


def test_parse_reports():
    reports, dropped = fixture_reports()
    assert dropped == {"no turbulence": 1, "below FL130 or above FL450": 1, "no position, time or level": 1}
    by_ac = {r["ac"]: r for r in reports}
    assert by_ac["A359"]["rep"] == "moderate" and by_ac["A359"]["int"] == "LGT MOD"   # worst of two groups
    assert by_ac["B38M"]["rep"] == "none" and by_ac["BE20"]["rep"] == "light-moderate"
    assert by_ac["B38M"]["t"] == "2026-11-24T10:05:00Z"
    assert verify.reported_category({"tbInt1": "MOD-SEV"}) == "severe"
    assert verify.reported_category({"tbInt1": "SMTH-LGT"}) == "light"
    assert verify.reported_category({"tbInt1": "???"}) is None


def test_inventory_and_step_choice(tmp_path):
    steps = verify.inventory(make_site(tmp_path))
    assert {s["file"] for s in steps} == {"2026112400/f009.bin.gz", "2026112318/f015.bin.gz",
                                          "2026112400/f012.bin.gz", "2026112400-cal2026112400/f054.bin.gz"}
    out = next(s for s in steps if s["kind"] == "outlook")
    assert out["valid"] == datetime(2026, 11, 26, 6, tzinfo=timezone.utc) and out["layers"] == tiles.LAYERS_OUTLOOK
    at = datetime(2026, 11, 24, 10, 5, tzinfo=timezone.utc)
    assert verify.choose_step(steps, at)["file"] == "2026112400/f009.bin.gz"     # nearest, then shortest lead
    assert verify.choose_step(steps, at + timedelta(minutes=60))["file"] == "2026112400/f012.bin.gz"
    assert verify.choose_step(steps, at - timedelta(hours=3)) is None             # 07:05: 2 h from any step


def test_match_reads_like_the_app(tmp_path):
    reports, _ = fixture_reports()
    matches, skipped = verify.match(reports, verify.inventory(make_site(tmp_path)))
    assert skipped == {"outside 75S..75N": 1, "no forecast step published for that time": 1}
    by_ac = {m["ac"]: m for m in matches}
    assert len(matches) == 5
    assert (by_ac["B38M"]["exp"], by_ac["B38M"]["band"]) == (0, "cruise")
    assert by_ac["UAL33"]["exp"] == 18
    assert (by_ac["BE20"]["band"], by_ac["BE20"]["exp"]) == ("low", 25)
    assert (by_ac["A359"]["exp"], by_ac["A359"]["reach"]) == (15, 30)
    assert by_ac["B739"]["exp"] == 50 and by_ac["B739"]["step"] == "2026112400/f009.bin.gz"
    assert all(m["lead"] == 9 and m["kind"] == "wafs" for m in matches)


def test_low_band_without_low_layer_reads_cruise():
    layers = blank(tiles.LAYERS_OUTLOOK)
    layers["cruise_mid"][10, 10] = 20
    layers["cruise_max"][11, 11] = 40
    assert verify.read_forecast(layers, 200, (10, 10)) == ("low", 20, 40)
    assert verify.read_forecast(layers, 350, (10, 10)) == ("cruise", 20, 40)
    # 3x3 maximum wraps across the date line.
    layers["cruise_max"][10, 719] = 44
    assert verify.read_forecast(layers, 350, (10, 0)) == ("cruise", 0, 44)


def test_score(tmp_path):
    reports, _ = fixture_reports()
    matches, _ = verify.match(reports, verify.inventory(make_site(tmp_path)))
    s = verify.score(matches, TH)
    assert s["n"] == 5 and s["agree"] == 4 and s["agree_rate"] == 0.8
    assert s["within_range"] == 5
    assert s["per_reported"]["moderate"] == {"n": 1, "agree": 0, "rate": 0.0}
    assert s["per_reported"]["severe"] == {"n": 1, "agree": 1, "rate": 1.0}
    mog = s["moderate_or_greater"]
    assert (mog["reports"], mog["hit_rate_expected"], mog["hit_rate_could_reach"]) == (2, 0.5, 1.0)
    assert s["smooth_reports"]["calm_rate"] == 1.0 and s["any_turbulence"]["hit_rate"] == 1.0
    assert s["confusion_expected"]["light"]["moderate"] == 1
    assert s["confusion_expected"]["calm"]["none"] == 1
    assert sum(sum(r.values()) for r in s["confusion_expected"].values()) == 5
    assert s["by_band"] == {"cruise": 4, "low": 1}
    assert verify.score([], TH)["agree_rate"] is None


def test_levels_follow_thresholds():
    assert [verify.level_of(v, TH) for v in (0, 14, 15, 21, 22, 33, 34, 44, 45, 100)] == \
        ["calm", "calm", "light", "light", "moderate", "moderate", "strong", "strong", "severe", "severe"]


def test_store_dedupes_and_doc_table(tmp_path):
    reports, _ = fixture_reports()
    matches, _ = verify.match(reports, verify.inventory(make_site(tmp_path)))
    mdir = tmp_path / "out" / "matches"
    assert verify.store_matches(mdir, matches) == 5
    assert verify.store_matches(mdir, matches) == 0
    assert [f.name for f in mdir.iterdir()] == ["2026-11-24.jsonl"]
    stored = verify.load_matches(mdir)
    assert len(stored) == 5
    summ = verify.summary(stored, TH, datetime(2026, 11, 24, 12, tzinfo=timezone.utc))
    assert summ["last_7_days"]["n"] == 5 and list(summ["daily"]) == ["2026-11-24"]
    doc = tmp_path / "verification.md"
    doc.write_text(f"# x\n\n{verify.DOC_START}\nold\n{verify.DOC_END}\n\ntail\n")
    verify.update_doc(doc, summ)
    text = doc.read_text()
    assert "old" not in text and "| 2026-11-24 | 5 | 80% | 100% | 50% / 100% of 2 | 100% of 1 |" in text
    assert text.endswith("tail\n")


class FakeResponse:
    def __init__(self, rows):
        self.rows, self.status_code = rows, 200
        self.content = json.dumps(rows).encode()

    def raise_for_status(self):
        pass

    def json(self):
        return self.rows


class FakeSession:
    """Returns one report a minute in the requested window, capped like the API."""

    def __init__(self):
        self.calls = []

    def get(self, url, params, timeout):
        self.calls.append(params)
        end = tiles.parse_iso(params["date"])
        start = end - timedelta(hours=params["age"])
        rows, t = [], end
        while t >= start and len(rows) < verify.API_CAP:
            rows.append({"obsTime": int(t.timestamp()), "lat": 1, "lon": 1, "fltLvl": 300,
                         "acType": params.get("inten", "all"), "rawOb": "x"})
            t -= timedelta(minutes=1)
        return FakeResponse(rows)


def test_fetch_splits_windows_under_the_cap():
    s = FakeSession()
    start = datetime(2026, 11, 24, 0, tzinfo=timezone.utc)
    rows = verify.fetch_reports(start, start + timedelta(hours=8), session=s, pause=lambda _: None, log=lambda *a: None)
    # 8 hours = 480 minutes per query, all of them kept once, none at or after the end.
    assert len(rows) == 2 * 480
    assert all(r["obsTime"] < (start + timedelta(hours=8)).timestamp() for r in rows)
    assert max(c["age"] for c in s.calls) <= 3
    assert all(c["format"] == "json" and "bbox" in c for c in s.calls)
