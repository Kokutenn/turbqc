# Requirements traceability

Requirement -> rule -> test case. Generated from `config/requirements.yaml`, `config/rules.yaml` and `tests/rule_cases.yaml`.

| Requirement | Title | Rules | Test cases | Covered |
|---|---|---|---|---|
| DQ-REQ-01 | Mandatory fields | R001, R002, R003, R004, R005, R007 | 6 | yes |
| DQ-REQ-02 | Physical value ranges | R010, R011, R012, R013, R014, R016, R017, R018, R019, R020, R021 | 12 | yes |
| DQ-REQ-03 | Controlled vocabulary | R006, R015 | 3 | yes |
| DQ-REQ-04 | Timestamp integrity | R030, R031 | 3 | yes |
| DQ-REQ-05 | Timeliness | R032, R033 | 2 | yes |
| DQ-REQ-06 | Uniqueness | R040, R041 | 2 | yes |
| DQ-REQ-07 | Kinematic plausibility | R050, R051, R052 | 6 | yes |
| DQ-REQ-08 | Internal consistency | R053, R054, R056, R057 | 6 | yes |
| DQ-REQ-09 | Spatial consistency | R055 | 1 | yes |
| DQ-REQ-10 | Provider quality flags | R022, R023, R034 | 3 | yes |
| DQ-REQ-11 | Feed continuity | volume control chart, dropout, gap, latency p95, flat-line, collector coverage | 4 | yes |
| DQ-REQ-12 | Output product integrity | P01-P06 product checks | 3 | yes |
| DQ-REQ-13 | Nowcast acceptance metrics | GTGN verification (verify.py), acceptance criteria | 6 | yes |
| DQ-REQ-14 | Migration reconciliation | v1/v2 parallel run (migration.py), known differences, sign-off SO-01..SO-07 | 6 | yes |

Controls that are not row-level rules are tested in `tests/test_pipeline.py`, `tests/test_verify.py` and `tests/test_migration.py`:

* **DQ-REQ-11**: volume control chart, dropout, gap, latency p95, flat-line, collector coverage, tested by `test_clean_synthetic_raises_no_alerts`, `test_dropout_is_alerted`, `test_collector_outage_is_not_blamed_on_sources`, `test_fault_injection_detects_every_scenario`
* **DQ-REQ-12**: P01-P06 product checks, tested by `test_product_reconciles_and_excludes_rejected`, `test_rejected_reports_never_reach_the_product`, `test_reconciliation_detects_tampering`
* **DQ-REQ-13**: GTGN verification (verify.py), acceptance criteria, tested by `test_lambert_projection_matches_eccodes`, `test_sampling_point_and_neighbourhood`, `test_scores_on_known_table`, `test_roc_area_perfect_and_random`, `test_synthetic_pairs_score_and_decay`, `test_qc_subsets_differ_only_by_rejected_reports`
* **DQ-REQ-14**: v1/v2 parallel run (migration.py), known differences, sign-off SO-01..SO-07, tested by `test_clean_parallel_run_is_go`, `test_known_differences_explain_planted_behaviour`, `test_v2_defects_are_unexplained_and_block_signoff`, `test_aggregate_without_record_cause_is_unexplained`, `test_parallel_run_needs_consecutive_green_batches`, `test_overflowing_free_text_is_repaired`
