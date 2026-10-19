"""Small shared helpers: logging, dates, slugs."""
from __future__ import annotations

import logging
import re
import sys
from datetime import date, datetime
from pathlib import Path


def setup_logging(log_dir: Path, level: int = logging.INFO) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("award_to_lane")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    fh = logging.FileHandler(log_dir / "award_to_lane.log", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(level)
    sh.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


def slugify(text: str, max_len: int = 60) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", text or "").strip("-").lower()
    return slug[:max_len] or "item"


def iso(d: date | datetime | None) -> str | None:
    return d.isoformat() if d else None


def parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def fmt_int(value: float | int | None) -> str:
    if value is None:
        return ""
    return f"{int(round(value)):,}"


def fmt_money(value: float | None) -> str:
    if value is None:
        return ""
    return f"${value:,.0f}"
