import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from turbqc import config, synthetic  # noqa: E402
from turbqc.feed import add_derived  # noqa: E402
from turbqc.schema import REPORT_COLS  # noqa: E402

T0 = pd.Timestamp("2026-01-15 12:00:00")
STEP_MIN = 6          # >= the stuck-position threshold so R052 can be exercised
GS_KT = 480


def _row(i, **kw):
    r = {c: None for c in REPORT_COLS}
    r.update(report_id=f"r{i}", run_id="test", stream="acars_airep", report_type="AIREP",
             aircraft_ref="TST123", alt_ft=35000, temp_c=-50.0, wind_dir_deg=270.0, wind_speed_kt=60.0,
             flag_bad_location=False, flag_no_time_stamp=False, flag_mid_point_assumed=False,
             flag_no_flt_lvl=False, flag_flt_lvl_range=False, flag_agl=False)
    r.update(kw)
    return r


def airep_text(callsign, lat, lon, obs, fl, relay="DDL"):
    return (f"ARP {callsign} {synthetic._fmt_pos(lat, lon)} {obs:%H%M} F{fl:03d} NXTWP {obs:%H%M} FIX "
            f"MS50 270/60 KT GABCD {relay} GWY {obs:%d%H%M} A01A")


def make_track(n=6) -> pd.DataFrame:
    """A clean AIREP track: one callsign eastbound at 480 kt, reports every 6 minutes."""
    rows = []
    lat, lon = 55.0, -40.0
    for i in range(n):
        obs = T0 + pd.Timedelta(minutes=STEP_MIN * i)
        la, lo = round(lat * 60) / 60, round(lon * 60) / 60
        rows.append(_row(i, obs_time=obs, receipt_time=obs + pd.Timedelta(minutes=3),
                         ingested_at=obs + pd.Timedelta(minutes=4), lat=la, lon=lo,
                         raw_text=airep_text("TST123", la, lo, obs, 350)))
        lon += GS_KT * STEP_MIN / 60 / (60 * np.cos(np.radians(lat)))
    return finish(pd.DataFrame(rows))


def pirep(i, lat, lon, alt, intensity, minute):
    obs = T0 + pd.Timedelta(minutes=minute)
    return _row(i, report_id=f"p{i}", stream="pirep", report_type="PIREP", aircraft_ref="B738",
                obs_time=obs, receipt_time=obs + pd.Timedelta(minutes=4), ingested_at=obs + pd.Timedelta(minutes=5),
                lat=lat, lon=lon, alt_ft=alt, temp_c=None, wind_dir_deg=None, wind_speed_kt=None,
                tb_intensity=intensity, raw_text=f"DEN UA /OV DEN/TM {obs:%H%M}/FL{alt // 100:03d}/TP B738/TB {intensity}")


def calm_cluster(target_intensity=None) -> pd.DataFrame:
    """Four calm PIREPs within ~20 km and 10 min; optionally a fifth 'target' report."""
    rows = [pirep(k, 39.80 + 0.05 * k, -104.60 + 0.05 * k, 30000 + 1000 * (k % 2), inten, 2 * k)
            for k, inten in enumerate(["NEG", "LGT", "LGT", "NEG"])]
    if target_intensity:
        t = pirep(9, 39.85, -104.55, 30000, target_intensity, 4)
        t["report_id"] = "target"
        rows.append(t)
    return finish(pd.DataFrame(rows))


def finish(df: pd.DataFrame) -> pd.DataFrame:
    for c in ["obs_time", "receipt_time", "ingested_at"]:
        df[c] = pd.to_datetime(df[c])
    for c in ["lat", "lon", "temp_c", "wind_dir_deg", "wind_speed_kt", "vert_gust_kt", "edr_est", "severity"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").astype(float)
    for c in ["alt_ft", "tb_base_ft", "tb_top_ft"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").astype("Int64")
    df["flight_level"] = (df["alt_ft"].astype("Float64") / 100).round().astype("Int64")
    add_derived(df, config.settings())
    return df[REPORT_COLS]


@pytest.fixture
def base_track():
    return make_track()


@pytest.fixture(scope="session")
def synthetic_48h():
    return synthetic.generate(hours=48, seed=42, end=pd.Timestamp("2026-01-20 00:00"))
