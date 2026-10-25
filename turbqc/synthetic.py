"""Synthetic but realistic report stream, used for tests, the offline demo and as the
clean baseline for fault injection. Shapes follow the live feed: North Atlantic AIREPs
relayed by three ground networks plus CONUS PIREPs around moving turbulence patches."""
import hashlib
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from . import config
from .feed import add_derived
from .schema import REPORT_COLS

OPERATORS = ["BAW", "DAL", "UAL", "AAL", "DLH", "AFR", "KLM", "VIR", "ACA", "WJA", "IBE", "SWR", "EIN", "TAP", "ICE"]
RELAYS = {"DDL": 0.55, "QXT": 0.28, "QXS": 0.17}
TYPES = ["B738", "B38M", "A320", "A321", "E75L", "CRJ9", "B739", "A20N", "B772", "B789", "C172", "PC12"]
HUBS = [(41.98, -87.90), (33.64, -84.43), (32.90, -97.04), (39.86, -104.67)]
STATIONS = ["DEN", "ORD", "ATL", "DFW", "LAX", "JFK", "SEA", "MSP", "SLC", "PHX", "IAH", "BOS"]
PIREP_INTENSITY_IN = (["MOD", "LGT-MOD", "SEV", "LGT"], [0.5, 0.25, 0.05, 0.2])
PIREP_INTENSITY_OUT = (["NEG", "LGT", "SMTH-LGT", "LGT-MOD"], [0.45, 0.4, 0.05, 0.1])


def _fmt_pos(lat: float, lon: float) -> str:
    def dm(v, width):
        a = abs(v)
        d = int(a)
        m = int(round((a - d) * 60))
        if m == 60:
            d, m = d + 1, 0
        return f"{d:0{width}d}{m:02d}"
    return f"{dm(lat, 2)}{'N' if lat >= 0 else 'S'}{dm(lon, 3)}{'E' if lon >= 0 else 'W'}"


def _round_minute_deg(v: np.ndarray) -> np.ndarray:
    return np.round(v * 60) / 60


def _diurnal(hours: np.ndarray, peak_utc: float, amplitude: float) -> np.ndarray:
    return 1 + amplitude * np.cos(2 * np.pi * (hours - peak_utc) / 24)


def _aireps(rng, start, end, flights_per_hour) -> pd.DataFrame:
    rows = []
    t = start - pd.Timedelta(hours=5)
    flight_no = 0
    while t < end:
        rate = flights_per_hour * _diurnal(np.array([t.hour]), 2, 0.35)[0]
        t += pd.Timedelta(minutes=float(rng.exponential(60 / rate)))
        flight_no += 1
        op = rng.choice(OPERATORS)
        callsign = f"{op}{flight_no}{rng.choice(['', 'A', 'K', ''])}"
        relay = rng.choice(list(RELAYS), p=list(RELAYS.values()))
        reg = "".join(rng.choice(list("ABCDEFGHJKLMNPRSTUVWXYZ"), 5))
        eastbound = rng.random() < 0.5
        lat0 = rng.uniform(45, 61)
        lon = -58.0 if eastbound else -12.0
        lat = lat0
        fl = int(rng.choice([330, 340, 350, 360, 370, 380, 390, 400]))
        gs_kt = rng.normal(480 if eastbound else 440, 25)
        heading_lat = rng.normal(0, 0.003)
        obs = t
        while obs < t + pd.Timedelta(hours=5) and -60 <= lon <= -10:
            if obs >= start - pd.Timedelta(minutes=10) and obs < end:
                rows.append((obs, callsign, op, relay, reg, lat, lon, fl))
            step = float(rng.normal(12, 2.5))
            nm = gs_kt * step / 60
            lon += (1 if eastbound else -1) * nm / (60 * np.cos(np.radians(lat)))
            lat += heading_lat * step
            if rng.random() < 0.04:
                fl += 20 if fl < 400 else -20
            obs = obs + pd.Timedelta(minutes=step)
    df = pd.DataFrame(rows, columns=["obs_time", "aircraft_ref", "operator", "relay", "reg", "lat", "lon", "fl"])
    df["obs_time"] = df["obs_time"].dt.floor("min")
    df["lat"] = _round_minute_deg(df["lat"].to_numpy())
    df["lon"] = _round_minute_deg(df["lon"].to_numpy())
    n = len(df)
    df["alt_ft"] = df["fl"] * 100
    df["temp_c"] = np.round(-56 + (400 - df["fl"]) * 0.2 + rng.normal(0, 3, n))
    df["wind_dir_deg"] = np.round(rng.normal(270, 20, n)) % 360
    df["wind_speed_kt"] = np.clip(np.round(rng.normal(70, 25, n)), 5, 200)
    df["tb_intensity"] = np.where(rng.random(n) < 0.02, "LGT", None)
    df["report_type"] = "AIREP"
    df["stream"] = "acars_airep"
    df["latency_min"] = rng.lognormal(np.log(3.5), 0.5, n)
    nxt = df["obs_time"] + pd.Timedelta(minutes=25)
    df["raw_text"] = [
        f"ARP {r.aircraft_ref} {_fmt_pos(r.lat, r.lon)} {r.obs_time:%H%M} F{r.fl:03d} "
        f"{_fmt_pos(r.lat, r.lon + 4)} {nt:%H%M} NXTWP "
        f"{'MS' if r.temp_c < 0 else 'PS'}{abs(int(r.temp_c)):02d} {int(r.wind_dir_deg):03d}/{int(r.wind_speed_kt)} KT "
        f"{r.reg} {r.relay} GWY {r.obs_time:%d%H%M} A01A"
        + (" TB LGT" if r.tb_intensity else "")
        for r, nt in zip(df.itertuples(), nxt)]
    return df.drop(columns=["operator", "relay", "reg", "fl"])


