"""State machine for the PURCHASE DETAILS table of the SAP 'PCA' report (layout family A).

Rows are recognised by column position, never by regex on the flattened text. A destination may wrap
onto one or two following lines (4,196 of 6,506 sample rows did) and the price is usually on the line
after the quantity. Anything the machine does not recognise is kept in award.unparsed_lines, and
quantities are reconciled against the report's own Subtotal / Contract Total / summary TOTAL lines.
"""
from __future__ import annotations

import re

from ...models import Award, Contract, LineItem
from .pdf_layout import Line

CONTRACT_RE = re.compile(r"^\d{2}-\d[A-Z0-9]{3}-\d{2}-[A-Z]-\d{4}$")
INV_RE = re.compile(r"^\d{10}$")
QTY_RE = re.compile(r"^-?[\d,]*\d\.\d{3}$")
MONEY_RE = re.compile(r"^[\d,]*\d\.\d{2,4}$")
DOLLARS_RE = re.compile(r"^[\d,]*\d\.\d{2}$")
BID_RE = re.compile(r"^\d{1,6}$")
PAGE_RE = re.compile(r"^Page \d+ / \d+$")
STREET_RE = re.compile(r"^(\d+\s|P\.?\s?O\.?\s?BOX|PO BOX|P O BOX|ONE\s|HWY|HIGHWAY|RR\s|ROUTE|RTE\b|SUITE|STE\b|C/O)", re.I)
CITY_STATE_RE = re.compile(r",\s*[A-Z]{2}(?:\s+\d{5}(?:-\d{4})?)?\s*$")
CITY_STATE_ZIP_RE = re.compile(r"([A-Za-z][A-Za-z .'()-]*?),?\s+([A-Z]{2})\s+(\d{5}(?:-\d{4})?)\s*$")
CITY_LINE_RE = re.compile(r"^(.*?),\s*([A-Z]{2})(?:\s+(\d{5}(?:-\d{4})?))?\s*$")
ZIP_RE = re.compile(r"^\d{5}(?:-\d{4})?$")


def to_number(text: str) -> float | None:
    try:
        return float(text.replace(",", "").replace("$", ""))
    except ValueError:
        return None


def split_city_state(raw: str) -> tuple[str | None, str | None]:
    s = re.sub(r"\s+", " ", raw).strip(" ,")
    s = re.sub(r"(,\s*([A-Z]{2}))(?:\s*,\s*\2)+$", r"\1", s)      # "Louisville, KY, KY" -> "Louisville, KY"
    m = re.match(r"^(.*?)[\s,]*\b([A-Z]{2})$", s)
    if m and m.group(1).strip(" ,"):
        city = m.group(1).strip(" ,")
        if "," in city:                                               # "7401 Trade Port Drive, Louisville" -> "Louisville"
            city = city.rsplit(",", 1)[-1].strip() or city
        return city, m.group(2)
    return (s or None), None


def contractor_from_lines(lines: list[str]) -> dict:
    business = None
    body: list[str] = []
    for line in lines:
        if line.upper().startswith("BUSINESS TYPE"):
            business = line.split(":", 1)[1].strip() if ":" in line else line
        else:
            body.append(line)
    name_lines: list[str] = []
    addr_lines: list[str] = []
    for line in body:
        if not addr_lines and not STREET_RE.match(line) and not CITY_STATE_RE.search(line):
            name_lines.append(line)
        else:
            addr_lines.append(line)
    if not name_lines and body:
        name_lines, addr_lines = body[:1], body[1:]
    address = " ".join(addr_lines)
    city = state = zip_code = None
    for i, line in enumerate(addr_lines):
        m = CITY_LINE_RE.match(line)
        if m:
            city, state, zip_code = m.group(1).strip(" ,"), m.group(2), m.group(3)
            if zip_code is None and i + 1 < len(addr_lines) and ZIP_RE.match(addr_lines[i + 1]):
                zip_code = addr_lines[i + 1]
    if city is None:
        m = CITY_STATE_ZIP_RE.search(address)
        if m:
            city, state, zip_code = m.group(1).split(",")[-1].strip(), m.group(2), m.group(3)
    return {"contractor_name": " ".join(name_lines).strip() or "UNKNOWN CONTRACTOR", "contractor_address": address,
            "contractor_city": city, "contractor_state": state, "contractor_zip": zip_code, "business_types": business}


