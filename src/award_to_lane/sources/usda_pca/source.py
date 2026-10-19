"""LeadSource implementation for USDA AMS purchase awards."""
from __future__ import annotations

import json
from datetime import date

from ...db import now_iso
from ...lanes import build_lead, lead_is_active
from ...models import Award, Contract, Lead, LineItem
from ...util import parse_iso_date
from ..base import LeadSource, RunContext
from .discover import SOURCE, discover
from .parse import parse_pdf


class UsdaPcaSource(LeadSource):
    name = SOURCE

    def discover(self, ctx: RunContext, *, force_backfill: bool = False, download: bool = True) -> dict:
        return discover(ctx.db, ctx.http, ctx.settings, ctx.run_date, force_backfill=force_backfill, download=download)

    def parse_pending(self, ctx: RunContext, *, reparse: bool = False) -> dict:
        pii_regex = ctx.settings.get("sources.usda_pca.international_pii_regex")
        stats = {"parsed": 0, "ok": 0, "warnings": 0, "failed": 0}
        for row in ctx.db.documents_to_parse(SOURCE, "PCA", reparse=reparse):
            path = ctx.settings.root / row["local_path"]
            meta = {"award_key": row["award_key"], "pii": row["award_key"], "sol_no": row["sol_no"],
                    "prod_subcat": row["prod_subcat"], "prod_grp": row["prod_grp"]}
            try:
                award = parse_pdf(path, meta=meta, international_pii_regex=pii_regex)
                ctx.db.replace_award(row["id"], SOURCE, award)
                ctx.db.update_document(row["id"], parse_status=award.parse_status, parsed_at=now_iso(),
                                       parse_error="; ".join(award.warnings[:3]) if award.warnings else None)
                stats[award.parse_status if award.parse_status in stats else "warnings"] += 1
                level = ctx.log.info if award.parse_status == "ok" else ctx.log.warning
                level("parsed %s [%s/%s] contracts=%d rows=%d", row["file_name"], award.family, award.parse_status,
                      len(award.contracts), sum(len(c.line_items) for c in award.contracts))
            except Exception as exc:  # never let one PDF stop the run
                ctx.log.exception("parse crashed for %s", row["file_name"])
                ctx.db.update_document(row["id"], parse_status="failed", parsed_at=now_iso(),
                                       parse_error=f"{type(exc).__name__}: {exc}")
                stats["failed"] += 1
            stats["parsed"] += 1
            ctx.db.commit()
        return stats

    def build_leads(self, ctx: RunContext) -> list[Lead]:
        leads: list[Lead] = []
        for row in ctx.db.awards_with_documents(SOURCE):
            award = award_from_row(ctx.db, row)
            posted = parse_iso_date(row["posted_date"])
            first_seen = parse_iso_date(row["first_seen_date"])
            for contract in award.contracts:
                lead = build_lead(award, contract, source=SOURCE, posted_date=posted, first_seen=first_seen,
                                  pdf_url=row["pdf_url"], geocoder=ctx.geocoder, settings=ctx.settings, today=ctx.run_date)
                if lead_is_active(lead, award, ctx.run_date, ctx.settings):
                    leads.append(lead)
        return leads


def award_from_row(db, row) -> Award:
    award = Award(award_key=row["award_key"], source_file=row["file_name"] or "", family=row["family"], sol_no=row["sol_no"],
                  pii=row["pii"], commodity=row["commodity"], commodity_title=row["commodity_title"],
                  award_date=parse_iso_date(row["award_date"]), delivery_start=parse_iso_date(row["delivery_start"]),
                  delivery_end=parse_iso_date(row["delivery_end"]), num_offers=row["num_offers"], total_qty=row["total_qty"],
                  total_uom=row["total_uom"], total_dollars=row["total_dollars"], is_international=bool(row["is_international"]),
                  is_blanket=bool(row["is_blanket"]), is_cancelled=bool(row["is_cancelled"]), parse_status=row["parse_status"],
                  warnings=json.loads(row["warnings_json"] or "[]"))
    for c in db.contracts_for_award(row["id"]):
        contract = Contract(contract_no=c["contract_no"], contractor_name=c["contractor_name"], contractor_address=c["contractor_address"] or "",
                            contractor_city=c["contractor_city"], contractor_state=c["contractor_state"], contractor_zip=c["contractor_zip"],
                            business_types=c["business_types"], plant_city=c["plant_city"], plant_state=c["plant_state"], inv_no=c["inv_no"],
                            total_qty=c["total_qty"], total_uom=c["total_uom"], total_dollars=c["total_dollars"],
                            flags=json.loads(c["flags_json"] or "[]"))
        for li in db.line_items_for_contract(c["id"]):
            contract.line_items.append(LineItem(bid_item=li["bid_item"], material=li["material"], dest_raw=li["dest_raw"] or "",
                                                dest_city=li["dest_city"], dest_state=li["dest_state"], qty=li["qty"], uom=li["uom"],
                                                price=li["price"], page=li["page"] or 0, flags=json.loads(li["flags_json"] or "[]")))
        award.contracts.append(contract)
    return award
