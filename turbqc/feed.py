"""Fetch and normalise the AWC aircraft-report bulk file (PIREPs + AIREPs, refreshed every minute)."""
import csv
import gzip
import hashlib
import io
import re
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import requests

from . import config

RELAY_RE = re.compile(r"KT\s+\S+\s+([A-Z]{3})\s+[A-Z0-9]{3}\s+\d{6}")
OPERATOR_RE = re.compile(r"^([A-Z]{3})\d")
STREAMS = {"AIREP": "acars_airep", "PIREP": "pirep", "Urgent PIREP": "pirep"}
TRUE_VALUES = {"TRUE", "T", "1", "Y", "YES"}


@dataclass
class ParseResult:
    frame: pd.DataFrame
    rows_in_file: int
    rows_malformed: int
    missing_columns: list = field(default_factory=list)
    unexpected_columns: list = field(default_factory=list)


def fetch(cfg: dict | None = None) -> tuple[int, bytes]:
    cfg = (cfg or config.settings())["feed"]
    r = requests.get(cfg["bulk_url"], timeout=cfg["timeout_s"],
                     headers={"User-Agent": cfg["user_agent"]})
    r.raise_for_status()
    return r.status_code, r.content


def decode(payload: bytes) -> str:
    if payload[:2] == b"\x1f\x8b":
        payload = gzip.decompress(payload)
    return payload.decode("utf-8", errors="replace")


def _unique_header(header: list[str]) -> list[str]:
    """The feed repeats sky/turbulence/icing column names for the second layer; suffix them."""
    seen, out = {}, []
    for col in header:
        seen[col] = seen.get(col, 0) + 1
        out.append(col if seen[col] == 1 else f"{col}_{seen[col]}")
    return out


def parse(text: str, run_id: str, ingested_at: pd.Timestamp, cfg: dict | None = None) -> ParseResult:
    cfg = cfg or config.settings()
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return ParseResult(pd.DataFrame(), 0, 0, cfg["expected_columns"])
    header = _unique_header(rows[0])
    body = [r for r in rows[1:] if r]
    # DEF-01 (found by the v1/v2 reconciliation): the provider does not quote raw_text, so a
    # comma in a pilot's remarks splits it. raw_text is the last column, so re-join the overflow
    # instead of dropping a valid report. Only lines that cannot be repaired count as malformed.
    if header and header[-1] == "raw_text":
        body = [r if len(r) <= len(header) else r[:len(header) - 1] + [",".join(r[len(header) - 1:])]
                for r in body]
    good = [r for r in body if len(r) == len(header)]
    raw = pd.DataFrame(good, columns=header, dtype=object).replace({"": None})

    missing = [c for c in cfg["expected_columns"] if c not in raw.columns]
    known = {"_".join(c.split("_")[:-1]) if c[-2:] in ("_2", "_3") else c for c in raw.columns}
    unexpected = sorted(known - set(_KNOWN_FEED_COLUMNS))
    result = ParseResult(pd.DataFrame(), len(body), len(body) - len(good), missing, unexpected)
    if missing:
        return result
    result.frame = normalise(raw, run_id, ingested_at, cfg)
    return result


def _col(raw: pd.DataFrame, name: str) -> pd.Series:
    return raw[name] if name in raw.columns else pd.Series([None] * len(raw), index=raw.index, dtype=object)


def _num(raw, name):
    return pd.to_numeric(_col(raw, name), errors="coerce")


def _time(raw, name):
    return pd.to_datetime(_col(raw, name), utc=True, errors="coerce").dt.tz_localize(None)


def _flag(raw, name):
    return _col(raw, name).fillna("").astype(str).str.strip().str.upper().isin(TRUE_VALUES)


def _int(series: pd.Series) -> pd.Series:
    return series.round().astype("Int64")


