"""apollo_import.csv: unique companies on the call sheet with domains where known, for bulk import."""
from __future__ import annotations

import csv
from pathlib import Path

from ..geo import norm_key
from ..models import Lead


def company_key(name: str) -> str:
    return norm_key(name)[:80]


def write_apollo_import(leads: list[Lead], out_dir: Path) -> Path:
    rows: dict[str, dict] = {}
    for lead in leads:
        key = company_key(lead.company)
        row = rows.setdefault(key, {"Company": lead.company, "Website": lead.domain or "", "City": lead.company_city or "",
                                    "State": lead.company_state or "", "Awards": set(), "Commodities": set(), "Truckloads": 0,
                                    "New": "NEW" if lead.is_new else "", "Fleet": lead.fleet_status or "",
                                    "USDOT": lead.usdot or "", "Power units": lead.power_units if lead.power_units is not None else ""})
        row["Awards"].add(lead.award_key)
        row["Commodities"].add(lead.commodity)
        row["Truckloads"] += lead.truckloads
        if lead.domain and not row["Website"]:
            row["Website"] = lead.domain
        if lead.is_new:
            row["New"] = "NEW"
        if lead.fleet_status and not row["Fleet"]:
            row["Fleet"], row["USDOT"] = lead.fleet_status, lead.usdot or ""
            row["Power units"] = lead.power_units if lead.power_units is not None else ""
    path = out_dir / "apollo_import.csv"
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Company", "Website", "City", "State", "Awards", "Commodities", "Truckloads", "New", "Fleet", "USDOT", "Power units"])
        for row in sorted(rows.values(), key=lambda r: (-r["Truckloads"], r["Company"])):
            w.writerow([row["Company"], row["Website"], row["City"], row["State"], "; ".join(sorted(row["Awards"])),
                        "; ".join(sorted(row["Commodities"]))[:200], row["Truckloads"], row["New"], row["Fleet"], row["USDOT"],
                        row["Power units"]])
    return path
