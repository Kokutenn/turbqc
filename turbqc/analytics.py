"""Statistical data-quality analytics: spatial consistency, volume control charts,
gaps and dropouts, latency, flat-lined values. Results land in the alerts table."""
import numpy as np
import pandas as pd

from . import config

HOUR = pd.Timedelta(hours=1)


# ------------------------------------------------------------------ spatial consistency
def build_spatial_status(con, cfg: dict | None = None) -> None:
    """For every turbulence report, compare its severity with independent neighbours
    (other aircraft within radius_km, +/- window_min, +/- alt_ft).

    status: isolated (no neighbours), sparse (fewer than min_neighbours),
            confirmed (within half a class of the neighbourhood median),
            partial (differs by less than conflict_severity_diff), conflict.
    """
    s = (cfg or config.settings())["spatial"]
    deg = s["radius_km"] / 111.0
    con.execute(f"""
        CREATE OR REPLACE TABLE spatial_status AS
        WITH obs AS (
            SELECT report_id, track_id, aircraft_ref, obs_time, lat, lon, alt_ft, severity
            FROM v_reports
            WHERE severity IS NOT NULL AND lat IS NOT NULL AND lon IS NOT NULL
              AND alt_ft IS NOT NULL AND obs_time IS NOT NULL
        ), pairs AS (
            SELECT a.report_id, b.severity AS nb_sev
            FROM obs a JOIN obs b
              ON b.lat BETWEEN a.lat - {deg} AND a.lat + {deg}
             AND b.obs_time BETWEEN a.obs_time - INTERVAL {s['window_min']} MINUTE
                                AND a.obs_time + INTERVAL {s['window_min']} MINUTE
             AND b.alt_ft BETWEEN a.alt_ft - {s['alt_ft']} AND a.alt_ft + {s['alt_ft']}
             AND a.report_id <> b.report_id
             AND coalesce(a.track_id, a.report_id) <> coalesce(b.track_id, b.report_id)
             AND NOT (a.aircraft_ref = b.aircraft_ref AND a.obs_time = b.obs_time)
            WHERE hav_km(a.lat, a.lon, b.lat, b.lon) <= {s['radius_km']}
        ), agg AS (
            SELECT report_id, count(*) AS n_neighbours, median(nb_sev) AS nb_median
            FROM pairs GROUP BY report_id
        )
        SELECT o.report_id, o.severity, coalesce(g.n_neighbours, 0) AS n_neighbours, g.nb_median,
               CASE WHEN g.n_neighbours IS NULL THEN 'isolated'
                    WHEN g.n_neighbours < {s['min_neighbours']} THEN 'sparse'
                    WHEN abs(o.severity - g.nb_median) >= {s['conflict_severity_diff']} THEN 'conflict'
                    WHEN abs(o.severity - g.nb_median) <= 0.5 THEN 'confirmed'
                    ELSE 'partial' END AS status
        FROM obs o LEFT JOIN agg g USING (report_id)""")


# ------------------------------------------------------------------ collector coverage
def collector_coverage(con, cfg: dict) -> tuple[set | None, list]:
    """Hours the collector fully observed, and periods it was down.

    A report received at time x is captured if some poll happens in [x, x + horizon],
    because the bulk file keeps roughly the last horizon minutes. Hour H is fully
    observed when that holds for every x in H: the first poll at or after H is within
    the horizon, no gap between polls exceeds it, and some poll happens after H ends.

    Returns (None, []) when there is no ingest log (synthetic or replayed data):
    every hour is then treated as observed.
    """
    runs = con.execute("""SELECT started_at FROM ingest_runs
                          WHERE status LIKE 'ok%' ORDER BY started_at""").df()["started_at"]
    if runs.empty:
        return None, []
    horizon = pd.Timedelta(minutes=cfg["feed"]["cache_horizon_min"])
    t = pd.DatetimeIndex(runs)
    covered = set()
    for h in pd.date_range(t.min().floor("h") - HOUR, t.max().floor("h"), freq="h"):
        r = t[(t >= h) & (t <= h + HOUR + horizon)]
        if (len(r) and r[0] - h <= horizon and r[-1] >= h + HOUR
                and (len(r) == 1 or (r[1:] - r[:-1]).max() <= horizon)):
            covered.add(h)
    down = [(a, b) for a, b in zip(runs[:-1], runs[1:]) if b - a > horizon]
    return covered, down


