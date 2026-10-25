# Rule catalogue

Generated from `config/rules.yaml` by `python -m turbqc docs`. Do not edit by hand.

| ID | Rule | Dimension | Severity | Requirement | Applies to | Description |
|---|---|---|---|---|---|---|
| R001 | Receipt time present | completeness | error | DQ-REQ-01 | acars_airep, pirep | The time the report reached the provider must be present; latency cannot be measured without it. |
| R002 | Observation time present | completeness | error | DQ-REQ-01 | acars_airep, pirep | The time the observation was made must be present. |
| R003 | Aircraft identifier present | completeness | error | DQ-REQ-01 | acars_airep, pirep | AIREPs carry a callsign, PIREPs an aircraft type. Without it duplicates and tracks cannot be resolved. |
| R004 | Position present | completeness | error | DQ-REQ-01 | acars_airep, pirep | Latitude and longitude must both be present. |
| R005 | Altitude present | completeness | error | DQ-REQ-01 | acars_airep, pirep | Turbulence is altitude-specific; a report without altitude cannot be placed in a flight-level band. |
| R006 | Report type in code list | validity | error | DQ-REQ-03 | acars_airep, pirep, unknown | Report type must be AIREP, PIREP or Urgent PIREP. |
| R007 | Original message text present | completeness | warning | DQ-REQ-01 | acars_airep, pirep | The raw message is needed for audit and for the cross-checks R053, R054 and R057. |
| R010 | Latitude in range | validity | error | DQ-REQ-02 | acars_airep, pirep | Latitude must lie in [-90, 90]. |
| R011 | Longitude in range | validity | error | DQ-REQ-02 | acars_airep, pirep | Longitude must lie in [-180, 180]. |
| R012 | Altitude in range | validity | error | DQ-REQ-02 | acars_airep, pirep | Altitude must lie between -1,500 ft (below-sea-level airfields) and 60,000 ft. |
| R013 | Not at null island | validity | error | DQ-REQ-02 | acars_airep, pirep | A position of exactly 0,0 is almost always a default value, not a real fix. |
| R014 | AIREP at en-route altitude | validity | warning | DQ-REQ-02 | acars_airep | Automated position reports come from aircraft en route; one below 10,000 ft is unusual and worth a look. |
| R015 | Turbulence intensity in code list | validity | error | DQ-REQ-03 | acars_airep, pirep | A reported intensity that has no EDR mapping cannot be used in the product. |
| R016 | Turbulence layer base below top | validity | warning | DQ-REQ-02 | acars_airep, pirep | When a turbulence layer is given, its base must not be above its top. |
| R017 | EDR estimate in range | validity | error | DQ-REQ-02 | acars_airep, pirep | EDR is reported on a 0 to 1 scale. |
| R018 | Temperature in range | validity | error | DQ-REQ-02 | acars_airep, pirep | Air temperature must lie in [-90, +50] C. |
| R019 | Wind speed in range | validity | error | DQ-REQ-02 | acars_airep, pirep | Wind speed must lie in [0, 300] kt. |
| R020 | Wind direction in range | validity | error | DQ-REQ-02 | acars_airep, pirep | Wind direction must lie in [0, 360] degrees. |
| R021 | Vertical gust in range | validity | warning | DQ-REQ-02 | acars_airep, pirep | Derived vertical gust above 100 kt is implausible. |
| R022 | Provider flagged bad location | validity | warning | DQ-REQ-10 | acars_airep, pirep | The provider could not locate the report reliably. Surfaced rather than dropped. |
| R023 | Provider flagged missing flight level | validity | warning | DQ-REQ-10 | acars_airep, pirep | The provider could not decode a flight level from the message. |
| R030 | Observation not after receipt | timeliness | error | DQ-REQ-04 | acars_airep, pirep | An observation stamped later than its own receipt time (beyond clock tolerance) points to a clock or decoding fault. |
| R031 | Observation not after ingestion | timeliness | error | DQ-REQ-04 | acars_airep, pirep | An observation stamped later than the moment TurbQC ingested it is in the future. |
| R032 | Not stale on arrival | timeliness | warning | DQ-REQ-05 | acars_airep, pirep | A report older than the stale threshold when received is of little nowcasting value. |
| R033 | Within latency SLA | timeliness | info | DQ-REQ-05 | acars_airep, pirep | Counted for the timeliness KPI; not a record defect on its own. |
| R034 | Provider assumed the time stamp | timeliness | info | DQ-REQ-10 | acars_airep, pirep | The provider inferred the observation time (no time in the message or mid-point assumed). |
| R040 | Duplicate delivery | uniqueness | warning | DQ-REQ-06 | acars_airep, pirep | The same observation (aircraft, time, position, altitude, intensity) delivered more than once. The first receipt is kept; later copies fail. |
| R041 | Conflicting duplicate | uniqueness | warning | DQ-REQ-06 | acars_airep | Two reports from the same callsign at the same minute that disagree on position or altitude. |
| R050 | Plausible implied ground speed | consistency | error | DQ-REQ-07 | acars_airep | Speed implied by consecutive positions of one callsign must not exceed the limit (a one-minute tolerance absorbs timestamp rounding). To blame the right report, a fast leg fails the report only if it is a spike (the next leg is fast too) or the start of a shift (skipping either this or the previous report does not help). The report after a one-off spike is therefore not flagged. |
| R051 | Plausible vertical rate | consistency | error | DQ-REQ-07 | acars_airep | Altitude change between consecutive reports of one callsign must not exceed the vertical-rate limit (same spike-or-shift blame logic as R050). |
| R052 | Position not frozen | consistency | error | DQ-REQ-07 | acars_airep | An aircraft en route cannot report the identical position minutes apart; a stuck position source is the likely cause. |
| R053 | Altitude matches message text | consistency | error | DQ-REQ-08 | acars_airep, pirep | The decoded altitude must match the flight level written in the original message (F360 / FL360). Catches unit and decoding errors that range checks miss. |
| R054 | Position matches message text | consistency | error | DQ-REQ-08 | acars_airep | The decoded latitude and longitude must match the position written in the AIREP (DDMMN DDDMMW). |
| R055 | Spatially consistent turbulence | consistency | warning | DQ-REQ-09 | acars_airep, pirep | A turbulence report with at least 2 neighbours (within 100 km, 60 min and 2000 ft) whose severity differs from the neighbourhood median by 2 classes or more. Built in table spatial_status; see docs/methods.md. |
| R056 | Temperature consistent with altitude | consistency | warning | DQ-REQ-08 | acars_airep, pirep | Above 25,000 ft the air is essentially never warmer than 0 C; a positive value suggests a sign or unit error. |
| R057 | Observation time matches message text | consistency | error | DQ-REQ-08 | acars_airep, pirep | The decoded observation time must match the HHMM written in the message (AIREP time after the position, PIREP /TM). Catches clock drift introduced in processing. |

