"""Per-report confidence and the distributed output product.

Most QC pipelines end in a binary accept/reject. TurbQC also scores how much each
accepted report should be trusted, and carries that score into the gridded product
so consumers see confidence next to every EDR value.
"""
import pandas as pd

from . import config

FL_BAND = """CASE WHEN alt_ft < 10000 THEN 'SFC-FL100'
                  WHEN alt_ft < 20000 THEN 'FL100-200'
                  WHEN alt_ft < 30000 THEN 'FL200-300'
                  WHEN alt_ft < 40000 THEN 'FL300-400'
                  ELSE 'FL400+' END"""

PLACEABLE = """edr_est IS NOT NULL AND obs_time IS NOT NULL AND lat IS NOT NULL
               AND lon IS NOT NULL AND alt_ft IS NOT NULL"""


def build_confidence(con, cfg: dict | None = None) -> None:
    cfg = cfg or config.settings()
    c = cfg["confidence"]
    corr = " ".join(f"WHEN '{k}' THEN {v}" for k, v in c["corroboration"].items())
    con.execute(f"""
        CREATE OR REPLACE TABLE report_confidence AS
        WITH f AS (
            SELECT report_id,
                   bool_or(severity = 'error') AS has_error,
                   count(*) FILTER (WHERE severity = 'warning') AS n_warn
            FROM rule_failures JOIN rule_meta USING (rule_id) GROUP BY report_id),
        recent AS (
            SELECT v.source_id,
                   CASE WHEN f.report_id IS NULL OR (NOT f.has_error AND f.n_warn = 0) THEN 1 ELSE 0 END AS clean
            FROM v_reports v LEFT JOIN f USING (report_id)
            WHERE v.receipt_time >= (SELECT max(receipt_time) FROM reports)
                                    - INTERVAL {c['source_window_hours']} HOUR),
        src AS (SELECT source_id, (1 + sum(clean)) / (2.0 + count(*)) AS reliability
                FROM recent GROUP BY source_id),
        parts AS (
            SELECT v.report_id,
                   CASE WHEN coalesce(f.has_error, false) THEN 0.0 ELSE 1.0 END AS f_gate,
                   pow({c['warning_factor']}, coalesce(f.n_warn, 0)) AS f_warnings,
                   CASE WHEN v.latency_min <= {cfg['thresholds']['latency_sla_min']} THEN 1.0
                        ELSE {c['late_factor']} END AS f_timeliness,
                   coalesce(src.reliability, 0.5) AS f_source,
                   CASE s.status {corr} ELSE 1.0 END AS f_corroboration,
                   CASE WHEN v.stream = 'pirep' THEN {c['subjectivity_factor']} ELSE 1.0 END AS f_subjectivity
            FROM v_reports v
            LEFT JOIN f USING (report_id)
            LEFT JOIN src USING (source_id)
            LEFT JOIN spatial_status s USING (report_id))
        SELECT *, f_gate * f_warnings * f_timeliness * f_source * f_corroboration * f_subjectivity AS confidence
        FROM parts""")


def build_product(con) -> None:
    """Hourly 1 x 1 degree x flight-level-band grid of observed turbulence.

    cell_confidence = 1 - prod(1 - c_i): the chance that at least one contributing
    report is right, treating reports as independent (noisy-OR).
    """
    con.execute(f"""
        CREATE OR REPLACE TABLE product_grid AS
        SELECT date_trunc('hour', obs_time) AS valid_hour,
               CAST(floor(lat) AS INTEGER) AS lat_cell,
               CAST(floor(lon) AS INTEGER) AS lon_cell,
               {FL_BAND} AS fl_band,
               count(*) AS n_reports,
               count(DISTINCT source_id) AS n_sources,
               max(edr_est) AS edr_max,
               sum(edr_est * confidence) / sum(confidence) AS edr_weighted,
               1 - exp(sum(ln(1 - least(confidence, 0.999)))) AS cell_confidence
        FROM v_reports JOIN report_confidence USING (report_id)
        WHERE {PLACEABLE} AND confidence > 0
        GROUP BY ALL""")


def check_product(con) -> pd.DataFrame:
    """Output-side controls. P01 is the reconciliation: every placeable input report is
    either in the grid or explicitly rejected, nothing more and nothing less."""
    q = lambda sql: con.execute(sql).fetchall()[0][0]
    placeable = q(f"SELECT count(*) FROM v_reports WHERE {PLACEABLE}")
    rejected = q(f"""SELECT count(*) FROM v_reports JOIN report_confidence USING (report_id)
                     WHERE {PLACEABLE} AND confidence = 0""")
    in_grid = int(q("SELECT coalesce(sum(n_reports), 0) FROM product_grid"))
    dup_keys = q("""SELECT count(*) FROM (SELECT valid_hour, lat_cell, lon_cell, fl_band FROM product_grid
                    GROUP BY ALL HAVING count(*) > 1)""")
    bad_edr = q("""SELECT count(*) FROM product_grid
                   WHERE edr_max NOT BETWEEN 0 AND 1 OR edr_weighted NOT BETWEEN 0 AND 1""")
    bad_conf = q("SELECT count(*) FROM product_grid WHERE cell_confidence NOT BETWEEN 0 AND 1")
    bad_cells = q("""SELECT count(*) FROM product_grid
                     WHERE lat_cell NOT BETWEEN -90 AND 89 OR lon_cell NOT BETWEEN -180 AND 179""")
    lag_h = q("""SELECT date_diff('minute', (SELECT max(valid_hour) FROM product_grid),
                                  (SELECT max(receipt_time) FROM reports)) / 60.0""")
    checks = [
        ("P01", "Reconciles to input (placeable = gridded + rejected)", placeable == in_grid + rejected,
         placeable, in_grid + rejected),
        ("P02", "Unique cell key", dup_keys == 0, 0, dup_keys),
        ("P03", "EDR values within [0, 1]", bad_edr == 0, 0, bad_edr),
        ("P04", "Cell confidence within [0, 1]", bad_conf == 0, 0, bad_conf),
        ("P05", "Cells on a valid 1 degree grid", bad_cells == 0, 0, bad_cells),
        ("P06", "Product fresh (latest cell within 3 h of latest receipt)",
         lag_h is not None and lag_h <= 3, "<= 3 h", None if lag_h is None else round(lag_h, 1)),
    ]
    df = pd.DataFrame(checks, columns=["check_id", "name", "passed", "expected", "actual"])
    df["expected"] = df["expected"].astype(str)
    df["actual"] = df["actual"].astype(str)
    con.execute("CREATE OR REPLACE TABLE product_checks AS SELECT * FROM df")
    return df