def _overlaps(start, end, periods) -> bool:
    return any(start < b and end > a for a, b in periods)


# ------------------------------------------------------------------ volume control chart
def hourly_volume(con) -> pd.DataFrame:
    return con.execute("""
        WITH bounds AS (
            SELECT date_trunc('hour', min(receipt_time)) AS lo, date_trunc('hour', max(receipt_time)) AS hi
            FROM reports),
        hours AS (SELECT unnest(generate_series(lo, hi, INTERVAL 1 HOUR)) AS hour FROM bounds),
        src AS (SELECT DISTINCT source_id, stream FROM reports WHERE receipt_time IS NOT NULL),
        cnt AS (SELECT source_id, date_trunc('hour', receipt_time) AS hour, count(*) AS n
                FROM reports WHERE receipt_time IS NOT NULL GROUP BY ALL)
        SELECT s.source_id, s.stream, h.hour, coalesce(c.n, 0) AS n
        FROM src s CROSS JOIN hours h LEFT JOIN cnt c USING (source_id, hour)
        ORDER BY s.source_id, h.hour""").df()


def control_chart(vol: pd.DataFrame, covered: set | None, cfg: dict) -> pd.DataFrame:
    """Robust control limits per source.

    Baseline: median of the same hour-of-day over the previous 7 days once
    min_history_days exist (captures the daily cycle), otherwise the median of the
    previous rolling_hours.
    Spread: 1.4826 * MAD of the baseline residuals over the previous residual_hours, so
    the chart learns each source's own burstiness. Floored at sqrt(overdispersion *
    baseline) so quiet hours cannot produce huge z-scores. Median and MAD keep the
    limits robust to the very outliers being hunted.
    """
    v = cfg["volume"]
    out = []
    for source, g in vol.groupby("source_id"):
        g = g.sort_values("hour").copy()
        hours = pd.DatetimeIndex(g["hour"])
        observed = np.ones(len(g), bool) if covered is None else hours.isin(list(covered))
        if covered is None and len(g) > 2:  # first and last hour are partial
            observed[0] = observed[-1] = False
        n = g["n"].to_numpy(float)
        hist = np.where(observed, n, np.nan)
        base, sigma = np.full(len(g), np.nan), np.full(len(g), np.nan)
        seasonal = np.zeros(len(g), bool)
        for i, h in enumerate(hours):
            same = hist[(hours < h) & (hours >= h - pd.Timedelta(days=7)) & (hours.hour == h.hour)]
            same = same[~np.isnan(same)]
            if len(same) >= v["min_history_days"]:
                ref = same
                seasonal[i] = True
            else:
                ref = hist[(hours < h) & (hours >= h - pd.Timedelta(hours=v["rolling_hours"]))]
                ref = ref[~np.isnan(ref)]
                if len(ref) < min(4, v["rolling_hours"]):
                    continue
            base[i] = float(np.median(ref))
        resid = hist - base
        for i, h in enumerate(hours):
            if np.isnan(base[i]):
                continue
            r = resid[(hours < h) & (hours >= h - pd.Timedelta(hours=v["residual_hours"]))]
            r = r[~np.isnan(r)]
            mad = float(np.median(np.abs(r - np.median(r)))) if len(r) >= 6 else 0.0
            sigma[i] = max(1.4826 * mad, np.sqrt(v["overdispersion"] * base[i]), 1.0)
        g["observed"] = observed
        g["baseline"] = base
        g["seasonal"] = seasonal
        g["lower"] = np.maximum(base - v["z_threshold"] * sigma, 0)
        g["upper"] = base + v["z_threshold"] * sigma
        g["z"] = (n - base) / sigma
        flag = np.full(len(g), None, dtype=object)
        ok = observed & ~np.isnan(base)
        # Learning period: until the hour-of-day baseline exists, a rolling baseline lags
        # the daily traffic cycle, so only hard dropouts are alerted.
        flag[ok & seasonal & (g["z"] > v["z_threshold"])] = "volume_high"
        flag[ok & seasonal & (g["z"] < -v["z_threshold"])] = "volume_low"
        flag[ok & (n == 0) & (base >= v["min_baseline_for_dropout"])] = "dropout"
        g["flag"] = flag
        out.append(g)
    return pd.concat(out, ignore_index=True) if out else vol.assign(flag=None)


