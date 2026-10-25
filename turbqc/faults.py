"""Fault injection: corrupt a copy of the data in known ways, rerun every check, and
measure what the monitor catches (recall) and what it wrongly raises (false alarms).

Row-level faults have a ground-truth set of report_ids. Event-level faults (a source
going dark, a latency spike) have a ground-truth (source, time window) and count as
detected when a matching alert overlaps the window.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

import numpy as np
import pandas as pd

from . import config, db, pipeline, rules
from .feed import add_derived

ROW, EVENT = "row", "event"
EVENT_KINDS = {"dropout", "volume_low", "volume_high", "gap", "latency_p95", "flatline"}


@dataclass
class Fault:
    name: str
    level: str
    description: str
    apply: Callable  # (df, rng, ctx) -> (df, truth) ; truth = set of ids or (source, start, end)


def _sources(df, stream="acars_airep"):
    return df.loc[df["stream"] == stream, "source_id"].value_counts().index.tolist()


def _window(df, start_frac, hours):
    t0, t1 = df["receipt_time"].min(), df["receipt_time"].max()
    start = (t0 + (t1 - t0) * start_frac).floor("h")
    return start, start + pd.Timedelta(hours=hours)


def _in(df, source, start, end):
    return (df["source_id"] == source) & (df["receipt_time"] >= start) & (df["receipt_time"] < end)


def clock_drift_ahead(df, rng, ctx):
    src = _sources(df)[0]
    start, end = _window(df, 0.30, 3)
    m = _in(df, src, start, end)
    df.loc[m, "obs_time"] += pd.Timedelta(minutes=20)
    return df, set(df.loc[m, "report_id"])


def clock_drift_subtle(df, rng, ctx):
    src = _sources(df)[1]
    start, end = _window(df, 0.45, 2)
    m = _in(df, src, start, end)
    df.loc[m, "obs_time"] -= pd.Timedelta(minutes=6)
    return df, set(df.loc[m, "report_id"])


def source_dark(df, rng, ctx):
    src = _sources(df)[1]
    start, end = _window(df, 0.60, 2)
    return df[~_in(df, src, start, end)].copy(), (src, start, end)


def duplicate_feed(df, rng, ctx):
    src = _sources(df)[2]
    start, end = _window(df, 0.50, 1)
    dup = df[_in(df, src, start, end)].copy()
    for col in ("receipt_time", "ingested_at"):
        dup[col] += pd.Timedelta(minutes=2)
    dup["report_id"] = dup["report_id"] + ":dup"
    return pd.concat([df, dup], ignore_index=True), set(dup["report_id"])


def stuck_position(df, rng, ctx):
    a = df[df["stream"] == "acars_airep"]
    sizes = a.groupby("aircraft_ref").size()
    tracks = rng.choice(sizes[sizes >= 8].index, size=min(8, (sizes >= 8).sum()), replace=False)
    truth = set()
    for t in tracks:
        idx = df.index[df["aircraft_ref"] == t]
        idx = df.loc[idx].sort_values("obs_time").index
        frozen = idx[3:]
        df.loc[frozen, ["lat", "lon"]] = df.loc[idx[2], ["lat", "lon"]].to_numpy()
        truth |= set(df.loc[frozen, "report_id"])
    return df, truth


def position_jump(df, rng, ctx):
    a = df[df["stream"] == "acars_airep"]
    sizes = a.groupby("aircraft_ref").size()
    tracks = rng.choice(sizes[sizes >= 6].index, size=min(15, (sizes >= 6).sum()), replace=False)
    truth = set()
    for t in tracks:
        idx = df[df["aircraft_ref"] == t].sort_values("obs_time").index
        i = idx[len(idx) // 2]
        df.loc[i, "lat"] += 3.0
        truth.add(df.loc[i, "report_id"])
    return df, truth


def altitude_in_metres(df, rng, ctx):
    src = _sources(df)[0]
    start, end = _window(df, 0.70, 1)
    m = _in(df, src, start, end)
    df.loc[m, "alt_ft"] = (df.loc[m, "alt_ft"].astype(float) * 0.3048).round().astype("Int64")
    return df, set(df.loc[m, "report_id"])


def latency_spike(df, rng, ctx):
    src = _sources(df)[0]
    start, end = _window(df, 0.20, 2)
    m = _in(df, src, start, end)
    for col in ("receipt_time", "ingested_at"):
        df.loc[m, col] += pd.Timedelta(minutes=40)
    return df, (src, start, end + pd.Timedelta(minutes=40))


def flatline_temperature(df, rng, ctx):
    src = _sources(df)[1]
    start, end = _window(df, 0.80, 2)
    df.loc[_in(df, src, start, end), "temp_c"] = -45.0
    return df, (src, start, end)


def turbulence_misreport(df, rng, ctx):
    """Turn PIREPs from calm, well-observed airspace into SEV reports. Picks are kept at
    least 3 h apart so they cannot corroborate each other."""
    s = ctx["spatial"]
    calm = s[(s["severity"] <= 0.5) & (s["nb_median"] <= 1) & (s["n_neighbours"] >= 2)]
    calm = df[df["report_id"].isin(calm["report_id"])].sample(frac=1, random_state=int(rng.integers(1 << 31)))
    pick, used = [], []
    for r in calm.itertuples():
        if all(abs((r.obs_time - t).total_seconds()) > 3 * 3600 for t in used):
            pick.append(r.report_id)
            used.append(r.obs_time)
        if len(pick) == 10:
            break
    m = df["report_id"].isin(pick)
    df.loc[m, "tb_intensity"] = "SEV"
    df.loc[m, "raw_text"] = df.loc[m, "raw_text"].str.replace(r"/TB .*$", "/TB SEV", regex=True)
    add_derived(df, config.settings())
    return df, set(pick)


FAULTS = [
    Fault("clock_drift_ahead", ROW, "One relay stamps observations 20 min late for 3 h (future timestamps)", clock_drift_ahead),
    Fault("clock_drift_subtle", ROW, "One relay's observation times shifted 6 min early for 2 h", clock_drift_subtle),
    Fault("duplicate_feed", ROW, "One relay's traffic replayed twice for 1 h", duplicate_feed),
    Fault("stuck_position", ROW, "Position source frozen on 8 flights", stuck_position),
    Fault("position_jump", ROW, "Single-report 3 degree position jumps on 15 flights", position_jump),
    Fault("altitude_in_metres", ROW, "Altitude decoded in metres instead of feet for 1 h on one relay", altitude_in_metres),
    Fault("turbulence_misreport", ROW, "10 calm, corroborated PIREPs rewritten as SEV", turbulence_misreport),
    Fault("source_dark", EVENT, "One relay delivers nothing for 2 h", source_dark),
    Fault("latency_spike", EVENT, "One relay delivers 40 min late for 2 h", latency_spike),
    Fault("flatline_temperature", EVENT, "Temperature stuck at -45 C on one relay for 2 h", flatline_temperature),
]


def _run_checks(df):
    con = db.load_frame(df)
    pipeline.refresh(con, with_product=False)
    flags = rules.flagged(con)
    alerts = con.execute("SELECT * FROM alerts").df()
    spatial = con.execute("SELECT * FROM spatial_status").df()
    con.close()
    return flags, alerts, spatial


def _alert_keys(alerts):
    return {(r.kind, r.source_id, r.window_start) for r in alerts.itertuples()}


def evaluate(df: pd.DataFrame, faults: list[Fault] | None = None, seed: int = 7) -> pd.DataFrame:
    """Inject each fault into its own copy of df and score the checks against ground truth."""
    faults = faults or FAULTS
    base_flags, base_alerts, spatial = _run_checks(df)
    base_alert_keys = _alert_keys(base_alerts)
    rows = []
    for f in faults:
        rng = np.random.default_rng(seed)
        bad, truth = f.apply(df.copy(), rng, {"spatial": spatial})
        flags, alerts, _ = _run_checks(bad)
        if f.level == ROW:
            flagged = set(flags)
            hit = truth & flagged
            new = flagged - truth - set(base_flags)
            # A fault can make an untouched neighbour in the same track look wrong too
            # (e.g. drifted timestamps re-order a flight). Those are reported as collateral,
            # not hidden and not counted as false alarms.
            touched = set(bad.loc[bad["report_id"].isin(truth), "aircraft_ref"])
            collateral = new & set(bad.loc[bad["aircraft_ref"].isin(touched), "report_id"])
            false = new - collateral
            detectors = pd.Series([r for rid in hit for r in flags[rid]]).value_counts()
            rows.append(dict(scenario=f.name, description=f.description, level=ROW,
                             injected=len(truth), detected=len(hit),
                             recall=len(hit) / len(truth) if truth else np.nan,
                             collateral=len(collateral), false_alarms=len(false),
                             false_alarm_rate=len(false) / max(len(bad) - len(truth), 1),
                             detectors=", ".join(f"{k} ({v})" for k, v in detectors.items())))
        else:
            src, start, end = truth
            new = alerts[[(r.kind, r.source_id, r.window_start) not in base_alert_keys
                          for r in alerts.itertuples()]]
            match = new[(new["source_id"] == src) & new["kind"].isin(EVENT_KINDS)
                        & (new["window_start"] < end) & (new["window_end"] > start)]
            monitored_hours = max(int((bad["receipt_time"].max() - bad["receipt_time"].min()) / pd.Timedelta(hours=1))
                                  * bad["source_id"].nunique(), 1)
            false = len(new) - len(match)
            rows.append(dict(scenario=f.name, description=f.description, level=EVENT,
                             injected=1, detected=int(len(match) > 0), recall=float(len(match) > 0),
                             collateral=0, false_alarms=false, false_alarm_rate=false / monitored_hours,
                             detectors=", ".join(sorted(set(match["kind"])))))
    out = pd.DataFrame(rows)
    out.insert(0, "evaluated_at", datetime.now(timezone.utc).replace(tzinfo=None))
    return out


def save(con, results: pd.DataFrame) -> None:
    con.execute("DELETE FROM fault_results")
    con.register("fr", results)
    con.execute("INSERT INTO fault_results SELECT * FROM fr")
    con.unregister("fr")
