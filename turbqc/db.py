"""DuckDB connection helpers.

DuckDB allows one writing process per file. The collector and the dashboard run as
separate processes, so both open short-lived connections and retry on lock contention.
"""
import time
from pathlib import Path

import duckdb
import pandas as pd

from . import config
from .schema import DDL, REPORT_COLS, VIEWS


def connect(path: str | Path | None = None, read_only: bool = False,
            retries: int = 20, wait_s: float = 0.5) -> duckdb.DuckDBPyConnection:
    """Open (and initialise) a database. path=':memory:' gives a throwaway DB for tests."""
    if path is None:
        path = config.resolve(config.settings()["paths"]["db"])
    if str(path) != ":memory:":
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if read_only and not path.exists():
            read_only = False  # nothing to read yet; create it
    last_err = None
    for _ in range(retries):
        try:
            con = duckdb.connect(str(path), read_only=read_only)
            break
        except duckdb.IOException as e:  # file locked by the other process
            last_err = e
            time.sleep(wait_s)
    else:
        raise last_err
    if not read_only:
        init(con)
    return con


def init(con: duckdb.DuckDBPyConnection) -> None:
    for stmt in DDL + VIEWS:
        con.execute(stmt)


def insert_reports(con: duckdb.DuckDBPyConnection, df: pd.DataFrame) -> int:
    """Insert reports not already stored. Returns the number of new rows."""
    if df.empty:
        return 0
    df = df.drop_duplicates("report_id")[REPORT_COLS]
    before = con.execute("SELECT count(*) FROM reports").fetchall()[0][0]
    con.register("incoming", df)
    con.execute(f"""
        INSERT INTO reports ({', '.join(REPORT_COLS)})
        SELECT {', '.join(REPORT_COLS)} FROM incoming i
        WHERE NOT EXISTS (SELECT 1 FROM reports r WHERE r.report_id = i.report_id)""")
    con.unregister("incoming")
    return con.execute("SELECT count(*) FROM reports").fetchall()[0][0] - before


def load_frame(df: pd.DataFrame) -> duckdb.DuckDBPyConnection:
    """In-memory database holding just this frame. Used by tests and fault injection."""
    con = connect(":memory:")
    insert_reports(con, df)
    return con