def _pireps(rng, start, end, per_hour) -> pd.DataFrame:
    hours = pd.date_range(start, end, freq="h", inclusive="left")
    counts = rng.poisson(per_hour * _diurnal(hours.hour.to_numpy(), 20, 0.6))
    obs = np.concatenate([
        (h + pd.to_timedelta(rng.uniform(0, 60, c), unit="min")).floor("min").to_numpy()
        for h, c in zip(hours, counts)]) if counts.sum() else np.array([], "datetime64[ns]")
    n = len(obs)
    df = pd.DataFrame({"obs_time": pd.to_datetime(obs)})
    lat = rng.uniform(30, 47, n)
    lon = rng.uniform(-120, -75, n)
    alt = np.where(rng.random(n) < 0.6, rng.integers(20, 41, n) * 1000, rng.integers(2, 16, n) * 1000)
    # Four turbulence patches drifting east at ~25 kt. Pilots report more where it is
    # bumpy, so about half of all PIREPs cluster around a patch.
    hours_in = ((df["obs_time"] - start).dt.total_seconds() / 3600).to_numpy()
    patches = [(rng.uniform(34, 44), rng.uniform(-115, -95), rng.choice([8000, 34000, 36000])) for _ in range(4)]
    centre = lambda k: (patches[k][0], np.clip(patches[k][1] + hours_in * 0.5, -120, -75), patches[k][2])
    which = rng.integers(0, 4, n)
    near = rng.random(n) < 0.5
    for k in range(4):
        m = near & (which == k)
        clat, clon, calt = centre(k)
        lat[m] = clat + rng.normal(0, 1.2, m.sum())
        lon[m] = clon[m] + rng.normal(0, 1.5, m.sum())
        alt[m] = np.round((calt + rng.normal(0, 2000, m.sum())) / 1000) * 1000
    # A third of the rest are climb/descent reports around hub airports: calm, dense airspace.
    hub = ~near & (rng.random(n) < 0.35)
    hub_idx = rng.integers(0, len(HUBS), n)
    for k, (hlat, hlon) in enumerate(HUBS):
        m = hub & (hub_idx == k)
        lat[m] = hlat + rng.normal(0, 0.5, m.sum())
        lon[m] = hlon + rng.normal(0, 0.6, m.sum())
        alt[m] = rng.integers(3, 11, m.sum()) * 1000
    df["lat"], df["lon"] = np.round(lat, 4), np.round(lon, 4)
    df["alt_ft"] = np.clip(alt, 1000, 43000).astype(int)
    inside = np.zeros(n, bool)
    for k in range(4):
        clat, clon, calt = centre(k)
        d_km = 111 * np.hypot(df["lat"] - clat, (df["lon"] - clon) * np.cos(np.radians(clat)))
        inside |= (d_km < 250) & (np.abs(df["alt_ft"] - calt) <= 4000)
    din, pin = PIREP_INTENSITY_IN
    dout, pout = PIREP_INTENSITY_OUT
    df["tb_intensity"] = np.where(inside, rng.choice(din, n, p=pin), rng.choice(dout, n, p=pout))
    df["aircraft_ref"] = rng.choice(TYPES, n)
    df["report_type"] = np.where(df["tb_intensity"] == "SEV", "Urgent PIREP", "PIREP")
    df["stream"] = "pirep"
    df["temp_c"] = np.where(rng.random(n) < 0.3, np.round(15 - df["alt_ft"] / 500 + rng.normal(0, 2, n)), np.nan)
    df["wind_dir_deg"] = np.nan
    df["wind_speed_kt"] = np.nan
    df["latency_min"] = rng.lognormal(np.log(5), 0.4, n)
    stn = rng.choice(STATIONS, n)
    df["raw_text"] = [
        f"{s} {'UUA' if rt == 'Urgent PIREP' else 'UA'} /OV {s}{rng.integers(0, 360):03d}{rng.integers(5, 80):03d}"
        f"/TM {o:%H%M}/FL{a // 100:03d}/TP {tp}/TB {tb}"
        for s, rt, o, a, tp, tb in zip(stn, df["report_type"], df["obs_time"], df["alt_ft"], df["aircraft_ref"], df["tb_intensity"])]
    # ~6% report a second turbulence layer (e.g. "LGT ... MOD BLO"), as real PIREPs do
    two = rng.random(n) < 0.06
    df["tb_intensity2"] = np.where(two, rng.choice(["LGT", "MOD"], n, p=[0.6, 0.4]), None)
    df.loc[two, "raw_text"] = df.loc[two, "raw_text"] + " " + df.loc[two, "tb_intensity2"] + " BLO"
    return df


