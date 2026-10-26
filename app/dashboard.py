"""TurbQC dashboard.  Run:  streamlit run app/dashboard.py"""
import sys
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from turbqc import analytics, config, db, demo, ingest, kpis, migration, verify  # noqa: E402

st.set_page_config(page_title="TurbQC", page_icon="✈", layout="wide")

CFG = config.settings()
RAG = {"green": "#2e7d32", "amber": "#ed8b00", "red": "#c62828", "grey": "#9e9e9e"}
STATUS_COLORS = {"confirmed": "#2e7d32", "partial": "#7cb342", "sparse": "#90a4ae",
                 "isolated": "#cfd8dc", "conflict": "#c62828"}
BAND_ORDER = CFG["band_order"]
KPI_LABELS = {"dq_score": "DQ score", "completeness": "Completeness", "validity": "Validity",
              "timeliness": "Timeliness", "consistency": "Consistency", "uniqueness": "Uniqueness"}
DB_CHOICES = {"Live feed (AWC)": config.resolve(CFG["paths"]["db"]),
              "Demo (synthetic, with injected incidents)": config.resolve(CFG["paths"]["demo_db"])}

st.markdown("""<style>
.tile {border-left: 6px solid var(--c); padding: .4rem .8rem; border-radius: 4px;
       background: rgba(127,127,127,.08); margin-bottom: .5rem}
.tile .v {font-size: clamp(1rem, 1.9vw, 1.7rem); font-weight: 600; white-space: nowrap}
.tile .l {font-size: .75rem; opacity: .75; text-transform: uppercase; letter-spacing: .04em;
          white-space: nowrap; overflow: hidden; text-overflow: ellipsis}
.tile .t {font-size: .75rem; opacity: .7}
</style>""", unsafe_allow_html=True)


# ------------------------------------------------------------------ data access
def _mtime(path: Path) -> float:
    return path.stat().st_mtime if path.exists() else 0.0


@st.cache_data(ttl=60, show_spinner="Reading quality tables…")
def load(path_str: str, _mtime_key: float) -> dict:
    con = db.connect(path_str, read_only=True)
    q = lambda sql: con.execute(sql).df()
    d = {}
    try:
        d["n"] = con.execute("SELECT count(*) FROM reports").fetchall()[0][0]
        if d["n"] == 0:
            return d
        d["range"] = con.execute("SELECT min(receipt_time), max(receipt_time) FROM reports").fetchall()[0]
        d["runs"] = q("SELECT * FROM ingest_runs ORDER BY started_at DESC LIMIT 200")
        d["kpi_stream"] = kpis.kpi_table(con, ["stream"])
        d["kpi_all"] = kpis.kpi_table(con)
        d["kpi_hour"] = kpis.kpi_table(con, ["stream", "hour"])
        d["kpi_source"] = kpis.kpi_table(con, ["source_id"])
        d["kpi_operator"] = kpis.kpi_table(con, ["operator"], where="operator IS NOT NULL")
        d["rules"] = q("""
            WITH last AS (SELECT * FROM rule_runs WHERE validated_at = (SELECT max(validated_at) FROM rule_runs))
            SELECT m.rule_id, m.name, m.dimension, m.severity, m.requirement, m.applies_to,
                   l.checked, l.failed, l.failed / nullif(l.checked, 0) AS fail_rate, m.description
            FROM rule_meta m LEFT JOIN last l USING (rule_id) ORDER BY m.rule_id""")
        d["failures"] = q("""
            SELECT f.rule_id, r.report_id, r.stream, r.source_id, r.aircraft_ref, r.obs_time, r.receipt_time,
                   r.lat, r.lon, r.alt_ft, r.tb_intensity, r.raw_text
            FROM rule_failures f JOIN reports r USING (report_id)
            QUALIFY row_number() OVER (PARTITION BY f.rule_id ORDER BY r.receipt_time DESC) <= 200""")
        d["alerts"] = q("SELECT * FROM alerts ORDER BY window_start DESC")
        covered, _ = analytics.collector_coverage(con, CFG)
        d["volume"] = analytics.control_chart(analytics.hourly_volume(con), covered, CFG)
        d["latency"] = analytics.latency_hourly(con)
        d["spatial"] = q("""
            SELECT v.report_id, v.obs_time, v.lat, v.lon, v.alt_ft, v.aircraft_ref, v.stream, v.edr_band,
                   v.tb_intensity, s.status, s.n_neighbours, s.nb_median, c.confidence, v.raw_text
            FROM spatial_status s JOIN v_reports v USING (report_id)
            LEFT JOIN report_confidence c USING (report_id)""")
        d["confidence"] = q("""SELECT c.*, v.stream FROM report_confidence c JOIN v_reports v USING (report_id)""")
        d["grid"] = q("SELECT * FROM product_grid")
        d["checks"] = q("SELECT * FROM product_checks")
        d["faults"] = q("SELECT * FROM fault_results")
        d["pairs"] = verify.pairs_frame(con, CFG)
        d["verif_files"] = q("SELECT * FROM verif_files ORDER BY valid_time DESC")
        tables = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
        for t in ("migration_runs", "migration_register", "migration_signoff"):
            d[t] = q(f"SELECT * FROM {t}") if t in tables else pd.DataFrame()
    finally:
        con.close()
    return d


