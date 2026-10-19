"""SAM.gov plug-in: name matching, caching, disabled no-op, possible-match flagging, quota handling. Network mocked."""
import csv
import logging

import pytest
import requests

from award_to_lane.contacts import apollo, sam_gov
from award_to_lane.contacts.sam_gov import FLAG_POSSIBLE, SOURCE, SOURCE_POSSIBLE, clean_domain, match_entity, name_key, query_name
from award_to_lane.db import Database
from award_to_lane.report.apollo_import import write_apollo_import
from award_to_lane.report.call_sheet import write_html
from test_lanes_score import TODAY, make_leads


class FakeResponse:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}

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
        self.calls.append((url, params))
        if not self.responses:
            raise AssertionError("unexpected SAM.gov request")
        return self.responses.pop(0)


def entity(legal="CARGILL MEAT SOLUTIONS CORPORATION", state="KS", url="https://www.cargill.com/", dba=None, status="A",
           display="Y", pocs=True):
    poc = {"governmentBusinessPOC": {"firstName": "Pat", "lastName": "Example", "title": "Contracts Manager"},
           "electronicBusinessPOC": {"firstName": "Pat", "middleInitial": "", "lastName": "Example"},
           "governmentBusinessAlternatePOC": {"firstName": "Sam", "middleInitial": "Q", "lastName": "Public", "title": ""}} if pocs else {}
    core = {"entityInformation": {"entityURL": url},
            "physicalAddress": {"addressLine1": "825 E Douglas Ave", "city": "WICHITA", "stateOrProvinceCode": state,
                                "zipCode": "67202", "countryCode": "USA"}} if state else {}
    return {"entityRegistration": {"ueiSAM": "ABC123DEF456", "cageCode": "1A2B3", "legalBusinessName": legal, "dbaName": dba,
                                   "registrationStatus": status, "publicDisplayFlag": display},
            "coreData": core, "pointsOfContact": poc}


def payload(*entities):
    return {"totalRecords": len(entities), "entityData": list(entities)}


def enable(settings, monkeypatch, key="test-key"):
    monkeypatch.setitem(settings.data["plugins"]["sam_gov"], "enabled", True)
    if key is None:
        monkeypatch.delenv("SAM_GOV_API_KEY", raising=False)
    else:
        monkeypatch.setenv("SAM_GOV_API_KEY", key)


# -- name handling and matching -------------------------------------------------------

@pytest.mark.parametrize("name, query, key", [
    ("TYSON FOODS INC.", "TYSON FOODS", "TYSONFOODS"),
    ("CALIFORNIA DAIRIES INC CALIFORNIA DAIRIES, INC.", "CALIFORNIA DAIRIES", "CALIFORNIADAIRIES"),          # doubled by the PDF
    ("HP HOOD LLC SIX KIMBALL LN STE 400", "HP HOOD", "HPHOOD"),                                            # address glued on
    ("MASTERS GALLERY FOODS, INCORPORATED POST OFFICE BOX 170", "MASTERS GALLERY FOODS", "MASTERSGALLERYFOODS"),
    ("PAUL PIAZZA & SON INC. PO 52049", "PAUL PIAZZA", "PAULPIAZZASON"),                                    # & is rejected by the API
    ("THE NEIL JONES FOOD COMPANY", "NEIL JONES FOOD", "NEILJONESFOOD"),
    ("J.R. SIMPLOT COMPANY", "J.R. SIMPLOT", "JRSIMPLOT"),
    ("WELCH FOODS INC., A COOPERATIVE", "WELCH FOODS", "WELCHFOODS"),
    ("PACIFIC COAST PRODUCERS", "PACIFIC COAST PRODUCERS", "PACIFICCOASTPRODUCERS"),
])
def test_query_name_and_key(name, query, key):
    assert query_name(name) == query
    assert name_key(name) == key


def test_query_name_rejects_junk():
    assert query_name("&&") is None and query_name("AB") is None
    assert "&" not in (query_name("M & B PRODUCTS, INC.") or "")


