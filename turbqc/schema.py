"""DuckDB schema. Column descriptions double as the data dictionary (docs/data_dictionary.md)."""

# (column, DuckDB type, description, origin)
REPORT_COLUMNS = [
    ("report_id", "VARCHAR", "SHA-1 of the delivered message (type, aircraft, receipt and observation time, position, text). Makes re-polling idempotent.", "derived"),
    ("run_id", "VARCHAR", "Ingest run that first stored the report.", "pipeline"),
    ("ingested_at", "TIMESTAMP", "UTC time TurbQC stored the report.", "pipeline"),
    ("receipt_time", "TIMESTAMP", "UTC time the provider received the report.", "feed: receipt_time"),
    ("obs_time", "TIMESTAMP", "UTC time of the observation.", "feed: observation_time"),
    ("report_type", "VARCHAR", "AIREP (automated ACARS position report), PIREP or Urgent PIREP.", "feed: report_type"),
    ("stream", "VARCHAR", "acars_airep or pirep. Mirrors the airline-ACARS vs other-input split of an operational platform.", "derived"),
    ("source_id", "VARCHAR", "Monitoring unit for volume, gaps and latency. AIREP: ground relay code parsed from the message trailer; PIREP: 'PIREP'.", "derived from raw_text"),
    ("operator", "VARCHAR", "ICAO airline designator taken from an AIREP callsign (e.g. BAW from BAW39D). Proxy for 'data-contributing airline'.", "derived from aircraft_ref"),
    ("aircraft_ref", "VARCHAR", "Callsign for AIREPs, aircraft type for PIREPs.", "feed: aircraft_ref"),
    ("lat", "DOUBLE", "Latitude, decimal degrees.", "feed: latitude"),
    ("lon", "DOUBLE", "Longitude, decimal degrees.", "feed: longitude"),
    ("alt_ft", "INTEGER", "Altitude, feet MSL.", "feed: altitude_ft_msl"),
    ("flight_level", "INTEGER", "alt_ft / 100, rounded.", "derived"),
    ("tb_intensity", "VARCHAR", "Turbulence intensity, first layer (NEG, LGT, MOD, SEV, ...).", "feed: turbulence_intensity"),
    ("tb_intensity2", "VARCHAR", "Turbulence intensity, second layer.", "feed: turbulence_intensity (2nd)"),
    ("tb_type", "VARCHAR", "Turbulence type (CAT, CHOP, ...).", "feed: turbulence_type"),
    ("tb_freq", "VARCHAR", "Turbulence frequency (OCNL, CONT, ...).", "feed: turbulence_freq"),
    ("tb_base_ft", "INTEGER", "Turbulence layer base, feet MSL.", "feed: turbulence_base_ft_msl"),
    ("tb_top_ft", "INTEGER", "Turbulence layer top, feet MSL.", "feed: turbulence_top_ft_msl"),
    ("edr_band", "VARCHAR", "EDR band mapped from the most severe reported intensity. An ESTIMATE for subjective reports, see docs/methods.md.", "derived"),
    ("edr_est", "DOUBLE", "Representative EDR value for edr_band (m^2/3 s^-1).", "derived"),
    ("severity", "DOUBLE", "Ordinal severity 0 (NIL) to 4 (EXTM) used for spatial consistency.", "derived"),
    ("temp_c", "DOUBLE", "Outside air temperature, C.", "feed: temp_c"),
    ("wind_dir_deg", "DOUBLE", "Wind direction, degrees true.", "feed: wind_dir_degrees"),
    ("wind_speed_kt", "DOUBLE", "Wind speed, knots.", "feed: wind_speed_kt"),
    ("vert_gust_kt", "DOUBLE", "Derived vertical gust, knots.", "feed: vert_gust_kt"),
    ("flag_bad_location", "BOOLEAN", "Provider could not locate the report reliably.", "feed: bad_location"),
    ("flag_no_time_stamp", "BOOLEAN", "Message had no time; provider assumed one.", "feed: no_time_stamp"),
    ("flag_mid_point_assumed", "BOOLEAN", "Provider assumed the mid-point of a route segment.", "feed: mid_point_assumed"),
    ("flag_no_flt_lvl", "BOOLEAN", "Provider could not decode a flight level.", "feed: no_flt_lvl"),
    ("flag_flt_lvl_range", "BOOLEAN", "Altitude was given as a range.", "feed: flt_lvl_range"),
    ("flag_agl", "BOOLEAN", "Altitude was given above ground level.", "feed: above_ground_level_indicated"),
    ("raw_text", "VARCHAR", "Original message text.", "feed: raw_text"),
]

REPORT_COLS = [c[0] for c in REPORT_COLUMNS]