@st.cache_data(ttl=600, show_spinner="Scoring the nowcast (bootstrap)…")
def verif_scores(path_str: str, _mtime_key: float, field: str, mode: str | None, threshold: float):
    pairs = load(path_str, _mtime_key)["pairs"]
    by_band_lag = verify.score_table(pairs, ["fl_band", "lag_min"], field, mode, threshold, CFG)
    by_lag = verify.score_table(pairs, ["lag_min"], field, mode, threshold, CFG)
    subsets = pd.concat([verify.score_table(pairs[pairs["lag_min"] == 0], [], field, m, threshold, CFG)
                         .assign(subset=name) for name, m in verify.SUBSETS.items()], ignore_index=True)
    return by_band_lag, by_lag, subsets


def tile(col, label, value, kpi, target=None):
    colour = RAG[kpis.rag(kpi, value, CFG)]
    tgt = f"target {target:.1%}" if target is not None else "&nbsp;"
    col.markdown(f"<div class='tile' style='--c:{colour}'><div class='l'>{label}</div>"
                 f"<div class='v'>{value:.2%}</div><div class='t'>{tgt}</div></div>", unsafe_allow_html=True)


def kpi_columns():
    return {k: st.column_config.ProgressColumn(KPI_LABELS[k], min_value=0, max_value=1, format="percent")
            for k in KPI_LABELS}


# ------------------------------------------------------------------ sidebar
st.sidebar.title("TurbQC")
st.sidebar.caption("Data quality monitor for public turbulence reports")
# The demo ships with the repo (synthetic data only) so a visitor sees everything at once.
choice = st.sidebar.radio("Database", list(DB_CHOICES), index=1)
path = DB_CHOICES[choice]
live = choice.startswith("Live")

if live:
    if st.sidebar.button("Poll feed now", type="primary", help="Fetch the AWC bulk file and rerun every check"):
        with st.spinner("Fetching aviationweather.gov and validating…"):
            run = ingest.run_once(path)
        st.sidebar.success(f"{run['status']}: {run['rows_new']} new, {run['rows_already_seen']} already seen")
        st.cache_data.clear()
    st.sidebar.caption(f"Schedule `python -m turbqc ingest --loop` to collect every "
                       f"{CFG['ingest']['poll_minutes']} min and build history (the API keeps ~15 days).")
else:
    if st.sidebar.button("Rebuild demo data", help="Regenerate synthetic traffic and rerun fault injection"):
        with st.spinner("Generating 5 days of traffic and running fault injection (about a minute)…"):
            demo.build(path, hours=120)
        st.cache_data.clear()

if not live and not path.exists():
    with st.spinner("First start: building the demo database (about a minute)…"):
        demo.build(path, hours=120)

try:
    data = load(str(path), _mtime(path))
except Exception:
    if live:
        raise
    # The committed demo file could not be read (e.g. a DuckDB storage-format change): rebuild it.
    with st.spinner("Rebuilding the demo database for this DuckDB version (about a minute)…"):
        demo.build(path, hours=120)
    st.cache_data.clear()
    data = load(str(path), _mtime(path))
