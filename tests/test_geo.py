from award_to_lane.geo import Gazetteer, Geocoder, norm_key, road_miles
from conftest import ROOT


def test_norm_key():
    assert norm_key("MURFREESBO RO") == "MURFREESBORO"
    assert norm_key("Ft. Worth") == "FORTWORTH"
    assert norm_key("St. Louis") == "SAINTLOUIS"
    assert norm_key("N Little Rock") == "NORTHLITTLEROCK"


def test_gazetteer_lookups():
    geo = Geocoder(Gazetteer(ROOT / "data" / "us_places.csv"))
    assert geo.locate("MURFREESBO RO", "TN").name == "Murfreesboro, TN"
    assert geo.locate("NEW HOLLAND", "PA").name == "New Holland, PA"
    assert geo.locate("LAKEWORTH", "FL").name == "Lake Worth Beach, FL"
    assert geo.locate("Nebraska City", "NE").name == "Nebraska City, NE"
    assert geo.locate("BHOU", None) is None
    assert geo.locate("NOWHEREVILLE", "TX") is None
    miles = road_miles(geo.locate("Nebraska City", "NE"), geo.locate("CLANTON", "AL"), 1.2)
    assert 800 < miles < 1000