class DetailsParser:
    def __init__(self, award: Award):
        self.award = award
        self.mode = "pre"
        self.pending_block: list[str] = []
        self.last_block: dict | None = None
        self.contract: Contract | None = None
        self.material: str | None = None
        self.group: list[LineItem] = []
        self.item: LineItem | None = None
        self.pending_totals: list[tuple[str, str | None, float | None]] = []   # label seen, quantity not yet
        self.orphan_qtys: list[tuple[float, str | None, float | None]] = []       # quantity seen, label not yet
        self.contract_fresh = False
        self.summary_total_pending = False
        self.sap_blanket = False

    # -- helpers -----------------------------------------------------------------
    def warn(self, msg: str) -> None:
        self.award.warnings.append(msg)

    def close_item(self) -> None:
        if self.item is not None:
            city, state = split_city_state(self.item.dest_raw)
            self.item.dest_city, self.item.dest_state = city, state
            if state is None:
                self.item.flags.append("dest_state_missing")
            if self.item.price is None:
                self.item.flags.append("price_missing")
            self.item = None

    def money_in(self, line: Line, col: str) -> float | None:
        for w in line.in_col(col):
            if MONEY_RE.match(w.text):
                return to_number(w.text)
        return None

    def qty_in(self, line: Line) -> float | None:
        for w in line.in_col("qty"):
            if QTY_RE.match(w.text):
                return to_number(w.text)
        return None

    def apply_total(self, kind: str, qty: float | None, uom: str | None, dollars: float | None) -> None:
        if kind == "subtotal":
            got = sum(li.qty or 0 for li in self.group)
            if qty is not None and abs(got - qty) > 0.01:
                self.warn(f"subtotal mismatch for {self.material!r}: rows sum {got:,.3f} vs subtotal {qty:,.3f}")
                for li in self.group:
                    li.flags.append("subtotal_mismatch")
            self.group = []
        elif kind == "contract" and self.contract is not None:
            self.contract.total_qty, self.contract.total_uom, self.contract.total_dollars = qty, uom, dollars
            got = sum(li.qty or 0 for li in self.contract.line_items)
            if qty is not None and abs(got - qty) > 0.01:
                self.warn(f"contract total mismatch {self.contract.contract_no}: rows sum {got:,.3f} vs {qty:,.3f}")
                self.contract.flags.append("contract_total_mismatch")
            if not self.contract.line_items:
                self.contract.flags.append("no_line_items")
        elif kind == "contractor":
            self.pending_block = []

    def start_total(self, kind: str, line: Line) -> None:
        self.close_item()
        qty = self.qty_in(line)
        uom = line.col_text("uom") or None
        dollars = self.money_in(line, "price")
        if qty is not None:
            self.apply_total(kind, qty, uom, dollars)
        elif self.orphan_qtys:                    # the number was printed before its label (page break)
            oq, ouom, odollars = self.orphan_qtys.pop(0)
            self.apply_total(kind, oq, uom or ouom, dollars if dollars is not None else odollars)
        else:
            self.pending_totals.append((kind, uom, dollars))   # the number follows on a later line

    def lone_quantity(self, line: Line) -> bool:
        """A line holding only a quantity (+ UOM, $ amount): the other half of a wrapped total line."""
        qty = self.qty_in(line)
        if qty is None or not all(w.x0 >= 340 for w in line.words):
            return False
        uom = line.col_text("uom") or None
        dollars = self.money_in(line, "price")
        if self.pending_totals:
            kind, puom, pdollars = self.pending_totals.pop(0)
            self.apply_total(kind, qty, puom or uom, pdollars if pdollars is not None else dollars)
        else:
            self.orphan_qtys.append((qty, uom, dollars))
        return True

    def read_summary_total(self, line: Line) -> None:
        toks = [w.text for w in line.words[1:]]
        for i, t in enumerate(toks):
            if QTY_RE.match(t):
                self.award.total_qty = to_number(t)
                if i + 1 < len(toks) and re.fullmatch(r"[A-Z]{2,4}", toks[i + 1]):
                    self.award.total_uom = toks[i + 1]
                break
        money = [t for t in toks if DOLLARS_RE.match(t)]
        self.award.total_dollars = to_number(money[-1]) if money else None
        self.summary_total_pending = self.award.total_dollars is None

    # -- main loop ---------------------------------------------------------------
    def feed(self, line: Line) -> None:
        text = line.text
        if PAGE_RE.match(text) or text.startswith("Contractor/ Material Plant") or text.startswith("Contract Number Location Item"):
            return
        if self.mode == "pre":
            if text.startswith("PURCHASE SUMMARY"):
                self.mode = "summary"
            elif text.startswith("PURCHASE DETAILS"):
                self.mode = "details"
            return
        if self.mode == "summary":
            if text.startswith("PURCHASE DETAILS"):
                self.mode = "details"
            elif text.startswith("Material Minimum Maximum"):
                self.sap_blanket = True
                self.award.is_blanket = True
            elif line.first.text == "TOTAL" and line.first.x0 < 60:
                self.read_summary_total(line)
                if self.sap_blanket:
                    self.award.total_dollars = None   # the summary shows a minimum value, not an award value
            elif self.summary_total_pending:
                money = [w.text for w in line.words if DOLLARS_RE.match(w.text)]
                if money and all(w.x0 >= 340 for w in line.words):
                    self.award.total_dollars = to_number(money[-1])
                self.summary_total_pending = False
            return
        self.feed_details(line)

    def feed_details(self, line: Line) -> None:
        first = line.first
        text = line.text
        if self.item is None and self.lone_quantity(line):
            return
        if CONTRACT_RE.match(first.text) and first.x0 < 60:
            self.close_item()
            if self.group:
                self.warn(f"p{line.page}: new contract {first.text} before subtotal; {len(self.group)} rows unreconciled")
                self.group = []
            block = contractor_from_lines(self.pending_block) if self.pending_block else self.last_block
            self.pending_block = []
            if block is None:
                block = {"contractor_name": "UNKNOWN CONTRACTOR", "contractor_address": ""}
                self.warn(f"p{line.page}: contract {first.text} has no contractor block")
            self.last_block = block
            plant_city, plant_state = split_city_state(" ".join(w.text for w in line.words[1:] if w.x0 >= 186))
            self.contract = Contract(contract_no=first.text, plant_city=plant_city, plant_state=plant_state, **block)
            self.award.contracts.append(self.contract)
            self.material = None
            self.contract_fresh = True
            return
        if INV_RE.match(first.text) and first.x0 < 60 and self.contract is not None:
            self.contract.inv_no = first.text
            return
        if BID_RE.match(first.text) and 243 <= first.x0 < 287 and self.qty_in(line) is not None and self.contract is not None:
            self.close_item()
            self.contract_fresh = False
            self.item = LineItem(bid_item=first.text, material=self.material, dest_raw=line.col_text("dest"), dest_city=None,
                                 dest_state=None, qty=self.qty_in(line), uom=line.col_text("uom") or None,
                                 price=self.money_in(line, "price"), page=line.page)
            if self.material is None:
                self.item.flags.append("material_missing")
            self.contract.line_items.append(self.item)
            self.group.append(self.item)
            return
        if first.text == "Subtotal" and 186 <= first.x0 < 243:
            self.start_total("subtotal", line)
            return
        if first.text in ("Contract", "Contractor") and len(line.words) > 1 and line.words[1].text == "Total" and first.x0 < 186:
            self.start_total("contract" if first.text == "Contract" else "contractor", line)
            return
        if self.item is not None and self.qty_in(line) is not None and all(w.x0 >= 340 for w in line.words):
            self.close_item()
            if self.lone_quantity(line):
                return
        if self.item is not None and all(w.x0 >= 287 for w in line.words) and self.qty_in(line) is None:
            more = line.col_text("dest")
            if more:
                self.item.dest_raw = f"{self.item.dest_raw} {more}".strip()
            price = self.money_in(line, "price")
            if price is not None:
                self.item.price = price
            return
        if self.contract_fresh and self.contract is not None and all(186 <= w.x0 < 243 for w in line.words):
            extra_city, extra_state = split_city_state(f"{self.contract.plant_city or ''} {text}".strip())
            self.contract.plant_city, self.contract.plant_state = extra_city, extra_state or self.contract.plant_state
            return
        if 127 <= first.x0 < 186 and self.contract is not None:
            self.close_item()
            if self.material and not self.group and line.first.x0 < 140 and len(self.material) > 25 and len(text) < 25:
                self.material = f"{self.material} {text}"
            else:
                self.material = text
            self.group = []
            return
        if first.x0 < 60:
            self.close_item()
            self.pending_block.append(text)
            return
        self.award.unparsed_lines.append(f"p{line.page}: {text}")

    def finish(self) -> None:
        self.close_item()
        if self.pending_block and not any(CITY_STATE_RE.search(l) for l in self.pending_block):
            pass
        for c in self.award.contracts:
            if c.total_qty is None:
                c.flags.append("contract_total_missing")
                c.total_qty = sum(li.qty or 0 for li in c.line_items) or None
                c.total_uom = next((li.uom for li in c.line_items if li.uom), None)
        rows = sum(len(c.line_items) for c in self.award.contracts)
        if self.award.total_qty is not None and rows:
            got = sum(li.qty or 0 for c in self.award.contracts for li in c.line_items)
            if abs(got - self.award.total_qty) > 0.01:
                self.warn(f"award total mismatch: rows sum {got:,.3f} vs summary TOTAL {self.award.total_qty:,.3f}")
        for kind, _, _ in self.pending_totals:
            self.warn(f"{kind} total line never received its quantity")
        if self.sap_blanket:
            for c in self.award.contracts:
                c.flags.append("blanket_no_destinations")
            self.award.unparsed_lines = []
            self.warn("blanket award (min/max quantities per contractor, destinations not listed)")
        elif self.award.unparsed_lines:
            self.warn(f"{len(self.award.unparsed_lines)} unparsed detail lines")


def parse_details(pages: list[list[Line]], award: Award) -> None:
    parser = DetailsParser(award)
    for lines in pages:
        for line in lines:
            parser.feed(line)
    parser.finish()
