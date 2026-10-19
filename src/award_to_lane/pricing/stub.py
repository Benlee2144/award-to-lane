"""Default pricing provider: returns nothing. Customers with their own rate tools keep this."""
from __future__ import annotations

from ..models import Lane, Lead, RateQuote
from .base import RateProvider


class StubRateProvider(RateProvider):
    name = "stub"

    def quote(self, lane: Lane, lead: Lead) -> RateQuote | None:
        return None
