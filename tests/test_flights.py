import json
import subprocess
from datetime import datetime, timezone

import pytest

from jetcalm_data import flights

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)
RECENT = "2026-10-01"
OLD = "2023-01-01"

AIRPORTS = {
    "EGLL": {"Code": "EGLL", "IATA": "LHR"},
    "KJFK": {"Code": "KJFK", "IATA": "JFK"},
    "WSSS": {"Code": "WSSS", "IATA": "SIN"},
    "YSSY": {"Code": "YSSY", "IATA": "SYD"},
    "EGPH": {"Code": "EGPH", "IATA": "EDI"},
    "XXNO": {"Code": "XXNO", "IATA": ""},          # an airfield without an IATA code
    "LFBO": {"Code": "LFBO", "IATA": "TLS"},
    "EDHI": {"Code": "EDHI", "IATA": "XFW"},
    "EDDF": {"Code": "EDDF", "IATA": "FRA"},
}


def airline(code, iata, name, positioning=""):
    return {"Code": code, "Name": name, "ICAO": code, "IATA": iata, "PositioningFlightPattern": positioning,
            "CharterFlightPattern": ""}


def route(op, number, airports):
    return {"Callsign": f"{op}{number}", "Code": op, "Number": number, "AirlineCode": op, "AirportCodes": airports}


AIRLINES = {a["Code"]: a for a in [
    airline("BAW", "BA", "British Airways", positioning=r"9\d\d\d"),
    airline("SHT", "BA", "British Airways Shuttle"),
    airline("XBA", "BA", "Unrelated Charter Co"),
    airline("FCA", "DP", "First Choice Airways"),
    airline("PBD", "DP", "Pobeda"),
    airline("OCN", "4Y", "Discover Airlines"),
    airline("BGA", "4Y", "Airbus Transport International"),
    airline("NOI", "", "No Iata Cargo"),
]}


def tables(routes, newest=None):
    newest = newest if newest is not None else {code: RECENT for code in AIRLINES}
    return flights.build_tables(AIRLINES, AIRPORTS, routes, newest, NOW)


def test_numeric_call_signs_only():
    index, files, stats = tables([route("BAW", "117", "EGLL-KJFK"), route("BAW", "8EP", "EGLL-EGPH")])
    assert files["BA"] == {"117": "LHR-JFK"}
    assert stats["not_numeric"] == 1


def test_airports_are_mapped_to_iata_and_stops_kept():
    _, files, _ = tables([route("BAW", "15", "EGLL-WSSS-YSSY"), route("BAW", "16", "SYD-SIN-LHR")])
    assert files["BA"]["15"] == "LHR-SIN-SYD"
    assert files["BA"]["16"] == "SYD-SIN-LHR"      # the source stored IATA codes itself


def test_route_with_an_airport_without_iata_is_left_out():
    _, files, stats = tables([route("BAW", "1", "EGLL-XXNO"), route("BAW", "2", "EGLL-QQQQ"),
                              route("BAW", "3", "EGLL-EGLL"), route("BAW", "4", "EGLL-KJFK")])
    assert files["BA"] == {"4": "LHR-JFK"}
    assert stats["airport_without_iata"] == 3


def test_positioning_flights_are_left_out():
    _, files, stats = tables([route("BAW", "9123", "EGLL-KJFK"), route("BAW", "912", "EGLL-KJFK")])
    assert files["BA"] == {"912": "LHR-JFK"}
    assert stats["positioning"] == 1


def test_airline_without_iata_code_is_left_out():
    index, _, stats = tables([route("NOI", "1", "EGLL-KJFK")])
    assert index == {}
    assert stats["no_airline_iata"] == 1


def test_inactive_operator_is_left_out_and_a_reused_code_goes_to_the_live_one():
    routes = [route("FCA", str(n), "EGLL-KJFK") for n in range(1, 20)] + [route("PBD", "5", "EDDF-LFBO")]
    newest = {"FCA": OLD, "PBD": RECENT}
    index, files, stats = tables(routes, newest)
    assert index["DP"]["name"] == "Pobeda"
    assert index["DP"]["icao"] == ["PBD"]
    assert files["DP"] == {"5": "FRA-TLS"}
    assert stats["inactive"] == 19


def test_no_new_call_sign_at_all_means_inactive():
    index, _, _ = tables([route("BAW", "1", "EGLL-KJFK")], newest={})
    assert index == {}


