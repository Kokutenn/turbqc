"""Analytics, KPIs, confidence, output product and the fault-injection harness."""
import numpy as np
import pandas as pd
import pytest

from conftest import calm_cluster, make_track
from turbqc import analytics, config, db, faults, kpis, pipeline, product


@pytest.fixture(scope="module")
def clean_con(synthetic_48h):
    con = db.load_frame(synthetic_48h)
    pipeline.refresh(con)
    yield con
    con.close()


def test_clean_synthetic_raises_no_alerts(clean_con):
    assert clean_con.execute("SELECT count(*) FROM alerts").fetchall()[0][0] == 0


def test_clean_synthetic_meets_kpi_targets(clean_con):
    k = kpis.kpi_table(clean_con).iloc[0]
    for name, target in config.settings()["kpi"]["targets"].items():
        if name != "timeliness":  # synthetic latency tail deliberately crosses the SLA now and then
            assert k[name] >= target, name


def test_kpis_on_known_data():
    df = make_track()
    df.loc[1, "lat"] = 95.0          # validity failure (R010) and position mismatch (R054)
    df.loc[2, "temp_c"] = np.nan     # one of 10 required fields missing
    con = db.load_frame(df)
    pipeline.refresh(con)
    k = kpis.kpi_table(con).iloc[0]
    assert k["reports"] == 6
    assert k["validity"] == pytest.approx(5 / 6)
    assert k["completeness"] == pytest.approx(1 - 0.1 / 6)
    assert k["uniqueness"] == 1.0
    assert kpis.rag("validity", k["validity"]) == "red"


def test_dropout_is_alerted(synthetic_48h):
    df = synthetic_48h
    t = df["receipt_time"].min() + pd.Timedelta(hours=30)
    gone = (df["source_id"] == "AIREP-QXT") & df["receipt_time"].between(t, t + pd.Timedelta(hours=2))
    con = db.load_frame(df[~gone])
    pipeline.refresh(con, with_product=False)
    kinds = {r[0] for r in con.execute("SELECT kind FROM alerts WHERE source_id = 'AIREP-QXT'").fetchall()}
    assert {"dropout", "gap"} & kinds


def test_collector_outage_is_not_blamed_on_sources():
    con = db.connect(":memory:")
    base = pd.Timestamp("2026-01-15 00:00")
    runs = [base + pd.Timedelta(minutes=5 * i) for i in range(12)] + \
           [base + pd.Timedelta(hours=4, minutes=5 * i) for i in range(12)]
    for i, t in enumerate(runs):
        con.execute("INSERT INTO ingest_runs (run_id, started_at, status) VALUES (?, ?, 'ok')", [f"r{i}", t])
    covered, down = analytics.collector_coverage(con, config.settings())
    assert len(down) == 1 and down[0][1] - down[0][0] > pd.Timedelta(hours=3)
    assert base + pd.Timedelta(hours=2) not in covered


def test_backfilled_history_before_first_poll_raises_no_gaps(synthetic_48h):
    df = synthetic_48h
    t = df["receipt_time"].min() + pd.Timedelta(hours=10)
    gone = (df["source_id"] == "AIREP-QXT") & df["receipt_time"].between(t, t + pd.Timedelta(hours=2))
    con = db.load_frame(df[~gone])
    first_poll = df["receipt_time"].max() - pd.Timedelta(minutes=30)
    con.execute("INSERT INTO ingest_runs (run_id, started_at, status) VALUES ('r1', ?, 'ok')", [first_poll])
    gaps = analytics.gaps(con, config.settings(), [])
    assert gaps[gaps["source_id"] == "AIREP-QXT"].empty


def test_spatial_conflict_and_confirmation():
    df = pd.concat([make_track(), calm_cluster("SEV")], ignore_index=True)
    con = db.load_frame(df)
    analytics.build_spatial_status(con)
    status = dict(con.execute("SELECT report_id, status FROM spatial_status").fetchall())
    assert status["target"] == "conflict"
    assert "confirmed" in status.values() and list(status.values()).count("conflict") == 1


def test_product_reconciles_and_excludes_rejected(clean_con):
    checks = product.check_product(clean_con)
    assert checks["passed"].all(), checks[~checks["passed"]]
    accepted = clean_con.execute(f"""
        SELECT count(*) FROM v_reports JOIN report_confidence USING (report_id)
        WHERE {product.PLACEABLE} AND confidence > 0""").fetchall()[0][0]
    gridded = clean_con.execute("SELECT sum(n_reports) FROM product_grid").fetchall()[0][0]
    assert accepted == gridded > 0


def test_rejected_reports_never_reach_the_product(synthetic_48h):
    df = synthetic_48h.copy()
    turb = df.index[df["edr_est"].notna()][:5]
    df.loc[turb, "edr_est"] = 1.5                      # R017 error -> must be rejected
    con = db.load_frame(df)
    pipeline.refresh(con)
    assert product.check_product(con)["passed"].all()
    assert con.execute("SELECT max(edr_max) FROM product_grid").fetchall()[0][0] <= 1


def test_reconciliation_detects_tampering(synthetic_48h):
    con = db.load_frame(synthetic_48h)
    pipeline.refresh(con)
    con.execute("DELETE FROM product_grid WHERE rowid IN (SELECT rowid FROM product_grid LIMIT 1)")
    checks = product.check_product(con).set_index("check_id")
    assert not checks.loc["P01", "passed"]


def test_confidence_is_bounded_and_gated(synthetic_48h):
    df = synthetic_48h.copy()
    df.loc[df.index[:5], "lat"] = 95.0
    con = db.load_frame(df)
    pipeline.refresh(con)
    lo, hi = con.execute("SELECT min(confidence), max(confidence) FROM report_confidence").fetchall()[0]
    assert 0 <= lo and hi <= 1
    gated = con.execute(f"""SELECT max(confidence) FROM report_confidence
                            WHERE report_id IN ({', '.join(repr(r) for r in df['report_id'][:5])})""").fetchall()[0][0]
    assert gated == 0


def test_fault_injection_detects_every_scenario(synthetic_48h):
    res = faults.evaluate(synthetic_48h).set_index("scenario")
    assert len(res) == len(faults.FAULTS)
    assert (res["recall"] >= 0.95).all(), res[res["recall"] < 0.95]
    assert (res["false_alarm_rate"] < 0.001).all(), res[res["false_alarm_rate"] >= 0.001]