## SQL

### R001 Receipt time present

`fail_when`

```sql
receipt_time IS NULL
```

### R002 Observation time present

`fail_when`

```sql
obs_time IS NULL
```

### R003 Aircraft identifier present

`fail_when`

```sql
aircraft_ref IS NULL OR trim(aircraft_ref) = ''
```

### R004 Position present

`fail_when`

```sql
lat IS NULL OR lon IS NULL
```

### R005 Altitude present

`fail_when`

```sql
alt_ft IS NULL
```

### R006 Report type in code list

`fail_when`

```sql
report_type IS NULL OR report_type NOT IN ('AIREP', 'PIREP', 'Urgent PIREP')
```

### R007 Original message text present

`fail_when`

```sql
raw_text IS NULL OR trim(raw_text) = ''
```

### R010 Latitude in range

`fail_when`

```sql
lat < -90 OR lat > 90
```

### R011 Longitude in range

`fail_when`

```sql
lon < -180 OR lon > 180
```

### R012 Altitude in range

`fail_when`

```sql
alt_ft < -1500 OR alt_ft > 60000
```

### R013 Not at null island

`fail_when`

```sql
lat = 0 AND lon = 0
```

### R014 AIREP at en-route altitude

`fail_when`

```sql
alt_ft < 10000
```

### R015 Turbulence intensity in code list

`fail_when`

```sql
tb_intensity IS NOT NULL AND edr_band IS NULL
```

### R016 Turbulence layer base below top

`fail_when`

```sql
tb_base_ft IS NOT NULL AND tb_top_ft IS NOT NULL AND tb_base_ft > tb_top_ft
```