def test_same_brand_operators_merge_and_the_larger_wins_a_shared_number():
    routes = [route("BAW", "1", "EGLL-KJFK"), route("BAW", "2", "KJFK-EGLL"),
              route("SHT", "2", "EGLL-EGPH"), route("SHT", "1306", "EGLL-EGPH")]
    index, files, stats = tables(routes)
    assert index["BA"]["icao"] == ["BAW", "SHT"]
    assert index["BA"]["name"] == "British Airways"
    assert files["BA"] == {"1": "LHR-JFK", "2": "JFK-LHR", "1306": "LHR-EDI"}
    assert stats["same_number_lower_priority"] == 1


def test_other_brand_on_the_same_code_is_not_merged():
    routes = [route("OCN", "1", "EDDF-KJFK"), route("OCN", "2", "KJFK-EDDF"), route("BGA", "77", "LFBO-EDHI"),
              route("XBA", "5", "EGLL-KJFK"), route("BAW", "6", "EGLL-KJFK"), route("BAW", "7", "EGLL-KJFK")]
    index, files, stats = tables(routes)
    assert index["4Y"]["icao"] == ["OCN"]
    assert "77" not in files["4Y"]
    assert index["BA"]["icao"] == ["BAW"]
    assert stats["other_brand"] == 2


def test_numbers_are_sorted_numerically():
    _, files, _ = tables([route("BAW", n, "EGLL-KJFK") for n in ("10", "2", "117", "1")])
    assert list(files["BA"]) == ["1", "2", "10", "117"]


@pytest.mark.parametrize("name_a,name_b,same", [
    ("British Airways", "British Airways Shuttle", True),
    ("Qantas", "QantasLink", True),
    ("Air China", "Air China Cargo", True),
    ("Air China", "Dalian Airlines", False),
    ("Discover Airlines", "Airbus Transport International", False),
    ("Lufthansa", "Lufthansa Cargo", True),
    ("Air Canada", "Air India", False),
])
def test_same_brand(name_a, name_b, same):
    assert flights.same_brand(name_a, name_b) is same


GIT_LOG = """DATE 2024-11-01T04:49:00+00:00
diff --git a/routes/schema-01/B/BAW-all.csv b/routes/schema-01/B/BAW-all.csv
+++ b/routes/schema-01/B/BAW-all.csv
@@ -0,0 +1,3 @@
+﻿Callsign,Code,Number,AirlineCode,AirportCodes
+BAW1,BAW,1,BAW,EGLL-KJFK
+FCA1,FCA,1,FCA,EGLL-KJFK
DATE 2025-03-02T04:49:00+00:00
@@ -2 +2 @@
-BAW1,BAW,1,BAW,EGLL-KJFK
+BAW1,BAW,1,BAW,EGLL-KEWR
+FCA1,FCA,1,FCA,EGLL-KJFF
DATE 2026-10-05T04:49:00+00:00
@@ -3,0 +4 @@
+BAW117,BAW,117,BAW,EGLL-KJFK
+PBD5,PBD,5,PBD,EDDF-LFBO
"""


def test_first_new_dates():
    newest = flights.first_new_dates(GIT_LOG.splitlines(keepends=True))
    # The oldest commit is the starting state; a changed route is not a new call sign.
    assert newest == {"BAW": "2026-10-05", "PBD": "2026-10-05"}


@pytest.mark.parametrize("text,parsed", [
    ("BA 117", ("BA", "117", "iata")),
    ("ba0117", ("BA", "117", "iata")),
    ("BA-117", ("BA", "117", "iata")),
    ("U2 8001", ("U2", "8001", "iata")),
    ("B61", ("B6", "1", "iata")),
    ("6E 123", ("6E", "123", "iata")),
    ("BAW117", ("BAW", "117", "icao")),
    ("AA100A", ("AA", "100", "iata")),
    ("AF 0000", ("AF", "0", "iata")),
    ("117", None),
    ("11 7", None),
    ("BA", None),
    ("BA12345", None),
    ("LHR JFK", None),
    ("", None),
])
def test_parse(text, parsed):
    assert flights.parse(text) == parsed


def make_source(path, routes_csv):
    """A tiny git repository shaped like the source, with two commits."""
    (path / "airlines/schema-01").mkdir(parents=True)
    (path / "airports/schema-01/E").mkdir(parents=True)
    (path / "routes/schema-01/B").mkdir(parents=True)
    (path / "airlines/schema-01/airlines.csv").write_text(
        "﻿Code,Name,ICAO,IATA,PositioningFlightPattern,CharterFlightPattern\n"
        "BAW,British Airways,BAW,BA,,\n", encoding="utf-8")
    (path / "airports/schema-01/E/EG.csv").write_text(
        "﻿Code,Name,ICAO,IATA,Location,CountryISO2,Latitude,Longitude,AltitudeFeet\n"
        "EGLL,Heathrow,EGLL,LHR,London,GB,51.47,-0.46,83\n"
        "KJFK,Kennedy,KJFK,JFK,New York,US,40.64,-73.78,13\n", encoding="utf-8")
    csv_path = path / "routes/schema-01/B/BAW-all.csv"
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    def commit(text, date):
        csv_path.write_text("﻿Callsign,Code,Number,AirlineCode,AirportCodes\n" + text, encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=path, check=True)
        subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "data"], cwd=path, check=True,
                       env={**env, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date, "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"})

    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    commit("BAW1,BAW,1,BAW,EGLL-KJFK\n", "2025-01-01T00:00:00Z")
    commit("BAW1,BAW,1,BAW,EGLL-KJFK\n" + routes_csv, "2026-11-01T00:00:00Z")


