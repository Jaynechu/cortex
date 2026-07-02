from __future__ import annotations

from cortex.collectors import geofence


def test_parse_lines_matches_real_sample_format():
    text = "Arrived home \n20:11 arrived: home\n"
    entries = geofence.parse_lines(text)
    assert entries == [("20:11", "arrived: home", "20:11 arrived: home")]


def test_parse_lines_handles_single_digit_hour():
    entries = geofence.parse_lines("9:05 left: home\n")
    assert entries == [("09:05", "left: home", "9:05 left: home")]


def test_collect_disabled_is_noop(marrow_conn, base_cfg):
    geofence.collect(marrow_conn, base_cfg)  # enabled=False by default
    count = marrow_conn.execute("SELECT COUNT(*) c FROM ct_geofence").fetchone()["c"]
    assert count == 0


def test_collect_ingests_new_lines_and_tracks_cursor(tmp_path, marrow_conn, base_cfg):
    geo_path = tmp_path / "location_log.txt"
    geo_path.write_text("Arrived home \n20:11 arrived: home\n")

    cfg = dict(base_cfg)
    cfg["paths"] = dict(base_cfg["paths"])
    cfg["paths"]["geofence_file"] = str(geo_path)
    cfg["geofence"] = {"enabled": True}

    geofence.collect(marrow_conn, cfg)
    rows = marrow_conn.execute("SELECT time, event FROM ct_geofence").fetchall()
    assert len(rows) == 1
    assert rows[0]["time"] == "20:11"
    assert rows[0]["event"] == "arrived: home"

    # rerun with no new bytes -> no duplicate, no error
    geofence.collect(marrow_conn, cfg)
    rows = marrow_conn.execute("SELECT time, event FROM ct_geofence").fetchall()
    assert len(rows) == 1

    # append a new line -> only the new line is ingested
    with geo_path.open("a") as f:
        f.write("21:30 left: home\n")
    geofence.collect(marrow_conn, cfg)
    rows = marrow_conn.execute("SELECT time, event FROM ct_geofence ORDER BY time").fetchall()
    assert [(r["time"], r["event"]) for r in rows] == [
        ("20:11", "arrived: home"),
        ("21:30", "left: home"),
    ]


def test_collect_raises_when_enabled_but_file_missing(marrow_conn, base_cfg, tmp_path):
    cfg = dict(base_cfg)
    cfg["paths"] = dict(base_cfg["paths"])
    cfg["paths"]["geofence_file"] = str(tmp_path / "missing.txt")
    cfg["geofence"] = {"enabled": True}
    try:
        geofence.collect(marrow_conn, cfg)
        assert False, "expected FileNotFoundError"
    except FileNotFoundError:
        pass