def test_clean_domain():
    assert clean_domain("https://www.Cargill.com/about") == "cargill.com"
    assert clean_domain("www.tyson.com") == "tyson.com"
    assert clean_domain("http://user@Example.org:8080/x") == "example.org"
    assert clean_domain("N/A") is None and clean_domain("") is None and clean_domain(None) is None


def test_match_prefers_exact_legal_name_in_same_state():
    other_state, no_site, exact = entity(state="MO"), entity(url=""), entity()
    picked, quality = match_entity("CARGILL MEAT SOLUTIONS CORPORATION", "KS", [other_state, no_site, exact])
    assert quality == "exact" and picked is exact
    assert match_entity("CARGILL MEAT SOLUTIONS CORPORATION", "KS", [entity(legal="CARGILL KITCHEN SOLUTIONS, INC")]) is None
    assert match_entity("CARGILL MEAT SOLUTIONS CORPORATION", None, [exact])[1] == "possible"            # no state to check
    vpg = entity(legal="VIRGINIA POULTRY GROWERS COOPERATIVE, INC.", state="VA")
    assert match_entity("VIRGINIA POULTRY GROWERS COOPERATIV", "VA", [vpg])[1] == "possible"             # truncated award name
    assert match_entity("GH DAIRY", "TX", [entity(legal="GH DAIRY FARMS OF TEXAS LLC", state="TX")]) is None   # prefix too short
    dba = entity(legal="TYSON HOLDINGS LLC", dba="Tyson Foods", state="AR")
    assert match_entity("TYSON FOODS INC.", "AR", [dba])[1] == "possible"


# -- the pipeline step ----------------------------------------------------------------

def test_disabled_is_a_noop(turkey, settings, monkeypatch):
    monkeypatch.setitem(settings.data["plugins"]["sam_gov"], "enabled", False)   # the live config may have it on
    leads = make_leads(turkey, settings)
    stats = sam_gov.enrich_leads(leads, Database(":memory:"), settings, http=None)
    assert stats["enabled"] is False and stats["lookups"] == 0
    assert leads[0].domain is None and leads[0].contacts == [] and leads[0].flags == []


def test_enabled_without_key_makes_no_requests(turkey, settings, monkeypatch):
    enable(settings, monkeypatch, key=None)
    http = FakeHttp([])
    stats = sam_gov.enrich_leads(make_leads(turkey, settings), Database(":memory:"), settings, http)
    assert stats["enabled"] is True and stats["lookups"] == 0 and http.calls == []


def test_lookup_fills_domain_and_contacts_and_caches(turkey, settings, monkeypatch, tmp_path):
    enable(settings, monkeypatch)
    db = Database(":memory:")
    http = FakeHttp([FakeResponse(200, payload(entity())), FakeResponse(200, payload())])
    leads = make_leads(turkey, settings)
    stats = sam_gov.enrich_leads(leads, db, settings, http)
    cargill, vpg = leads
    assert stats["lookups"] == 2 and stats["matched"] == 1 and stats["misses"] == 1 and stats["errors"] == 0
    url, params = http.calls[0]
    assert url == sam_gov.DEFAULT_URL and params["api_key"] == "test-key"
    assert params["legalBusinessName"] == "CARGILL MEAT SOLUTIONS" and params["physicalAddressProvinceOrStateCode"] == "KS"
    assert params["includeSections"] == "entityRegistration,coreData,pointsOfContact" and params["registrationStatus"] == "A"
    assert cargill.domain == "cargill.com" and FLAG_POSSIBLE not in cargill.flags
    assert [c["name"] for c in cargill.contacts] == ["Pat Example", "Sam Q Public"]                 # e-business POC deduped
    assert cargill.contacts[0]["title"] == "Contracts Manager, Govt business POC"
    assert cargill.contacts[1]["title"] == "Alt govt business POC"
    assert all(c["source"] == SOURCE and c["email"] is None for c in cargill.contacts)            # no e-mail on a public key
    assert vpg.domain is None and vpg.contacts == []
    ck = sam_gov.company_key(cargill.company)
    assert db.domain_get(ck) == "cargill.com" and {r["source"] for r in db.contacts_get(ck)} == {SOURCE}
    # second run: hits and the miss both come from the cache, no request is made
    again = make_leads(turkey, settings)
    stats2 = sam_gov.enrich_leads(again, db, settings, FakeHttp([]))
    assert stats2["lookups"] == 0 and again[0].domain == "cargill.com" and len(again[0].contacts) == 2
    # and the outputs show it
    html = write_html(again, tmp_path, TODAY, {"discover": {}, "parse": {}}).read_text(encoding="utf-8")
    assert "cargill.com" in html and "Pat Example (Contracts Manager, Govt business POC) [SAM.gov]" in html
    rows = list(csv.DictReader(write_apollo_import(again, tmp_path).open(encoding="utf-8")))
    assert next(r for r in rows if r["Company"].startswith("CARGILL"))["Website"] == "cargill.com"


