import copy
from datetime import date

from award_to_lane.geo import Gazetteer, Geocoder
from award_to_lane.lanes import build_lead, lead_is_active, truckloads_for_pounds
from award_to_lane.pipeline import sort_leads
from award_to_lane.score import score_lead, urgency_factor
from conftest import ROOT

TODAY = date(2026, 10, 19)


def make_leads(award, settings, first_seen=TODAY, posted=date(2026, 10, 18)):
    geo = Geocoder(Gazetteer(ROOT / "data" / "us_places.csv"))
    return [build_lead(award, c, source="usda_pca", posted_date=posted, first_seen=first_seen, pdf_url="http://x/pca.pdf",
                       geocoder=geo, settings=settings, today=TODAY) for c in award.contracts]


def test_destination_alias(canned_veg, settings, monkeypatch):
    monkeypatch.setitem(settings.data["lanes"], "destination_aliases", {"O-JACK": "JACKSON, MS"})
    canned_veg = copy.deepcopy(canned_veg)
    li = canned_veg.contracts[0].line_items[0]
    li.dest_raw, li.dest_city, li.dest_state, li.flags = "O-JACK", "O-JACK", None, ["dest_state_missing"]
    lead = make_leads(canned_veg, settings)[0]
    lane = next(l for l in lead.lanes if l.dest_state == "MS")
    assert lane.dest_display == "Jackson, MS" and "dest_alias" in lane.flags


def test_truckload_math(settings):
    assert truckloads_for_pounds(0, settings) == 0
    assert truckloads_for_pounds(13000, settings) == 1
    assert truckloads_for_pounds(40000, settings) == 1
    assert truckloads_for_pounds(40001, settings) == 2


def test_turkey_leads(turkey, settings):
    cargill, vpg = make_leads(turkey, settings)
    assert cargill.company == "CARGILL MEAT SOLUTIONS CORPORATION"
    assert (cargill.plant_city, cargill.plant_state) == ("Nebraska City", "NE")
    assert cargill.destinations == 4 and cargill.truckloads == 4 and cargill.total_pounds == 153600
    assert cargill.equipment == "reefer" and cargill.dollars == 659328.0
    assert cargill.days_until_open == 13 and cargill.days_until_close == 27
    assert cargill.is_new
    omaha = next(l for l in cargill.lanes if l.dest_state == "NE")
    assert omaha.dest_display == "Omaha, NE" and 30 < omaha.miles < 80
    assert vpg.truckloads == 3 and vpg.destinations == 3
    for lead in (cargill, vpg):
        lead.score = score_lead(lead, TODAY, settings)
        assert 0 < lead.score <= 100
    assert cargill.score > vpg.score


def test_new_flag_requires_recent_posting(turkey, settings):
    old_post = make_leads(turkey, settings, first_seen=TODAY, posted=date(2026, 9, 1))[0]
    assert not old_post.is_new
    seen_before = make_leads(turkey, settings, first_seen=date(2026, 10, 1), posted=date(2026, 10, 18))[0]
    assert not seen_before.is_new


def test_rolling_window(turkey, settings):
    lead = make_leads(turkey, settings)[0]
    assert lead_is_active(lead, turkey, TODAY, settings)
    assert not lead_is_active(lead, turkey, date(2026, 11, 16), settings)
    assert lead_is_active(lead, turkey, date(2026, 11, 15), settings)


def test_urgency_and_sort(turkey, settings):
    assert urgency_factor(10, 30) == 1.0 > urgency_factor(45, 90) > urgency_factor(200, 300)
    assert urgency_factor(-5, -1) == 0.0
    a, b = make_leads(turkey, settings)
    a.is_new, b.is_new = False, True
    a.score, b.score = 90, 10
    assert sort_leads([a, b])[0] is b
    c, d = make_leads(turkey, settings)
    c.is_new = d.is_new = False
    c.days_until_open, d.days_until_open = 40, 5
    assert sort_leads([c, d])[0] is d
