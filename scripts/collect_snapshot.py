"""Fetch the AWC bulk file once and merge it into a daily Parquet file (deduplicated).

Used by the scheduled GitHub Action so history accumulates even when your laptop is off
(the AWC API only keeps ~15 days). Load it locally with:  python -m turbqc replay data/history
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from turbqc import config, feed  # noqa: E402
from turbqc.schema import REPORT_COLS  # noqa: E402


def main(out_dir: str = "data/history") -> None:
    now = pd.Timestamp(datetime.now(timezone.utc)).tz_localize(None)
    _, payload = feed.fetch()
    parsed = feed.parse(feed.decode(payload), f"gha-{now:%Y%m%dT%H%M}", now)
    if parsed.missing_columns:
        raise SystemExit(f"schema drift: missing {parsed.missing_columns}")
    new = parsed.frame[REPORT_COLS]
    out = config.resolve(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.register("new", new)
    for day, _ in new.groupby(new["receipt_time"].dt.strftime("%Y-%m-%d")):
        path = out / f"{day}.parquet"
        src = f"SELECT * FROM read_parquet('{path.as_posix()}') UNION ALL " if path.exists() else ""
        con.execute(f"""
            COPY (SELECT * FROM ({src} SELECT * FROM new WHERE strftime(receipt_time, '%Y-%m-%d') = '{day}')
                  QUALIFY row_number() OVER (PARTITION BY report_id ORDER BY ingested_at) = 1)
            TO '{path.as_posix()}.tmp' (FORMAT parquet, COMPRESSION zstd)""")
        Path(f"{path}.tmp").replace(path)
        print(day, con.execute(f"SELECT count(*) FROM read_parquet('{path.as_posix()}')").fetchall()[0][0], "reports")


if __name__ == "__main__":
    main(*sys.argv[1:])
