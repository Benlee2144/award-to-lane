from datetime import date

from award_to_lane.sources.usda_pca import parse_narrative as nar

WINDOW_VARIANTS = {
    "Deliveries are to be made fromN ovember 1, 2026 through November 15, 2026.": (date(2026, 11, 1), date(2026, 11, 15)),
    "Deliveries are to be made from November 1, 2026 - December 31, 2026.": (date(2026, 11, 1), date(2026, 12, 31)),
    "Deliveries are to be made between November 1,2026 # December 31,2026.": (date(2026, 11, 1), date(2026, 12, 31)),
    "Deliveries are to be made between November 1, 2026, and May 31, 2027.": (date(2026, 11, 1), date(2027, 5, 31)),
    "Deliveries are to be made between November 1, 2026, through December 31, 2026.": (date(2026, 11, 1), date(2026, 12, 31)),
    "Programs.D eliveries are to be made from October 1, 2026, through December 31, 2026.": (date(2026, 10, 1), date(2026, 12, 31)),
    "Deliveries are to be made from: December 01, 2026 through April 30, 2027": (date(2026, 12, 1), date(2027, 4, 30)),
    "with deliveries scheduled from October 1, 2026, through June 30, 2027.": (date(2026, 10, 1), date(2027, 6, 30)),
    "Deliveries are to be made between April 1, 2026 – June 30, 2026.": (date(2026, 4, 1), date(2026, 6, 30)),
    "Delivery period: November 1 - December 31, 2026": (date(2026, 11, 1), date(2026, 12, 31)),
}


def test_delivery_window_variants():
    for text, expected in WINDOW_VARIANTS.items():
        assert nar.find_delivery_window(text) == expected, text


def test_no_window_for_export_awards():
    assert nar.find_delivery_window("for shipment during the period(s) indicated in the solicitation.") == (None, None)


def test_offers_pii_wbscm_date():
    text = "PCA : 12-3J14-26-B-0410\nSeptember 16, 2026 12:30:00\nPURCHASE AWARD DESCRIPTION\nTURKEY & TURKEY PRODUCTS, SOLICITATION 12-3J14-26-B-0410 WBSCM NO: 2000011515\nNumber of Offers/Quotes(Vendor Responses) received: 3"
    assert nar.find_pii(text) == "12-3J14-26-B-0410"
    assert nar.find_wbscm_no(text) == "2000011515"
    assert nar.find_num_offers(text) == 3
    assert nar.find_award_date(text) == date(2026, 9, 16)
    assert nar.find_commodity_title(text) == "TURKEY & TURKEY PRODUCTS"


def test_commodity_title_from_purchased_sentence():
    text = "PURCHASE AWARD DESCRIPTION\nThe Department of Agriculture (USDA) purchased Canned Vegetables as indicated below for distribution"
    assert nar.find_commodity_title(text) == "Canned Vegetables"


def test_boilerplate_title_is_dropped():
    text = "PURCHASE AWARD DESCRIPTION\nThe USDA/Agricultural Marketing Service/Commodity Procurement Program (CPP) hereby announces the award"
    assert nar.find_commodity_title(text) is None


def test_international_and_cancelled_detection():
    assert nar.looks_international("export distribution", "12-1NFP-26-B-0022", r"^\d\d-\dN")
    assert nar.looks_international("The products listed below are for the FAS McGovern-Dole program", None, None)
    assert not nar.looks_international("Child Nutrition programs", "12-3J14-26-B-0410", r"^\d\d-\dN")
    assert nar.looks_cancelled("", "12-3J14-26-B-0283-PCA Cancellation.pdf")
    assert not nar.looks_cancelled("hereby announces the award", "x-PCA.pdf")
