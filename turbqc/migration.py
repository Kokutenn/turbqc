"""Migration reconciliation: v1 (legacy pandas) against v2 (DuckDB/SQL), run in parallel
on the same raw files.

Three levels, each feeding the next:

1. **Base** discrepancies on the agreed key: missing keys, field values, rule flags.
   Each is matched against the known-differences catalogue (config/migration.yaml).
2. **Derived** discrepancies (accepted vs rejected). A derived difference is explained
   only if every rule-flag difference that causes it is explained.
3. **Aggregate** discrepancies (volumes by stream/hour, product cells). An aggregate
   difference is explained only if the record-level differences of its member reports
   are all explained. A mismatch with no record-level cause is an aggregation defect.

Anything unexplained blocks sign-off.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import gzip
import operator

import pandas as pd
import yaml

from . import config, db, feed, ingest, legacy, pipeline

OPS = {"==": operator.eq, ">=": operator.ge, "<=": operator.le, ">": operator.gt, "<": operator.lt}
CONTEXT_FIELDS = ["tb_intensity2"]  # v2-only fields that known differences may refer to


def migration_config() -> dict:
    with open(config.CONFIG_DIR / "migration.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


@dataclass
class BatchResult:
    batch: str
    register: pd.DataFrame
    metrics: dict
    criteria: pd.DataFrame
    green: bool = field(default=False)


# ------------------------------------------------------------------ running both systems
def run_v2(payloads):
    con = db.connect(":memory:")
    for stamp, payload in payloads:
        ingest.ingest_payload(con, payload, stamp, "parallel-run")
    pipeline.refresh(con)
    return con


def run_v1(payloads, cfg):
    recs = pd.concat([legacy.parse_v1(feed.decode(p), cfg) for _, p in payloads], ignore_index=True)
    recs = recs.drop_duplicates("report_id").reset_index(drop=True)
    flags = legacy.rules_v1(recs, cfg)
    return recs, flags, legacy.product_v1(recs, flags, cfg)


# ------------------------------------------------------------------ reconciliation
def _field_sql(col: str, tol) -> str:
    if tol is None:
        cond = f"a.{col} IS DISTINCT FROM b.{col}"
    elif isinstance(tol, dict):
        ms = tol["seconds"] * 1000
        cond = f"(a.{col} IS NULL) <> (b.{col} IS NULL) OR abs(epoch_ms(a.{col}) - epoch_ms(b.{col})) >= {ms}"
    else:
        cond = f"(a.{col} IS NULL) <> (b.{col} IS NULL) OR abs(a.{col} - b.{col}) > {tol}"
    return (f"SELECT 'field' AS check_type, report_id AS key, '{col}' AS col, "
            f"CAST(a.{col} AS VARCHAR) AS v1_value, CAST(b.{col} AS VARCHAR) AS v2_value "
            f"FROM v1r a JOIN v2r b USING (report_id) WHERE {cond}")


def reconcile(payloads, batch: str = "all", mcfg: dict | None = None, cfg: dict | None = None,
              tamper_v2=None) -> BatchResult:
    """Run v1 and v2 on the same payloads and build the discrepancy register."""
    cfg = cfg or config.settings()
    mcfg = mcfg or migration_config()
    con = run_v2(payloads)
    if tamper_v2:
        tamper_v2(con)  # tests only: simulate a defect in the new system
    v1r, f1, g1 = run_v1(payloads, cfg)
    fields = list(mcfg["compare_fields"])
    con.register("v1r_df", v1r)
    con.register("f1_df", f1)
    con.register("g1_df", g1)
    con.execute("CREATE OR REPLACE TEMP TABLE v1r AS SELECT * FROM v1r_df")
    con.execute("CREATE OR REPLACE TEMP TABLE f1 AS SELECT DISTINCT rule_id, report_id FROM f1_df")
    con.execute("CREATE OR REPLACE TEMP TABLE g1 AS SELECT * FROM g1_df")
    con.execute(f"""CREATE OR REPLACE TEMP TABLE v2r AS
                    SELECT report_id, {', '.join(fields + CONTEXT_FIELDS)} FROM reports""")
    common = ", ".join(f"'{r}'" for r in legacy.V1_RULES)

    # ---- level 1: base discrepancies
    parts = [
        """SELECT 'key', report_id, 'report_id', 'present', 'missing' FROM v1r ANTI JOIN v2r USING (report_id)""",
        """SELECT 'key', report_id, 'report_id', 'missing', 'present' FROM v2r ANTI JOIN v1r USING (report_id)""",
        *[_field_sql(c, t) for c, t in mcfg["compare_fields"].items()],
        f"""SELECT 'rule_flag', report_id, rule_id, 'true', 'false' FROM (
                SELECT rule_id, report_id FROM f1 ANTI JOIN (SELECT rule_id, report_id FROM rule_failures
                                                             WHERE rule_id IN ({common})) b USING (rule_id, report_id))
            WHERE report_id IN (SELECT report_id FROM v2r)""",
        f"""SELECT 'rule_flag', report_id, rule_id, 'false', 'true' FROM (
                SELECT rule_id, report_id FROM (SELECT rule_id, report_id FROM rule_failures
                                                WHERE rule_id IN ({common})) b
                ANTI JOIN f1 USING (rule_id, report_id))
            WHERE report_id IN (SELECT report_id FROM v1r)""",
        f"""SELECT 'rule_flag', report_id, rule_id, 'not in v1', 'true'
            FROM rule_failures JOIN rule_meta USING (rule_id)
            WHERE severity = 'error' AND rule_id NOT IN ({common})
              AND report_id IN (SELECT report_id FROM v1r)""",
    ]
    con.execute("CREATE OR REPLACE TEMP TABLE disc AS SELECT row_number() OVER () AS disc_id, 'record' AS level, *, "
                "CAST(NULL AS VARCHAR) AS explained_by, 0 AS contributing FROM ("
                + " UNION ALL ".join(f"({p})" for p in parts) + ") t(check_type, key, col, v1_value, v2_value)")

    # context for known-difference conditions
    t = cfg["thresholds"]
    con.execute(f"""CREATE OR REPLACE TEMP TABLE prev_ctx AS
                   SELECT report_id,
                          lag(report_id) OVER (PARTITION BY track_id ORDER BY obs_time, receipt_time, report_id) AS prev_id,
                          coalesce(date_diff('second', prev_obs_time, obs_time) BETWEEN 1 AND {t['track_max_gap_min']} * 60
                                   AND kts(prev_lat, prev_lon, prev_obs_time, lat, lon, obs_time) > {t['max_groundspeed_kt']},
                                   false) AS v2_fast_leg_in
                   FROM v_track""")
    v1_cols = ", ".join(f"a.{c} AS v1_{c}" for c in fields)
    v2_cols = ", ".join(f"b.{c} AS v2_{c}" for c in fields + CONTEXT_FIELDS)
    con.execute(f"""CREATE OR REPLACE TEMP VIEW disc_ctx AS
        SELECT d.*, {v1_cols}, {v2_cols},
               EXISTS (SELECT 1 FROM rule_failures rf WHERE rf.report_id = p.prev_id AND rf.rule_id = d.col)
                   AS prev_flagged_v2,
               coalesce(p.v2_fast_leg_in, false) AS v2_fast_leg_in
        FROM disc d LEFT JOIN v1r a ON a.report_id = d.key LEFT JOIN v2r b ON b.report_id = d.key
        LEFT JOIN prev_ctx p ON p.report_id = d.key""")
    for kd in mcfg["known_differences"]:
        if "when" not in kd:   # normalisations are applied in the comparison itself
            continue
        con.execute(f"""UPDATE disc SET explained_by = '{kd['id']}'
                        WHERE explained_by IS NULL AND disc_id IN (
                            SELECT disc_id FROM disc_ctx WHERE {' '.join(kd['when'].split())})""")

    # ---- level 2: derived (accepted vs rejected), explained only through its causes
    con.execute(f"""
        INSERT INTO disc
        WITH r1 AS (SELECT DISTINCT report_id FROM f1 JOIN rule_meta USING (rule_id) WHERE severity = 'error'),
             r2 AS (SELECT DISTINCT report_id FROM rule_failures JOIN rule_meta USING (rule_id) WHERE severity = 'error'),
             both_keys AS (SELECT report_id,
                                  report_id IN (SELECT report_id FROM r1) AS v1_rej,
                                  report_id IN (SELECT report_id FROM r2) AS v2_rej
                           FROM v1r JOIN v2r USING (report_id)),
             causes AS (SELECT key, count(*) AS n, count(explained_by) AS n_exp,
                               string_agg(DISTINCT explained_by, ', ' ORDER BY explained_by) AS kds
                        FROM disc JOIN rule_meta m ON m.rule_id = disc.col
                        WHERE check_type = 'rule_flag' AND m.severity = 'error' GROUP BY key)
        SELECT (SELECT max(disc_id) FROM disc) + row_number() OVER (), 'record', 'derived', b.report_id, 'rejected',
               CAST(b.v1_rej AS VARCHAR), CAST(b.v2_rej AS VARCHAR),
               CASE WHEN c.n > 0 AND c.n = c.n_exp THEN c.kds END, coalesce(c.n, 0)
        FROM both_keys b LEFT JOIN causes c ON c.key = b.report_id
        WHERE b.v1_rej <> b.v2_rej""")

    # ---- level 3: aggregates, explained only through their member records
    con.execute("""CREATE OR REPLACE TEMP TABLE rec_status AS
                   SELECT key AS report_id, count(*) AS n, count(explained_by) AS n_exp,
                          string_agg(DISTINCT explained_by, ', ' ORDER BY explained_by) AS kds
                   FROM disc WHERE level = 'record' GROUP BY key""")
    con.execute("""CREATE OR REPLACE TEMP TABLE members AS
        SELECT 'volume' AS agg, stream || ' ' || strftime(date_trunc('hour', receipt_time), '%Y-%m-%d %H:00') AS gkey, report_id
        FROM v1r WHERE receipt_time IS NOT NULL
        UNION SELECT 'volume', stream || ' ' || strftime(date_trunc('hour', receipt_time), '%Y-%m-%d %H:00'), report_id
        FROM v2r WHERE receipt_time IS NOT NULL""")
    v1_ok = f"""report_id NOT IN (SELECT report_id FROM f1 JOIN rule_meta USING (rule_id) WHERE severity = 'error')"""
    con.execute(f"""INSERT INTO members
        SELECT 'product_cell', strftime(date_trunc('hour', obs_time), '%Y-%m-%d %H:00') || ' ' || CAST(floor(lat) AS INTEGER)
               || ' ' || CAST(floor(lon) AS INTEGER) || ' ' || {_band('alt_ft')}, report_id
        FROM v1r WHERE edr_est IS NOT NULL AND obs_time IS NOT NULL AND lat IS NOT NULL AND lon IS NOT NULL
              AND alt_ft IS NOT NULL AND {v1_ok}
        UNION
        SELECT 'product_cell', strftime(date_trunc('hour', obs_time), '%Y-%m-%d %H:00') || ' ' || CAST(floor(lat) AS INTEGER)
               || ' ' || CAST(floor(lon) AS INTEGER) || ' ' || {_band('alt_ft')}, report_id
        FROM v_reports JOIN report_confidence USING (report_id)
        WHERE edr_est IS NOT NULL AND obs_time IS NOT NULL AND lat IS NOT NULL AND lon IS NOT NULL
              AND alt_ft IS NOT NULL AND confidence > 0""")
    tol = mcfg["product_edr_tolerance"]
    con.execute(f"""CREATE OR REPLACE TEMP TABLE agg_diff AS
        WITH vol1 AS (SELECT stream || ' ' || strftime(date_trunc('hour', receipt_time), '%Y-%m-%d %H:00') AS gkey,
                             count(*) AS n FROM v1r WHERE receipt_time IS NOT NULL GROUP BY 1),
             vol2 AS (SELECT stream || ' ' || strftime(date_trunc('hour', receipt_time), '%Y-%m-%d %H:00') AS gkey,
                             count(*) AS n FROM v2r WHERE receipt_time IS NOT NULL GROUP BY 1),
             c1 AS (SELECT strftime(valid_hour, '%Y-%m-%d %H:00') || ' ' || lat_cell || ' ' || lon_cell || ' ' || fl_band
                           AS gkey, n_reports, edr_max FROM g1),
             c2 AS (SELECT strftime(valid_hour, '%Y-%m-%d %H:00') || ' ' || lat_cell || ' ' || lon_cell || ' ' || fl_band
                           AS gkey, n_reports, edr_max FROM product_grid)
        SELECT 'volume' AS agg, gkey, 'n_reports' AS col, CAST(a.n AS VARCHAR) AS v1_value, CAST(b.n AS VARCHAR) AS v2_value
        FROM vol1 a FULL JOIN vol2 b USING (gkey) WHERE a.n IS DISTINCT FROM b.n
        UNION ALL
        SELECT 'product_cell', gkey, 'n_reports', CAST(a.n_reports AS VARCHAR), CAST(b.n_reports AS VARCHAR)
        FROM c1 a FULL JOIN c2 b USING (gkey) WHERE a.n_reports IS DISTINCT FROM b.n_reports
        UNION ALL
        SELECT 'product_cell', gkey, 'edr_max', CAST(a.edr_max AS VARCHAR), CAST(b.edr_max AS VARCHAR)
        FROM c1 a JOIN c2 b USING (gkey) WHERE abs(a.edr_max - b.edr_max) > {tol}""")
    con.execute("""
        INSERT INTO disc
        WITH att AS (
            SELECT d.agg, d.gkey, d.col, count(s.report_id) AS n, sum(s.n) AS n_disc, sum(s.n_exp) AS n_exp,
                   string_agg(DISTINCT s.kds, ', ') AS kds
            FROM agg_diff d LEFT JOIN members m ON m.agg = d.agg AND m.gkey = d.gkey
            LEFT JOIN rec_status s ON s.report_id = m.report_id
            GROUP BY d.agg, d.gkey, d.col)
        SELECT (SELECT coalesce(max(disc_id), 0) FROM disc) + row_number() OVER (), 'aggregate', d.agg, d.gkey, d.col,
               d.v1_value, d.v2_value,
               CASE WHEN a.n > 0 AND a.n_disc = a.n_exp THEN a.kds END, coalesce(a.n, 0)
        FROM agg_diff d JOIN att a USING (agg, gkey, col)""")

    normalised = sum(
        con.execute(f"""SELECT count(*) FROM v1r a JOIN v2r b USING (report_id)
                        WHERE a.{c} IS DISTINCT FROM b.{c}
                          AND abs(epoch_ms(a.{c}) - epoch_ms(b.{c})) < {t['seconds'] * 1000}""").fetchall()[0][0]
        for c, t in mcfg["compare_fields"].items() if isinstance(t, dict))
    register = con.execute("SELECT * FROM disc ORDER BY disc_id").df()
    register.insert(0, "batch", batch)
    q = lambda sql: con.execute(sql).fetchall()[0][0]
    keys_v1, keys_v2 = len(v1r), q("SELECT count(*) FROM v2r")
    compared = q("SELECT count(*) FROM v1r JOIN v2r USING (report_id)")
    rec = register[register["level"] == "record"]
    base = rec[rec["check_type"].isin(["field", "rule_flag"])]
    kd_dispo = {k["id"]: k.get("disposition", "open") for k in mcfg["known_differences"]}
    used = {k.strip() for v in register["explained_by"].dropna() for k in v.split(",")}
    metrics = dict(
        keys_v1=keys_v1, keys_v2=keys_v2, records_compared=compared,
        missing_keys=int((rec["check_type"] == "key").sum()),
        record_discrepancies=len(rec), record_match_rate=1 - base["key"].nunique() / compared if compared else 0.0,
        unexplained_record=int(rec["explained_by"].isna().sum()),
        aggregate_discrepancies=int((register["level"] == "aggregate").sum()),
        unexplained_aggregate=int(((register["level"] == "aggregate") & register["explained_by"].isna()).sum()),
        volume_mismatches=int((register["check_type"] == "volume").sum()),
        unapproved_differences_used=sum(1 for k in used if kd_dispo.get(k) != "accepted"),
        normalised_differences=int(normalised),
    )
    con.close()
    criteria = evaluate(metrics, mcfg)
    return BatchResult(batch, register, metrics, criteria, bool(criteria["passed"].all()))


def _band(col: str) -> str:
    return (f"CASE WHEN {col} < 10000 THEN 'SFC-FL100' WHEN {col} < 20000 THEN 'FL100-200' "
            f"WHEN {col} < 30000 THEN 'FL200-300' WHEN {col} < 40000 THEN 'FL300-400' ELSE 'FL400+' END")


def evaluate(metrics: dict, mcfg: dict) -> pd.DataFrame:
    rows = []
    for c in mcfg["sign_off"]:
        value = metrics[c["metric"]]
        rows.append(dict(criterion=c["id"], name=c["name"], metric=c["metric"], value=value,
                         target=f"{c['op']} {c['target']}", passed=bool(OPS[c["op"]](value, c["target"]))))
    return pd.DataFrame(rows)


# ------------------------------------------------------------------ parallel run
def parallel_run(batches: dict, mcfg: dict | None = None, cfg: dict | None = None) -> dict:
    """Reconcile each batch (e.g. each day) and decide GO / NO-GO for cutover."""
    mcfg = mcfg or migration_config()
    results = [reconcile(p, b, mcfg, cfg) for b, p in sorted(batches.items())]
    need = mcfg["parallel_run"]["consecutive_green_required"]
    streak = 0
    for r in reversed(results):
        if not r.green:
            break
        streak += 1
    runs = pd.DataFrame([dict(batch=r.batch, green=r.green, **r.metrics) for r in results])
    so7 = dict(criterion="SO-07", name=f"Criteria met on {need} consecutive parallel-run batches",
               metric="consecutive_green", value=streak, target=f">= {need}", passed=streak >= need)
    latest = results[-1].criteria if results else pd.DataFrame()
    signoff = pd.concat([latest, pd.DataFrame([so7])], ignore_index=True)
    return dict(runs=runs, register=pd.concat([r.register for r in results], ignore_index=True),
                criteria=pd.concat([r.criteria.assign(batch=r.batch) for r in results], ignore_index=True),
                signoff=signoff, decision="GO" if signoff["passed"].all() else "NO-GO")


def batches_from_frame(df: pd.DataFrame) -> dict:
    """Daily CSV payloads in the provider's format, so v1 and v2 both start from raw text."""
    out = {}
    for day, g in df.groupby(df["receipt_time"].dt.strftime("%Y-%m-%d")):
        stamp = g["receipt_time"].max() + pd.Timedelta(minutes=1)
        out[day] = [(stamp, gzip.compress(feed.to_feed_csv(g).encode()))]
    return out