def test_possible_match_is_flagged_not_asserted(turkey, settings, monkeypatch):
    enable(settings, monkeypatch)
    db = Database(":memory:")
    vpg_entity = entity(legal="VIRGINIA POULTRY GROWERS COOPERATIVE, INC.", state="VA", url="vpgrowers.example")
    http = FakeHttp([FakeResponse(200, payload()), FakeResponse(200, payload(vpg_entity))])
    leads = make_leads(turkey, settings)
    stats = sam_gov.enrich_leads(leads, db, settings, http)
    vpg = leads[1]
    assert stats["possible"] == 1 and stats["matched"] == 0
    assert vpg.domain == "vpgrowers.example" and FLAG_POSSIBLE in vpg.flags
    assert vpg.contacts and all(c["source"] == SOURCE_POSSIBLE for c in vpg.contacts)
    row = db.one("SELECT source FROM company_domains WHERE company_key = ?", (sam_gov.company_key(vpg.company),))
    assert row["source"] == SOURCE_POSSIBLE
    again = make_leads(turkey, settings)                       # the flag comes back from the cache on later runs too
    sam_gov.enrich_leads(again, db, settings, FakeHttp([]))
    assert FLAG_POSSIBLE in again[1].flags and FLAG_POSSIBLE not in again[0].flags


def test_opted_out_entity_yields_no_data(turkey, settings, monkeypatch):
    enable(settings, monkeypatch)
    stub = {"entityRegistration": {"ueiSAM": "X", "legalBusinessName": "CARGILL MEAT SOLUTIONS CORPORATION",
                                   "publicDisplayFlag": "N", "registrationStatus": "A"}}
    http = FakeHttp([FakeResponse(200, payload(stub)), FakeResponse(200, payload())])
    leads = make_leads(turkey, settings)
    stats = sam_gov.enrich_leads(leads, Database(":memory:"), settings, http)
    assert stats["lookups"] == 2 and leads[0].domain is None and leads[0].contacts == [] and leads[0].flags == []


def test_quota_exhausted_stops_lookups_without_caching(turkey, settings, monkeypatch):
    enable(settings, monkeypatch)
    db = Database(":memory:")
    http = FakeHttp([FakeResponse(429, {"error": {"code": "OVER_RATE_LIMIT"}})])
    leads = make_leads(turkey, settings)
    stats = sam_gov.enrich_leads(leads, db, settings, http)
    assert stats["lookups"] == 1 and stats["errors"] == 1 and "quota" in stats["stopped"]
    assert len(http.calls) == 1 and db.one("SELECT COUNT(*) AS n FROM company_domains")["n"] == 0
    assert leads[0].domain is None


def test_rejected_key_stops_lookups(turkey, settings, monkeypatch):
    enable(settings, monkeypatch)
    http = FakeHttp([FakeResponse(403, {"error": {"code": "API_KEY_INVALID"}})])
    stats = sam_gov.enrich_leads(make_leads(turkey, settings), Database(":memory:"), settings, http)
    assert stats["lookups"] == 1 and "key" in stats["stopped"] and len(http.calls) == 1


