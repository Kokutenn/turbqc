# Methods and assumptions

## 1. What the data is, and what it is not

The feed is the NOAA Aviation Weather Center bulk file of aircraft reports
(`aircraftreports.cache.csv.gz`, refreshed every minute, roughly the last 90 minutes).
A live sample on 24 Oct 2026 held 1,840 reports: **91% AIREPs, 9% PIREPs**.

| Stream | What it is | Turbulence content |
|---|---|---|
| `acars_airep` | Automated position reports sent over ACARS, mostly North Atlantic. Objective position, time, temperature and wind. | Rare. Fewer than 1% carry a turbulence group, and those are pilot-entered intensities, not measured EDR. |
| `pirep` | Pilot reports, mostly over the US. | Common, but **subjective**: "moderate" in a regional jet is not "moderate" in a widebody. |

**This feed contains no automated, aircraft-computed EDR.** Operational EDR programmes
(where the aircraft computes eddy dissipation rate and downlinks it) are not public. TurbQC
therefore:

* uses AIREPs as the stand-in for an airline-provided ACARS stream: high-volume, automated
  and kinematically checkable, which is ideal for completeness, timeliness, uniqueness and
  consistency controls;
* converts PIREP intensities into an **EDR estimate** (below) and labels it as an estimate
  everywhere it appears.

## 2. Intensity to EDR mapping

| Intensity | Band | EDR estimate (m^2/3 s^-1) | Ordinal severity |
|---|---|---|---|
| NEG, SMTH | NIL | 0.05 | 0 |
| SMTH-LGT | LGT | 0.10 | 0.5 |
| LGT | LGT | 0.15 | 1 |
| LGT-MOD | LGT-MOD | 0.20 | 1.5 |
| MOD | MOD | 0.30 | 2 |
| MOD-SEV | MOD-SEV | 0.45 | 2.5 |
| SEV | SEV | 0.55 | 3 |
| SEV-EXTM | SEV | 0.65 | 3.5 |
| EXTM | EXTM | 0.75 | 4 |

Anchors: the ICAO Annex 3 peak-EDR reporting thresholds (moderate above 0.20, severe above
0.45). Values inside a band are representative mid-points. **Assumption, not truth.** A
pilot's "moderate" spans a wide EDR range and depends on aircraft weight and speed. The
mapping lives in `config/settings.yaml` so it can be replaced, for example with a
type-aware mapping or one calibrated against co-located automated EDR, without code
changes. When two turbulence layers are reported, the more severe one drives the estimate.

## 3. Rule engine

Rules are SQL predicates or queries in `config/rules.yaml`, executed by DuckDB. Thresholds
are `${placeholders}` filled from `settings.yaml`, so changing an acceptance criterion is a
reviewed config change, not a code change. Each rule carries a severity:

* **error**: the record is rejected from the output product (confidence 0);
* **warning**: the record is kept, flagged, and its confidence reduced;
* **info**: the record is counted for monitoring only.

### Blame assignment in kinematic checks (R050, R051)

A naive "speed between consecutive reports" check flags two reports for every bad one:
the jump out and the jump back. TurbQC flags report *i* only if the leg into it is
implausible **and** either

* the leg out of it is implausible too (a one-off spike), or
* skipping report *i* or report *i−1* does not repair the track (the start of a persistent
  shift).

A one-minute tolerance is added to every time difference because AIREP times have
minute resolution.

### Cross-checks against the original message (R053, R054, R057)

Decoded fields are compared with what the original text says: flight level (`F360`,
`/FL360`), position (`6017N04413W`) and time (`1526`, `/TM 1522`). These catch faults that
range checks cannot see. An altitude decoded in metres (10,668 instead of 35,000 ft) is
in range; a clock that drifts by 6 minutes looks normal. On live data these three rules
raised **zero** false positives on the first 1,839 reports.

## 4. Spatial consistency (R055)

For every report with a turbulence intensity, neighbours are reports from **other**
aircraft within 100 km, ±60 min and ±2,000 ft (all configurable). Self-matches and
duplicate deliveries are excluded. With *m* = median neighbour severity:

| Status | Condition |
|---|---|
| isolated | no neighbours |
| sparse | fewer than 2 neighbours |
| confirmed | \|severity − m\| ≤ 0.5 |
| partial | between confirmed and conflict |
| conflict | \|severity − m\| ≥ 2 classes (e.g. SEV among LGT) |

Conflicts are warnings, not errors. Turbulence is patchy, and a lone severe report can be
real. The status also feeds the confidence score, and an isolated report is not
penalised as heavily as a contradicted one.

## 5. Volume control chart

Per source and hour, from receipt time:

* **Baseline**: once 3 days of history exist, the median of the same hour of day over the
  previous 7 days, which captures the strong daily cycle of oceanic traffic. Before that, a
  6-hour rolling median.
* **Spread**: 1.4826 × MAD of the baseline residuals over the previous 48 h, so each
  source's own burstiness is learned. Floored at √(2 × baseline): reports come in bursts
  (one flight produces several), so the variance is greater than Poisson.
* **Alerts**: |z| > 3 gives volume_high or volume_low; zero reports where the baseline
  expects 5 or more gives a dropout. Consecutive flagged hours merge into one alert.
* **Learning period**: high/low alerts are suppressed until the hour-of-day baseline
  exists, because a rolling median lags the daily ramp and produced repeated false alarms
  in testing. Dropouts and gaps are alerted from the first hour.
* **Monitoring the monitor**: an hour only counts as observed if the collector's polls
  cover it completely (the bulk file keeps ~90 minutes, so a poll at time *t* captures
  everything received after *t* − 75 min, conservatively). Collector outages raise a
  `collector_gap` alert instead of false dropouts on every source.

