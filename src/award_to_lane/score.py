"""Lead scoring. Everything tunable is in config.yaml under `scoring`; the formula is this one function."""
from __future__ import annotations

import math
from datetime import date

from .models import FLEET_PRIVATE, Lead


def urgency_factor(days_until_open: int | None, days_until_close: int | None) -> float:
    """1.0 when the window opens within two weeks or is already open with time left; lower the further out."""
    if days_until_open is None:
        return 0.5
    if days_until_close is not None and days_until_close < 0:
        return 0.0
    if days_until_open <= 0:
        return 0.8 if (days_until_close is None or days_until_close > 14) else 0.6
    if days_until_open <= 14:
        return 1.0
    if days_until_open <= 30:
        return 0.85
    if days_until_open <= 60:
        return 0.65
    if days_until_open <= 120:
        return 0.45
    return 0.25


def score_lead(lead: Lead, today: date, settings) -> float:
    weights = settings.get("scoring.weights", {}) or {}
    penalties = settings.get("scoring.penalties", {}) or {}
    w_value = float(weights.get("value", 1.0))
    w_trucks = float(weights.get("truckloads", 1.0))
    w_urgency = float(weights.get("urgency", 1.5))

    # value: $10k -> 0, $100M -> 1 (log scale)
    dollars = lead.dollars or 0.0
    value_pts = min(max((math.log10(dollars + 1) - 4.0) / 4.0, 0.0), 1.0)
    # truckloads: 1 -> ~0.1, 1,000 -> 1 (log scale)
    trucks_pts = min(max(math.log10(lead.truckloads + 1) / 3.0, 0.0), 1.0)
    urgency_pts = urgency_factor(lead.days_until_open, lead.days_until_close)

    total_w = w_value + w_trucks + w_urgency
    raw = (value_pts * w_value + trucks_pts * w_trucks + urgency_pts * w_urgency) / total_w if total_w else 0.0

    multiplier = 1.0
    if lead.is_blanket:
        multiplier *= float(penalties.get("blanket_award", 0.5))
    if "needs_review" in lead.flags:
        multiplier *= float(penalties.get("needs_review", 0.7))
    if lead.is_international:
        multiplier *= float(penalties.get("international", 0.6))
    if lead.delivery_start is None:
        multiplier *= float(penalties.get("window_unknown", 0.8))
    if lead.fleet_status == FLEET_PRIVATE:                      # FMCSA plug-in confirmed the company runs its own trucks
        multiplier *= float(penalties.get("private_fleet", 0.8))
    return round(100.0 * raw * multiplier, 1)
