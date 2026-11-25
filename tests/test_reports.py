import json
from datetime import datetime, timezone

from conftest import FIXTURES
from jetcalm_data import reports

NOW = datetime(2026, 11, 24, 12, tzinfo=timezone.utc)


def fixture():
    return json.loads((FIXTURES / "pireps.json").read_text())


def test_levels_keep_smooth_and_split_severe():
    assert reports.level({"tbInt1": "NEG"}) == "smooth"
    assert reports.level({"tbInt1": "LGT", "tbInt2": "MOD"}) == "moderate"      # worst of two groups
    assert reports.level({"tbInt1": "MOD-SEV"}) == "moderate-severe"
    assert reports.level({"tbInt1": "sev"}) == "severe"
    assert reports.level({"tbInt1": "EXTM"}) == "extreme"
    assert reports.level({"tbInt1": "", "tbInt2": None}) is None


def test_build_keeps_turbulence_reports_at_any_altitude():
    doc, dropped = reports.build(fixture(), NOW, 3)
    assert doc["generated"] == "2026-11-24T12:00:00Z" and doc["window_hours"] == 3
    assert doc["levels"] == reports.LEVELS and "Aviation Weather Center" in doc["source"]
    rs = doc["reports"]
    # 10 records: one without turbulence, one without a level; the FL080 one is kept here
    # (verification drops it), as is the FL380 report at 80N.
    assert dropped == {"no turbulence": 1, "no position, time or level": 1}
    assert len(rs) == 8
    assert {r["level"] for r in rs} == {"smooth", "light", "light-moderate", "moderate", "severe"}
    assert any(r["fl"] == 80 and r["level"] == "moderate" for r in rs)
    assert [r["t"] for r in rs] == sorted((r["t"] for r in rs), reverse=True)   # newest first
    assert set(rs[0]) == {"t", "lat", "lon", "fl", "ac", "type", "level"}


def test_no_call_signs_or_raw_text():
    doc, _ = reports.build(fixture(), NOW, 3)
    acs = {r["ac"] for r in doc["reports"]}
    assert "UAL33" not in acs and "B739" in acs and "A359" in acs
    assert "rawOb" not in json.dumps(doc) and "/OV" not in json.dumps(doc)
    assert reports.aircraft_type({"ac": "BAW88C", "type": "AIREP"}) == ""
    assert reports.aircraft_type({"ac": "GTR1634", "type": "PIREP"}) == ""
    assert reports.aircraft_type({"ac": "CRJ9", "type": "PIREP"}) == "CRJ9"


def test_main_writes_compact_file(tmp_path, monkeypatch):
    monkeypatch.setattr(reports.verify, "fetch_reports", lambda start, end: fixture())
    out = tmp_path / "reports" / "latest.json"
    assert reports.main(["--out", str(out)]) == 0
    doc = json.loads(out.read_text())
    assert len(doc["reports"]) == 8 and doc["window_hours"] == 3
    assert "\n" not in out.read_text().strip()
