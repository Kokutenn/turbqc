# Migration approach: v1 → v2 parallel run

A miniature platform migration. **v1** ([turbqc/legacy.py](../turbqc/legacy.py)) is a
pandas-only legacy pipeline. **v2** is the current DuckDB/SQL pipeline. Both run on the
*same raw provider files*, and an automated reconciliation decides whether v2 can replace
v1.

## 1. Agreed key and scope

* **Business key:** `report_id`, the SHA-1 of the raw message fields (type, aircraft,
  receipt and observation time, position, altitude, text). Both systems compute it the
  same way from the raw strings.
* **Record fields:** the 18 fields in `config/migration.yaml › compare_fields`, with
  per-field tolerances.
* **Rules:** the 17 rules v1 implements, compared flag by flag, plus every error-level
  rule only v2 has, since those change which reports are accepted.
* **Outputs:** volumes by stream × hour, and the product grid (`n_reports`, `edr_max`
  per cell).

## 2. Three levels of reconciliation

| Level | What is compared | How a discrepancy is explained |
|---|---|---|
| Base | keys present in each system; field values; rule flags | matches a **known difference** (approved behaviour change, SQL condition in `migration.yaml`) |
| Derived | accepted vs rejected | **inherits** from its causes: explained only if every rule-flag difference behind it is explained |
| Aggregate | volumes, product cells | **inherits** from its member reports: explained only if all their record-level differences are explained; a mismatch with no record-level cause is an aggregation defect |

Everything is computed in SQL inside DuckDB, so the register is reproducible and auditable.
Each discrepancy row carries: batch, level, check, key, column, v1 value, v2 value,
explanation, and the number of contributing records.

## 3. Known differences (approved)

| ID | Change | Effect |
|---|---|---|
| KD-01 | v2 uses the more severe of two turbulence layers; v1 read only the first | `edr_band`, `edr_est`, `severity` and product `edr_max` differ for multi-layer reports |
| KD-02 | v2 keeps millisecond timestamps; v1 stored whole seconds | normalisation: times compared to the second; count reported, not one row per report |
| KD-03 | v2's speed check blames only the faulty report (spike or start of a shift) | v1 also fails the neighbours of a fast leg; explained when v2 measures the same fast leg |
| KD-04 | v2 adds error-level rules (message cross-checks, frozen position, vertical rate, code lists) | v2 rejects reports v1 accepted |

A known difference must be specific. KD-03 was first written as "the previous report is
flagged", which left 17 discrepancies unexplained. They turned out to be clock-drift
boundaries. Rather than widening the rule loosely, the condition was rewritten to the
precise behaviour (v2 sees the same fast leg but does not blame this report). Every
explanation stays falsifiable.

## 4. Defects found by the reconciliation

**DEF-01: v2 lost reports with a comma in the pilot's remarks.** It was found on the
first live parallel run (24 Oct 2026). The provider does not quote `raw_text`, so a PIREP
ending "...ONCE OUT OF CLOUDS, CEILINGS 011 MSL" had 45 fields instead of 44. v2
quarantined it as malformed. v1 re-joined the overflow and kept it. The reconciliation
showed one missing key in v2 and a broken volume total for that hour, both unexplained,
so the decision was NO-GO. Fix: v2 re-joins the overflow into the last column. A
regression test was added and the rerun was green.

This is the point of a parallel run: the new system was "better" in every documented way
and still had a silent data-loss path that only a like-for-like comparison exposed.

## 5. Sign-off criteria

| ID | Criterion | Target |
|---|---|---|
| SO-01 | Every key present in both systems | 0 missing |
| SO-02 | Volumes reconcile by stream and hour | 0 mismatches |
| SO-03 | No unexplained record-level discrepancies | 0 |
| SO-04 | No unexplained aggregate discrepancies | 0 |
| SO-05 | Raw record match rate, before explanations | ≥ 95% |
| SO-06 | Only approved known differences applied | 0 unapproved |
| SO-07 | SO-01 to SO-06 met on consecutive daily batches | ≥ 3 |

**GO** only when all seven pass. SO-07 is what turns a one-off comparison into cutover
readiness: one clean day is not evidence of stability.

## 6. Results

* **Synthetic, 5 days with injected incidents:** GO. 2,116 discrepancies, all explained,
  every batch green. Evidence pack: [reports/parallel_run_demo.md](../reports/parallel_run_demo.md).
* **Live archive (1 day):** SO-01 to SO-06 green after the DEF-01 fix, but NO-GO on SO-07
  (1 of 3 required days). Evidence pack: [reports/parallel_run_live.md](../reports/parallel_run_live.md).
  The live run also confirmed KD-01 on real data: 7 PIREPs had a more severe second layer.

## 7. Post-cutover stabilisation

After cutover, keep running `reconcile` daily against the archived raw files until three
more consecutive green batches confirm nothing regressed. Keep v1 runnable, read-only, for
that window. Any new unexplained discrepancy reopens the decision.
