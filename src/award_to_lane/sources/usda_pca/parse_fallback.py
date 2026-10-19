"""Fallback parsers for the rare non-SAP award PDFs.

Family B ("blanket"): fiscal-year indefinite-quantity awards. The summary lists Minimum Value / Maximum
Quantity and the details are CLIN tables per contractor with state pricing. There are no destinations.
Family C ("generic"): hand-made Word/Print-to-PDF documents (bulk apples, bulk potatoes). Best effort,
always flagged for manual review. Both work on pdfplumber's layout-faithful page text.
"""
from __future__ import annotations

import re

from ...models import Award, Contract, LineItem
from .parse_details import CITY_LINE_RE, CITY_STATE_ZIP_RE, STREET_RE, contractor_from_lines, split_city_state, to_number
from .parse_narrative import squash

CLIN_ROW_RE = re.compile(r"^(\d{1,3})\s+(\d{6})\s*-\s*(.+)$")
CLIN_HEADER_RE = re.compile(r"^CLIN\b|^(Pricing|Price|Value|Description|State|QTY)\b", re.I)
MONEY_ANY_RE = re.compile(r"\$\s?([\d,]+\.\d{2,4})")
NUMBER_ANY_RE = re.compile(r"(?<![\d,.$])(\d{1,3}(?:,\d{3})+|\d{4,})(?![\d,]*\.\d)")
CITY_ST_ANY_RE = re.compile(r"\b([A-Z][A-Za-z.'-]+(?: [A-Z][A-Za-z.'-]+){0,3}),?\s+([A-Z]{2})\b(?=\s*$|\s+\$|\s+\d)")
COMPANY_RE = re.compile(r"\b(Inc\.?|LLC\.?|L\.L\.C\.|Ltd\.?|Co\.?|Corp\.?|Corporation|Company|Farms?|Foods?|Canning|Cooperative|Coop|Growers|Packing|Products|Enterprises|Industries|Sons)\b", re.I)
CONTINUED_RE = re.compile(r"\(?Continued on Next Page\)?\s*", re.I)


def _summary_total_blanket(text: str, award: Award) -> None:
    m = re.search(r"^TOTAL\s+\$([\d,]+\.\d{2})\s+([\d,]+\.\d{3})\s+([A-Z]{2,3})", text, re.M)
    if m:
        award.total_dollars = to_number(m.group(1))
        award.total_qty = to_number(m.group(2))
        award.total_uom = m.group(3)


def _pick_name(recent: list[str]) -> str | None:
    for line in reversed(recent):
        if STREET_RE.match(line) or CITY_LINE_RE.match(line) or CLIN_HEADER_RE.match(line):
            continue
        if re.search(r"\d{3,}|CAN-|CTN|FRZ|\bLB\b|\bOZ\b", line):
            continue
        return CONTINUED_RE.sub("", line).strip()
    return None


def parse_blanket(pages_text: list[str], award: Award) -> None:
    award.family = "blanket"
    award.is_blanket = True
    full = "\n".join(pages_text)
    _summary_total_blanket(full, award)
    current: Contract | None = None
    recent: list[str] = []
    addr: list[str] = []
    for raw in full.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = re.search(r"ContractNumber:?(\d{10})", squash(line), re.I)
        if m:
            name_part = re.split(r"\bC\s*o\s*n\s*t\s*r\s*a\s*c\s*t\b", line, maxsplit=1, flags=re.I)[0].strip()
            name = CONTINUED_RE.sub("", name_part).strip() or _pick_name(recent) or "UNKNOWN CONTRACTOR"
            current = Contract(contract_no=m.group(1), contractor_name=name, flags=["blanket_no_destinations"])
            award.contracts.append(current)
            addr = [l for l in recent if STREET_RE.match(l) or CITY_LINE_RE.match(l)] if not name_part else []
            recent = []
            if addr:
                _apply_address(current, addr)
            continue
        row = CLIN_ROW_RE.match(line)
        if row is None and current is not None and not current.line_items and (STREET_RE.match(line) or CITY_LINE_RE.match(line)):
            addr.append(line)
            _apply_address(current, addr)
            continue
        if row and current is not None:
            desc = row.group(3)
            dollars = [to_number(x) for x in MONEY_ANY_RE.findall(desc)]
            desc_text = MONEY_ANY_RE.split(desc)[0].strip()
            plain = [to_number(x) for x in NUMBER_ANY_RE.findall(MONEY_ANY_RE.sub(" ", desc))]
            li = LineItem(bid_item=row.group(1), material=f"{row.group(2)} {desc_text}".strip(), dest_raw="", dest_city=None,
                          dest_state=None, qty=plain[-1] if plain else None, uom=award.total_uom or "CS",
                          price=dollars[0] if dollars else None, page=0, flags=["blanket_max_qty", "no_destination"])
            current.line_items.append(li)
            if dollars:
                current.total_dollars = (current.total_dollars or 0) + (dollars[-1] or 0)
            recent = []
            continue
        if CLIN_HEADER_RE.match(line):
            recent = []
            continue
        recent = (recent + [line])[-4:]
    for c in award.contracts:
        c.total_qty = sum(li.qty or 0 for li in c.line_items) or None
        c.total_uom = award.total_uom or "CS"
    if not award.contracts:
        award.warnings.append("blanket award: no contractor blocks recognised")
    award.warnings.append("blanket award: destinations are not listed in the PCA")


