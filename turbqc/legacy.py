"""TurbQC "v1": the legacy pipeline, pure pandas, kept only to be reconciled against v2.

It deliberately behaves the way the earlier implementation did, so the migration
exercise has real differences to find and explain:

* timestamps are stored to whole seconds (legacy DATETIME column);
* only the first turbulence group is read (pandas renames the repeated header
  columns and the legacy mapping never used them);
* only the core rule set is implemented;
* the implied-speed check flags every leg above the limit, so the report after a
  one-off spike is flagged as well.

It also has one behaviour that turned out to be *better*: lines whose free-text field
contains an unquoted comma are repaired by re-joining the overflow into the message.
"""
import csv
import hashlib
import io

import numpy as np
import pandas as pd

from . import config
from .feed import OPERATOR_RE, RELAY_RE, STREAMS

# Rules v1 implements, with v2's ids so the reconciliation can pair them up.
V1_RULES = ["R001", "R002", "R003", "R004", "R005", "R010", "R011", "R012", "R013",
            "R017", "R018", "R019", "R020", "R030", "R032", "R040", "R050"]

KEY_FIELDS = ["report_type", "aircraft_ref", "receipt_time", "observation_time",
              "latitude", "longitude", "altitude_ft_msl", "raw_text"]


def parse_v1(text: str, cfg: dict | None = None) -> pd.DataFrame:
    cfg = cfg or config.settings()
    rows = list(csv.reader(io.StringIO(text)))
    header, body = rows[0], []
    for r in rows[1:]:
        if not r:
            continue
        if len(r) > len(header):              # unquoted comma in the last (free-text) field
            r = r[:len(header) - 1] + [",".join(r[len(header) - 1:])]
        if len(r) == len(header):
            body.append(r)
    # Same as pd.read_csv: repeated column names become name.1, name.2 ... and are never used.
    seen, cols = {}, []
    for c in header:
        cols.append(c if c not in seen else f"{c}.{seen[c]}")
        seen[c] = seen.get(c, 0) + 1
    raw = pd.DataFrame(body, columns=cols, dtype=object)

    out = pd.DataFrame()
    # The business key agreed for the migration: identical definition in both systems.
    out["report_id"] = raw[KEY_FIELDS].fillna("").astype(str).agg("|".join, axis=1) \
        .map(lambda s: hashlib.sha1(s.encode()).hexdigest())
    for src, dst in (("receipt_time", "receipt_time"), ("observation_time", "obs_time")):
        t = pd.to_datetime(raw[src].replace("", None), utc=True, errors="coerce").dt.tz_localize(None)
        out[dst] = t.dt.floor("s")
    out["report_type"] = raw["report_type"].replace("", None)
    out["stream"] = out["report_type"].map(STREAMS).fillna("unknown")
    out["aircraft_ref"] = raw["aircraft_ref"].str.strip().replace("", None)
    for src, dst in (("latitude", "lat"), ("longitude", "lon"), ("altitude_ft_msl", "alt_ft"),
                     ("temp_c", "temp_c"), ("wind_dir_degrees", "wind_dir_deg"), ("wind_speed_kt", "wind_speed_kt")):
        out[dst] = pd.to_numeric(raw[src], errors="coerce")
    out["tb_intensity"] = raw["turbulence_intensity"].str.strip().str.upper().replace("", None)
    out["raw_text"] = raw["raw_text"].str.strip()
    out["source_id"] = np.where(out["stream"] == "acars_airep",
                                "AIREP-" + out["raw_text"].fillna("").str.extract(RELAY_RE, expand=False).fillna("UNK"),
                                "PIREP")
    op = out["aircraft_ref"].fillna("").str.extract(OPERATOR_RE, expand=False)
    out["operator"] = np.where(out["stream"] == "acars_airep", op.fillna("OTHER"), None)
    m = cfg["edr_mapping"]
    out["edr_band"] = out["tb_intensity"].map(lambda v: m.get(v, {}).get("band") if isinstance(v, str) else None)
    out["edr_est"] = out["tb_intensity"].map(lambda v: m.get(v, {}).get("edr") if isinstance(v, str) else None).astype(float)
    out["severity"] = out["tb_intensity"].map(lambda v: m.get(v, {}).get("severity") if isinstance(v, str) else None).astype(float)
    return out.drop_duplicates("report_id").reset_index(drop=True)