if not data.get("n"):
    st.info("No reports yet. Use **Poll feed now** in the sidebar, or switch to the demo database.")
    st.stop()

lo, hi = data["range"]
st.sidebar.markdown(f"**{data['n']:,} reports**  \n{lo:%d %b %H:%M} – {hi:%d %b %H:%M} UTC")
if live and not data["runs"].empty:
    last = data["runs"].iloc[0]
    st.sidebar.markdown(f"Last poll: {last['started_at']:%d %b %H:%M:%S} UTC · `{last['status']}`")
st.sidebar.divider()
st.sidebar.caption("Public data from the NOAA Aviation Weather Center. Non-commercial portfolio project; "
                   "not affiliated with any airline, vendor or industry body.")

tabs = st.tabs(["Overview", "Validation rules", "Feed health", "Scorecards", "Spatial & confidence",
                "Output product", "Nowcast verification", "Migration", "Fault injection", "Documentation"])

# ------------------------------------------------------------------ overview
with tabs[0]:
    k = data["kpi_all"].iloc[0]
    targets = CFG["kpi"]["targets"]
    cols = st.columns(6)
    tile(cols[0], "DQ score", k["dq_score"], "dq_score")
    for c, name in zip(cols[1:], kpis.KPIS):
        tile(c, KPI_LABELS[name], k[name], name, targets[name])

    left, right = st.columns([3, 2])
    with left:
        st.subheader("Quality over time")
        metric = st.selectbox("KPI", list(KPI_LABELS), format_func=KPI_LABELS.get, key="trend_kpi")
        h = data["kpi_hour"].dropna(subset=["hour"])
        fig = px.line(h, x="hour", y=metric, color="stream", markers=False,
                      labels={"hour": "Receipt hour (UTC)", metric: KPI_LABELS[metric]})
        if metric in targets:
            fig.add_hline(y=targets[metric], line_dash="dot", line_color=RAG["amber"],
                          annotation_text="target", annotation_position="bottom right")
        fig.update_yaxes(tickformat=".0%")
        fig.update_layout(height=340, margin=dict(l=0, r=0, t=10, b=0), legend_title_text="")
        st.plotly_chart(fig, width="stretch")
    with right:
        st.subheader("Open alerts")
        a = data["alerts"]
        if a.empty:
            st.success("No alerts")
        else:
            st.dataframe(a[["severity", "kind", "source_id", "window_start", "message"]], hide_index=True,
                         height=340, width="stretch")
    st.subheader("By stream")
    st.dataframe(data["kpi_stream"], hide_index=True, column_config=kpi_columns(), width="stretch")
    st.caption("AIREPs are automated ACARS position reports (objective, sparse turbulence content). "
               "PIREPs are pilot reports (subjective intensity, mapped to an EDR estimate). See Documentation › Methods.")

# ------------------------------------------------------------------ rules
with tabs[1]:
    r = data["rules"]
    c1, c2, c3 = st.columns(3)
    c1.metric("Rules in catalogue", len(r))
    c2.metric("Rules with failures", int((r["failed"] > 0).sum()))
    c3.metric("Requirements covered", r["requirement"].nunique())
    dims = st.multiselect("Dimension", sorted(r["dimension"].unique()), default=sorted(r["dimension"].unique()))
    view = r[r["dimension"].isin(dims)]
    st.dataframe(view.drop(columns=["description"]), hide_index=True, width="stretch",
                 column_config={"fail_rate": st.column_config.NumberColumn("fail rate", format="percent")})
    pick = st.selectbox("Inspect a rule", view["rule_id"], format_func=lambda x: f"{x} · {r.set_index('rule_id').loc[x, 'name']}")
    st.caption(r.set_index("rule_id").loc[pick, "description"])
    sample = data["failures"][data["failures"]["rule_id"] == pick]
    if sample.empty:
        st.success("No failing records.")
    else:
        st.dataframe(sample.drop(columns=["rule_id", "report_id"]), hide_index=True, width="stretch")

