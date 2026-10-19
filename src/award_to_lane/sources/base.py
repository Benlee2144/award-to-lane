"""The lead-source interface. Every signal source (USDA awards today; recalls, embargoes, WARN notices,
USAspending later) implements this and feeds the same call sheet."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
import logging

from ..db import Database
from ..geo import Geocoder
from ..http import PoliteSession
from ..models import Lead


@dataclass
class RunContext:
    settings: object
    db: Database
    http: PoliteSession
    geocoder: Geocoder | None
    run_date: date
    log: logging.Logger


class LeadSource(ABC):
    name: str = "base"

    def enabled(self, settings) -> bool:
        return bool(settings.get(f"sources.{self.name}.enabled", False))

    @abstractmethod
    def discover(self, ctx: RunContext, *, force_backfill: bool = False, download: bool = True) -> dict:
        """Find new documents/signals, record them in SQLite, download what is needed. Returns stats."""

    @abstractmethod
    def parse_pending(self, ctx: RunContext, *, reparse: bool = False) -> dict:
        """Parse downloaded documents into awards. A failure on one document must not stop the others."""

    @abstractmethod
    def build_leads(self, ctx: RunContext) -> list[Lead]:
        """Return every active lead (rolling window), scored later by the pipeline."""


def all_sources() -> list[LeadSource]:
    from .usda_pca.source import UsdaPcaSource  # local import keeps optional sources optional
    return [UsdaPcaSource()]
