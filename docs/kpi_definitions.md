# KPI definitions

All KPIs are shares between 0 and 1. Each is computed per report in table
`report_quality` and then averaged over any grouping (stream, source, operator, hour), so
every view uses one definition.

| KPI | Per-report value | Counts against it | Target |
|---|---|---|---|
| **Completeness** | share of the stream's required fields that are present (`settings.yaml › required_fields`) | missing fields | 98% |
| **Validity** | 1 unless an error- or warning-level *validity* rule fails | R006, R010–R023 | 99% |
| **Timeliness** | 1 if −5 ≤ latency ≤ 15 min and no error/warning *timeliness* rule fails | R030–R032, SLA | 95% |
| **Consistency** | 1 unless an error- or warning-level *consistency* rule fails | R050–R057 | 97% |
| **Uniqueness** | 1 unless a *uniqueness* rule fails | R040, R041 | 99.9% |
| **DQ score** | weighted mean of the five (equal weights by default) | — | — |

Latency = receipt time − observation time.

**RAG status**: green at or above target, amber within 2 percentage points below it, red
otherwise. Targets, weights and the amber margin are in `settings.yaml › kpi`.

## Monitoring KPIs (alerts, not per-report)

| Signal | Definition | Default threshold |
|---|---|---|
| Volume control | robust z-score of hourly report count vs baseline (methods §5) | \|z\| > 3 |
| Dropout | zero reports in an hour whose baseline is ≥ 5 | — |
| Gap | minutes between consecutive reports of a source | AIREP 20 min, PIREP 60 min |
| Latency p95 | 95th percentile latency per source-hour (≥ 5 reports) | > SLA (15 min) |
| Flat-line | one field identical across ≥ 10 reports of a source-hour | temp, wind dir, wind speed |
| Collector gap | time between successful polls | > 75 min |

## Product acceptance KPIs (nowcast verification)

Per flight-level band and nowcast age (0, 30, 60, 120 min), on QC-passed reports by
default. Full definitions are in [methods §9](methods.md#9-nowcast-verification-gtg-nowcast-vs-reports).

| KPI | Definition | Illustrative acceptance (lag 0) |
|---|---|---|
| POD | hits / observed MOD+ events | ≥ 0.50 |
| FAR | false alarms / forecast events | ≤ 0.80 |
| CSI | hits / (hits + misses + false alarms) | reported with 90% CI |
| ROC area | discrimination across EDR thresholds | ≥ 0.65 |
| Staleness | relative CSI drop from 0 to 60 min age | ≤ 25% |
| Sample size | matched pairs | ≥ 30 |

## Detection KPIs (fault injection)

| KPI | Definition |
|---|---|
| Recall | detected injected faults / injected faults |
| False-alarm rate | new flags outside the fault / clean rows (row faults) or per source-hour (event faults) |
| Collateral | untouched reports in the same flight made inconsistent by the fault (reported, not penalised) |
