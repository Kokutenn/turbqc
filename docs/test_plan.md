# Test plan

Run everything with `pytest`. CI runs the same suite on every push (`.github/workflows/ci.yml`).

| Level | What | Where | Pass criterion |
|---|---|---|---|
| Unit: parsing | Duplicate header names, type coercion, flags, source/operator extraction, two-layer EDR mapping, malformed lines | `tests/test_feed.py` | Field-level assertions |
| Unit: ingestion | Stable report ids; re-polling the same file adds nothing; schema drift is detected and logged as a run | `tests/test_feed.py` | 2 new then 0 new; `schema_drift` status |
| Unit: rules | Catalogue well-formed; every rule traces to a requirement; **every rule has at least one failing case**; boundary cases pass; clean baseline passes all rules | `tests/test_rules.py` + `tests/rule_cases.yaml` | Failing set equals expected set exactly |
| Unit: blame logic | Spike flags only the bad report; the report after a spike is not blamed; a persistent shift flags its first report | `rule_cases.yaml` (R050) | As stated |
| Integration: analytics | Clean synthetic data raises no alerts; a 2 h source outage raises dropout/gap; collector outages are not blamed on sources; spatial conflict detected and calm reports confirmed | `tests/test_pipeline.py` | As stated |
| Integration: KPIs | Hand-computed values on a 6-report track (validity 5/6, completeness 1 − 0.1/6) | `tests/test_pipeline.py` | Exact match |
| Integration: product | Reconciliation passes; rejected reports never reach the grid; deleting a grid row breaks P01 | `tests/test_pipeline.py` | As stated |
| System: fault injection | 10 scenarios on 48 h of synthetic traffic | `tests/test_pipeline.py` | Recall ≥ 95% and false-alarm rate < 0.1% for every scenario |
| Unit: verification | Lambert projection matches ecCodes at 7 grid points; GRIB message splitting; point vs neighbourhood sampling incl. vertical window; contingency scores on a hand-made table; ROC area 1.0 for a perfect forecast and ~0.5 for noise | `tests/test_verify.py` | Exact / tolerance as stated |
| Integration: verification | Skill decays with nowcast age; bootstrap CI brackets the estimate; QC subset drops exactly the rejected report; API-backfilled report gets the same id as its bulk-file copy | `tests/test_verify.py` | As stated |
| Migration: reconciliation | CSV round trip; DEF-01 regression (comma in free text); clean 3-day parallel run is GO; planted behaviour changes are explained by KD-01/03/04 and derived rejections inherit them; injected v2 defects (missing report, altered altitude) are unexplained and fail SO-01..SO-04; a product-cell change with no record cause is an unexplained aggregate; 2 green days are not enough (SO-07) | `tests/test_migration.py` | As stated |
| Docs | Generated docs render without placeholders; every requirement is covered by rules with tests | `tests/test_docs.py` | No `NO` in traceability |

## Adding a rule (definition of done)

1. Add the rule to `config/rules.yaml` with a requirement id.
2. Add at least one failing case (and ideally a boundary pass case) to `tests/rule_cases.yaml`.
3. `pytest`. `test_every_rule_has_a_failing_case` fails until step 2 is done.
4. `python -m turbqc docs` to regenerate the catalogue and traceability matrix.
5. `python -m turbqc faults` to confirm no new false alarms on collected data.

## Acceptance criteria for a change in thresholds

* All tests green.
* Fault-injection recall does not drop for any scenario.
* False-alarm rate on the latest 24 h of live data does not rise by more than 0.05 pp.
