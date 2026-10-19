from datetime import date

from award_to_lane.contacts import apollo
from award_to_lane.db import Database
from award_to_lane.models import Lane, RateQuote
from award_to_lane.pricing.base import apply_rates, get_provider
from award_to_lane.pricing.usda_market_news import UsdaMarketNewsRateProvider
from test_lanes_score import make_leads


def test_apollo_disabled_is_a_noop(turkey, settings):
    leads = make_leads(turkey, settings)
    stats = apollo.enrich_leads(leads, Database(":memory:"), settings, http=None)
    assert stats["enabled"] is False and leads[0].domain is None


def test_apollo_enabled_uses_cache(turkey, settings, monkeypatch):
    monkeypatch.setitem(settings.data["plugins"]["apollo"], "enabled", True)
    monkeypatch.setenv("APOLLO_API_KEY", "test-key")
    monkeypatch.setattr(apollo.ApolloClient, "find_domain", lambda self, company, state=None: "cargill.com")
    monkeypatch.setattr(apollo.ApolloClient, "find_contacts", lambda self, domain, titles, limit=5: [{"name": "Pat Example", "title": "Logistics Manager", "email": "pat@cargill.com"}])
    db = Database(":memory:")
    leads = make_leads(turkey, settings)
    stats = apollo.enrich_leads(leads, db, settings, http=object())
    assert stats["lookups"] == 2 and leads[0].domain == "cargill.com" and leads[0].contacts[0]["email"] == "pat@cargill.com"
    stats2 = apollo.enrich_leads(make_leads(turkey, settings), db, settings, http=object())
    assert stats2["lookups"] == 0                      # cached, no credits spent


def test_stub_and_market_news_provider(turkey, settings, monkeypatch):
    leads = make_leads(turkey, settings)
    assert get_provider(settings, None, None).name == "stub"
    assert apply_rates(leads, get_provider(settings, None, None)) == 0
    provider = UsdaMarketNewsRateProvider(settings, None, None)
    provider._rows = [{"origin": "Nebraska", "destination": "Atlanta, Alabama region", "rate_low": "3,200", "rate_high": "3,600"}]
    lane = next(l for l in leads[0].lanes if l.dest_state == "AL")
    quote = provider.quote(lane, leads[0])
    assert isinstance(quote, RateQuote) and quote.low == 3200 and quote.high == 3600
    assert provider.quote(Lane("X", "NE", "Y", "OR", "Y, OR", 1, 1, "LB", 1, False, 1, "dry van", None), leads[0]) is None