def generate(hours: int = 48, end: pd.Timestamp | None = None, seed: int = 42,
             flights_per_hour: float = 20, pireps_per_hour: float = 60, cfg: dict | None = None) -> pd.DataFrame:
    cfg = cfg or config.settings()
    rng = np.random.default_rng(seed)
    if end is None:
        end = pd.Timestamp(datetime.now(timezone.utc)).tz_localize(None).floor("h")
    start = end - pd.Timedelta(hours=hours)
    df = pd.concat([_aireps(rng, start, end, flights_per_hour), _pireps(rng, start, end, pireps_per_hour)],
                   ignore_index=True)
    df["receipt_time"] = (df["obs_time"] + pd.to_timedelta(df.pop("latency_min"), unit="min")).dt.round("ms")
    df["ingested_at"] = df["receipt_time"] + pd.to_timedelta(rng.uniform(0.5, 5, len(df)), unit="min")
    df = df[(df["receipt_time"] >= start) & (df["receipt_time"] < end)].copy()
    df["run_id"] = "synthetic"
    df["flight_level"] = (df["alt_ft"] / 100).round().astype("Int64")
    df["alt_ft"] = df["alt_ft"].astype("Int64")
    df["tb_intensity2"] = df["tb_intensity2"].astype(object).where(df["tb_intensity2"].notna(), None)
    for col in ["tb_type", "tb_freq"]:
        df[col] = None
    df["tb_base_ft"] = pd.array([pd.NA] * len(df), dtype="Int64")
    df["tb_top_ft"] = pd.array([pd.NA] * len(df), dtype="Int64")
    df["vert_gust_kt"] = np.nan
    for flag in ["flag_bad_location", "flag_no_time_stamp", "flag_mid_point_assumed",
                 "flag_no_flt_lvl", "flag_flt_lvl_range", "flag_agl"]:
        df[flag] = False
    add_derived(df, cfg)
    df["report_id"] = [hashlib.sha1(f"{a}|{o}|{r}|{t}".encode()).hexdigest()
                       for a, o, r, t in zip(df["aircraft_ref"], df["obs_time"], df["receipt_time"], df["raw_text"])]
    return df.sort_values("receipt_time")[REPORT_COLS].reset_index(drop=True)
