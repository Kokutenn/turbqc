"""Rule engine: executes the YAML rule catalogue as SQL inside DuckDB."""
import time
from datetime import datetime, timezone
from string import Template

import pandas as pd

from . import config


def render(text: str, params: dict) -> str:
    return Template(text).substitute(params)


def describe(rule: dict, params: dict) -> str:
    return " ".join(Template(rule.get("description", "")).safe_substitute(params).split())


def _scope(rule: dict) -> str:
    return ", ".join(f"'{s}'" for s in rule["applies_to"])


def rule_sql(rule: dict, params: dict) -> str:
    """SQL returning the report_id of every row failing this rule."""
    scope = _scope(rule)
    if "fail_when" in rule:
        return (f"SELECT report_id FROM v_reports "
                f"WHERE stream IN ({scope}) AND ({render(rule['fail_when'], params)})")
    query = render(rule["fail_query"], params)
    return (f"SELECT DISTINCT q.report_id FROM ({query}) q "
            f"JOIN reports r USING (report_id) WHERE r.stream IN ({scope})")


def write_meta(con, rules: list, params: dict) -> None:
    con.execute("DELETE FROM rule_meta")
    con.executemany("INSERT INTO rule_meta VALUES (?, ?, ?, ?, ?, ?, ?)", [
        (r["id"], r["name"], r["dimension"], r["severity"], r["requirement"],
         ", ".join(r["applies_to"]), describe(r, params)) for r in rules])


def validate(con, rules: list | None = None, cfg: dict | None = None) -> pd.DataFrame:
    """Run every rule, replace rule_failures with the current result, append to rule_runs."""
    from .analytics import build_spatial_status  # R055 reads spatial_status

    cfg = cfg or config.settings()
    rules = rules or config.rules()
    params = config.rule_params(cfg)
    build_spatial_status(con, cfg)
    write_meta(con, rules, params)
    con.execute("DELETE FROM rule_failures")
    validated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    rows = []
    for rule in rules:
        t0 = time.perf_counter()
        con.execute(f"INSERT INTO rule_failures SELECT '{rule['id']}', report_id FROM ({rule_sql(rule, params)})")
        failed = con.execute("SELECT count(*) FROM rule_failures WHERE rule_id = ?", [rule["id"]]).fetchall()[0][0]
        checked = con.execute(f"SELECT count(*) FROM reports WHERE stream IN ({_scope(rule)})").fetchall()[0][0]
        rows.append(dict(validated_at=validated_at, rule_id=rule["id"], checked=checked,
                         failed=failed, seconds=round(time.perf_counter() - t0, 4)))
    summary = pd.DataFrame(rows)
    con.register("summary_df", summary)
    con.execute("INSERT INTO rule_runs SELECT * FROM summary_df")
    con.unregister("summary_df")
    return summary


def flagged(con, severities=("error", "warning")) -> dict:
    """report_id -> set of rule ids, for the given severities."""
    sev = ", ".join(f"'{s}'" for s in severities)
    df = con.execute(f"""
        SELECT report_id, list(DISTINCT rule_id) AS rules FROM rule_failures
        JOIN rule_meta USING (rule_id) WHERE severity IN ({sev}) GROUP BY report_id""").df()
    return {r.report_id: set(r.rules) for r in df.itertuples()}