### R017 EDR estimate in range

`fail_when`

```sql
edr_est < 0 OR edr_est > 1
```

### R018 Temperature in range

`fail_when`

```sql
temp_c < -90 OR temp_c > 50
```

### R019 Wind speed in range

`fail_when`

```sql
wind_speed_kt < 0 OR wind_speed_kt > 300
```

### R020 Wind direction in range

`fail_when`

```sql
wind_dir_deg < 0 OR wind_dir_deg > 360
```

### R021 Vertical gust in range

`fail_when`

```sql
abs(vert_gust_kt) > 100
```

### R022 Provider flagged bad location

`fail_when`

```sql
coalesce(flag_bad_location, false)
```

### R023 Provider flagged missing flight level

`fail_when`

```sql
coalesce(flag_no_flt_lvl, false)
```

### R030 Observation not after receipt

`fail_when`

```sql
date_diff('second', receipt_time, obs_time) > 5 * 60
```

### R031 Observation not after ingestion

`fail_when`

```sql
date_diff('second', ingested_at, obs_time) > 5 * 60
```

### R032 Not stale on arrival

`fail_when`

```sql
latency_min > 60
```

### R033 Within latency SLA

`fail_when`

```sql
latency_min > 15
```

### R034 Provider assumed the time stamp

`fail_when`

```sql
coalesce(flag_no_time_stamp, false) OR coalesce(flag_mid_point_assumed, false)
```

### R040 Duplicate delivery

`fail_query`

```sql
SELECT report_id FROM (
  SELECT report_id,
         row_number() OVER (
           PARTITION BY aircraft_ref, obs_time, round(lat, 2), round(lon, 2), alt_ft,
                        coalesce(tb_intensity, '')
           ORDER BY receipt_time, report_id) AS rn
  FROM v_reports
  WHERE obs_time IS NOT NULL AND aircraft_ref IS NOT NULL
) WHERE rn > 1
```

### R041 Conflicting duplicate

`fail_query`

```sql
SELECT report_id FROM (
  SELECT report_id, lat, lon, alt_ft,
         first_value(lat) OVER w AS lat0,
         first_value(lon) OVER w AS lon0,
         first_value(alt_ft) OVER w AS alt0,
         row_number() OVER w AS rn
  FROM v_reports
  WHERE track_id IS NOT NULL AND obs_time IS NOT NULL
  WINDOW w AS (PARTITION BY track_id, obs_time ORDER BY receipt_time, report_id)
) WHERE rn > 1 AND (abs(lat - lat0) > 0.01 OR abs(lon - lon0) > 0.01 OR abs(alt_ft - alt0) > 100)
```

### R050 Plausible implied ground speed

`fail_query`

```sql
WITH legs AS (
  SELECT report_id,
    CASE WHEN date_diff('second', prev_obs_time, obs_time) BETWEEN 1 AND 180 * 60
         THEN kts(prev_lat, prev_lon, prev_obs_time, lat, lon, obs_time) END AS v_in,
    CASE WHEN date_diff('second', obs_time, next_obs_time) BETWEEN 1 AND 180 * 60
         THEN kts(lat, lon, obs_time, next_lat, next_lon, next_obs_time) END AS v_out,
    CASE WHEN date_diff('second', prev2_obs_time, obs_time) BETWEEN 1 AND 180 * 60
         THEN kts(prev2_lat, prev2_lon, prev2_obs_time, lat, lon, obs_time) END AS v_skip_prev,
    CASE WHEN date_diff('second', prev_obs_time, next_obs_time) BETWEEN 1 AND 180 * 60
         THEN kts(prev_lat, prev_lon, prev_obs_time, next_lat, next_lon, next_obs_time) END AS v_skip_self
  FROM v_track)
SELECT report_id FROM legs
WHERE v_in > 750
  AND (v_out IS NULL OR v_out > 750
       OR (v_skip_prev > 750 AND v_skip_self > 750))
```

### R051 Plausible vertical rate

`fail_query`

