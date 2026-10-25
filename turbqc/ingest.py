"""Near-real-time collector: poll the bulk file, archive it, store new reports, log the run."""
import gzip
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from . import config, db, feed

log = logging.getLogger("turbqc.ingest")


def _now() -> pd.Timestamp:
    return pd.Timestamp(datetime.now(timezone.utc)).tz_localize(None)


def _archive_stamp(path: Path) -> pd.Timestamp:
    return pd.Timestamp(datetime.strptime(path.name.split("_")[-1][:15], "%Y%m%dT%H%M%S"))


def _archive(payload: bytes, started: pd.Timestamp, cfg: dict) -> str | None:
    """Keep the raw file so the database can be rebuilt (replayed) at any time."""
    if not cfg["ingest"]["archive_raw"]:
        return None
    root = config.resolve(cfg["paths"]["raw_archive"])
    day_dir = root / started.strftime("%Y/%m/%d")
    existing = sorted(day_dir.glob("*.csv.gz")) if day_dir.exists() else []
    if existing:
        last = _archive_stamp(existing[-1])
        if (started - last).total_seconds() < cfg["ingest"]["archive_every_min"] * 60:
            return None
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"aircraftreports_{started.strftime('%Y%m%dT%H%M%S')}.csv.gz"
    data = payload if payload[:2] == b"\x1f\x8b" else gzip.compress(payload)
    path.write_bytes(data)
    return str(path.relative_to(config.ROOT))


def ingest_payload(con, payload: bytes, started: pd.Timestamp, source: str,
                   http_status: int | None = None, archive_path: str | None = None,
                   cfg: dict | None = None) -> dict:
    cfg = cfg or config.settings()
    run_id = f"{started.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
    parsed = feed.parse(feed.decode(payload), run_id, started, cfg)
    new = db.insert_reports(con, parsed.frame) if not parsed.frame.empty else 0
    if parsed.missing_columns:
        status, msg = "schema_drift", f"missing columns: {', '.join(parsed.missing_columns)}"
    elif parsed.unexpected_columns:
        status, msg = "ok_with_warnings", f"unexpected columns: {', '.join(parsed.unexpected_columns)}"
    else:
        status, msg = "ok", None
    run = dict(run_id=run_id, started_at=started, finished_at=_now(), source_url=source,
               http_status=http_status, bytes=len(payload), rows_in_file=parsed.rows_in_file,
               rows_malformed=parsed.rows_malformed, rows_new=new,
               rows_already_seen=len(parsed.frame) - new, status=status, message=msg,
               archive_path=archive_path)
    _log_run(con, run)
    return run


def _log_run(con, run: dict) -> None:
    con.execute(f"INSERT INTO ingest_runs ({', '.join(run)}) VALUES ({', '.join('?' * len(run))})",
                list(run.values()))


def run_once(db_path=None, refresh: bool = True) -> dict:
    """One poll of the live feed. Failures are logged as runs too: an outage is data."""
    cfg = config.settings()
    started = _now()
    url = cfg["feed"]["bulk_url"]
    try:
        status, payload = feed.fetch(cfg)
    except Exception as e:  # network or HTTP error
        con = db.connect(db_path)
        run = dict(run_id=f"{started.strftime('%Y%m%dT%H%M%S')}-err", started_at=started,
                   finished_at=_now(), source_url=url, http_status=None, bytes=0, rows_in_file=0,
                   rows_malformed=0, rows_new=0, rows_already_seen=0, status="fetch_error",
                   message=str(e)[:500], archive_path=None)
        _log_run(con, run)
        con.close()
        return run
    archive = _archive(payload, started, cfg)
    con = db.connect(db_path)
    try:
        run = ingest_payload(con, payload, started, url, status, archive, cfg)
        if refresh:
            from . import pipeline
            pipeline.refresh(con)
    finally:
        con.close()
    return run


def replay(archive_dir: str | Path, db_path=None) -> int:
    """Rebuild a database from archived raw files (*.csv.gz) or collected history (*.parquet)."""
    root = Path(archive_dir)
    con = db.connect(db_path)
    total = 0
    for f in sorted(root.rglob("*.csv.gz")):
        total += ingest_payload(con, f.read_bytes(), _archive_stamp(f), f"file:{f.name}")["rows_new"]
    for f in sorted(root.rglob("*.parquet")):
        total += db.insert_reports(con, con.execute(f"SELECT * FROM read_parquet('{f.as_posix()}')").df())
    from . import pipeline
    pipeline.refresh(con)
    con.close()
    return total


def loop(db_path=None, minutes: float | None = None) -> None:
    minutes = minutes or config.settings()["ingest"]["poll_minutes"]
    log.info("polling every %s min; Ctrl+C to stop", minutes)
    while True:
        run = run_once(db_path)
        log.info("%s %s new=%s seen=%s %s", run["started_at"], run["status"], run["rows_new"],
                 run["rows_already_seen"], run["message"] or "")
        time.sleep(minutes * 60)