def _hav_nm(lat1, lon1, lat2, lon2):
    p1, p2 = np.radians(lat1), np.radians(lat2)
    a = np.sin((p2 - p1) / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(np.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371.0 * np.arcsin(np.sqrt(a)) / 1.852


def rules_v1(df: pd.DataFrame, cfg: dict | None = None) -> pd.DataFrame:
    """Core rules as pandas masks. Returns one row per (rule_id, report_id) failure."""
    cfg = cfg or config.settings()
    t = cfg["thresholds"]
    both = df["stream"].isin(["acars_airep", "pirep"])
    airep = df["stream"] == "acars_airep"
    latency = (df["receipt_time"] - df["obs_time"]).dt.total_seconds() / 60
    masks = {
        "R001": both & df["receipt_time"].isna(),
        "R002": both & df["obs_time"].isna(),
        "R003": both & (df["aircraft_ref"].isna() | (df["aircraft_ref"].fillna("").str.strip() == "")),
        "R004": both & (df["lat"].isna() | df["lon"].isna()),
        "R005": both & df["alt_ft"].isna(),
        "R010": both & ((df["lat"] < -90) | (df["lat"] > 90)),
        "R011": both & ((df["lon"] < -180) | (df["lon"] > 180)),
        "R012": both & ((df["alt_ft"] < -1500) | (df["alt_ft"] > 60000)),
        "R013": both & (df["lat"] == 0) & (df["lon"] == 0),
        "R017": both & ((df["edr_est"] < 0) | (df["edr_est"] > 1)),
        "R018": both & ((df["temp_c"] < -90) | (df["temp_c"] > 50)),
        "R019": both & ((df["wind_speed_kt"] < 0) | (df["wind_speed_kt"] > 300)),
        "R020": both & ((df["wind_dir_deg"] < 0) | (df["wind_dir_deg"] > 360)),
        "R030": both & (-latency > t["future_tolerance_min"]),
        "R032": both & (latency > t["stale_min"]),
    }
    # R040: later copies of the same observation
    d = df[both & df["obs_time"].notna() & df["aircraft_ref"].notna()].copy()
    d["_k"] = list(zip(d["aircraft_ref"], d["obs_time"], d["lat"].round(2), d["lon"].round(2),
                       d["alt_ft"], d["tb_intensity"].fillna("")))
    d = d.sort_values(["receipt_time", "report_id"])
    masks["R040"] = df["report_id"].isin(d.loc[d.duplicated("_k"), "report_id"])
    # R050: every leg faster than the limit fails the report it arrives at (no blame logic)
    tr = df[airep & df["obs_time"].notna()].sort_values(["aircraft_ref", "obs_time", "receipt_time", "report_id"])
    g = tr.groupby("aircraft_ref")
    dt = (tr["obs_time"] - g["obs_time"].shift()).dt.total_seconds()
    kt = _hav_nm(g["lat"].shift(), g["lon"].shift(), tr["lat"], tr["lon"]) / ((dt + 60) / 3600)
    fast = (dt >= 1) & (dt <= t["track_max_gap_min"] * 60) & (kt > t["max_groundspeed_kt"])
    masks["R050"] = df["report_id"].isin(tr.loc[fast, "report_id"])
    out = [pd.DataFrame({"rule_id": rid, "report_id": df.loc[m.fillna(False).astype(bool), "report_id"]})
           for rid, m in masks.items()]
    return pd.concat(out, ignore_index=True)


def fl_band_v1(alt: pd.Series) -> pd.Series:
    return pd.cut(alt, [-np.inf, 10000, 20000, 30000, 40000, np.inf], right=False,
                  labels=["SFC-FL100", "FL100-200", "FL200-300", "FL300-400", "FL400+"]).astype(str)


def product_v1(df: pd.DataFrame, flags: pd.DataFrame, cfg: dict | None = None) -> pd.DataFrame:
    cfg = cfg or config.settings()
    sev = {r["id"]: r["severity"] for r in config.rules()}
    rejected = set(flags.loc[flags["rule_id"].map(sev) == "error", "report_id"])
    ok = df[~df["report_id"].isin(rejected) & df["edr_est"].notna() & df["obs_time"].notna()
            & df["lat"].notna() & df["lon"].notna() & df["alt_ft"].notna()].copy()
    ok["valid_hour"] = ok["obs_time"].dt.floor("h")
    ok["lat_cell"] = np.floor(ok["lat"]).astype(int)
    ok["lon_cell"] = np.floor(ok["lon"]).astype(int)
    ok["fl_band"] = fl_band_v1(ok["alt_ft"])
    return ok.groupby(["valid_hour", "lat_cell", "lon_cell", "fl_band"], as_index=False) \
        .agg(n_reports=("report_id", "size"), edr_max=("edr_est", "max"))
