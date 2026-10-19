"""Commodity-derived facts: equipment type and pounds per unit.

Equipment rules live in config.yaml (equipment.rules) and are matched in order against the line item's
material text first and the portal's commodity subcategory second. Case weights are read from the pack
size embedded in the material text ("CTN-30 LB", "PKG 8/5 LB", "CUP-96/4.5 OZ", "CAN-6/10").
"""
from __future__ import annotations

import re

LB_PER_KG = 2.20462
LB_PER_MT = 2204.62
CAN_SIZE_LB = {"10": 6.6, "300": 0.95, "303": 1.0, "2.5": 1.8, "401": 1.0, "211": 0.5, "8": 0.5}


def equipment_for(material: str | None, subcat: str | None, settings) -> str:
    rules = settings.get("equipment.rules", []) or []
    default = settings.get("equipment.default", "dry van")
    for text in (material or "", subcat or ""):
        if not text:
            continue
        hay = f" {text.upper()} "
        for rule in rules:
            for kw in rule.get("keywords", []):
                if str(kw).upper() in hay:
                    return str(rule.get("equipment", default))
    return str(default)


def case_weight_lb(material: str | None, default_lb: float) -> tuple[float, bool]:
    """Return (pounds per case, estimated?) from the pack size in a material description."""
    text = (material or "").upper().replace("LBS", "LB")
    m = re.search(r"(\d+)\s*/\s*(\d+(?:\.\d+)?)\s*LB\b", text)
    if m:
        return int(m.group(1)) * float(m.group(2)), False
    m = re.search(r"(\d+)\s*/\s*(\d+(?:\.\d+)?)\s*(?:FL\s*)?OZ\b", text)
    if m:
        return int(m.group(1)) * float(m.group(2)) / 16.0, False
    m = re.search(r"(\d+)\s*/\s*(\d+(?:\.\d+)?)\s*GAL\b", text)
    if m:
        return int(m.group(1)) * float(m.group(2)) * 8.6, True
    m = re.search(r"(\d+)\s*-\s*(\d+)\s*LB\b", text)
    if m:
        return (int(m.group(1)) + int(m.group(2))) / 2.0, True
    m = re.search(r"(?:CTN|BOX|BAG|PKG|CASE|CS|CARTON|TUB|PAIL|DRUM|BULK|TOTE)\s*-?\s*(\d+(?:\.\d+)?)\s*LB\b", text)
    if m:
        return float(m.group(1)), False
    m = re.search(r"(\d+(?:\.\d+)?)\s*KG\b", text)
    if m:
        return float(m.group(1)) * LB_PER_KG, False
    m = re.search(r"(\d+)\s*DZ\b", text)
    if m:
        return int(m.group(1)) * 1.5, True
    m = re.search(r"(\d+)\s*/\s*(\d+(?:\.\d)?)\b", text)
    if m and m.group(2) in CAN_SIZE_LB:
        return int(m.group(1)) * CAN_SIZE_LB[m.group(2)], True
    m = re.search(r"\b(\d+(?:\.\d+)?)\s*LB\b", text)
    if m:
        return float(m.group(1)), True
    return float(default_lb), True


def pounds_for(qty: float | None, uom: str | None, material: str | None, settings) -> tuple[float, bool]:
    """Convert a quantity in the PDF's unit of measure to pounds. Returns (pounds, estimated?)."""
    if not qty:
        return 0.0, False
    unit = (uom or "").upper().strip()
    if unit in ("LB", "LBS", "POUND", "POUNDS"):
        return float(qty), False
    if unit == "MT":
        return float(qty) * LB_PER_MT, False
    if unit == "KG":
        return float(qty) * LB_PER_KG, False
    if unit in ("GAL", "GALLON", "GALLONS"):
        return float(qty) * 8.6, True          # fluid milk / juice: ~8.6 lb per gallon
    if unit in ("DZ", "DOZ", "DOZEN"):
        return float(qty) * 1.5, True          # shell eggs: ~24 oz per dozen
    default_case = float(settings.get("lanes.default_case_weight_lb", 30))
    per_case, estimated = case_weight_lb(material, default_case)
    return float(qty) * per_case, True if unit not in ("CS", "CASE", "CASES") else estimated
