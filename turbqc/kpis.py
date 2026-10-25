"""Quality KPIs: completeness, validity, timeliness, consistency, uniqueness and a DQ score.

Each KPI is a share between 0 and 1 computed per report (table report_quality) and
then averaged over whatever grouping a view needs (stream, source, operator, hour).
"""
import pandas as pd

from . import config

KPIS = ["completeness", "validity", "timeliness", "consistency", "uniqueness"]


def build_report_quality(con, cfg: dict | None = None) -> None:
    cfg = cfg or config.settings()
    req = cfg["required_fields"]
    fill = {s: "(" + " + ".join(f"CASE WHEN {c} IS NOT NULL THEN 1 ELSE 0 END" for c in cols) + f") / {len(cols)}.0"
            for s, cols in req.items()}
    completeness = "CASE " + " ".join(f"WHEN stream = '{s}' THEN ({expr})" for s, expr in fill.items()) + " ELSE 0 END"
    t = cfg["thresholds"]
    con.execute(f"""
        CREATE OR REPLACE TABLE report_quality AS
        WITH f AS (
            SELECT report_id,
                   bool_or(dimension = 'validity')    AS invalid,
                   bool_or(dimension = 'consistency') AS inconsistent,
                   bool_or(dimension = 'uniqueness')  AS duplicate,
                   bool_or(dimension = 'timeliness')  AS untimely_rule
            FROM rule_failures JOIN rule_meta USING (rule_id)
            WHERE severity IN ('error', 'warning')
            GROUP BY report_id)
        SELECT v.report_id, v.stream, v.source_id, v.operator, v.receipt_time, v.obs_time,
               date_trunc('hour', v.receipt_time) AS hour,
               {completeness} AS completeness,
               CASE WHEN coalesce(f.invalid, false) THEN 0 ELSE 1 END AS validity,
               CASE WHEN v.latency_min BETWEEN -{t['future_tolerance_min']} AND {t['latency_sla_min']}
                     AND NOT coalesce(f.untimely_rule, false) THEN 1 ELSE 0 END AS timeliness,
               CASE WHEN coalesce(f.inconsistent, false) THEN 0 ELSE 1 END AS consistency,
               CASE WHEN coalesce(f.duplicate, false) THEN 0 ELSE 1 END AS uniqueness
        FROM v_reports v LEFT JOIN f USING (report_id)""")


def kpi_table(con, by: list[str] | None = None, where: str = "TRUE", cfg: dict | None = None) -> pd.DataFrame:
    cfg = cfg or config.settings()
    w = cfg["kpi"]["weights"]
    keys = ", ".join(by) + ", " if by else ""
    score = " + ".join(f"{w[k]} * avg({k})" for k in KPIS) + f") / {sum(w.values())}"
    df = con.execute(f"""
        SELECT {keys} count(*) AS reports,
               {', '.join(f'avg({k}) AS {k}' for k in KPIS)},
               ({score} AS dq_score
        FROM report_quality WHERE {where}
        {'GROUP BY ' + ', '.join(by) + ' ORDER BY ' + ', '.join(by) if by else ''}""").df()
    return df


def rag(kpi: str, value: float, cfg: dict | None = None) -> str:
    cfg = cfg or config.settings()
    targets = cfg["kpi"]["targets"]
    target = targets.get(kpi, min(targets.values()))
    if value is None or pd.isna(value):
        return "grey"
    if value >= target:
        return "green"
    return "amber" if value >= target - cfg["kpi"]["amber_margin"] else "red"