def test_build_writes_the_files_and_the_reference_lookup_reads_them(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    make_source(src, "BAW117,BAW,117,BAW,EGLL-KJFK\nBAW112,BAW,112,BAW,KJFK-EGLL\n")
    monkeypatch.setattr(flights, "MIN_FLIGHTS", 1)
    site = tmp_path / "site"
    (site / "flights/v1").mkdir(parents=True)
    (site / "flights/v1/ZZ.json").write_text("{}")          # an airline that left: removed
    changed, problems = flights.build(site, NOW, source_dir=src)
    assert (changed, problems) == (True, [])
    out = site / "flights/v1"
    index = json.loads((out / "index.json").read_text())
    assert index["version"] == 1 and index["flights"] == 3
    assert index["source"]["license"] == "CC0-1.0"
    assert len(index["source"]["commit"]) == 40
    assert index["airlines"]["BA"]["file"] == "BA.json"
    assert not (out / "ZZ.json").exists()
    ba = json.loads((out / "BA.json").read_text())
    assert ba["flights"] == {"1": "LHR-JFK", "112": "JFK-LHR", "117": "LHR-JFK"}
    read = lambda f: json.loads((out / f).read_text())  # noqa: E731
    assert flights.lookup(index, read, "BA 117") == ("BA", "British Airways", "117", ["LHR", "JFK"])
    assert flights.lookup(index, read, "baw0112") == ("BA", "British Airways", "112", ["JFK", "LHR"])
    assert flights.lookup(index, read, "BA 999") is None
    assert flights.lookup(index, read, "XX 1") is None


def test_a_table_that_shrinks_by_half_is_not_published(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    make_source(src, "BAW117,BAW,117,BAW,EGLL-KJFK\n")
    monkeypatch.setattr(flights, "MIN_FLIGHTS", 1)
    out = tmp_path / "site/flights/v1"
    out.mkdir(parents=True)
    (out / "index.json").write_text(json.dumps({"flights": 100, "builder": flights.BUILDER, "source": {}}))
    changed, problems = flights.build(tmp_path / "site", NOW, source_dir=src)
    assert changed is False
    assert "under half" in problems[0]["error"]
    assert json.loads((out / "index.json").read_text())["flights"] == 100


def test_too_few_flights_is_not_published(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    make_source(src, "")
    changed, problems = flights.build(tmp_path / "site", NOW, source_dir=src)
    assert changed is False and "only" in problems[0]["error"]
    assert not (tmp_path / "site/flights/v1/index.json").exists()


def test_nothing_to_do_when_the_source_has_not_moved_today(tmp_path):
    out = tmp_path / "site/flights/v1"
    out.mkdir(parents=True)
    sha = "a" * 40
    prev = {"flights": 5, "builder": flights.BUILDER, "generated": "2026-11-27T06:00:00Z", "source": {"commit": sha}}
    (out / "index.json").write_text(json.dumps(prev))
    assert flights.build(tmp_path / "site", NOW, head=lambda: sha) == (False, [])
    assert flights.current(prev, sha, NOW)
    assert not flights.current(prev, "b" * 40, NOW)                       # the source moved
    assert not flights.current({**prev, "builder": 0}, sha, NOW)          # the rules changed
    assert not flights.current({**prev, "generated": "2026-11-26T12:00:00Z"}, sha, NOW)   # a day old


def test_check_published_index():
    good = {"version": 1, "generated": "2026-11-26T12:00:00Z", "flights": 300000, "airlines": {"BA": {}}}
    assert flights.check(good, NOW) == []
    assert "stale" in flights.check({**good, "generated": "2026-11-23T12:00:00Z"}, NOW)[0]
    assert "flights" in flights.check({**good, "flights": 10}, NOW)[0]
    assert flights.check({"version": 2}, NOW)


def test_a_source_failure_keeps_the_table(tmp_path):
    def fail():
        raise flights.SourceError("network down")
    changed, problems = flights.build(tmp_path / "site", NOW, head=fail)
    assert changed is False and problems[0]["error"] == "network down"
