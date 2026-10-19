"""Rate-reference interface. Implementations return a RateQuote for a lane, or None when they have nothing.

To add SONAR, DAT or a customer's own rate table: subclass RateProvider in a new module in this package,
then add it to PROVIDERS below and set plugins.pricing.provider in config.yaml.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Lane, Lead, RateQuote


class RateProvider(ABC):
    name = "base"

    @abstractmethod
    def quote(self, lane: Lane, lead: Lead) -> RateQuote | None:
        """Return a reference rate for the lane or None."""


def get_provider(settings, db, http) -> RateProvider:
    from .stub import StubRateProvider
    from .usda_market_news import UsdaMarketNewsRateProvider

    providers = {"stub": lambda: StubRateProvider(),
                 "usda_market_news": lambda: UsdaMarketNewsRateProvider(settings, db, http)}
    name = str(settings.get("plugins.pricing.provider", "stub") or "stub")
    factory = providers.get(name)
    if factory is None:
        raise ValueError(f"unknown pricing provider {name!r}; known: {sorted(providers)}")
    return factory()


def apply_rates(leads: list[Lead], provider: RateProvider) -> int:
    quoted = 0
    for lead in leads:
        for lane in lead.lanes:
            try:
                lane.rate = provider.quote(lane, lead)
            except Exception:  # pricing is advisory; never fail the run
                lane.rate = None
            quoted += 1 if lane.rate else 0
    return quoted
