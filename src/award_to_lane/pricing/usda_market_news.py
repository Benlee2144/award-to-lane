"""USDA Market News (MARS API) truck-rate reference for reefer lanes.

Free API key: https://mymarketnews.ams.usda.gov/mars-api/getting-started . The key goes in the .env
variable named by plugins.pricing.usda_market_news.api_key_env. The provider lists the available reports
once, picks the first whose name contains `report_name_contains` (default "Truck Rate"), pulls the latest
report rows, and matches a lane by origin region / destination city words. Matches are approximate and
labelled as such: these are produce truck-rate ranges by region, not lane quotes.
"""
from __future__ import annotations

import logging
import re

from ..models import Lane, Lead, RateQuote
from .base import RateProvider

log = logging.getLogger("award_to_lane.pricing")

STATE_NAMES = {"AL": "Alabama", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado", "FL": "Florida",
               "GA": "Georgia", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "MI": "Michigan", "MN": "Minnesota",
               "NC": "North Carolina", "NJ": "New Jersey", "NY": "New York", "OR": "Oregon", "PA": "Pennsylvania",
               "SC": "South Carolina", "TX": "Texas", "WA": "Washington", "WI": "Wisconsin", "NM": "New Mexico", "NE": "Nebraska",
               "MO": "Missouri", "OH": "Ohio", "TN": "Tennessee", "VA": "Virginia", "KY": "Kentucky", "MS": "Mississippi",
               "LA": "Louisiana", "OK": "Oklahoma", "KS": "Kansas", "IA": "Iowa", "ND": "North Dakota", "SD": "South Dakota",
               "MT": "Montana", "WY": "Wyoming", "UT": "Utah", "NV": "Nevada", "MA": "Massachusetts", "MD": "Maryland",
               "DE": "Delaware", "CT": "Connecticut", "ME": "Maine", "VT": "Vermont", "NH": "New Hampshire", "WV": "West Virginia",
               "RI": "Rhode Island", "DC": "District of Columbia"}


class UsdaMarketNewsRateProvider(RateProvider):
    name = "usda_market_news"

    def __init__(self, settings, db, http):
        cfg = settings.get("plugins.pricing.usda_market_news", {}) or {}
        self.base_url = str(cfg.get("base_url", "https://marsapi.ams.usda.gov/services/v1.2")).rstrip("/")
        self.key = settings.env(cfg.get("api_key_env", "USDA_MARS_API_KEY"))
        self.name_filter = str(cfg.get("report_name_contains", "Truck Rate")).lower()
        self.cache_hours = float(cfg.get("cache_hours", 24))
        self.db = db
        self.http = http
        self._rows: list[dict] | None = None
        if not self.key:
            log.warning("usda_market_news pricing selected but no API key is set; rates will be empty")

    def _get(self, path: str) -> list | dict:
        self.http._wait()
        resp = self.http.session.get(f"{self.base_url}{path}", auth=(self.key, ""), timeout=self.http.timeout)
        self.http._last_request = __import__("time").monotonic()
        resp.raise_for_status()
        return resp.json()

    def rows(self) -> list[dict]:
        if self._rows is not None:
            return self._rows
        if not self.key:
            self._rows = []
            return self._rows
        cached = self.db.cache_get("mars:truck_rates", self.cache_hours) if self.db else None
        if cached is not None:
            self._rows = cached
            return cached
        rows: list[dict] = []
        try:
            reports = self._get("/reports")
            report = next((r for r in reports if self.name_filter in str(r.get("report_title", "")).lower()), None)
            if report:
                data = self._get(f"/reports/{report.get('slug_id') or report.get('slug_name')}")
                rows = data.get("results", data) if isinstance(data, dict) else data
                rows = [r for r in rows if isinstance(r, dict)]
            else:
                log.warning("no MARS report matching %r", self.name_filter)
        except Exception as exc:
            log.warning("MARS API failed: %s", exc)
        if self.db:
            self.db.cache_put("mars:truck_rates", rows)
            self.db.commit()
        self._rows = rows
        return rows

    @staticmethod
    def _text(row: dict, *keys: str) -> str:
        for k in keys:
            if row.get(k):
                return str(row[k]).lower()
        return ""

    def quote(self, lane: Lane, lead: Lead) -> RateQuote | None:
        if lane.equipment != "reefer" or not lane.dest_city or not lane.origin_state:
            return None
        rows = self.rows()
        if not rows:
            return None
        origin_words = {STATE_NAMES.get(lane.origin_state, "").lower(), (lane.origin_city or "").lower()} - {""}
        dest_words = {lane.dest_city.lower(), STATE_NAMES.get(lane.dest_state or "", "").lower()} - {""}
        best = None
        for row in rows:
            origin = self._text(row, "origin", "region", "market_location_name", "office_name")
            dest = self._text(row, "destination", "commodity_destination", "market_type", "destination_city")
            if any(w in origin for w in origin_words) and any(w in dest for w in dest_words):
                best = row
                break
        if best is None:
            return None
        low = _num(best.get("rate_low") or best.get("low") or best.get("price_min"))
        high = _num(best.get("rate_high") or best.get("high") or best.get("price_max"))
        if low is None and high is None:
            return None
        return RateQuote(low=low, high=high, unit="per load", source="USDA Market News truck rates",
                         note=f"{best.get('origin', '')} -> {best.get('destination', '')} (regional reference)")


def _num(value) -> float | None:
    if value is None:
        return None
    try:
        return float(re.sub(r"[^\d.]", "", str(value)))
    except ValueError:
        return None
