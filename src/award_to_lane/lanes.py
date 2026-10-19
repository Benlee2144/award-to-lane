"""Explode an award into origin -> destination lanes and roll each contract up into a sales lead."""
from __future__ import annotations

import math
from collections import Counter
from datetime import date

from .commodity import equipment_for, pounds_for
from .geo import Geocoder, norm_key, road_miles
from .models import Award, Contract, Lane, Lead, LineItem


def truckloads_for_pounds(pounds: float, settings) -> int:
    per_truck = float(settings.get("lanes.pounds_per_truckload", 40000))
    if pounds <= 0:
        return 0
    return max(1, math.ceil(pounds / per_truck - 1e-9))


def _display_city(raw: str | None) -> str:
    return " ".join(w.capitalize() if not w.startswith("(") else w for w in (raw or "").split())


def build_lanes(award: Award, contract: Contract, geocoder: Geocoder | None, settings) -> list[Lane]:
    origin_city, origin_state = contract.plant_city, contract.plant_state
    origin_flags: list[str] = []
    if not origin_city:
        origin_city, origin_state = contract.contractor_city, contract.contractor_state
        origin_flags.append("origin_is_contractor_address")
    origin = geocoder.locate(origin_city, origin_state) if geocoder else None
    if origin is not None and not origin_state and origin.source == "gazetteer?":
        origin_flags.append("origin_state_inferred")
    factor = float(settings.get("lanes.road_distance_factor", 1.2))
    aliases = {str(k).upper(): str(v) for k, v in (settings.get("lanes.destination_aliases", {}) or {}).items()}
    groups: dict[tuple[str, str | None], list[LineItem]] = {}
    for li in contract.line_items:
        alias = aliases.get((li.dest_raw or "").strip().upper())
        if alias and not li.dest_state:
            city, _, state = alias.rpartition(",")
            li.dest_city, li.dest_state = city.strip() or li.dest_city, state.strip() or None
            li.flags = [f for f in li.flags if f != "dest_state_missing"] + ["dest_alias"]
        groups.setdefault((norm_key(li.dest_city) or li.dest_raw, li.dest_state), []).append(li)
    lanes: list[Lane] = []
    for (_, state), items in groups.items():
        first = items[0]
        point = geocoder.locate(first.dest_city, state) if (geocoder and first.dest_city and state) else None
        flags = list(origin_flags)
        pounds = 0.0
        estimated = False
        trucks = 0
        equipment = Counter()
        for li in items:
            lb, est = pounds_for(li.qty, li.uom, li.material, settings)
            pounds += lb
            estimated = estimated or est
            trucks += truckloads_for_pounds(lb, settings)
            equipment[equipment_for(li.material, award.commodity, settings)] += 1
            for f in li.flags:
                if f not in flags:
                    flags.append(f)
        if estimated:
            flags.append("weight_estimated")
        if point is None:
            flags.append("dest_not_geocoded")
        if origin is None:
            flags.append("origin_not_geocoded")
        display = point.name if point and point.source.startswith("gazetteer") else (
            f"{_display_city(first.dest_city)}, {state}" if state else first.dest_raw)
        lanes.append(Lane(origin_city=origin_city, origin_state=origin_state, dest_city=first.dest_city, dest_state=state,
                          dest_display=display, drops=len(items), qty=sum(li.qty or 0 for li in items),
                          uom=first.uom or (award.total_uom or ""), pounds=round(pounds), pounds_estimated=estimated,
                          truckloads=trucks, equipment=equipment.most_common(1)[0][0] if equipment else settings.get("equipment.default", "dry van"),
                          miles=road_miles(origin, point, factor), flags=flags))
    lanes.sort(key=lambda l: (-l.truckloads, l.dest_display))
    return lanes


def build_lead(award: Award, contract: Contract, *, source: str, posted_date: date | None, first_seen: date | None,
               pdf_url: str | None, geocoder: Geocoder | None, settings, today: date) -> Lead:
    lanes = build_lanes(award, contract, geocoder, settings)
    pounds = sum(l.pounds for l in lanes)
    equipment_set = sorted({l.equipment for l in lanes})
    if not equipment_set:
        equipment_set = [equipment_for(None, award.commodity, settings)]
    dollars = contract.total_dollars
    if dollars is None:
        dollars = sum((li.qty or 0) * (li.price or 0) for li in contract.line_items) or None
    flags: list[str] = []
    if award.needs_review():
        flags.append("needs_review")
    if award.is_blanket:
        flags.append("blanket_award")
    if award.is_international:
        flags.append("international")
    if award.delivery_start is None:
        flags.append("window_unknown")
    for f in contract.flags:
        if f not in flags:
            flags.append(f)
    new_days = int(settings.get("call_sheet.new_window_days", 3))
    is_new = first_seen == today and (posted_date is None or (today - posted_date).days <= new_days)
    return Lead(
        source=source, award_key=award.award_key, contract_no=contract.contract_no, company=contract.contractor_name,
        company_city=contract.contractor_city, company_state=contract.contractor_state,
        plant_city=contract.plant_city or contract.contractor_city, plant_state=contract.plant_state or contract.contractor_state,
        commodity=award.commodity or award.commodity_title or "", equipment=" + ".join(equipment_set),
        award_date=award.award_date, posted_date=posted_date, delivery_start=award.delivery_start,
        delivery_end=award.delivery_end,
        days_until_open=(award.delivery_start - today).days if award.delivery_start else None,
        days_until_close=(award.delivery_end - today).days if award.delivery_end else None,
        num_offers=award.num_offers, total_qty=sum(li.qty or 0 for li in contract.line_items) or (contract.total_qty or 0),
        total_uom=contract.total_uom or (contract.line_items[0].uom if contract.line_items and contract.line_items[0].uom else (award.total_uom or "")),
        total_pounds=round(pounds), truckloads=sum(l.truckloads for l in lanes), dollars=dollars,
        destinations=len(lanes), lanes=lanes, is_new=bool(is_new), is_international=award.is_international,
        is_blanket=award.is_blanket, flags=flags, pdf_url=pdf_url)


def lead_is_active(lead: Lead, award: Award, today: date, settings) -> bool:
    """Rolling window: keep every lead whose delivery window has not closed."""
    if award.is_cancelled:
        return False
    if lead.is_international and not settings.get("sources.usda_pca.include_international", False):
        return False
    if lead.is_blanket and not settings.get("sources.usda_pca.include_blanket_awards", True):
        return False
    if lead.delivery_end is not None:
        return lead.delivery_end >= today
    anchor = lead.posted_date or lead.award_date
    max_age = int(settings.get("call_sheet.max_window_unknown_age_days", 120))
    return anchor is not None and (today - anchor).days <= max_age
