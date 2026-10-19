from datetime import date

from award_to_lane.sources.usda_pca.parse_details import contractor_from_lines, split_city_state


def test_turkey_header(turkey):
    assert turkey.family == "sap_details"
    assert turkey.parse_status == "ok"
    assert turkey.pii == "12-3J14-26-B-0410"
    assert turkey.sol_no == "2000011515"
    assert turkey.award_date == date(2026, 9, 16)
    assert (turkey.delivery_start, turkey.delivery_end) == (date(2026, 11, 1), date(2026, 11, 15))
    assert turkey.num_offers == 3
    assert turkey.total_qty == 233600.0 and turkey.total_uom == "LB" and turkey.total_dollars == 919328.0
    assert turkey.commodity == "POULTRY/EGGS (TURKEY FROZEN)"
    assert not turkey.unparsed_lines and not turkey.warnings


def test_turkey_contracts_and_rows(turkey):
    assert [c.contract_no for c in turkey.contracts] == ["12-3J14-26-C-1952", "12-3J14-26-C-1953"]
    cargill, vpg = turkey.contracts
    assert cargill.contractor_name == "CARGILL MEAT SOLUTIONS CORPORATION"
    assert (cargill.contractor_city, cargill.contractor_state, cargill.contractor_zip) == ("WICHITA", "KS", "67202-1487")
    assert (cargill.plant_city, cargill.plant_state) == ("Nebraska City", "NE")
    assert cargill.inv_no == "4100035012"
    assert cargill.total_qty == 153600.0 and cargill.total_dollars == 659328.0
    rows = [(li.bid_item, li.dest_city, li.dest_state, li.qty, li.uom, li.price) for li in cargill.line_items]
    assert rows == [("40", "CLANTON", "AL", 38400.0, "LB", 4.39), ("60", "VAN BUREN", "AR", 38400.0, "LB", 4.29),
                    ("80", "OMAHA", "NE", 38400.0, "LB", 3.99), ("100", "MILWAUKIE", "OR", 38400.0, "LB", 4.5)]
    assert all(li.material == "TURKEY BREAST DELI SLICED FRZP KG 8/5 LB" for li in cargill.line_items)
    assert (vpg.plant_city, vpg.plant_state) == ("HINTON", "VA")
    assert [li.qty for li in vpg.line_items] == [40000.0, 27000.0, 13000.0]


def test_wrapped_destinations_in_canned_vegetables(canned_veg):
    lakeside = canned_veg.contracts[0]
    dests = [(li.dest_city, li.dest_state) for li in lakeside.line_items[:8]]
    assert dests == [("LINDEN", "NJ"), ("WAYNE", "NJ"), ("HAUPPAUGE", "NY"), ("HUNTINGDON", "PA"), ("DOUGLAS", "GA"),
                     ("LEESBURG", "GA"), ("POMONA", "CA"), ("SACRAMENTO", "CA")]
    assert lakeside.line_items[2].price == 34.38 and lakeside.line_items[7].price == 39.05
    assert lakeside.line_items[0].uom == "CS"
    assert canned_veg.total_uom == "CS" and canned_veg.total_qty == 102144.0


def test_split_city_state():
    assert split_city_state("CLANTON, AL") == ("CLANTON", "AL")
    assert split_city_state("SACRAMENTO , CA") == ("SACRAMENTO", "CA")
    assert split_city_state("NEW HOLLAND, PA") == ("NEW HOLLAND", "PA")
    assert split_city_state("MURFREESBO RO, TN") == ("MURFREESBO RO", "TN")
    assert split_city_state("BHOU") == ("BHOU", None)
    assert split_city_state("7401 Trade Port Drive, Lousiville, KY, KY") == ("Lousiville", "KY")


def test_contractor_block_variants():
    a = contractor_from_lines(["CARGILL MEAT", "SOLUTIONS CORPORATION", "825 EAST DOUGLAS", "AVENUE", "WICHITA, KS 67202-1487"])
    assert a["contractor_name"] == "CARGILL MEAT SOLUTIONS CORPORATION"
    assert (a["contractor_city"], a["contractor_state"], a["contractor_zip"]) == ("WICHITA", "KS", "67202-1487")
    b = contractor_from_lines(["FOSTER POULTRY FARMS,", "LLC", "PO BOX 198", "LIVINGSTON, CA", "95334-0457"])
    assert b["contractor_name"] == "FOSTER POULTRY FARMS, LLC"
    assert (b["contractor_city"], b["contractor_state"], b["contractor_zip"]) == ("LIVINGSTON", "CA", "95334-0457")
    c = contractor_from_lines(["NEW COAST TRADING,", "(NCT) LLC", "110 Via Villagio", "Hypoluxo, FL 33462", "Business Types : Small, 8(a)"])
    assert c["contractor_name"] == "NEW COAST TRADING, (NCT) LLC"
    assert c["contractor_city"] == "Hypoluxo" and c["business_types"] == "Small, 8(a)"
    d = contractor_from_lines(["CALIFORNIA DAIRIES INC", "CALIFORNIA DAIRIES, INC.", "VISALIA, CA 93291-9358"])
    assert d["contractor_city"] == "VISALIA" and d["contractor_zip"] == "93291-9358"