# ------------------------------------------------------------------ feed health
with tabs[2]:
    vol = data["volume"]
    sources = sorted(vol["source_id"].unique())
    src = st.selectbox("Source", sources, key="vol_src")
    g = vol[vol["source_id"] == src]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=g["hour"], y=g["upper"], line=dict(width=0), showlegend=False, hoverinfo="skip"))
    fig.add_trace(go.Scatter(x=g["hour"], y=g["lower"], fill="tonexty", fillcolor="rgba(120,144,156,.2)",
                             line=dict(width=0), name="control band"))
    fig.add_trace(go.Scatter(x=g["hour"], y=g["baseline"], line=dict(dash="dot", color="#78909c"), name="baseline"))
    fig.add_trace(go.Scatter(x=g["hour"], y=g["n"], line=dict(color="#1565c0"), name="reports / hour"))
    f = g.dropna(subset=["flag"])
    fig.add_trace(go.Scatter(x=f["hour"], y=f["n"], mode="markers", marker=dict(color=RAG["red"], size=10, symbol="x"),
                             name="out of control", text=f["flag"]))
    fig.update_layout(height=330, margin=dict(l=0, r=0, t=30, b=0), title="Reporting volume control chart",
                      legend=dict(orientation="h", y=1.12))
    st.plotly_chart(fig, width="stretch")
    if not g["seasonal"].any():
        st.caption("Learning period: high/low volume alerts start once 3 days of history give an hour-of-day "
                   "baseline. Dropouts and gaps are alerted from the start.")

    lat = data["latency"][data["latency"]["source_id"] == src]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=lat["hour"], y=lat["p50"], name="p50", line=dict(color="#1565c0")))
    fig.add_trace(go.Scatter(x=lat["hour"], y=lat["p95"], name="p95", line=dict(color="#ef6c00")))
    fig.add_hline(y=CFG["thresholds"]["latency_sla_min"], line_dash="dot", line_color=RAG["red"],
                  annotation_text="SLA", annotation_position="top left")
    fig.update_layout(height=280, margin=dict(l=0, r=0, t=30, b=0), title="Latency: receipt minus observation (min)",
                      legend=dict(orientation="h", y=1.15))
    st.plotly_chart(fig, width="stretch")

    if live:
        st.subheader("Collector runs")
        st.caption("Every poll is logged, including failures and schema drift: an outage of the monitor is data too.")
        st.dataframe(data["runs"].drop(columns=["source_url"]), hide_index=True, width="stretch", height=260)

# ------------------------------------------------------------------ scorecards
with tabs[3]:
    st.subheader("By source (ground relay / report channel)")
    st.dataframe(data["kpi_source"], hide_index=True, column_config=kpi_columns(), width="stretch")
    st.subheader("By operator (ICAO designator from AIREP callsign)")
    min_n = st.slider("Minimum reports", 5, 200, 20)
    op = data["kpi_operator"][data["kpi_operator"]["reports"] >= min_n].sort_values("dq_score")
    st.dataframe(op, hide_index=True, column_config=kpi_columns(), width="stretch", height=420)
    st.caption("A per-contributor scorecard is the evidence base for data-quality conversations with each "
               "data provider: which dimension is weak, how many records, since when.")