DDL = [
    "CREATE TABLE IF NOT EXISTS reports ("
    + ", ".join(f"{name} {typ}" for name, typ, _, _ in REPORT_COLUMNS)
    + ", PRIMARY KEY (report_id))",
    """CREATE TABLE IF NOT EXISTS ingest_runs (
        run_id VARCHAR PRIMARY KEY, started_at TIMESTAMP, finished_at TIMESTAMP,
        source_url VARCHAR, http_status INTEGER, bytes BIGINT,
        rows_in_file INTEGER, rows_malformed INTEGER, rows_new INTEGER, rows_already_seen INTEGER,
        status VARCHAR, message VARCHAR, archive_path VARCHAR)""",
    """CREATE TABLE IF NOT EXISTS rule_meta (
        rule_id VARCHAR PRIMARY KEY, name VARCHAR, dimension VARCHAR, severity VARCHAR,
        requirement VARCHAR, applies_to VARCHAR, description VARCHAR)""",
    "CREATE TABLE IF NOT EXISTS rule_failures (rule_id VARCHAR, report_id VARCHAR)",
    """CREATE TABLE IF NOT EXISTS rule_runs (
        validated_at TIMESTAMP, rule_id VARCHAR, checked BIGINT, failed BIGINT, seconds DOUBLE)""",
    """CREATE TABLE IF NOT EXISTS alerts (
        kind VARCHAR, source_id VARCHAR, window_start TIMESTAMP, window_end TIMESTAMP,
        severity VARCHAR, metric DOUBLE, threshold DOUBLE, message VARCHAR)""",
    """CREATE TABLE IF NOT EXISTS verif_pairs (
        report_id VARCHAR, lag_min INTEGER, valid_time TIMESTAMP, edr_point DOUBLE, edr_nbhd DOUBLE,
        source VARCHAR, PRIMARY KEY (report_id, lag_min))""",
    """CREATE TABLE IF NOT EXISTS verif_files (
        valid_time TIMESTAMP, fetched_at TIMESTAMP, status VARCHAR, bytes BIGINT,
        seconds DOUBLE, pairs INTEGER, message VARCHAR)""",
    """CREATE TABLE IF NOT EXISTS fault_results (
        evaluated_at TIMESTAMP, scenario VARCHAR, description VARCHAR, level VARCHAR,
        injected BIGINT, detected BIGINT, recall DOUBLE, collateral BIGINT, false_alarms BIGINT,
        false_alarm_rate DOUBLE, detectors VARCHAR)""",
]

# Derived views; recreated on every connection so rule SQL can rely on them.
VIEWS = [
    """CREATE OR REPLACE MACRO hav_km(lat1, lon1, lat2, lon2) AS
        2 * 6371.0 * asin(sqrt(
            pow(sin(radians(lat2 - lat1) / 2), 2)
            + cos(radians(lat1)) * cos(radians(lat2)) * pow(sin(radians(lon2 - lon1) / 2), 2)))""",
    # Implied speed / vertical rate between two fixes; +60 s absorbs minute-resolution timestamps.
    """CREATE OR REPLACE MACRO kts(lat1, lon1, t1, lat2, lon2, t2) AS
        hav_km(lat1, lon1, lat2, lon2) / 1.852 / ((date_diff('second', t1, t2) + 60) / 3600.0)""",
    """CREATE OR REPLACE MACRO fpm(alt1, t1, alt2, t2) AS
        abs(alt2 - alt1) / ((date_diff('second', t1, t2) + 60) / 60.0)""",
    """CREATE OR REPLACE VIEW v_reports AS
        SELECT *,
               date_diff('second', obs_time, receipt_time) / 60.0 AS latency_min,
               CASE WHEN stream = 'acars_airep' THEN aircraft_ref END AS track_id
        FROM reports""",
    """CREATE OR REPLACE VIEW v_track AS
        SELECT *,
               lag(obs_time)    OVER w AS prev_obs_time,
               lag(lat)         OVER w AS prev_lat,
               lag(lon)         OVER w AS prev_lon,
               lag(alt_ft)      OVER w AS prev_alt_ft,
               lag(obs_time, 2) OVER w AS prev2_obs_time,
               lag(lat, 2)      OVER w AS prev2_lat,
               lag(lon, 2)      OVER w AS prev2_lon,
               lag(alt_ft, 2)   OVER w AS prev2_alt_ft,
               lead(obs_time)   OVER w AS next_obs_time,
               lead(lat)        OVER w AS next_lat,
               lead(lon)        OVER w AS next_lon,
               lead(alt_ft)     OVER w AS next_alt_ft
        FROM v_reports
        WHERE track_id IS NOT NULL AND obs_time IS NOT NULL
        WINDOW w AS (PARTITION BY track_id ORDER BY obs_time, receipt_time, report_id)""",
]
