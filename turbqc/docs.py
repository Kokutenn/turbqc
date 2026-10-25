"""Docs as code: the rule catalogue, data dictionary and traceability matrix are generated
from the same YAML and schema the pipeline runs on, so documentation cannot drift."""
from collections import defaultdict

import yaml

from . import config, rules as rules_mod
from .schema import REPORT_COLUMNS

DOCS = config.ROOT / "docs"
CASES = config.ROOT / "tests" / "rule_cases.yaml"


def _cell(text) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def rule_catalogue() -> str:
    params = config.rule_params()
    lines = ["# Rule catalogue", "",
             "Generated from `config/rules.yaml` by `python -m turbqc docs`. Do not edit by hand.", "",
             "| ID | Rule | Dimension | Severity | Requirement | Applies to | Description |",
             "|---|---|---|---|---|---|---|"]
    for r in config.rules():
        lines.append(f"| {r['id']} | {_cell(r['name'])} | {r['dimension']} | {r['severity']} | {r['requirement']} "
                     f"| {', '.join(r['applies_to'])} | {_cell(rules_mod.describe(r, params))} |")
    lines += ["", "## SQL", ""]
    for r in config.rules():
        body = r.get("fail_when") or r.get("fail_query")
        kind = "fail_when" if "fail_when" in r else "fail_query"
        lines += [f"### {r['id']} {r['name']}", "", f"`{kind}`", "", "```sql",
                  rules_mod.render(body, params).strip(), "```", ""]
    return "\n".join(lines)


def data_dictionary() -> str:
    lines = ["# Data dictionary", "",
             "Generated from `turbqc/schema.py`. Table `reports`, one row per delivered report.", "",
             "| Column | Type | Origin | Description |", "|---|---|---|---|"]
    lines += [f"| {n} | {t} | {_cell(o)} | {_cell(d)} |" for n, t, d, o in REPORT_COLUMNS]
    lines += ["", "## Derived tables", "",
              "| Table | Grain | Purpose |", "|---|---|---|",
              "| ingest_runs | one row per poll | Audit of every collection attempt, including failures and schema drift |",
              "| rule_meta | one row per rule | Rule catalogue as loaded at the last validation |",
              "| rule_failures | rule x report | Current failures (rebuilt on each validation) |",
              "| rule_runs | rule x validation | History of checked / failed counts |",
              "| spatial_status | turbulence report | Neighbour count, neighbourhood median severity, status |",
              "| report_quality | report | 0/1 per KPI dimension, the basis of every KPI view |",
              "| report_confidence | report | Confidence factors and the final 0..1 confidence |",
              "| alerts | alert | Volume, dropout, gap, latency, flat-line and collector alerts |",
              "| product_grid | hour x 1 deg cell x FL band | Output product with confidence-weighted EDR |",
              "| product_checks | check | Output controls including the input/output reconciliation |",
              "| fault_results | scenario | Latest fault-injection scores |",
              "| verif_pairs | report x nowcast age | GTG Nowcast EDR (grid point and neighbourhood max) matched to each turbulence report |",
              "| verif_files | GTGN analysis | Audit of every nowcast file fetched (status, size, pairs produced) |",
              "| migration_runs | parallel-run batch | Keys, match rate, discrepancy counts and green/red per batch |",
              "| migration_register | discrepancy | Every v1/v2 difference with its explanation (known difference or inherited) |",
              "| migration_criteria | batch x criterion | Sign-off criteria SO-01..SO-06 per batch |",
              "| migration_signoff | criterion | Latest sign-off incl. SO-07 parallel-run streak; GO only if all pass |"]
    return "\n".join(lines)


def traceability() -> str:
    cases = yaml.safe_load(CASES.read_text(encoding="utf-8"))["cases"] if CASES.exists() else []
    tests_by_rule = defaultdict(list)
    for c in cases:
        tests_by_rule[c["rule"]].append(c["name"])
    rules_by_req = defaultdict(list)
    for r in config.rules():
        rules_by_req[r["requirement"]].append(r)
    lines = ["# Requirements traceability", "",
             "Requirement -> rule -> test case. Generated from `config/requirements.yaml`, "
             "`config/rules.yaml` and `tests/rule_cases.yaml`.", "",
             "| Requirement | Title | Rules | Test cases | Covered |", "|---|---|---|---|---|"]
    for req in config.requirements():
        rs = rules_by_req.get(req["id"], [])
        tests = [t for r in rs for t in tests_by_rule.get(r["id"], [])]
        controls = [r["id"] for r in rs]
        if req["id"] in NON_RULE_CONTROLS:
            extra_controls, extra_tests = NON_RULE_CONTROLS[req["id"]]
            controls += extra_controls
            tests += extra_tests
        covered = "yes" if controls and tests and all(tests_by_rule.get(r["id"]) for r in rs) else "NO"
        lines.append(f"| {req['id']} | {_cell(req['title'])} | {', '.join(controls) or '-'} "
                     f"| {len(tests)} | {covered} |")
    lines += ["", "Controls that are not row-level rules are tested in `tests/test_pipeline.py`, "
              "`tests/test_verify.py` and `tests/test_migration.py`:", ""]
    for req, (controls, tests) in NON_RULE_CONTROLS.items():
        lines.append(f"* **{req}**: {', '.join(controls)}, tested by {', '.join(f'`{t}`' for t in tests)}")
    return "\n".join(lines)


# Requirement -> (controls implemented outside rules.yaml, pytest functions that verify them)
NON_RULE_CONTROLS = {
    "DQ-REQ-11": (["volume control chart", "dropout", "gap", "latency p95", "flat-line", "collector coverage"],
                  ["test_clean_synthetic_raises_no_alerts", "test_dropout_is_alerted",
                   "test_collector_outage_is_not_blamed_on_sources", "test_fault_injection_detects_every_scenario"]),
    "DQ-REQ-12": (["P01-P06 product checks"],
                  ["test_product_reconciles_and_excludes_rejected", "test_rejected_reports_never_reach_the_product",
                   "test_reconciliation_detects_tampering"]),
    "DQ-REQ-13": (["GTGN verification (verify.py)", "acceptance criteria"],
                  ["test_lambert_projection_matches_eccodes", "test_sampling_point_and_neighbourhood",
                   "test_scores_on_known_table", "test_roc_area_perfect_and_random",
                   "test_synthetic_pairs_score_and_decay", "test_qc_subsets_differ_only_by_rejected_reports"]),
    "DQ-REQ-14": (["v1/v2 parallel run (migration.py)", "known differences", "sign-off SO-01..SO-07"],
                  ["test_clean_parallel_run_is_go", "test_known_differences_explain_planted_behaviour",
                   "test_v2_defects_are_unexplained_and_block_signoff",
                   "test_aggregate_without_record_cause_is_unexplained",
                   "test_parallel_run_needs_consecutive_green_batches", "test_overflowing_free_text_is_repaired"]),
}


def generate() -> list:
    DOCS.mkdir(exist_ok=True)
    out = []
    for name, fn in [("rule_catalogue.md", rule_catalogue), ("data_dictionary.md", data_dictionary),
                     ("traceability.md", traceability)]:
        path = DOCS / name
        path.write_text(fn() + "\n", encoding="utf-8")
        out.append(path)
    return out