def test_per_run_cap(turkey, settings, monkeypatch):
    enable(settings, monkeypatch)
    monkeypatch.setitem(settings.data["plugins"]["sam_gov"], "max_companies_per_run", 1)
    http = FakeHttp([FakeResponse(200, payload())])
    stats = sam_gov.enrich_leads(make_leads(turkey, settings), Database(":memory:"), settings, http)
    assert stats["lookups"] == 1 and len(http.calls) == 1


def test_cached_miss_is_retried_after_miss_retry_days(turkey, settings, monkeypatch):
    enable(settings, monkeypatch)
    db = Database(":memory:")
    sam_gov.enrich_leads(make_leads(turkey, settings), db, settings, FakeHttp([FakeResponse(200, payload()), FakeResponse(200, payload())]))
    assert sam_gov.enrich_leads(make_leads(turkey, settings), db, settings, FakeHttp([]))["lookups"] == 0
    db.execute("UPDATE company_domains SET updated_at = '2020-01-01T00:00:00+00:00'")
    again = make_leads(turkey, settings)
    stats = sam_gov.enrich_leads(again, db, settings, FakeHttp([FakeResponse(200, payload(entity())), FakeResponse(200, payload())]))
    assert stats["lookups"] == 2 and again[0].domain == "cargill.com"


def test_network_errors_are_swallowed_and_never_log_the_key(turkey, settings, monkeypatch, caplog):
    enable(settings, monkeypatch, key="SECRET-KEY-123")

    class Boom(FakeHttp):
        def get(self, url, params=None, timeout=None, headers=None):
            self.calls.append((url, params))
            raise requests.ConnectionError(f"boom {url}?api_key={params['api_key']}")

    leads = make_leads(turkey, settings)
    with caplog.at_level(logging.WARNING, logger="award_to_lane.sam_gov"):
        stats = sam_gov.enrich_leads(leads, Database(":memory:"), settings, Boom([]))
    assert stats["errors"] == 2 and stats["stopped"] is None and leads[0].domain is None
    assert "SECRET-KEY-123" not in caplog.text and "ConnectionError" in caplog.text


def test_apollo_only_fills_the_gaps(turkey, settings, monkeypatch):
    """SAM.gov found Cargill (site + POCs) and VPG's POCs but no site; Apollo then looks up only VPG and appends to it."""
    enable(settings, monkeypatch)
    db = Database(":memory:")
    vpg_entity = entity(legal="VIRGINIA POULTRY GROWERS COOPERATIVE, INC.", state="VA", url="")
    leads = make_leads(turkey, settings)
    sam_gov.enrich_leads(leads, db, settings, FakeHttp([FakeResponse(200, payload(entity())), FakeResponse(200, payload(vpg_entity))]))
    assert leads[1].domain is None and len(leads[1].contacts) == 2
    monkeypatch.setitem(settings.data["plugins"]["apollo"], "enabled", True)
    monkeypatch.setenv("APOLLO_API_KEY", "test-key")
    asked = []
    monkeypatch.setattr(apollo.ApolloClient, "find_domain", lambda self, company, state=None: asked.append(company) or "vpgrowers.com")
    monkeypatch.setattr(apollo.ApolloClient, "find_contacts",
                        lambda self, domain, titles, limit=5: [{"name": "Lee Broker", "title": "Logistics Manager", "email": "lee@vpgrowers.com"}])
    stats = apollo.enrich_leads(leads, db, settings, http=object())
    assert asked == [leads[1].company] and stats["lookups"] == 1
    assert leads[0].domain == "cargill.com" and [c["source"] for c in leads[0].contacts] == [SOURCE, SOURCE]
    assert leads[1].domain == "vpgrowers.com"
    assert [c["source"] for c in leads[1].contacts] == [SOURCE_POSSIBLE, SOURCE_POSSIBLE, "apollo"]
    assert leads[1].contacts[-1]["email"] == "lee@vpgrowers.com"