def batches_from_archive(root) -> dict:
    out = {}
    for f in sorted(Path(root).rglob("*.csv.gz")):
        stamp = ingest._archive_stamp(f)
        out.setdefault(stamp.strftime("%Y-%m-%d"), []).append((stamp, f.read_bytes()))
    return out


def to_markdown(result: dict, title: str, mcfg: dict | None = None) -> str:
    """Sign-off evidence pack: decision, criteria, per-batch results, explanations, defects."""
    mcfg = mcfg or migration_config()
    reg, runs, so = result["register"], result["runs"], result["signoff"]
    fmt = lambda v: (f"{int(v)}" if float(v).is_integer() else f"{v:.4f}") if isinstance(v, (int, float)) else str(v)
    lines = [f"# {title}", "", f"**Decision: {result['decision']}**", "",
             "Generated by `python -m turbqc reconcile`. v1 = legacy pandas pipeline, v2 = DuckDB/SQL pipeline, "
             "both run on the same raw provider files.", "", "## Sign-off criteria", "",
             "| Criterion | Description | Value | Target | Passed |", "|---|---|---|---|---|"]
    lines += [f"| {r.criterion} | {r.name} | {fmt(r.value)} | {r.target} | {'yes' if r.passed else '**no**'} |"
              for r in so.itertuples()]
    lines += ["", "## Parallel run by batch", "",
              "| Batch | Green | Keys v1 | Keys v2 | Record match rate | Discrepancies | Unexplained |",
              "|---|---|---|---|---|---|---|"]
    for r in runs.itertuples():
        lines.append(f"| {r.batch} | {'yes' if r.green else 'no'} | {r.keys_v1:,} | {r.keys_v2:,} | "
                     f"{r.record_match_rate:.2%} | {r.record_discrepancies + r.aggregate_discrepancies:,} | "
                     f"{r.unexplained_record + r.unexplained_aggregate} |")
    lines += ["", "## Discrepancies by explanation", "", "| Level | Check | Explained by | Count |", "|---|---|---|---|"]
    g = reg.assign(explained_by=reg["explained_by"].fillna("**UNEXPLAINED**")) \
        .groupby(["level", "check_type", "explained_by"]).size().reset_index(name="n")
    lines += [f"| {r.level} | {r.check_type} | {r.explained_by} | {r.n:,} |" for r in g.itertuples()]
    lines += ["", f"Timestamp values normalised by KD-02 (sub-second only): {int(runs['normalised_differences'].sum()):,}",
              "", "## Known differences", "", "| ID | Title | Disposition |", "|---|---|---|"]
    lines += [f"| {k['id']} | {k['title']} | {k.get('disposition', 'open')} |" for k in mcfg["known_differences"]]
    lines += ["", "## Defects found and fixed", ""]
    for d in mcfg.get("resolved_defects", []):
        lines += [f"**{d['id']}** ({d['found']})", "", f"- Symptom: {' '.join(d['symptom'].split())}",
                  f"- Root cause: {' '.join(d['root_cause'].split())}", f"- Fix: {' '.join(d['fix'].split())}",
                  f"- Verified by: {d['verified_by']}", ""]
    unexplained = reg[reg["explained_by"].isna()]
    lines += ["## Unexplained discrepancies", ""]
    if unexplained.empty:
        lines.append("None.")
    else:
        lines += ["| Batch | Check | Key | Column | v1 | v2 |", "|---|---|---|---|---|---|"]
        lines += [f"| {r.batch} | {r.check_type} | `{str(r.key)[:16]}` | {r.col} | {r.v1_value} | {r.v2_value} |"
                  for r in unexplained.head(50).itertuples()]
    return "\n".join(lines) + "\n"


def save(con, result: dict) -> None:
    stamp = datetime.now(timezone.utc).replace(tzinfo=None)
    for name, df in (("migration_runs", result["runs"]), ("migration_register", result["register"]),
                     ("migration_criteria", result["criteria"]), ("migration_signoff", result["signoff"])):
        con.register("m_df", df.assign(reconciled_at=stamp))
        con.execute(f"CREATE OR REPLACE TABLE {name} AS SELECT * FROM m_df")
        con.unregister("m_df")
