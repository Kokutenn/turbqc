"""TurbQC command line.

  python -m turbqc ingest            poll the live feed once and refresh all checks
  python -m turbqc ingest --loop     keep polling every N minutes
  python -m turbqc replay DIR        rebuild the database from archived raw files
  python -m turbqc refresh           rerun rules, analytics, KPIs and the product
  python -m turbqc faults            fault-injection test on the latest 24 h of stored data
  python -m turbqc verify --backfill pull 48 h of PIREPs, match GTG Nowcast analyses, print scores
  python -m turbqc reconcile         parallel run v1 (legacy) vs v2 on the raw archive, GO/NO-GO sign-off
  python -m turbqc daily             scheduled daily job: backfill + verify + reconcile, logs in data/logs
  python -m turbqc demo              build the offline demo database
  python -m turbqc docs              regenerate rule catalogue, dictionary and traceability docs
"""
import argparse
import logging
from pathlib import Path

import pandas as pd

from . import config, db, demo, docs, faults, ingest, migration, pipeline, verify
from .schema import REPORT_COLS


def daily(db_path=None) -> None:
    """Once a day: back-fill PIREPs, verify the GTG Nowcast (NOMADS keeps ~2 days, so a
    skipped day is lost for good) and run the v1/v2 parallel run on the raw archive.
    Each step is independent: a failure is logged and the next step still runs."""
    logs = config.resolve("data/logs")
    logs.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now()
    handler = logging.FileHandler(logs / f"daily_{stamp:%Y%m%d}.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    log = logging.getLogger("turbqc.daily")

    def step(name, fn):
        try:
            log.info("%s: %s", name, fn())
        except Exception:
            log.exception("%s failed", name)

    def refresh():
        con = db.connect(db_path)
        try:
            return pipeline.refresh(con)
        finally:
            con.close()

    def reconcile():
        archive = config.resolve(config.settings()["paths"]["raw_archive"])
        result = migration.parallel_run(migration.batches_from_archive(archive))
        con = db.connect(db_path)
        migration.save(con, result)
        con.close()
        (logs / f"parallel_run_{stamp:%Y%m%d}.md").write_text(
            migration.to_markdown(result, f"Parallel run: live archive ({stamp:%Y-%m-%d})"), encoding="utf-8")
        return result["decision"]

    step("backfill", lambda: verify.backfill_pireps(db_path, 48))
    step("refresh", refresh)
    step("verify", lambda: verify.run(db_path, 48))
    step("reconcile", reconcile)
    root.removeHandler(handler)
    handler.close()


def main(argv=None):
    p = argparse.ArgumentParser(prog="turbqc", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", help="DuckDB file (default from config/settings.yaml)")
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("ingest")
    i.add_argument("--loop", action="store_true")
    i.add_argument("--minutes", type=float)
    r = sub.add_parser("replay")
    r.add_argument("archive_dir")
    sub.add_parser("refresh")
    f = sub.add_parser("faults")
    f.add_argument("--hours", type=int, default=24)
    v = sub.add_parser("verify", help="match reports with GTG Nowcast analyses and print scores")
    v.add_argument("--hours", type=float, default=48)
    v.add_argument("--backfill", action="store_true", help="first pull past PIREPs from the Data API")
    v.add_argument("--max-files", type=int)
    m = sub.add_parser("reconcile", help="parallel run: legacy v1 vs v2 on the same raw files, then sign-off")
    m.add_argument("--archive", default=None, help="raw archive dir (default from settings)")
    m.add_argument("--report", help="write the sign-off evidence pack (markdown) to this path")
    sub.add_parser("daily", help="scheduled job: backfill, nowcast verification, parallel run (logs to data/logs)")
    sub.add_parser("demo")
    sub.add_parser("docs")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    if a.cmd == "ingest":
        if a.loop:
            ingest.loop(a.db, a.minutes)
        else:
            print(ingest.run_once(a.db))
    elif a.cmd == "replay":
        print(f"{ingest.replay(a.archive_dir, a.db)} new reports")
    elif a.cmd == "refresh":
        con = db.connect(a.db)
        print(pipeline.refresh(con))
        con.close()
    elif a.cmd == "faults":
        con = db.connect(a.db)
        df = con.execute(f"""SELECT {', '.join(REPORT_COLS)} FROM reports
                             WHERE receipt_time >= (SELECT max(receipt_time) FROM reports) - INTERVAL {a.hours} HOUR""").df()
        res = faults.evaluate(df)
        faults.save(con, res)
        con.close()
        with pd.option_context("display.width", 200, "display.max_columns", 20):
            print(res[["scenario", "level", "injected", "detected", "recall", "false_alarms", "false_alarm_rate"]])
    elif a.cmd == "verify":
        if a.backfill:
            print("backfill:", verify.backfill_pireps(a.db, int(a.hours)))
            con = db.connect(a.db)
            print("refresh:", pipeline.refresh(con))
            con.close()
        print("gtgn:", verify.run(a.db, a.hours, max_files=a.max_files))
        con = db.connect(a.db, read_only=True)
        pairs = verify.pairs_frame(con)
        cols = ["fl_band", "lag_min", "pairs", "events", "pod", "far", "csi", "pss", "auc"]
        with pd.option_context("display.width", 200, "display.max_columns", 20, "display.precision", 3):
            print(verify.score_table(pairs, ["fl_band", "lag_min"])[cols])
        con.close()
    elif a.cmd == "reconcile":
        archive = a.archive or config.resolve(config.settings()["paths"]["raw_archive"])
        result = migration.parallel_run(migration.batches_from_archive(archive))
        con = db.connect(a.db)
        migration.save(con, result)
        con.close()
        if a.report:
            Path(a.report).write_text(migration.to_markdown(result, "Parallel run: live archive"), encoding="utf-8")
        print(f"decision: {result['decision']}")
        with pd.option_context("display.width", 200, "display.max_columns", 20):
            print(result["signoff"][["criterion", "name", "value", "target", "passed"]])
    elif a.cmd == "daily":
        daily(a.db)
    elif a.cmd == "demo":
        print(demo.build(a.db))
    elif a.cmd == "docs":
        for path in docs.generate():
            print("wrote", path)


if __name__ == "__main__":
    main()
