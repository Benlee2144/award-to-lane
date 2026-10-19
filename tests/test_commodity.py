import pytest

from award_to_lane.commodity import case_weight_lb, equipment_for, pounds_for


@pytest.mark.parametrize("material,subcat,expected", [
    ("TURKEY ROASTS FRZ CTN-32-48 LB", "POULTRY/EGGS (TURKEY FROZEN)", "reefer"),
    ("PEAS CAN-6/10", "VEGETABLES (CARROTS CANNED)", "dry van"),
    ("CHEESE MOZ LM PT SKM STRING BOX-360/1 OZ", "CHEESE (MOZZARELLA)", "reefer"),
    ("EGG NOODLE BOX", "PASTA (EGG NOODLE BOX)", "dry van"),
    ("MILK 1% MILKFAT UHT 1500 BOX-12/32 FL OZ", "MILK (UHT BOX)", "dry van"),
    ("BUTTERY SPREAD TUB-30 LB", "BUTTER (SALTED CARTON)", "reefer"),
    ("ORANGES FRESH CTN-40 LB", "FRUIT (ORANGE FRESH)", "reefer"),
    ("OIL, VEGETABLE BOTTLE, PLS-6/4L", "OIL (VEGETABLE BOTTLE)", "dry van"),
    ("BEANS GREEN FRZ CTN-30 LB", "VEGETABLES (BEANS GREEN CANNED, BEANS GREEN FROZEN)", "reefer"),
    ("BEANS GREEN CAN-24/300", "VEGETABLES (BEANS GREEN CANNED, BEANS GREEN FROZEN)", "dry van"),
    (None, "POULTRY/EGGS (CHICKEN CHILLED)", "reefer"),
])
def test_equipment_rules(settings, material, subcat, expected):
    assert equipment_for(material, subcat, settings) == expected


def test_case_weights():
    assert case_weight_lb("PKG 8/5 LB", 30) == (40.0, False)
    assert case_weight_lb("CTN-30 LB", 30) == (30.0, False)
    assert case_weight_lb("APRICOT FRZ CUP-96/4.5 OZ", 30) == (27.0, False)
    assert case_weight_lb("BAG-50 KG", 30)[0] == pytest.approx(110.23, abs=0.01)
    lb, est = case_weight_lb("PEAS CAN-6/10", 30)
    assert est and lb == pytest.approx(39.6)
    assert case_weight_lb("MYSTERY", 30) == (30.0, True)


def test_pounds_for(settings):
    assert pounds_for(38400, "LB", "", settings) == (38400.0, False)
    assert pounds_for(60, "MT", "", settings)[0] == pytest.approx(132277.2)
    lb, est = pounds_for(912, "CS", "PEAS CAN-6/10", settings)
    assert est and lb == pytest.approx(36115.2)
    assert pounds_for(0, "CS", "x", settings) == (0.0, False)
    assert pounds_for(1000, "GAL", "MILK 1% GAL", settings) == (8600.0, True)
    assert pounds_for(100, "DZ", "SHELL EGGS", settings) == (150.0, True)
