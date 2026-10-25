"""Builds a self-contained demo database: 5 days of synthetic traffic with real-looking
incidents baked in, plus fault-injection, verification and v1/v2 parallel-run results.
Lets anyone open the dashboard offline."""
import numpy as np
import pandas as pd

from . import config, db, faults, migration, pipeline, synthetic, verify

INCIDENTS = ["source_dark", "clock_drift_subtle", "duplicate_feed", "stuck_position", "altitude_in_metres",
             "position_jump"]
DEMO_END = pd.Timestamp("2026-10-24 00:00")  # fixed, so the README numbers are reproducible


def build(path=None, hours: int = 120, seed: int = 42) -> dict:
    path = path or config.resolve(config.settings()["paths"]["demo_db"])
    clean = synthetic.generate(hours=hours, seed=seed, end=DEMO_END)
    con0 = db.load_frame(clean)
    pipeline.refresh(con0, with_product=False)
    ctx = {"spatial": con0.execute("SELECT * FROM spatial_status").df()}
    con0.close()

    dirty = clean.copy()
    by_name = {f.name: f for f in faults.FAULTS}
    for name in INCIDENTS:
        dirty, _ = by_name[name].apply(dirty, np.random.default_rng(seed), ctx)

    if config.resolve(path).exists():
        config.resolve(path).unlink()
    con = db.connect(path)
    db.insert_reports(con, dirty)
    summary = pipeline.refresh(con)
    verify.synthetic_pairs(con)
    faults.save(con, faults.evaluate(clean))
    result = migration.parallel_run(migration.batches_from_frame(dirty))
    migration.save(con, result)
    report = config.ROOT / "reports" / "parallel_run_demo.md"
    report.parent.mkdir(exist_ok=True)
    report.write_text(migration.to_markdown(result, "Parallel run: synthetic demo (5 days, with incidents)"),
                      encoding="utf-8")
    con.close()
    return dict(path=str(path), reports=len(dirty), **summary)