```sql
WITH legs AS (
  SELECT report_id,
    CASE WHEN date_diff('second', prev_obs_time, obs_time) BETWEEN 1 AND 180 * 60
         THEN fpm(prev_alt_ft, prev_obs_time, alt_ft, obs_time) END AS v_in,
    CASE WHEN date_diff('second', obs_time, next_obs_time) BETWEEN 1 AND 180 * 60
         THEN fpm(alt_ft, obs_time, next_alt_ft, next_obs_time) END AS v_out,
    CASE WHEN date_diff('second', prev2_obs_time, obs_time) BETWEEN 1 AND 180 * 60
         THEN fpm(prev2_alt_ft, prev2_obs_time, alt_ft, obs_time) END AS v_skip_prev,
    CASE WHEN date_diff('second', prev_obs_time, next_obs_time) BETWEEN 1 AND 180 * 60
         THEN fpm(prev_alt_ft, prev_obs_time, next_alt_ft, next_obs_time) END AS v_skip_self
  FROM v_track)
SELECT report_id FROM legs
WHERE v_in > 8000
  AND (v_out IS NULL OR v_out > 8000
       OR (v_skip_prev > 8000 AND v_skip_self > 8000))
```

### R052 Position not frozen

`fail_query`

```sql
SELECT report_id FROM v_track
WHERE prev_obs_time IS NOT NULL
  AND date_diff('second', prev_obs_time, obs_time) >= 5 * 60
  AND date_diff('second', prev_obs_time, obs_time) <= 180 * 60
  AND lat = prev_lat AND lon = prev_lon
```

### R053 Altitude matches message text

`fail_query`

```sql
SELECT report_id FROM (
  SELECT report_id, alt_ft,
         coalesce(nullif(regexp_extract(raw_text, '\sF(\d{3})\s', 1), ''),
                  nullif(regexp_extract(raw_text, '/FL(\d{3})(\s|/|$)', 1), '')) AS fl_raw
  FROM v_reports
  WHERE alt_ft IS NOT NULL AND raw_text IS NOT NULL
    AND NOT coalesce(flag_flt_lvl_range, false)
    AND NOT coalesce(flag_agl, false)
) WHERE fl_raw IS NOT NULL
    AND abs(try_cast(fl_raw AS INTEGER) * 100 - alt_ft) > 100
```

### R054 Position matches message text

`fail_query`

```sql
SELECT report_id FROM (
  SELECT report_id, lat, lon,
         regexp_extract(raw_text, '\b(\d{2})(\d{2})([NS])\s?(\d{3})(\d{2})([EW])\b',
                        ['la', 'lam', 'ns', 'lo', 'lom', 'ew']) AS p
  FROM v_reports
  WHERE lat IS NOT NULL AND lon IS NOT NULL AND raw_text IS NOT NULL
) WHERE p.la <> ''
    AND (abs((try_cast(p.la AS DOUBLE) + try_cast(p.lam AS DOUBLE) / 60)
             * CASE WHEN p.ns = 'S' THEN -1 ELSE 1 END - lat) > 0.05
         OR abs((try_cast(p.lo AS DOUBLE) + try_cast(p.lom AS DOUBLE) / 60)
             * CASE WHEN p.ew = 'W' THEN -1 ELSE 1 END - lon) > 0.05)
```

### R055 Spatially consistent turbulence

`fail_query`

```sql
SELECT report_id FROM spatial_status WHERE status = 'conflict'
```

### R056 Temperature consistent with altitude

`fail_when`

```sql
alt_ft >= 25000 AND temp_c > 0
```

### R057 Observation time matches message text

`fail_query`

```sql
SELECT report_id FROM (
  SELECT report_id,
         abs(try_cast(substr(hhmm, 1, 2) AS INTEGER) * 60 + try_cast(substr(hhmm, 3, 2) AS INTEGER)
             - (hour(obs_time) * 60 + minute(obs_time))) AS d
  FROM (
    SELECT report_id, obs_time,
           coalesce(nullif(regexp_extract(raw_text, '[NS]\s?\d{5}[EW]\s+(\d{4})\s', 1), ''),
                    nullif(regexp_extract(raw_text, '/TM\s?(\d{4})', 1), '')) AS hhmm
    FROM v_reports
    WHERE obs_time IS NOT NULL AND raw_text IS NOT NULL
      AND NOT coalesce(flag_no_time_stamp, false)
      AND NOT coalesce(flag_mid_point_assumed, false)
  ) WHERE hhmm IS NOT NULL
) WHERE least(d, 1440 - d) > 2
```

