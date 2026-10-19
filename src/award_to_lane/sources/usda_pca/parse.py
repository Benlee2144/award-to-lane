"""Parse one award PDF into an Award. Detects the layout family and dispatches.

Never raises for layout problems: the returned Award carries parse_status, warnings and unparsed lines.
Only a file that cannot be opened at all raises.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pdfplumber

from ...models import Award
from . import parse_narrative as nar
from .parse_details import parse_details
from .parse_fallback import parse_blanket, parse_generic
from .pdf_layout import page_flow_text, page_lines

log = logging.getLogger("award_to_lane.parse")


def detect_family(texts: list[str]) -> str:
    total_chars = sum(len(t) for t in texts)
    if total_chars < 80:
        return "scanned"
    joined = "\n".join(texts[:3])
    if "PURCHASE DETAILS" in "\n".join(texts) and "Contractor/" in "\n".join(texts):
        return "sap_details"
    if "Minimum Value" in joined or "CLIN" in "\n".join(texts):
        return "blanket"
    return "generic"


def parse_pdf(path: Path | str, meta: dict | None = None, international_pii_regex: str | None = None) -> Award:
    path = Path(path)
    meta = meta or {}
    award = Award(award_key=meta.get("award_key") or path.stem.replace("-PCA", ""), source_file=path.name)
    with pdfplumber.open(path) as pdf:
        texts = [page_flow_text(p) for p in pdf.pages]
        family = detect_family(texts)
        award.family = family
        narrative_text = "\n".join(texts[:2])
        _fill_narrative(award, narrative_text, path.name, meta, international_pii_regex)
        if award.is_cancelled:
            award.warnings.append("cancellation notice, not an award")
            award.parse_status = "warnings"
            return award
        try:
            if family == "sap_details":
                pages = [page_lines(p, i + 1) for i, p in enumerate(pdf.pages)]
                parse_details(pages, award)
            elif family == "blanket":
                parse_blanket([p.extract_text() or "" for p in pdf.pages], award)
            elif family == "generic":
                parse_generic([p.extract_text() or "" for p in pdf.pages], award)
            else:
                award.warnings.append("no text layer (scanned image); OCR not attempted")
        except Exception as exc:  # keep the narrative fields even if the table parse blows up
            log.exception("detail parse failed for %s", path.name)
            award.warnings.append(f"detail parse error: {type(exc).__name__}: {exc}")
            award.parse_status = "failed"
            return award
    blank = sum(1 for t in texts if len(t.strip()) < 20)
    if blank and family != "scanned":
        award.warnings.append(f"{blank} page(s) have no text layer (scanned image); their contents were not read")
    if not award.contracts and family != "scanned":
        award.parse_status = "failed"
        award.warnings.append("no contracts recognised")
    elif award.warnings:
        award.parse_status = "warnings"
    else:
        award.parse_status = "ok"
    return award


def _fill_narrative(award: Award, text: str, file_name: str, meta: dict, pii_regex: str | None) -> None:
    award.pii = meta.get("pii") or nar.find_pii(text)
    if award.pii and (not meta.get("award_key")):
        award.award_key = award.pii
    award.sol_no = meta.get("sol_no") or nar.find_wbscm_no(text)
    award.award_date = nar.find_award_date(text)
    award.delivery_start, award.delivery_end = nar.find_delivery_window(text)
    award.num_offers = nar.find_num_offers(text)
    award.commodity_title = nar.find_commodity_title(text)
    award.commodity = meta.get("prod_subcat") or meta.get("prod_grp") or award.commodity_title
    award.is_international = nar.looks_international(text, award.pii, pii_regex)
    award.is_cancelled = nar.looks_cancelled(text, file_name)
    if award.delivery_start is None and not award.is_international and not award.is_cancelled:
        award.warnings.append("delivery window not found in narrative")
    if award.delivery_start and award.delivery_end and award.delivery_end < award.delivery_start:
        award.warnings.append("delivery window end precedes start")
    if award.is_cancelled and award.delivery_start is None and "delivery window not found in narrative" in award.warnings:
        award.warnings.remove("delivery window not found in narrative")
