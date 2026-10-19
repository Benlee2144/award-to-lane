"""FMCSA fleet plug-in: name matching, classification, caching, disabled no-op, scoring penalty. Network mocked."""
import csv
import logging

import pytest
import requests

from award_to_lane.contacts import fmcsa
from award_to_lane.contacts.fmcsa import classify, like_pattern, name_tokens, pick_row, rank_rows, soql_where
from award_to_lane.db import Database
from award_to_lane.models import (FLAG_PRIVATE_FLEET, FLEET_FOR_HIRE, FLEET_NONE, FLEET_NOT_FOUND, FLEET_PRIVATE,
                                  FLEET_UNVERIFIED)
from award_to_lane.report.apollo_import import write_apollo_import
from award_to_lane.report.call_sheet import write_csvs, write_html
from award_to_lane.score import score_lead
from test_lanes_score import TODAY, make_leads


class FakeResponse:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else []

    def json(self):
        return self._payload


class FakeHttp:
    """Just enough of PoliteSession for the client: a session with get(), a timeout and the spacing bookkeeping."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.timeout = 5
        self._last_request = 0.0
        self.requests_made = 0
        self.session = self

    def _wait(self):
        pass

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append((url, params, headers))
        if not self.responses:
            raise AssertionError("unexpected census request")
        return self.responses.pop(0)


def row(legal="CARGILL MEAT SOLUTIONS CORPORATION", state="KS", city="WICHITA", classdef="PRIVATE PROPERTY;AUTHORIZED FOR HIRE",
        units="168", dot="230724", dba=None, drivers="165", mc="160279", mc_status="A", mcs150="20260408"):
    r = {"dot_number": dot, "legal_name": legal, "phy_city": city, "phy_state": state, "status_code": "A", "carrier_operation": "A",
         "power_units": units, "truck_units": units, "total_drivers": drivers, "carship": "C", "classdef": classdef}
    if dba:
        r["dba_name"] = dba
    if mc:
        r.update({"docket1prefix": "MC", "docket1": mc, "docket1_status_code": mc_status})
    if mcs150:
        r["mcs150_date"] = mcs150
    return r


def enable(settings, monkeypatch, **overrides):
    monkeypatch.setitem(settings.data["plugins"]["fmcsa"], "enabled", True)
    for k, v in overrides.items():
        monkeypatch.setitem(settings.data["plugins"]["fmcsa"], k, v)
    monkeypatch.delenv("SOCRATA_APP_TOKEN", raising=False)


# -- name handling, query, classification ---------------------------------------------

@pytest.mark.parametrize("name, tokens, pattern", [
    ("CARGILL MEAT SOLUTIONS CORPORATION", ["CARGILL", "MEAT", "SOLUTIONS"], "CARGILL%MEAT%SOLUTIONS%"),
    ("J.R. SIMPLOT COMPANY", ["J", "R", "SIMPLOT"], "J%R%SIMPLOT%"),
    ("HP HOOD LLC SIX KIMBALL LN STE 400", ["HP", "HOOD"], "HP%HOOD%"),                      # address glued on by the PDF
    ("THE NEIL JONES FOOD COMPANY", ["NEIL", "JONES", "FOOD"], "NEIL%JONES%FOOD%"),
    ("KELLOGG'S SNACKS, INC.", ["KELLOGG", "S", "SNACKS"], "KELLOGG%S%SNACKS%"),             # quotes never reach SoQL
    ("VIRGINIA POULTRY GROWERS COOPERATIV", ["VIRGINIA", "POULTRY", "GROWERS", "COOPERATIV"], "VIRGINIA%POULTRY%GROWERS%COOPERATIV%"),
    ("AB", ["AB"], None),
    ("", [], None),
])
def test_tokens_and_like_pattern(name, tokens, pattern):
    assert name_tokens(name) == tokens
    assert like_pattern(name) == pattern


def test_soql_where():
    assert soql_where("J.R. SIMPLOT COMPANY") == ("status_code='A' AND (upper(legal_name) like 'J%R%SIMPLOT%' "
                                                 "OR upper(dba_name) like 'J%R%SIMPLOT%')")
    assert soql_where("HP HOOD LLC", active_only=False) == "(upper(legal_name) like 'HP%HOOD%' OR upper(dba_name) like 'HP%HOOD%')"
    assert soql_where("HP HOOD LLC", state="ma").endswith(" AND phy_state='MA'")
    assert "phy_state" not in soql_where("HP HOOD LLC", state="M'A")                    # only a two-letter code is interpolated
    assert soql_where("&&") is None and soql_where(None) is None


def test_classify():
    assert classify("PRIVATE PROPERTY;AUTHORIZED FOR HIRE", 168) == FLEET_PRIVATE
    assert classify("PRIVATE PROPERTY", 1) == FLEET_PRIVATE
    assert classify("AUTHORIZED FOR HIRE;EXEMPT FOR HIRE", 3) == FLEET_FOR_HIRE
    assert classify("PRIVATE PROPERTY", 0) == FLEET_NONE and classify("PRIVATE PROPERTY", None) == FLEET_NONE
    assert classify("PRIVATE PROPERTY", 4, min_units=5) == FLEET_NONE
    assert classify("PRIVATE PASSENGER, BUSINESS", 5) == FLEET_NONE and classify("OTHER-REGISTRANT", 2) == FLEET_NONE
    assert classify(None, 80) == FLEET_UNVERIFIED                                        # trucks on file, no classification


def test_rank_and_pick():
    exact, other_state = row(), row(state="PA", city="WYALUSING", dot="268332", units="16")
    dba = row(legal="CARGILL REGIONAL BEEF INC", dba="Cargill Meat Solutions", dot="1")
    subset = row(legal="CARGILL MEAT LOGISTICS SOLUTIONS INC", dot="2")
    stranger = row(legal="CARGILL KITCHEN SOLUTIONS INC", state="KS", dot="3")
    ranked = rank_rows("CARGILL MEAT SOLUTIONS CORPORATION", "KS", [stranger, subset, dba, other_state, exact])
    assert [(r["dot_number"], q) for r, q in ranked] == [("230724", "exact"), ("268332", "possible"), ("1", "possible"), ("2", "possible")]
    assert pick_row("CARGILL MEAT SOLUTIONS CORPORATION", "KS", "WICHITA", [stranger, subset, dba, other_state, exact])[0] is exact
    assert pick_row("CARGILL MEAT SOLUTIONS CORPORATION", "KS", None, [stranger, subset])[0] is subset
    assert pick_row("CARGILL MEAT SOLUTIONS CORPORATION", "KS", None, [stranger]) is None
    assert pick_row("CARGILL MEAT SOLUTIONS CORPORATION", None, None, [exact])[1] == "possible"       # no state to check
    # a truncated award name is a possible match, but only with enough characters
    vpg = row(legal="VIRGINIA POULTRY GROWERS COOPERATIVE INC", state="VA", city="HINTON", dot="9")
    assert rank_rows("VIRGINIA POULTRY GROWERS COOPERATIV", "VA", [vpg])[0][1] == "possible"
    assert rank_rows("GH DAIRY", "TX", [row(legal="GH DAIRY FARMS OF TEXAS LLC", state="TX")])[0][1] == "possible"   # contains our words
    # the contains-all-words rule needs two or more words, every one of them, the same first word and the same state
    assert rank_rows("SMITH FARMS", "KS", [row(legal="SMITH TRUCKING", state="KS")]) == []
    assert rank_rows("SMITH FARMS INC", "KS", [row(legal="SMITH FAMILY FARMS LLC", state="KS")])[0][1] == "possible"
    assert rank_rows("SMITH FARMS INC", "KS", [row(legal="SMITH FAMILY FARMS LLC", state="MO")]) == []
    assert rank_rows("AMERICAN FOODS", "WI", [row(legal="NORTH AMERICAN FOODS INC", state="WI")]) == []
    assert rank_rows("SMITH", "KS", [row(legal="SMITH FARMS", state="KS")]) == []
    # several exact matches are divisions of one company: keep the private fleet with the most power units
    divisions = [row(legal="J R SIMPLOT COMPANY", state="ID", city="CALDWELL", dot="12411", units="17", classdef="PRIVATE PROPERTY", mc=None),
                 row(legal="J R SIMPLOT COMPANY", state="ID", city="BOISE", dot="62813", units="169"),
                 row(legal="JR SIMPLOT CO", state="ID", city="HEYBURN", dot="7", units="400", classdef="AUTHORIZED FOR HIRE")]
    picked, quality = pick_row("J.R. SIMPLOT COMPANY", "ID", "BOISE", divisions)
    assert quality == "exact" and picked["dot_number"] == "62813"


def test_build_match_and_note():
    m = fmcsa.build_match(row(), "exact")
    assert (m.status, m.usdot, m.power_units, m.total_drivers, m.mc_number, m.mcs150_date) == (FLEET_PRIVATE, "230724", 168, 165, "MC-160279", "2026-04-08")
    assert m.note == ("CARGILL MEAT SOLUTIONS CORPORATION (Wichita, KS); Authorized For Hire; Private Property; 168 power units, "
                      "165 drivers; MC 160279; MCS-150 2026-04-08")
    p = fmcsa.build_match(row(state="PA", city="WYALUSING", dba="CARGILL REGIONAL BEEF", mc_status="I", mcs150=None), "possible")
    assert p.status == FLEET_UNVERIFIED and p.note.startswith("possible match: CARGILL MEAT SOLUTIONS CORPORATION dba CARGILL REGIONAL BEEF (Wyalusing, PA)")
    assert "MC 160279 inactive" in p.note and "MCS-150" not in p.note
    assert "no operation classification on file" in fmcsa.build_match(row(classdef=None, mc=None), "exact").note


# -- the pipeline step ----------------------------------------------------------------

def test_disabled_is_a_noop(turkey, settings):
    leads = make_leads(turkey, settings)
    http = FakeHttp([])
    stats = fmcsa.enrich_leads(leads, Database(":memory:"), settings, http)
    assert stats["enabled"] is False and stats["lookups"] == 0 and http.calls == []
    assert all(l.fleet_status is None and l.usdot is None and l.power_units is None and l.flags == [] for l in leads)


def test_lookup_fills_fields_caches_and_shows_on_the_sheet(turkey, settings, monkeypatch, tmp_path):
    enable(settings, monkeypatch)
    db = Database(":memory:")
    http = FakeHttp([FakeResponse(200, [row()]), FakeResponse(200, [])])
    leads = make_leads(turkey, settings)
    stats = fmcsa.enrich_leads(leads, db, settings, http)
    cargill, vpg = leads
    assert stats["lookups"] == 2 and stats["private"] == 1 and stats["misses"] == 1 and stats["errors"] == 0
    url, params, headers = http.calls[0]
    assert url == fmcsa.DEFAULT_URL and params["$limit"] == 50 and "X-App-Token" not in headers
    assert params["$where"] == ("status_code='A' AND (upper(legal_name) like 'CARGILL%MEAT%SOLUTIONS%' "
                                "OR upper(dba_name) like 'CARGILL%MEAT%SOLUTIONS%')")
    assert (cargill.fleet_status, cargill.usdot, cargill.power_units) == (FLEET_PRIVATE, "230724", 168)
    assert FLAG_PRIVATE_FLEET in cargill.flags and cargill.fleet_note.startswith("CARGILL MEAT SOLUTIONS CORPORATION (Wichita, KS)")
    assert (vpg.fleet_status, vpg.usdot, vpg.power_units) == (FLEET_NOT_FOUND, None, None) and vpg.flags == []
    cached = db.fleet_get(fmcsa.company_key(cargill.company))
    assert cached["fleet_status"] == FLEET_PRIVATE and cached["source"] == fmcsa.SOURCE and cached["mc_number"] == "MC-160279"
    # second run: the hit and the miss both come from the cache, no request is made
    again = make_leads(turkey, settings)
    stats2 = fmcsa.enrich_leads(again, db, settings, FakeHttp([]))
    assert stats2["lookups"] == 0 and again[0].fleet_status == FLEET_PRIVATE and FLAG_PRIVATE_FLEET in again[0].flags
    assert again[1].fleet_status == FLEET_NOT_FOUND
    # and the outputs show it
    html = write_html(again, tmp_path, TODAY, {"discover": {}, "parse": {}}).read_text(encoding="utf-8")
    assert "<th title='FMCSA census: does the company run its own trucks? (plugins.fmcsa)'>Fleet</th>" in html
    assert "<span class='tag fleet'>private fleet</span>" in html and "168 units &middot; USDOT 230724" in html
    assert "<span class='tag flag'>private_fleet</span>" in html and "<span class='small'>no fleet on record</span>" in html
    assert "colspan='16'" in html and "colspan='15'" not in html   # Fleet and Status columns both on the sheet
    sheet, _ = write_csvs(again, tmp_path)
    rows = list(csv.DictReader(sheet.open(encoding="utf-8")))
    assert (rows[0]["fleet_status"], rows[0]["usdot"], rows[0]["power_units"]) == (FLEET_PRIVATE, "230724", "168")
    assert "private_fleet" in rows[0]["flags"] and rows[1]["fleet_status"] == FLEET_NOT_FOUND and rows[1]["power_units"] == ""
    imp = list(csv.DictReader(write_apollo_import(again, tmp_path).open(encoding="utf-8")))
    by_name = {r["Company"]: r for r in imp}
    assert by_name[cargill.company]["Fleet"] == FLEET_PRIVATE and by_name[cargill.company]["USDOT"] == "230724"
    assert by_name[cargill.company]["Power units"] == "168" and by_name[vpg.company]["Fleet"] == FLEET_NOT_FOUND


def test_ambiguous_match_is_unverified_not_asserted(turkey, settings, monkeypatch, tmp_path):
    enable(settings, monkeypatch)
    db = Database(":memory:")
    elsewhere = row(state="PA", city="WYALUSING", dot="268332", units="16")             # right name, wrong state
    http = FakeHttp([FakeResponse(200, [elsewhere]), FakeResponse(200, [])])
    leads = make_leads(turkey, settings)
    stats = fmcsa.enrich_leads(leads, db, settings, http)
    cargill = leads[0]
    assert stats["unverified"] == 1 and stats["private"] == 0
    assert cargill.fleet_status == FLEET_UNVERIFIED and cargill.usdot == "268332" and cargill.power_units == 16
    assert FLAG_PRIVATE_FLEET not in cargill.flags and cargill.fleet_note.startswith("possible match: ")
    assert db.fleet_get(fmcsa.company_key(cargill.company))["source"] == fmcsa.SOURCE_POSSIBLE
    html = write_html(leads, tmp_path, TODAY, {"discover": {}, "parse": {}}).read_text(encoding="utf-8")
    assert "<span class='tag unsure'>unverified</span>" in html and "title='possible match: CARGILL MEAT SOLUTIONS CORPORATION" in html
    base = score_lead(cargill, TODAY, settings)
    cargill.fleet_status = FLEET_PRIVATE
    assert score_lead(cargill, TODAY, settings) < base                                   # only a confirmed fleet is penalised


def test_scoring_penalty_is_configurable(turkey, settings, monkeypatch):
    lead = make_leads(turkey, settings)[0]
    base = score_lead(lead, TODAY, settings)
    lead.fleet_status = FLEET_PRIVATE
    assert score_lead(lead, TODAY, settings) == pytest.approx(round(base * 0.8, 1), abs=0.11)
    monkeypatch.setitem(settings.data["scoring"]["penalties"], "private_fleet", 0.5)
    assert score_lead(lead, TODAY, settings) == pytest.approx(round(base * 0.5, 1), abs=0.11)
    for status in (FLEET_FOR_HIRE, FLEET_NONE, FLEET_UNVERIFIED, FLEET_NOT_FOUND, None):
        lead.fleet_status = status
        assert score_lead(lead, TODAY, settings) == base


def test_throttled_stops_lookups_without_caching(turkey, settings, monkeypatch):
    enable(settings, monkeypatch)
    db = Database(":memory:")
    http = FakeHttp([FakeResponse(429, {"error": True, "message": "throttled"})])
    leads = make_leads(turkey, settings)
    stats = fmcsa.enrich_leads(leads, db, settings, http)
    assert stats["lookups"] == 1 and stats["errors"] == 1 and "429" in stats["stopped"]
    assert len(http.calls) == 1 and db.one("SELECT COUNT(*) AS n FROM company_fleet")["n"] == 0
    assert leads[0].fleet_status is None and leads[0].flags == []


def test_rejected_app_token_stops_lookups_and_is_sent_as_a_header(turkey, settings, monkeypatch):
    enable(settings, monkeypatch)
    monkeypatch.setenv("SOCRATA_APP_TOKEN", "SECRET-TOKEN-123")
    http = FakeHttp([FakeResponse(403, {"error": True, "message": "Invalid app_token"})])
    stats = fmcsa.enrich_leads(make_leads(turkey, settings), Database(":memory:"), settings, http)
    assert stats["lookups"] == 1 and "token" in stats["stopped"] and len(http.calls) == 1
    assert http.calls[0][2]["X-App-Token"] == "SECRET-TOKEN-123" and "SECRET-TOKEN-123" not in str(http.calls[0][1])


def test_soql_error_counts_as_a_failure_and_three_failures_stop_the_run(turkey, settings, monkeypatch, caplog):
    enable(settings, monkeypatch)
    bad = {"code": "query.compiler.malformed", "error": True, "message": "Could not parse SoQL query"}
    http = FakeHttp([FakeResponse(400, bad), FakeResponse(200, {"error": True, "message": "odd shape"})])
    leads = make_leads(turkey, settings)
    with caplog.at_level(logging.WARNING, logger="award_to_lane.fmcsa"):
        stats = fmcsa.enrich_leads(leads, Database(":memory:"), settings, http)
    assert stats["errors"] == 2 and stats["stopped"] is None and "odd shape" in caplog.text
    assert all(l.fleet_status is None for l in leads)


def test_network_errors_are_swallowed_and_never_log_the_token(turkey, settings, monkeypatch, caplog):
    enable(settings, monkeypatch)
    monkeypatch.setenv("SOCRATA_APP_TOKEN", "SECRET-TOKEN-123")

    class Boom(FakeHttp):
        def get(self, url, params=None, timeout=None, headers=None):
            self.calls.append((url, params, headers))
            raise requests.ConnectionError(f"boom {url} {headers}")

    leads = make_leads(turkey, settings)
    with caplog.at_level(logging.WARNING, logger="award_to_lane.fmcsa"):
        stats = fmcsa.enrich_leads(leads, Database(":memory:"), settings, Boom([]))
    assert stats["errors"] == 2 and stats["stopped"] is None and leads[0].fleet_status is None
    assert "SECRET-TOKEN-123" not in caplog.text and "ConnectionError" in caplog.text


def test_per_run_cap(turkey, settings, monkeypatch):
    enable(settings, monkeypatch, max_companies_per_run=1)
    http = FakeHttp([FakeResponse(200, [])])
    stats = fmcsa.enrich_leads(make_leads(turkey, settings), Database(":memory:"), settings, http)
    assert stats["lookups"] == 1 and len(http.calls) == 1


def test_full_page_is_narrowed_to_the_company_state(turkey, settings, monkeypatch):
    enable(settings, monkeypatch)
    page = [row(legal=f"CARGILL MEAT SOLUTIONS {i}", state="TX", dot=str(i)) for i in range(50)]
    http = FakeHttp([FakeResponse(200, page), FakeResponse(200, [row()]), FakeResponse(200, [])])
    leads = make_leads(turkey, settings)
    stats = fmcsa.enrich_leads(leads, Database(":memory:"), settings, http)
    assert stats["lookups"] == 2 and len(http.calls) == 3
    assert http.calls[1][1]["$where"].endswith("AND phy_state='KS'") and leads[0].fleet_status == FLEET_PRIVATE


def test_cached_results_are_refreshed_on_their_own_schedules(turkey, settings, monkeypatch):
    enable(settings, monkeypatch, refresh_days=90, miss_retry_days=30)
    db = Database(":memory:")
    fmcsa.enrich_leads(make_leads(turkey, settings), db, settings, FakeHttp([FakeResponse(200, [row()]), FakeResponse(200, [])]))
    assert fmcsa.enrich_leads(make_leads(turkey, settings), db, settings, FakeHttp([]))["lookups"] == 0
    db.execute("UPDATE company_fleet SET updated_at = '2020-01-01T00:00:00+00:00' WHERE fleet_status = ?", (FLEET_NOT_FOUND,))
    stats = fmcsa.enrich_leads(make_leads(turkey, settings), db, settings, FakeHttp([FakeResponse(200, [])]))
    assert stats["lookups"] == 1                                                          # only the stale miss is retried
    db.execute("UPDATE company_fleet SET updated_at = '2020-01-01T00:00:00+00:00'")
    again = make_leads(turkey, settings)
    stats = fmcsa.enrich_leads(again, db, settings, FakeHttp([FakeResponse(200, [row(units="200")]), FakeResponse(200, [])]))
    assert stats["lookups"] == 2 and again[0].power_units == 200


def test_min_power_units(turkey, settings, monkeypatch):
    enable(settings, monkeypatch, min_power_units=5)
    http = FakeHttp([FakeResponse(200, [row(units="2")]), FakeResponse(200, [])])
    leads = make_leads(turkey, settings)
    fmcsa.enrich_leads(leads, Database(":memory:"), settings, http)
    assert leads[0].fleet_status == FLEET_NONE and leads[0].power_units == 2 and FLAG_PRIVATE_FLEET not in leads[0].flags


def test_broker_with_a_record_but_no_trucks(turkey, settings, monkeypatch):
    """Axle Logistics (a brokerage): an exact FMCSA record with carrier authority and 0 power units is not a fleet.
    Rows are the live census answer for 'AXLE%LOGISTICS%' on 2026-10-19."""
    enable(settings, monkeypatch)
    census = [
        {"dot_number": "4208749", "legal_name": "AXLE LOGISTICS CORP", "phy_city": "MONTCLAIR", "phy_state": "NJ", "status_code": "A",
         "carrier_operation": "A", "classdef": "AUTHORIZED FOR HIRE", "power_units": "0", "docket1prefix": "MC", "docket1": "1624935",
         "docket1_status_code": "A", "carship": "B", "mcs150_date": "20240312"},
        {"dot_number": "3666271", "legal_name": "AXLE LOGISTICS & FREIGHT LLC", "phy_city": "VALPARAISO", "phy_state": "IN",
         "status_code": "A", "classdef": "AUTHORIZED FOR HIRE", "power_units": "0", "carship": "B"},
        {"dot_number": "4492623", "legal_name": "KNIGHT AND SONS TRUCKING LLC", "dba_name": "AXLE BRAIN LOGISTICS", "phy_city": "ALPHARETTA",
         "phy_state": "GA", "status_code": "A", "classdef": "PRIVATE PROPERTY;AUTHORIZED FOR HIRE", "power_units": "2", "carship": "C"},
        {"dot_number": "2253967", "legal_name": "AXLE LOGISTICS LLC", "phy_city": "KNOXVILLE", "phy_state": "TN", "status_code": "A",
         "carrier_operation": "C", "classdef": "AUTHORIZED FOR HIRE", "power_units": "0", "truck_units": "0", "docket1prefix": "MC",
         "docket1": "765587", "docket1_status_code": "A", "carship": "B"},
    ]
    assert [(r["dot_number"], q) for r, q in rank_rows("AXLE LOGISTICS LLC", "TN", census)] == [("2253967", "exact"), ("4208749", "possible")]
    lead = make_leads(turkey, settings)[0]
    lead.company, lead.company_city, lead.company_state = "AXLE LOGISTICS LLC", "KNOXVILLE", "TN"
    base = score_lead(lead, TODAY, settings)
    stats = fmcsa.enrich_leads([lead], Database(":memory:"), settings, FakeHttp([FakeResponse(200, census)]))
    assert stats["no_fleet"] == 1 and stats["private"] == 0
    assert (lead.fleet_status, lead.usdot, lead.power_units) == (FLEET_NONE, "2253967", 0) and lead.flags == []
    assert lead.fleet_note == "AXLE LOGISTICS LLC (Knoxville, TN); Authorized For Hire; 0 power units; MC 765587"
    assert score_lead(lead, TODAY, settings) == base
