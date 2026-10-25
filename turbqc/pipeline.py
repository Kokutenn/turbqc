"""One call that brings every derived table up to date after new data lands."""
import time

from . import analytics, config, kpis, product, rules


def refresh(con, cfg: dict | None = None, with_product: bool = True) -> dict:
    cfg = cfg or config.settings()
    t0 = time.perf_counter()
    summary = rules.validate(con, cfg=cfg)
    alerts = analytics.run(con, cfg)
    kpis.build_report_quality(con, cfg)
    product.build_confidence(con, cfg)
    checks = None
    if with_product:
        product.build_product(con)
        checks = product.check_product(con)
    return dict(rules_failed=int(summary["failed"].sum()), alerts=len(alerts),
                product_checks_failed=None if checks is None else int((~checks["passed"]).sum()),
                seconds=round(time.perf_counter() - t0, 2))