# ------------------------------------------------------------------ spatial & confidence
with tabs[4]:
    sp = data["spatial"]
    c1, c2 = st.columns([3, 1])
    with c2:
        counts = sp["status"].value_counts()
        for s in ["confirmed", "partial", "sparse", "isolated", "conflict"]:
            st.metric(s.capitalize(), int(counts.get(s, 0)))
        last_h = st.slider("Hours shown", 1, 48, 6)
    with c1:
        recent = sp[sp["obs_time"] >= sp["obs_time"].max() - pd.Timedelta(hours=last_h)]
        fig = px.scatter_geo(recent, lat="lat", lon="lon", color="status", color_discrete_map=STATUS_COLORS,
                             hover_data=["aircraft_ref", "tb_intensity", "alt_ft", "n_neighbours", "confidence"],
                             category_orders={"status": list(STATUS_COLORS)})
        fig.update_geos(fitbounds="locations", showcountries=True, showland=True, landcolor="rgba(200,200,200,.25)")
        fig.update_layout(height=430, margin=dict(l=0, r=0, t=10, b=0), legend_title_text="")
        st.plotly_chart(fig, width="stretch")
    st.caption(f"Neighbours = other aircraft within {CFG['spatial']['radius_km']} km, ±{CFG['spatial']['window_min']} min "
               f"and ±{CFG['spatial']['alt_ft']} ft. Conflict = severity differs from the neighbourhood median by "
               f"≥ {CFG['spatial']['conflict_severity_diff']} classes.")
    conflicts = sp[sp["status"] == "conflict"].sort_values("obs_time", ascending=False)
    if not conflicts.empty:
        st.subheader("Conflicting reports")
        st.dataframe(conflicts.drop(columns=["report_id"]), hide_index=True, width="stretch")

    st.subheader("Report confidence")
    conf = data["confidence"]
    fig = px.histogram(conf, x="confidence", color="stream", nbins=40, barmode="overlay", opacity=0.7)
    fig.update_layout(height=260, margin=dict(l=0, r=0, t=10, b=0), legend_title_text="")
    st.plotly_chart(fig, width="stretch")
    factors = ["f_gate", "f_warnings", "f_timeliness", "f_source", "f_corroboration", "f_subjectivity"]
    st.dataframe(conf.groupby("stream")[factors + ["confidence"]].mean().round(3), width="stretch")
    st.caption("Confidence = rule gate × warnings × timeliness × source reliability × corroboration × subjectivity. "
               "Rejected records score 0; everything else carries a graded trust value into the product.")

# ------------------------------------------------------------------ product
with tabs[5]:
    ch = data["checks"]
    st.subheader("Output controls")
    st.dataframe(ch, hide_index=True, width="stretch",
                 column_config={"passed": st.column_config.CheckboxColumn("passed")})
    grid = data["grid"]
    if not grid.empty:
        hours = sorted(grid["valid_hour"].unique())
        hsel = st.select_slider("Valid hour (UTC)", options=hours, value=hours[-1],
                                format_func=lambda t: pd.Timestamp(t).strftime("%d %b %H:%M"))
        band = st.selectbox("Flight-level band", sorted(grid["fl_band"].unique()), index=0)
        cell = grid[(grid["valid_hour"] == hsel) & (grid["fl_band"] == band)]
        fig = px.scatter_geo(cell, lat=cell["lat_cell"] + 0.5, lon=cell["lon_cell"] + 0.5, color="edr_weighted",
                             size="cell_confidence", range_color=(0, 0.6), color_continuous_scale="YlOrRd",
                             hover_data=["n_reports", "edr_max", "edr_weighted", "cell_confidence"])
        fig.update_geos(fitbounds="locations", showcountries=True, showland=True, landcolor="rgba(200,200,200,.25)")
        fig.update_layout(height=420, margin=dict(l=0, r=0, t=10, b=0), coloraxis_colorbar_title="EDR")
        st.plotly_chart(fig, width="stretch")
        st.caption("1° grid of observed turbulence. Colour = confidence-weighted EDR; marker size = cell confidence "
                   "(1 − ∏(1 − cᵢ)). Consumers get a trust value next to every number.")

