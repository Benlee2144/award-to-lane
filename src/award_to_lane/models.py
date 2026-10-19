"""Data model shared by every lead source, the lane builder, the scorer and the reports."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

# Lead.fleet_status values set by the FMCSA plug-in (contacts/fmcsa.py). Only FLEET_PRIVATE is asserted strongly enough
# to carry the `private_fleet` flag and the scoring penalty; FLEET_UNVERIFIED means a plausible but unconfirmed match.
FLEET_PRIVATE = "private fleet"
FLEET_FOR_HIRE = "for-hire carrier"
FLEET_NONE = "registered, no trucks"
FLEET_UNVERIFIED = "unverified"
FLEET_NOT_FOUND = "no fleet on record"
FLAG_PRIVATE_FLEET = "private_fleet"


@dataclass
class LineItem:
    bid_item: str | None
    material: str | None
    dest_raw: str
    dest_city: str | None
    dest_state: str | None
    qty: float | None
    uom: str | None
    price: float | None
    page: int
    flags: list[str] = field(default_factory=list)


@dataclass
class Contract:
    contract_no: str | None
    contractor_name: str
    contractor_address: str = ""
    contractor_city: str | None = None
    contractor_state: str | None = None
    contractor_zip: str | None = None
    business_types: str | None = None
    plant_city: str | None = None
    plant_state: str | None = None
    inv_no: str | None = None
    total_qty: float | None = None
    total_uom: str | None = None
    total_dollars: float | None = None
    line_items: list[LineItem] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


@dataclass
class Award:
    award_key: str
    source_file: str
    family: str = "unknown"          # sap_details | blanket | generic | scanned
    sol_no: str | None = None        # WBSCM solicitation number (10 digits)
    pii: str | None = None           # procurement instrument identifier, e.g. 12-3J14-26-B-0410
    commodity: str | None = None
    commodity_title: str | None = None
    award_date: date | None = None
    delivery_start: date | None = None
    delivery_end: date | None = None
    num_offers: int | None = None
    total_qty: float | None = None
    total_uom: str | None = None
    total_dollars: float | None = None
    is_international: bool = False
    is_blanket: bool = False
    is_cancelled: bool = False
    contracts: list[Contract] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unparsed_lines: list[str] = field(default_factory=list)
    parse_status: str = "ok"         # ok | warnings | failed

    def needs_review(self) -> bool:
        return self.parse_status != "ok" or self.family in ("generic", "scanned")


@dataclass
class RateQuote:
    low: float | None
    high: float | None
    unit: str
    source: str
    note: str = ""


@dataclass
class Lane:
    origin_city: str | None
    origin_state: str | None
    dest_city: str | None
    dest_state: str | None
    dest_display: str
    drops: int
    qty: float
    uom: str
    pounds: float
    pounds_estimated: bool
    truckloads: int
    equipment: str
    miles: float | None
    flags: list[str] = field(default_factory=list)
    rate: RateQuote | None = None


@dataclass
class Lead:
    source: str
    award_key: str
    contract_no: str | None
    company: str
    company_city: str | None
    company_state: str | None
    plant_city: str | None
    plant_state: str | None
    commodity: str
    equipment: str
    award_date: date | None
    posted_date: date | None
    delivery_start: date | None
    delivery_end: date | None
    days_until_open: int | None
    days_until_close: int | None
    num_offers: int | None
    total_qty: float
    total_uom: str
    total_pounds: float
    truckloads: int
    dollars: float | None
    destinations: int
    lanes: list[Lane]
    is_new: bool
    is_international: bool
    is_blanket: bool
    flags: list[str]
    pdf_url: str | None
    score: float = 0.0
    domain: str | None = None
    contacts: list[dict] = field(default_factory=list)
    fleet_status: str | None = None     # FMCSA plug-in: one of the FLEET_* values above, None when the plug-in is off
    usdot: str | None = None            # USDOT number of the matched (or candidate) census record
    power_units: int | None = None      # power units on that record's latest MCS-150
    fleet_note: str | None = None       # one line of detail for the sheet: legal name, city, classification, MC number
    status: str = "new"                 # what a rep did with the lead (lead_status.STATUSES); set by lead_status.attach
    status_note: str | None = None      # free text from the rep, shown on hover on the sheet
    status_rep: str | None = None       # who recorded the status
    status_updated_at: str | None = None