def normalise(raw: pd.DataFrame, run_id: str, ingested_at: pd.Timestamp, cfg: dict) -> pd.DataFrame:
    out = pd.DataFrame(index=raw.index)
    out["run_id"] = run_id
    out["ingested_at"] = ingested_at
    out["receipt_time"] = _time(raw, "receipt_time")
    out["obs_time"] = _time(raw, "observation_time")
    out["report_type"] = _col(raw, "report_type")
    out["stream"] = out["report_type"].map(STREAMS).fillna("unknown")
    out["aircraft_ref"] = _col(raw, "aircraft_ref").str.strip()
    out["lat"] = _num(raw, "latitude")
    out["lon"] = _num(raw, "longitude")
    out["alt_ft"] = _int(_num(raw, "altitude_ft_msl"))
    out["flight_level"] = _int(out["alt_ft"] / 100)
    out["tb_intensity"] = _col(raw, "turbulence_intensity").str.strip().str.upper()
    out["tb_intensity2"] = _col(raw, "turbulence_intensity_2").str.strip().str.upper()
    out["tb_type"] = _col(raw, "turbulence_type")
    out["tb_freq"] = _col(raw, "turbulence_freq")
    out["tb_base_ft"] = _int(_num(raw, "turbulence_base_ft_msl"))
    out["tb_top_ft"] = _int(_num(raw, "turbulence_top_ft_msl"))
    out["temp_c"] = _num(raw, "temp_c")
    out["wind_dir_deg"] = _num(raw, "wind_dir_degrees")
    out["wind_speed_kt"] = _num(raw, "wind_speed_kt")
    out["vert_gust_kt"] = _num(raw, "vert_gust_kt")
    out["flag_bad_location"] = _flag(raw, "bad_location")
    out["flag_no_time_stamp"] = _flag(raw, "no_time_stamp")
    out["flag_mid_point_assumed"] = _flag(raw, "mid_point_assumed")
    out["flag_no_flt_lvl"] = _flag(raw, "no_flt_lvl")
    out["flag_flt_lvl_range"] = _flag(raw, "flt_lvl_range")
    out["flag_agl"] = _flag(raw, "above_ground_level_indicated")
    out["raw_text"] = _col(raw, "raw_text").str.strip()
    add_derived(out, cfg)

    key_cols = ["report_type", "aircraft_ref", "receipt_time", "observation_time",
                "latitude", "longitude", "altitude_ft_msl", "raw_text"]
    keys = raw.reindex(columns=key_cols).fillna("").astype(str).agg("|".join, axis=1)
    out["report_id"] = keys.map(lambda s: hashlib.sha1(s.encode()).hexdigest())
    return out.reset_index(drop=True)


def add_derived(df: pd.DataFrame, cfg: dict) -> None:
    """Source, operator and EDR columns. Shared with the synthetic generator."""
    raw = df["raw_text"].fillna("")
    relay = raw.str.extract(RELAY_RE, expand=False)
    df["source_id"] = np.where(df["stream"] == "acars_airep", "AIREP-" + relay.fillna("UNK"), "PIREP")
    op = df["aircraft_ref"].fillna("").str.extract(OPERATOR_RE, expand=False)
    df["operator"] = np.where(df["stream"] == "acars_airep", op.fillna("OTHER"), None)

    mapping = cfg["edr_mapping"]
    sev1 = df["tb_intensity"].map(lambda v: mapping.get(v, {}).get("severity") if isinstance(v, str) else None)
    sev2 = df["tb_intensity2"].map(lambda v: mapping.get(v, {}).get("severity") if isinstance(v, str) else None)
    worst = df["tb_intensity"].where(sev1.fillna(-1) >= sev2.fillna(-1), df["tb_intensity2"])
    df["edr_band"] = worst.map(lambda v: mapping.get(v, {}).get("band") if isinstance(v, str) else None)
    df["edr_est"] = worst.map(lambda v: mapping.get(v, {}).get("edr") if isinstance(v, str) else None).astype(float)
    df["severity"] = worst.map(lambda v: mapping.get(v, {}).get("severity") if isinstance(v, str) else None).astype(float)