# ------------------------------------------------------------------ verification
with tabs[6]:
    pairs = data["pairs"]
    vcfg = CFG["verification"]
    if pairs.empty:
        st.info("No verification pairs yet. Run `python -m turbqc verify --backfill` "
                "(pulls 48 h of PIREPs and the matching GTG Nowcast analyses from NOAA NOMADS).")
    else:
        synthetic_only = (pairs["source"] == "synthetic").all()
        if synthetic_only:
            st.warning("Demo database: the 'forecast' here is synthetic, generated only so these views can be "
                       "explored offline. Switch to the live database for real GTG Nowcast scores.")
        else:
            lo_t, hi_t = pairs["obs_time"].min(), pairs["obs_time"].max()
            st.caption(f"NWS Graphical Turbulence Guidance **Nowcast** (GTGN, 3 km CONUS, EDR) vs turbulence reports, "
                       f"{lo_t:%d %b %H:%M} – {hi_t:%d %b %H:%M} UTC. Observed event = MOD or worse; "
                       f"forecast event = EDR ≥ threshold.")
        c1, c2, c3 = st.columns(3)
        field = c1.radio("Matching", ["edr_nbhd", "edr_point"], horizontal=True,
                         format_func=lambda f: {"edr_nbhd": f"neighbourhood max ({vcfg['neighbourhood_km']} km, ±1,000 ft)",
                                                "edr_point": "grid point"}[f])
        subset = c2.radio("Reports", list(verify.SUBSETS), index=1, horizontal=True)
        thr = c3.slider("EDR threshold", 0.04, 0.45, float(vcfg["edr_threshold"]), 0.01)
        band_lag, by_lag, subsets = verif_scores(str(path), _mtime(path), field, verify.SUBSETS[subset], thr)

        if not by_lag.empty and 0 in set(by_lag["lag_min"]):
            r0 = by_lag.set_index("lag_min").loc[0]
            m = st.columns(6)
            m[0].metric("Pairs (lag 0)", f"{int(r0['pairs']):,}")
            m[1].metric("MOD+ observed", int(r0["events"]))
            m[2].metric("POD", f"{r0['pod']:.2f}", help=f"90% CI {r0['pod_lo']:.2f}–{r0['pod_hi']:.2f}")
            m[3].metric("FAR", f"{r0['far']:.2f}", help=f"90% CI {r0['far_lo']:.2f}–{r0['far_hi']:.2f}")
            m[4].metric("CSI", f"{r0['csi']:.2f}", help=f"90% CI {r0['csi_lo']:.2f}–{r0['csi_hi']:.2f}")
            m[5].metric("ROC area", f"{r0['auc']:.2f}")

        left, right = st.columns(2)
        with left:
            b0 = band_lag[band_lag["lag_min"] == 0].melt(id_vars=["fl_band", "pairs"], value_vars=["pod", "far", "csi"],
                                                        var_name="score", value_name="value")
            ci = band_lag[band_lag["lag_min"] == 0].set_index("fl_band")
            b0["lo"] = [ci.loc[r.fl_band, f"{r.score}_lo"] for r in b0.itertuples()]
            b0["hi"] = [ci.loc[r.fl_band, f"{r.score}_hi"] for r in b0.itertuples()]
            fig = px.bar(b0, x="fl_band", y="value", color="score", barmode="group",
                         error_y=b0["hi"] - b0["value"], error_y_minus=b0["value"] - b0["lo"],
                         category_orders={"fl_band": ["SFC-FL100", "FL100-200", "FL200-300", "FL300-400", "FL400+"]},
                         color_discrete_map={"pod": "#2e7d32", "far": "#c62828", "csi": "#1565c0"},
                         hover_data=["pairs"])
            fig.update_layout(height=340, margin=dict(l=0, r=0, t=30, b=0), title="By flight level (lag 0, 90% CI)",
                              yaxis_range=(0, 1), legend_title_text="")
            st.plotly_chart(fig, width="stretch")
        with right:
            fig = px.line(band_lag, x="lag_min", y="csi", color="fl_band", markers=True,
                          labels={"lag_min": "Nowcast age (min)", "csi": "CSI"})
            fig.add_trace(go.Scatter(x=by_lag["lag_min"], y=by_lag["csi"], name="all levels",
                                     line=dict(color="black", width=3)))
            fig.update_layout(height=340, margin=dict(l=0, r=0, t=30, b=0), legend_title_text="",
                              title="Skill vs staleness: how old can the nowcast be?")
            st.plotly_chart(fig, width="stretch")

        left, right = st.columns(2)
        with left:
            fig = go.Figure()
            fig.add_trace(go.Scatter(x=[0, 1], y=[0, 1], line=dict(dash="dot", color="#9e9e9e"), name="no skill"))
            for lag, g in pairs.groupby("lag_min"):
                g, w = verify.subset(g[g[field].notna()], verify.SUBSETS[subset])
                curve, auc = verify.roc(g["obs_event"].to_numpy(), g[field].to_numpy(), vcfg["sweep"], w)
                fig.add_trace(go.Scatter(x=curve["pofd"], y=curve["pod"], mode="lines+markers",
                                         name=f"{lag} min (AUC {auc:.2f})", text=curve["threshold"],
                                         hovertemplate="EDR ≥ %{text}<br>POD %{y:.2f}<br>POFD %{x:.2f}"))
            fig.update_layout(height=340, margin=dict(l=0, r=0, t=30, b=0), title="ROC by nowcast age",
                              xaxis_title="POFD", yaxis_title="POD", legend_title_text="")
            st.plotly_chart(fig, width="stretch")
        with right:
            st.markdown("**Does QC change the verdict?** (lag 0)")
            st.dataframe(subsets[["subset", "pairs", "events", "pod", "far", "csi", "auc"]].round(3),
                         hide_index=True, width="stretch")
            st.caption("Verification is only as good as its observations. Scores are computed on all reports, on "
                       "QC-passed reports, and with each report weighted by its confidence.")
            st.markdown("**Acceptance criteria** (illustrative, `settings.yaml › verification.acceptance`)")
            acc = verify.acceptance(pairs, CFG)
            if not acc.empty:
                st.dataframe(acc.pivot(index="criterion", columns="fl_band", values="passed"), width="stretch")

        with st.expander("Full score table"):
            st.dataframe(band_lag.round(3), hide_index=True, width="stretch")
        if not data["verif_files"].empty:
            vf = data["verif_files"]
            st.caption(f"GTGN files processed: {(vf['status'] == 'ok').sum()} ok, "
                       f"{(vf['status'] != 'ok').sum()} unavailable · streamed and discarded, nothing stored on disk.")
        st.caption("Caveat: pilots report more often when it is bumpy, so PIREP samples are not random. POD and FAR "
                   "are conditional on a report existing; ROC area is the most robust comparison across bands and lags.")