def _merge_windows(df: pd.DataFrame, kind_col: str, value_col: str, agg) -> list[dict]:
    """Collapse consecutive flagged hours of the same kind into one alert window."""
    alerts = []
    for (source, kind), g in df.dropna(subset=[kind_col]).groupby(["source_id", kind_col]):
        g = g.sort_values("hour")
        block = (g["hour"].diff() != HOUR).cumsum()
        for _, b in g.groupby(block):
            alerts.append(dict(kind=kind, source_id=source, window_start=b["hour"].min(),
                               window_end=b["hour"].max() + HOUR, metric=float(agg(b[value_col]))))
    return alerts


# ------------------------------------------------------------------ gaps
def gaps(con, cfg: dict, down: list) -> pd.DataFrame:
    g = cfg["gaps"]
    first_poll, last_poll = con.execute(
        "SELECT min(started_at), max(started_at) FROM ingest_runs WHERE status LIKE 'ok%'").fetchall()[0]
    # An open gap runs to the latest evidence that we were watching: the last poll if the
    # collector is in use (back-filled reports can be newer), otherwise the newest report.
    now_t = "max(receipt_time)" if last_poll is None else f"least(max(receipt_time), TIMESTAMP '{last_poll}')"
    df = con.execute(f"""
        WITH t AS (
            SELECT source_id, stream, receipt_time,
                   lag(receipt_time) OVER (PARTITION BY source_id ORDER BY receipt_time) AS prev_t
            FROM reports WHERE receipt_time IS NOT NULL),
        ref AS (SELECT {now_t} AS now_t FROM reports),
        closed AS (
            SELECT source_id, stream, prev_t AS gap_start, receipt_time AS gap_end FROM t
            WHERE prev_t IS NOT NULL),
        open_gaps AS (
            SELECT source_id, any_value(stream) AS stream, max(receipt_time) AS gap_start,
                   (SELECT now_t FROM ref) AS gap_end
            FROM t GROUP BY source_id)
        SELECT *, date_diff('second', gap_start, gap_end) / 60.0 AS minutes
        FROM (SELECT * FROM closed UNION ALL SELECT * FROM open_gaps)
        WHERE date_diff('second', gap_start, gap_end) / 60.0 >
              CASE WHEN stream = 'pirep' THEN {g['pirep_min']} ELSE {g['acars_airep_min']} END
        ORDER BY gap_start""").df()
    df = df[~df["source_id"].isin(g.get("exclude_sources", []))]
    # History back-filled from the API predates the collector: nobody was watching, so no gaps.
    if first_poll is not None and not df.empty:
        horizon = pd.Timedelta(minutes=cfg["feed"]["cache_horizon_min"])
        df = df[df["gap_start"] >= pd.Timestamp(first_poll) - horizon]
    if down and not df.empty:
        df = df[[not _overlaps(r.gap_start, r.gap_end, down) for r in df.itertuples()]]
    return df


# ------------------------------------------------------------------ latency
def latency_hourly(con) -> pd.DataFrame:
    return con.execute("""
        SELECT source_id, stream, date_trunc('hour', receipt_time) AS hour, count(*) AS n,
               quantile_cont(latency_min, 0.5) AS p50, quantile_cont(latency_min, 0.95) AS p95,
               max(latency_min) AS max_latency, count(*) FILTER (WHERE latency_min < 0) AS negative
        FROM v_reports WHERE latency_min IS NOT NULL
        GROUP BY ALL ORDER BY source_id, hour""").df()