## 6. Per-report confidence

```
confidence = gate × warnings × timeliness × source × corroboration × subjectivity
```

| Factor | Value |
|---|---|
| gate | 0 if any error-level rule fails, else 1 |
| warnings | 0.7 per warning-level failure |
| timeliness | 0.8 if latency above SLA |
| source | Beta(1,1) posterior mean of the clean-report share of the source over 24 h |
| corroboration | confirmed 1.0 · partial 0.9 · sparse 0.9 · isolated 0.85 · conflict 0.5 |
| subjectivity | 0.8 for PIREPs (pilot judgement) |

The factors are deliberately simple and transparent. They are a ranking device, not a
calibrated probability. Calibrating them, for example against later confirmation by other
aircraft, is listed as future work.

## 7. Output product

`product_grid`: hourly × 1° × flight-level band (SFC–FL100, FL100–200, FL200–300,
FL300–400, FL400+). Per cell: report count, sources, max EDR, confidence-weighted EDR, and
`cell_confidence = 1 − ∏(1 − cᵢ)` (noisy-OR: the chance that at least one contributing
report is right, assuming independence).

Output controls (`product_checks`): P01 reconciles every placeable input report to
*gridded + rejected*, with nothing lost or invented; P02–P05 check keys and value ranges;
P06 checks freshness.

## 8. Fault injection

Each scenario corrupts its own copy of a clean dataset, reruns every check and scores the
flags against ground truth:

* **recall** = injected faults detected / injected;
* **collateral** = untouched reports *in the same flight* that the fault made genuinely
  inconsistent (for example, drifted timestamps re-order a track). They are reported
  separately rather than hidden or counted as false alarms;
* **false alarms** = new flags anywhere else; rate = false alarms / clean rows (row level)
  or per monitored source-hour (event level).

Flags already present in the clean baseline are excluded from both counts.

## 9. Nowcast verification (GTG Nowcast vs reports)

**Product.** The NWS Graphical Turbulence Guidance **Nowcast** (GTGN) is EDR on the 3 km
HRRR Lambert grid over CONUS: 51 levels from 100 ft to FL500 in 1,000 ft steps, a new
analysis every 15 minutes. It is published as GRIB2 on NOAA NOMADS, which keeps about
2 days. GTG *forecast* grids with lead times were not found in a public GRIB feed, so
"lead time" is studied as **nowcast age** instead: the report is compared with the
analysis valid 0, 30, 60 and 120 minutes before it. That answers an acceptance question
directly: how stale can the nowcast be before its skill drops?

**Matching.**

* Observations: every report with a turbulence intensity (mostly PIREPs; past reports are
  back-filled hour by hour from the Data API, because the bulk file only holds ~90 min).
* Each report goes to the analysis on the nearest 30-minute slot, minus the lag.
* Position: the report's latitude/longitude are projected onto the Lambert grid with the
  grid's own parameters (sphere R = 6,371,229 m). Unit tests check the projection against
  ecCodes at 7 reference points to within 0.01 grid cells.
* Two matched values per report: the **grid point** at the nearest level, and the
  **neighbourhood maximum** within 20 km and ±1,000 ft. PIREP positions are given relative
  to a navaid and are often tens of km off, which a point match would punish unfairly.
* GRIB2 is streamed into memory, split into messages using the length in section 0, and
  decoded with ecCodes one level at a time. Only the levels some report needs are
  decoded, and nothing is written to disk.

**Events.** Observed event = reported severity MOD or worse (≥ 2). Forecast event =
GTGN EDR ≥ 0.20, the same ICAO moderate anchor used in the intensity mapping.

**Scores.** From the contingency table (hits *a*, false alarms *b*, misses *c*, correct
negatives *d*):

| Score | Formula | Perfect |
|---|---|---|
| POD (probability of detection) | a / (a + c) | 1 |
| FAR (false-alarm ratio) | b / (a + b) | 0 |
| CSI (critical success index) | a / (a + b + c) | 1 |
| POFD (probability of false detection) | b / (b + d) | 0 |
| Bias | (a + b) / (a + c) | 1 |
| Peirce skill score | POD − POFD | 1 |
| ROC area | area under POD vs POFD across EDR thresholds 0.04–0.45 | 1 (0.5 = no skill) |

90% intervals come from 300 bootstrap resamples of the matched pairs. Scores are given
per flight-level band and per nowcast age.

**QC → verification.** Scores are computed three ways: on all reports, on QC-passed
reports (confidence > 0), and with each report weighted by its confidence. If the verdict
on a product changes with the QC subset, the observations are not yet good enough to
accept or reject that product.

**Acceptance criteria** (`settings.yaml › verification.acceptance`, illustrative). Per
flight-level band at lag 0: at least 30 pairs, POD ≥ 0.5, FAR ≤ 0.8, ROC area ≥ 0.65, and a
relative CSI drop of at most 25% at 60 minutes' age.

**Caveats.**

* PIREPs are not a random sample: pilots report more when it is bumpy, and "NEG" reports
  are under-represented. POD and FAR are therefore conditional on a report existing. ROC
  area is the most robust comparison across bands and ages.
* A subjective intensity is compared with an objective EDR (see §2), so part of any
  disagreement is observation error, not forecast error.
* Only about 2 days of GTGN are available at any time, so verification must run at least
  daily to build a long record.

## 10. Known limitations

* No measured EDR in the public feed (see §1).
* Relay codes parsed from AIREP trailers are used as "sources". They are a reasonable proxy
  for distinct delivery paths, but they are inferred, not documented.
* Synthetic data (used for tests and the offline demo) is realistic in shape, not in every
  statistical detail. Thresholds tuned on it must be re-validated on collected history.
* Confidence factors are expert-set, not learned.