# ------------------------------------------------------------------ migration
with tabs[7]:
    so, runs, reg = data["migration_signoff"], data["migration_runs"], data["migration_register"]
    if so.empty:
        st.info("No parallel run stored yet. Run `python -m turbqc reconcile` (live archive) "
                "or rebuild the demo database.")
    else:
        mcfg = migration.migration_config()
        is_go = bool(so["passed"].all())
        failed = ", ".join(f"{r.criterion} {r.name}" for r in so.itertuples() if not r.passed)
        (st.success if is_go else st.error)(
            f"**Cutover decision: {'GO' if is_go else 'NO-GO'}**" + ("" if is_go else f" · failing: {failed}"))
        st.caption("v1 = legacy pandas pipeline, v2 = this DuckDB/SQL pipeline, both run on the same raw provider "
                   "files. Every discrepancy must match an approved known difference, or inherit its explanation "
                   "from the record-level differences that cause it.")
        left, right = st.columns([2, 3])
        with left:
            st.markdown("**Sign-off criteria** (latest batch + parallel-run streak)")
            st.dataframe(so[["criterion", "name", "value", "target", "passed"]], hide_index=True, width="stretch",
                         column_config={"passed": st.column_config.CheckboxColumn("passed")})
        with right:
            r2 = runs.assign(explained=runs["record_discrepancies"] + runs["aggregate_discrepancies"]
                             - runs["unexplained_record"] - runs["unexplained_aggregate"],
                             unexplained=runs["unexplained_record"] + runs["unexplained_aggregate"])
            fig = go.Figure()
            fig.add_bar(x=r2["batch"], y=r2["explained"], name="explained", marker_color="#90a4ae")
            fig.add_bar(x=r2["batch"], y=r2["unexplained"], name="unexplained", marker_color=RAG["red"])
            fig.add_trace(go.Scatter(x=r2["batch"], y=r2["record_match_rate"], name="raw record match rate",
                                     yaxis="y2", mode="lines+markers", line=dict(color="#1565c0")))
            fig.update_layout(barmode="stack", height=300, margin=dict(l=0, r=0, t=30, b=0),
                              title="Parallel run by day", legend=dict(orientation="h", y=1.18),
                              yaxis=dict(title="discrepancies"), xaxis=dict(type="category"),
                              yaxis2=dict(overlaying="y", side="right", range=(0.9, 1), tickformat=".0%"))
            st.plotly_chart(fig, width="stretch")

        st.markdown("**Known differences** (approved behaviour changes)")
        used = reg["explained_by"].dropna().str.split(", ").explode().value_counts()
        kd = pd.DataFrame([dict(id=k["id"], title=k["title"], disposition=k.get("disposition", "open"),
                                discrepancies_explained=int(used.get(k["id"], 0)),
                                description=" ".join(k["description"].split())) for k in mcfg["known_differences"]])
        st.dataframe(kd, hide_index=True, width="stretch")
        for d in mcfg.get("resolved_defects", []):
            with st.expander(f"{d['id']} · defect found by the reconciliation and fixed in v2"):
                st.markdown(f"- **Found:** {d['found']}\n- **Symptom:** {' '.join(d['symptom'].split())}\n"
                            f"- **Root cause:** {' '.join(d['root_cause'].split())}\n- **Fix:** {' '.join(d['fix'].split())}\n"
                            f"- **Verified by:** {d['verified_by']}")

        st.markdown("**Discrepancy register**")
        f1, f2, f3 = st.columns(3)
        lvl = f1.multiselect("Level", sorted(reg["level"].unique()), default=sorted(reg["level"].unique()))
        status = f2.radio("Status", ["all", "unexplained only"], horizontal=True)
        batch = f3.selectbox("Batch", ["all"] + sorted(reg["batch"].unique()))
        view = reg[reg["level"].isin(lvl)]
        if status != "all":
            view = view[view["explained_by"].isna()]
        if batch != "all":
            view = view[view["batch"] == batch]
        st.caption(f"{len(view):,} rows" + (" (first 2,000 shown)" if len(view) > 2000 else ""))
        st.dataframe(view.drop(columns=["disc_id", "reconciled_at"], errors="ignore").head(2000),
                     hide_index=True, width="stretch", height=320)