def _apply_address(contract: Contract, addr_lines: list[str]) -> None:
    info = contractor_from_lines([contract.contractor_name] + addr_lines)
    contract.contractor_address = info["contractor_address"]
    contract.contractor_city, contract.contractor_state, contract.contractor_zip = (
        info["contractor_city"], info["contractor_state"], info["contractor_zip"])


def parse_generic(pages_text: list[str], award: Award) -> None:
    award.family = "generic"
    award.warnings.append("hand-made PDF layout; review manually")
    lines = [CONTINUED_RE.sub("", l).strip() for l in "\n".join(pages_text).splitlines()]
    lines = [l for l in lines if l]
    current: Contract | None = None
    for i, line in enumerate(lines):
        has_money = bool(MONEY_ANY_RE.search(line))
        if not has_money and (CITY_STATE_ZIP_RE.search(line) or (COMPANY_RE.search(line) and len(line) < 60
                                                                    and not line.lower().startswith(("the ", "deliver", "further")))):
            if CITY_STATE_ZIP_RE.search(line):
                block = [l for l in lines[max(0, i - 3):i + 1] if not MONEY_ANY_RE.search(l) and len(l) < 70
                         and not l.lower().startswith(("the ", "deliver", "awards to", "material"))]
            else:
                block = [line]
            info = contractor_from_lines(block)
            if current is not None and not current.line_items and current.contractor_name in block and not current.contractor_city:
                _apply_address(current, block[block.index(current.contractor_name) + 1:])
                continue
            if current is not None and not current.line_items and current.contractor_name == info["contractor_name"]:
                continue
            current = Contract(flags=["generic_layout"], contract_no=None, **info)
            award.contracts.append(current)
            continue
        if has_money and current is not None:
            dest = CITY_ST_ANY_RE.search(line)
            if not dest:
                continue
            dollars = [to_number(x) for x in MONEY_ANY_RE.findall(line)]
            plain = [to_number(x) for x in NUMBER_ANY_RE.findall(MONEY_ANY_RE.sub(" ", line))]
            city, state = split_city_state(f"{dest.group(1)}, {dest.group(2)}")
            lead = line[:dest.start()]
            li = LineItem(bid_item=None, material=re.split(r"\s\d{6}\s|\$|\d{1,2}/\d{1,2}/\d{4}|[A-Z][a-z]+ \d{1,2}, \d{4}", lead)[0].strip(" -") or None,
                          dest_raw=dest.group(0).strip(), dest_city=city, dest_state=state, qty=max(plain) if plain else None,
                          uom="LB" if plain else None, price=min(dollars) if dollars else None, page=0, flags=["generic_row"])
            current.line_items.append(li)
    for c in award.contracts:
        c.total_qty = sum(li.qty or 0 for li in c.line_items) or None
        c.total_uom = "LB" if c.total_qty else None
    award.contracts = [c for c in award.contracts if c.line_items]
    if not award.contracts:
        award.warnings.append("generic layout: no contractor blocks recognised")
