"""Loads the YAML configuration that drives rules, thresholds and KPIs."""
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"


def _load(name: str) -> dict:
    with open(CONFIG_DIR / name, encoding="utf-8") as f:
        return yaml.safe_load(f)


@lru_cache(maxsize=None)
def settings() -> dict:
    return _load("settings.yaml")


@lru_cache(maxsize=None)
def rules() -> list:
    return _load("rules.yaml")["rules"]


@lru_cache(maxsize=None)
def requirements() -> list:
    return _load("requirements.yaml")["requirements"]


def rule_params(cfg: dict | None = None) -> dict:
    """Flat dict of every value a rule may reference as ${name}."""
    cfg = cfg or settings()
    params = dict(cfg["thresholds"])
    params.update(cfg["spatial"])
    return params


def resolve(path: str | Path) -> Path:
    """Paths in settings are relative to the repo root."""
    p = Path(path)
    return p if p.is_absolute() else ROOT / p
