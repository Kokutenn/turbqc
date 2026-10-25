"""Migration reconciliation: v1 (legacy pandas) vs v2 (SQL) parallel run and sign-off."""
import gzip

import numpy as np
import pandas as pd
import pytest

import test_feed
from turbqc import db, faults, feed, migration, pipeline, synthetic

END = pd.Timestamp("2026-01-20 00:00")


@pytest.fixture(scope="module")
def three_days():
    return synthetic.generate(hours=72, seed=11, end=END)


def _payloads(df):
    return [(df["receipt_time"].max() + pd.Timedelta(minutes=1), gzip.compress(feed.to_feed_csv(df).encode()))]


def test_feed_csv_round_trip(three_days):
    df = three_days.iloc[:3000]
    back = feed.parse(feed.to_feed_csv(df), "rt", END).frame
    assert len(back) == len(df) and back["report_id"].is_unique
    cols = ["obs_time", "receipt_time", "aircraft_ref", "alt_ft", "tb_intensity", "tb_intensity2", "raw_text", "stream"]
    a = df[cols].sort_values(["receipt_time", "raw_text"]).reset_index(drop=True)
    b = back[cols].sort_values(["receipt_time", "raw_text"]).reset_index(drop=True)
    pd.testing.assert_frame_equal(a.astype(str), b.astype(str))


def test_overflowing_free_text_is_repaired():
    """DEF-01: an unquoted comma in the raw text must not lose the report."""
    line = test_feed.line(test_feed.PIREP)
    text = test_feed.HEADER + "\n" + line.replace("RY34R DEN", "RY34R, CEILINGS 011 MSL") + "\n"
    res = feed.parse(text, "r", test_feed.NOW)
    assert res.rows_malformed == 0 and len(res.frame) == 1
    assert res.frame.iloc[0]["raw_text"].endswith("RY34R, CEILINGS 011 MSL")


def test_clean_parallel_run_is_go(three_days):
    res = migration.parallel_run(migration.batches_from_frame(three_days))
    assert len(res["runs"]) == 3
    assert res["register"]["explained_by"].isna().sum() == 0
    assert res["decision"] == "GO", res["signoff"]


def test_known_differences_explain_planted_behaviour(three_days):
    df = three_days.copy()
    rng = np.random.default_rng(1)
    for name in ("position_jump", "altitude_in_metres"):
        df, _ = {f.name: f for f in faults.FAULTS}[name].apply(df, rng, {})
    r = migration.reconcile(_payloads(df), "planted")
    used = set(r.register["explained_by"].dropna().str.split(", ").explode())
    assert {"KD-01", "KD-03", "KD-04"} <= used               # second layer, blame logic, new rules
    derived = r.register[r.register["check_type"] == "derived"]
    assert len(derived) and derived["explained_by"].notna().all()   # rejections inherit their causes
    assert r.metrics["unexplained_record"] == 0 and r.metrics["unexplained_aggregate"] == 0
    assert r.metrics["normalised_differences"] > 0           # KD-02 millisecond normalisation applied


def test_v2_defects_are_unexplained_and_block_signoff(three_days):
    df = three_days[three_days["receipt_time"] >= END - pd.Timedelta(hours=24)]

    def break_v2(con):
        con.execute("DELETE FROM reports WHERE report_id = (SELECT min(report_id) FROM reports)")
        con.execute("""UPDATE reports SET alt_ft = alt_ft + 1000 WHERE report_id IN
                       (SELECT report_id FROM reports WHERE stream = 'acars_airep' ORDER BY report_id LIMIT 3)""")
    r = migration.reconcile(_payloads(df), "broken", tamper_v2=break_v2)
    m = r.metrics
    assert m["missing_keys"] == 1 and m["volume_mismatches"] == 1
    bad = r.register[r.register["explained_by"].isna()]
    assert set(bad["check_type"]) >= {"key", "field", "volume"}
    assert (bad["col"] == "alt_ft").sum() == 3
    assert not r.green
    failed = set(r.criteria.loc[~r.criteria["passed"], "criterion"])
    assert {"SO-01", "SO-02", "SO-03", "SO-04"} <= failed


def test_aggregate_without_record_cause_is_unexplained(three_days):
    df = three_days[three_days["receipt_time"] >= END - pd.Timedelta(hours=24)]
    tamper = lambda con: con.execute(
        "UPDATE product_grid SET n_reports = n_reports + 1 WHERE rowid = (SELECT min(rowid) FROM product_grid)")
    r = migration.reconcile(_payloads(df), "agg", tamper_v2=tamper)
    agg = r.register[(r.register["level"] == "aggregate") & r.register["explained_by"].isna()]
    assert len(agg) == 1 and agg.iloc[0]["check_type"] == "product_cell"
    assert r.metrics["unexplained_record"] == 0 and not r.green


def test_parallel_run_needs_consecutive_green_batches(three_days):
    two = {k: v for k, v in list(migration.batches_from_frame(three_days).items())[:2]}
    res = migration.parallel_run(two)
    so = res["signoff"].set_index("criterion")
    assert so.loc[["SO-01", "SO-02", "SO-03", "SO-04"], "passed"].all()
    assert not so.loc["SO-07", "passed"] and res["decision"] == "NO-GO"


def test_evidence_pack_renders(three_days):
    df = three_days[three_days["receipt_time"] >= END - pd.Timedelta(hours=24)]
    res = migration.parallel_run({"d1": _payloads(df)})
    md = migration.to_markdown(res, "test")
    assert "**Decision: NO-GO**" in md and "SO-07" in md and "DEF-01" in md
