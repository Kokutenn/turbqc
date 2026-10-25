"""Parsing the AWC bulk file, schema-drift detection and idempotent ingestion."""
import gzip

import pandas as pd

from turbqc import db, feed, ingest

HEADER = ("receipt_time,observation_time,mid_point_assumed,no_time_stamp,flt_lvl_range,"
          "above_ground_level_indicated,no_flt_lvl,bad_location,aircraft_ref,latitude,longitude,altitude_ft_msl,"
          "sky_cover,cloud_base_ft_msl,cloud_top_ft_msl,sky_cover,cloud_base_ft_msl,cloud_top_ft_msl,"
          "turbulence_type,turbulence_intensity,turbulence_base_ft_msl,turbulence_top_ft_msl,turbulence_freq,"
          "turbulence_type,turbulence_intensity,turbulence_base_ft_msl,turbulence_top_ft_msl,turbulence_freq,"
          "icing_type,icing_intensity,icing_base_ft_msl,icing_top_ft_msl,icing_type,icing_intensity,"
          "icing_base_ft_msl,icing_top_ft_msl,visibility_statute_mi,wx_string,temp_c,wind_dir_degrees,"
          "wind_speed_kt,vert_gust_kt,report_type,raw_text")
N = len(HEADER.split(","))


def line(values: dict) -> str:
    cols = HEADER.split(",")
    out, seen = [""] * N, {}
    for i, c in enumerate(cols):
        seen[c] = seen.get(c, 0) + 1
        key = c if seen[c] == 1 else f"{c}_{seen[c]}"
        if key in values:
            out[i] = str(values[key])
    return ",".join(out)


AIREP = dict(receipt_time="2026-10-24T15:26:19.943Z", observation_time="2026-10-24T15:26:00.000Z",
             aircraft_ref="WJA4117", latitude="60.2833", longitude="-44.2167", altitude_ft_msl="36000",
             temp_c="-43", wind_dir_degrees="266", wind_speed_kt="47", report_type="AIREP",
             raw_text="ARP WJA4117 6017N04413W 1526 F360 5900N05000W 1553 AVUTI MS43 266/47 KT CFHCM QXS UAK 241526 F65A")
PIREP = dict(receipt_time="2026-10-24T15:25:03.104Z", observation_time="2026-10-24T15:22:00.000Z",
             aircraft_ref="E75L", latitude="39.8125", longitude="-104.6607", altitude_ft_msl="6000",
             turbulence_intensity="LGT", turbulence_intensity_2="MOD", turbulence_type="CHOP",
             report_type="PIREP", bad_location="TRUE",
             raw_text="DEN UA /OV DEN/TM 1522/FL060/TP E75L/TB LGT CHOP/RM DURD RY34R DEN")
TEXT = "\n".join([HEADER, line(AIREP), line(PIREP), "too,few,columns"]) + "\n"
NOW = pd.Timestamp("2026-10-24 15:30:00")


def test_parse_normalises_fields():
    res = feed.parse(TEXT, "run1", NOW)
    assert res.rows_in_file == 3 and res.rows_malformed == 1 and not res.missing_columns
    df = res.frame.set_index("aircraft_ref")
    a, p = df.loc["WJA4117"], df.loc["E75L"]
    assert a["stream"] == "acars_airep" and a["source_id"] == "AIREP-QXS" and a["operator"] == "WJA"
    assert a["alt_ft"] == 36000 and a["flight_level"] == 360
    assert a["obs_time"] == pd.Timestamp("2026-10-24 15:26:00")
    assert p["stream"] == "pirep" and p["source_id"] == "PIREP" and pd.isna(p["operator"])
    # the more severe of the two turbulence layers drives the EDR estimate
    assert p["tb_intensity2"] == "MOD" and p["edr_band"] == "MOD" and p["edr_est"] == 0.30
    assert bool(p["flag_bad_location"]) and not bool(a["flag_bad_location"])


def test_report_id_is_stable():
    a = feed.parse(TEXT, "run1", NOW).frame["report_id"].tolist()
    b = feed.parse(TEXT, "run2", NOW + pd.Timedelta(minutes=5)).frame["report_id"].tolist()
    assert a == b and len(set(a)) == 2


def test_schema_drift_is_detected():
    broken = TEXT.replace("altitude_ft_msl", "altitude_m", 1)
    res = feed.parse(broken, "run1", NOW)
    assert "altitude_ft_msl" in res.missing_columns and res.frame.empty


def test_ingest_is_idempotent_and_logged():
    con = db.connect(":memory:")
    payload = gzip.compress(TEXT.encode())
    first = ingest.ingest_payload(con, payload, NOW, "test")
    second = ingest.ingest_payload(con, payload, NOW + pd.Timedelta(minutes=5), "test")
    assert (first["rows_new"], second["rows_new"], second["rows_already_seen"]) == (2, 0, 2)
    assert first["status"] == "ok"
    assert con.execute("SELECT count(*) FROM ingest_runs").fetchall()[0][0] == 2


def test_schema_drift_run_is_logged():
    con = db.connect(":memory:")
    run = ingest.ingest_payload(con, TEXT.replace("raw_text", "message").encode(), NOW, "test")
    assert run["status"] == "schema_drift" and "raw_text" in run["message"]
