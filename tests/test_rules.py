"""Every rule in the catalogue has data-driven test cases (tests/rule_cases.yaml)."""
import pandas as pd
import pytest
import yaml

from conftest import calm_cluster, finish, make_track
from turbqc import config, db, rules
from turbqc.docs import CASES

CASE_LIST = yaml.safe_load(CASES.read_text(encoding="utf-8"))["cases"]
RULE_IDS = [r["id"] for r in config.rules()]
DERIVED = {"edr_est", "edr_band", "severity", "source_id", "operator"}


def failing(df: pd.DataFrame, rule_id: str) -> set:
    con = db.load_frame(df)
    rules.validate(con)
    out = {r[0] for r in con.execute("SELECT report_id FROM rule_failures WHERE rule_id = ?", [rule_id]).fetchall()}
    con.close()
    return out


def build_case(case) -> pd.DataFrame:
    if case.get("spatial"):
        return pd.concat([make_track(), calm_cluster("SEV")], ignore_index=True)
    df = make_track()
    if "duplicate_row" in case:
        dup = df.iloc[[case["duplicate_row"]]].copy()
        dup["report_id"] = "dup"
        for c in ("receipt_time", "ingested_at"):
            dup[c] += pd.Timedelta(minutes=2)
        for k, v in case.get("dup_set", {}).items():
            dup[k] = v
        return pd.concat([df, dup], ignore_index=True)
    idx = [case["row"]] if "row" in case else list(range(case["rows_from"], len(df)))
    for k, v in case.get("set", {}).items():
        df[k] = df[k].astype(object) if v is None or isinstance(v, str) else df[k]
        df.loc[idx, k] = v
    for k, v in case.get("add", {}).items():
        df.loc[idx, k] = df.loc[idx, k] + v
    for k, v in case.get("shift_min", {}).items():
        df.loc[idx, k] = df.loc[idx, k] + pd.Timedelta(minutes=v)
    if "copy_position_from" in case:
        src = case["copy_position_from"]
        df.loc[idx, "lat"], df.loc[idx, "lon"] = df.loc[src, "lat"], df.loc[src, "lon"]
    overrides = {k: v for k, v in case.get("set", {}).items() if k in DERIVED}
    df = finish(df)
    for k, v in overrides.items():
        df.loc[idx, k] = v
    return df


def _ids(items):
    return {i if isinstance(i, str) else f"r{i}" for i in items}


def test_catalogue_is_well_formed():
    reqs = {r["id"] for r in config.requirements()}
    assert len(RULE_IDS) == len(set(RULE_IDS)), "duplicate rule ids"
    assert len(RULE_IDS) >= 20
    for r in config.rules():
        assert {"id", "name", "dimension", "severity", "requirement", "applies_to"} <= r.keys()
        assert ("fail_when" in r) != ("fail_query" in r), f"{r['id']}: exactly one of fail_when / fail_query"
        assert r["requirement"] in reqs, f"{r['id']} traces to unknown requirement"
        assert r["severity"] in ("error", "warning", "info")
        assert r["dimension"] in ("completeness", "validity", "timeliness", "uniqueness", "consistency")


def test_every_rule_has_a_failing_case():
    covered = {c["rule"] for c in CASE_LIST if c.get("fails")}
    assert set(RULE_IDS) <= covered, f"rules without a failing test case: {sorted(set(RULE_IDS) - covered)}"


def test_clean_baseline_passes_every_rule():
    df = pd.concat([make_track(), calm_cluster()], ignore_index=True)
    con = db.load_frame(df)
    rules.validate(con)
    assert con.execute("SELECT rule_id, report_id FROM rule_failures").fetchall() == []


@pytest.mark.parametrize("case", CASE_LIST, ids=[f"{c['rule']}-{c['name']}" for c in CASE_LIST])
def test_rule_case(case):
    got = failing(build_case(case), case["rule"])
    if "fails" in case:
        assert got == _ids(case["fails"])
    if "not_fails" in case:
        assert not (got & _ids(case["not_fails"]))
