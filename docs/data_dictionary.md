# Data dictionary

Generated from `turbqc/schema.py`. Table `reports`, one row per delivered report.

| Column | Type | Origin | Description |
|---|---|---|---|
| report_id | VARCHAR | derived | SHA-1 of the delivered message (type, aircraft, receipt and observation time, position, text). Makes re-polling idempotent. |
| run_id | VARCHAR | pipeline | Ingest run that first stored the report. |
| ingested_at | TIMESTAMP | pipeline | UTC time TurbQC stored the report. |
| receipt_time | TIMESTAMP | feed: receipt_time | UTC time the provider received the report. |
| obs_time | TIMESTAMP | feed: observation_time | UTC time of the observation. |
| report_type | VARCHAR | feed: report_type | AIREP (automated ACARS position report), PIREP or Urgent PIREP. |
| stream | VARCHAR | derived | acars_airep or pirep. Mirrors the airline-ACARS vs other-input split of an operational platform. |
| source_id | VARCHAR | derived from raw_text | Monitoring unit for volume, gaps and latency. AIREP: ground relay code parsed from the message trailer; PIREP: 'PIREP'. |
| operator | VARCHAR | derived from aircraft_ref | ICAO airline designator taken from an AIREP callsign (e.g. BAW from BAW39D). Proxy for 'data-contributing airline'. |
| aircraft_ref | VARCHAR | feed: aircraft_ref | Callsign for AIREPs, aircraft type for PIREPs. |
| lat | DOUBLE | feed: latitude | Latitude, decimal degrees. |
| lon | DOUBLE | feed: longitude | Longitude, decimal degrees. |
| alt_ft | INTEGER | feed: altitude_ft_msl | Altitude, feet MSL. |
| flight_level | INTEGER | derived | alt_ft / 100, rounded. |
| tb_intensity | VARCHAR | feed: turbulence_intensity | Turbulence intensity, first layer (NEG, LGT, MOD, SEV, ...). |
| tb_intensity2 | VARCHAR | feed: turbulence_intensity (2nd) | Turbulence intensity, second layer. |
| tb_type | VARCHAR | feed: turbulence_type | Turbulence type (CAT, CHOP, ...). |
| tb_freq | VARCHAR | feed: turbulence_freq | Turbulence frequency (OCNL, CONT, ...). |
| tb_base_ft | INTEGER | feed: turbulence_base_ft_msl | Turbulence layer base, feet MSL. |
| tb_top_ft | INTEGER | feed: turbulence_top_ft_msl | Turbulence layer top, feet MSL. |
| edr_band | VARCHAR | derived | EDR band mapped from the most severe reported intensity. An ESTIMATE for subjective reports, see docs/methods.md. |
| edr_est | DOUBLE | derived | Representative EDR value for edr_band (m^2/3 s^-1). |
| severity | DOUBLE | derived | Ordinal severity 0 (NIL) to 4 (EXTM) used for spatial consistency. |
| temp_c | DOUBLE | feed: temp_c | Outside air temperature, C. |
| wind_dir_deg | DOUBLE | feed: wind_dir_degrees | Wind direction, degrees true. |
| wind_speed_kt | DOUBLE | feed: wind_speed_kt | Wind speed, knots. |
| vert_gust_kt | DOUBLE | feed: vert_gust_kt | Derived vertical gust, knots. |
| flag_bad_location | BOOLEAN | feed: bad_location | Provider could not locate the report reliably. |
| flag_no_time_stamp | BOOLEAN | feed: no_time_stamp | Message had no time; provider assumed one. |
| flag_mid_point_assumed | BOOLEAN | feed: mid_point_assumed | Provider assumed the mid-point of a route segment. |
| flag_no_flt_lvl | BOOLEAN | feed: no_flt_lvl | Provider could not decode a flight level. |
| flag_flt_lvl_range | BOOLEAN | feed: flt_lvl_range | Altitude was given as a range. |
| flag_agl | BOOLEAN | feed: above_ground_level_indicated | Altitude was given above ground level. |
| raw_text | VARCHAR | feed: raw_text | Original message text. |

## Derived tables

| Table | Grain | Purpose |
|---|---|---|
| ingest_runs | one row per poll | Audit of every collection attempt, including failures and schema drift |
| rule_meta | one row per rule | Rule catalogue as loaded at the last validation |
| rule_failures | rule x report | Current failures (rebuilt on each validation) |
| rule_runs | rule x validation | History of checked / failed counts |
| spatial_status | turbulence report | Neighbour count, neighbourhood median severity, status |
| report_quality | report | 0/1 per KPI dimension, the basis of every KPI view |
| report_confidence | report | Confidence factors and the final 0..1 confidence |
| alerts | alert | Volume, dropout, gap, latency, flat-line and collector alerts |
| product_grid | hour x 1 deg cell x FL band | Output product with confidence-weighted EDR |
| product_checks | check | Output controls including the input/output reconciliation |
| fault_results | scenario | Latest fault-injection scores |
| verif_pairs | report x nowcast age | GTG Nowcast EDR (grid point and neighbourhood max) matched to each turbulence report |
| verif_files | GTGN analysis | Audit of every nowcast file fetched (status, size, pairs produced) |
| migration_runs | parallel-run batch | Keys, match rate, discrepancy counts and green/red per batch |
| migration_register | discrepancy | Every v1/v2 difference with its explanation (known difference or inherited) |
| migration_criteria | batch x criterion | Sign-off criteria SO-01..SO-06 per batch |
| migration_signoff | criterion | Latest sign-off incl. SO-07 parallel-run streak; GO only if all pass |