FEED_HEADER = [  # the bulk file's header, repeated layer columns included
    "receipt_time", "observation_time", "mid_point_assumed", "no_time_stamp", "flt_lvl_range",
    "above_ground_level_indicated", "no_flt_lvl", "bad_location", "aircraft_ref", "latitude", "longitude",
    "altitude_ft_msl", "sky_cover", "cloud_base_ft_msl", "cloud_top_ft_msl", "sky_cover", "cloud_base_ft_msl",
    "cloud_top_ft_msl", "turbulence_type", "turbulence_intensity", "turbulence_base_ft_msl",
    "turbulence_top_ft_msl", "turbulence_freq", "turbulence_type", "turbulence_intensity",
    "turbulence_base_ft_msl", "turbulence_top_ft_msl", "turbulence_freq", "icing_type", "icing_intensity",
    "icing_base_ft_msl", "icing_top_ft_msl", "icing_type", "icing_intensity", "icing_base_ft_msl",
    "icing_top_ft_msl", "visibility_statute_mi", "wx_string", "temp_c", "wind_dir_degrees", "wind_speed_kt",
    "vert_gust_kt", "report_type", "raw_text"]


def to_feed_csv(df: pd.DataFrame) -> str:
    """Write normalised reports back out in the provider's CSV layout (synthetic replay)."""
    def ts(s):
        return s.dt.strftime("%Y-%m-%dT%H:%M:%S.%f").str[:-3].add("Z").where(s.notna(), "")

    def num(s, fmt=None):
        s = pd.to_numeric(s, errors="coerce").astype(float)
        out = s.map(lambda v: "" if pd.isna(v) else (fmt.format(v) if fmt else (str(int(v)) if v == int(v) else str(v))))
        return out

    def txt(s):
        return s.astype(object).where(s.notna(), "").astype(str)

    flag = lambda s: s.fillna(False).astype(bool).map({True: "TRUE", False: ""})
    cols = {name: pd.Series("", index=df.index) for name in dict.fromkeys(FEED_HEADER)}
    cols.update(receipt_time=ts(df["receipt_time"]), observation_time=ts(df["obs_time"]),
                mid_point_assumed=flag(df["flag_mid_point_assumed"]), no_time_stamp=flag(df["flag_no_time_stamp"]),
                flt_lvl_range=flag(df["flag_flt_lvl_range"]), above_ground_level_indicated=flag(df["flag_agl"]),
                no_flt_lvl=flag(df["flag_no_flt_lvl"]), bad_location=flag(df["flag_bad_location"]),
                aircraft_ref=txt(df["aircraft_ref"]), latitude=num(df["lat"], "{:.4f}"),
                longitude=num(df["lon"], "{:.4f}"), altitude_ft_msl=num(df["alt_ft"]),
                temp_c=num(df["temp_c"]), wind_dir_degrees=num(df["wind_dir_deg"]),
                wind_speed_kt=num(df["wind_speed_kt"]), vert_gust_kt=num(df["vert_gust_kt"]),
                report_type=txt(df["report_type"]), raw_text=txt(df["raw_text"]))
    layer = {"turbulence_type": "tb_type", "turbulence_intensity": "tb_intensity", "turbulence_freq": "tb_freq",
             "turbulence_base_ft_msl": "tb_base_ft", "turbulence_top_ft_msl": "tb_top_ft"}
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(FEED_HEADER)
    seen = {}
    series = []
    for name in FEED_HEADER:
        seen[name] = seen.get(name, 0) + 1
        if name in layer and seen[name] == 1:
            src = df[layer[name]]
            series.append(num(src) if name.endswith("_msl") else txt(src))
        elif name == "turbulence_intensity" and seen[name] == 2:
            series.append(txt(df["tb_intensity2"]))
        elif seen[name] == 1:
            series.append(cols[name])
        else:
            series.append(pd.Series("", index=df.index))
    w.writerows(zip(*series))
    return buf.getvalue()


_KNOWN_FEED_COLUMNS = [
    "receipt_time", "observation_time", "mid_point_assumed", "no_time_stamp", "flt_lvl_range",
    "above_ground_level_indicated", "no_flt_lvl", "bad_location", "aircraft_ref", "latitude",
    "longitude", "altitude_ft_msl", "sky_cover", "cloud_base_ft_msl", "cloud_top_ft_msl",
    "turbulence_type", "turbulence_intensity", "turbulence_base_ft_msl", "turbulence_top_ft_msl",
    "turbulence_freq", "icing_type", "icing_intensity", "icing_base_ft_msl", "icing_top_ft_msl",
    "visibility_statute_mi", "wx_string", "temp_c", "wind_dir_degrees", "wind_speed_kt",
    "vert_gust_kt", "report_type", "raw_text",
]