# ------------------------------------------------------------------ flat-lined values
def flatlines(con, cfg: dict) -> pd.DataFrame:
    f = cfg["flatline"]
    parts = [f"""
        SELECT source_id, date_trunc('hour', receipt_time) AS hour, '{field}' AS field,
               count({field}) AS n, any_value({field}) AS value
        FROM reports WHERE receipt_time IS NOT NULL
        GROUP BY ALL HAVING count({field}) >= {f['min_reports']} AND count(DISTINCT {field}) = 1"""
             for field in f["fields"]]
    return con.execute(" UNION ALL ".join(parts) + " ORDER BY hour").df()


# ------------------------------------------------------------------ alert assembly
def run(con, cfg: dict | None = None) -> pd.DataFrame:
    cfg = cfg or config.settings()
    covered, down = collector_coverage(con, cfg)
    alerts = []

    for a, b in down:
        alerts.append(dict(kind="collector_gap", source_id="COLLECTOR", window_start=a, window_end=b,
                           severity="high", metric=(b - a).total_seconds() / 60,
                           threshold=cfg["feed"]["cache_horizon_min"],
                           message=f"Collector offline {(b - a).total_seconds() / 60:.0f} min; reports older than the feed window may be lost"))

    chart = control_chart(hourly_volume(con), covered, cfg)
    for a in _merge_windows(chart, "flag", "z", min):
        a["severity"] = "high" if a["kind"] == "dropout" else "medium"
        a["threshold"] = cfg["volume"]["z_threshold"]
        a["message"] = {"dropout": "No reports where the baseline expects traffic",
                        "volume_low": "Reporting volume below control limit",
                        "volume_high": "Reporting volume above control limit (possible duplicate feed)"}[a["kind"]]
        if a["kind"] == "volume_high":
            a["metric"] = float(chart.loc[(chart.source_id == a["source_id"]) & (chart.hour >= a["window_start"])
                                          & (chart.hour < a["window_end"]), "z"].max())
        alerts.append(a)

    for r in gaps(con, cfg, down).itertuples():
        thr = cfg["gaps"]["pirep_min"] if r.stream == "pirep" else cfg["gaps"]["acars_airep_min"]
        alerts.append(dict(kind="gap", source_id=r.source_id, window_start=r.gap_start, window_end=r.gap_end,
                           severity="high", metric=r.minutes, threshold=thr,
                           message=f"No reports for {r.minutes:.0f} min"))

    lat = latency_hourly(con)
    sla = cfg["thresholds"]["latency_sla_min"]
    lat["flag"] = np.where((lat["p95"] > sla) & (lat["n"] >= cfg["latency"]["min_reports_per_hour"]),
                           "latency_p95", None)
    for a in _merge_windows(lat, "flag", "p95", max):
        a.update(severity="medium", threshold=sla, message=f"p95 latency {a['metric']:.0f} min exceeds SLA {sla} min")
        alerts.append(a)

    for r in flatlines(con, cfg).itertuples():
        alerts.append(dict(kind="flatline", source_id=r.source_id, window_start=r.hour, window_end=r.hour + HOUR,
                           severity="medium", metric=float(r.n), threshold=cfg["flatline"]["min_reports"],
                           message=f"{r.field} identical ({r.value}) across {r.n} reports"))

    cols = ["kind", "source_id", "window_start", "window_end", "severity", "metric", "threshold", "message"]
    df = pd.DataFrame(alerts, columns=cols)
    con.execute("DELETE FROM alerts")
    if not df.empty:
        con.register("alerts_df", df)
        con.execute(f"INSERT INTO alerts SELECT {', '.join(cols)} FROM alerts_df")
        con.unregister("alerts_df")
    return df