# ------------------------------------------------------------------ faults
with tabs[8]:
    fr = data["faults"]
    if fr.empty:
        st.info("No fault-injection run stored for this database. Run `python -m turbqc faults`.")
    else:
        rows = fr[fr["level"] == "row"]
        c1, c2, c3 = st.columns(3)
        c1.metric("Scenarios", len(fr))
        c2.metric("Row-level recall", f"{rows['detected'].sum() / max(rows['injected'].sum(), 1):.1%}")
        c3.metric("False alarms (rows + alerts)", int(fr["false_alarms"].sum()))
        fig = px.bar(fr, x="recall", y="scenario", orientation="h", color="level", range_x=(0, 1),
                     hover_data=["injected", "detected", "false_alarms", "detectors"])
        fig.update_layout(height=360, margin=dict(l=0, r=0, t=10, b=0), xaxis_tickformat=".0%", legend_title_text="")
        st.plotly_chart(fig, width="stretch")
        st.dataframe(fr.drop(columns=["evaluated_at"]), hide_index=True, width="stretch",
                     column_config={"recall": st.column_config.NumberColumn(format="percent"),
                                    "false_alarm_rate": st.column_config.NumberColumn(format="%.5f")})
        st.caption("Each scenario corrupts its own copy of the data, all checks are rerun, and flags are scored "
                   "against ground truth. Collateral = untouched reports in the same flight that the fault "
                   "made inconsistent; false alarms = new flags anywhere else.")

# ------------------------------------------------------------------ docs
with tabs[9]:
    docs_dir = config.ROOT / "docs"
    pages = {"Methods & assumptions": "methods.md", "KPI definitions": "kpi_definitions.md",
             "Migration approach": "migration.md",
             "Rule catalogue": "rule_catalogue.md", "Data dictionary": "data_dictionary.md",
             "Traceability": "traceability.md", "Test plan": "test_plan.md"}
    page = st.radio("Document", list(pages), horizontal=True)
    p = docs_dir / pages[page]
    st.markdown(p.read_text(encoding="utf-8") if p.exists() else f"`{p.name}` not generated yet: run `python -m turbqc docs`.")
