"""Regex extraction from the PURCHASE AWARD DESCRIPTION narrative.

pdfplumber sometimes moves a space one character ("fromN ovember 1"), and wording differs by commodity
desk ("from X through Y", "between X and Y", "from: X - Y"). Matching against the text with ALL whitespace
removed makes both problems disappear, so most patterns here run on squash(text).
"""
from __future__ import annotations

import re
from datetime import date

MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November",
     "December"], start=1)}
MONTHS.update({k[:3]: v for k, v in list(MONTHS.items())})
MONTH_RE = r"(January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.?"

PII_RE = re.compile(r"\b(\d{2}-\d[A-Z0-9]{3}-\d{2}-[A-Z]-\d{4}(?:-\d{4})?)\b")
WBSCM_RE = re.compile(r"(?<!\d)(2\d{9})(?!\d)")
HEADER_DATE_RE = re.compile(MONTH_RE + r"\s*(\d{1,2}),?\s*(\d{4})\s+\d{1,2}:\d{2}(?::\d{2})?")
DATE_LABEL_RE = re.compile(r"DATE:?\s*" + MONTH_RE + r"\s*(\d{1,2}),?\s*(\d{4})", re.I)
_SEP = r"(?:through|thru|to|and|until|[-–—#]+)"
WINDOW_RE = re.compile(
    r"De[il]{2}ver(?:ies|y)[a-z,]{0,40}?(?:from|between|during):?" + MONTH_RE + r"(\d{1,2})(?:st|nd|rd|th)?,?(\d{4}),?" + _SEP
    + MONTH_RE + r"(\d{1,2})(?:st|nd|rd|th)?,?(\d{4})", re.I)
WINDOW_NOYEAR_RE = re.compile(
    r"De[il]{2}ver(?:ies|y)[a-z,]{0,40}?(?:from|between|during|period|:)" + ":?" + MONTH_RE + r"(\d{1,2}),?" + _SEP + MONTH_RE
    + r"(\d{1,2}),?(\d{4})", re.I)
OFFERS_RE = re.compile(r"NumberofOffers/?Quotes\(?VendorResponses\)?received:?(\d+)", re.I)
INTERNATIONAL_WORDS = ("exportdistribution", "mcgovern-dole", "foodforpeace", "usaid", "foodforprogress", "titleii")


def squash(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def make_date(month: str, day: str, year: str) -> date | None:
    m = MONTHS.get(month.lower().rstrip("."))
    if m is None and month.lower().startswith("sept"):
        m = 9
    try:
        return date(int(year), m, int(day)) if m else None
    except ValueError:
        return None


def find_delivery_window(text: str) -> tuple[date | None, date | None]:
    s = squash(text)
    m = WINDOW_RE.search(s)
    if m:
        start, end = make_date(m.group(1), m.group(2), m.group(3)), make_date(m.group(4), m.group(5), m.group(6))
        if start and end and end < start and end.replace(year=end.year + 1) > start:
            end = end.replace(year=end.year + 1)      # the PDF's end year is a typo ("November 2026 through April 2026")
        return start, end
    m = WINDOW_NOYEAR_RE.search(s)
    if m:
        end = make_date(m.group(3), m.group(4), m.group(5))
        start = make_date(m.group(1), m.group(2), m.group(5))
        if start and end and start > end:
            start = start.replace(year=start.year - 1)
        return start, end
    return None, None


def find_num_offers(text: str) -> int | None:
    m = OFFERS_RE.search(squash(text))
    return int(m.group(1)) if m else None


def find_pii(text: str) -> str | None:
    m = re.search(r"PCA\s*:\s*(\d{2}-\d[A-Z0-9]{3}-\d{2}-[A-Z]-\d{4}(?:-\d{4})?)", text)
    if m:
        return m.group(1)
    m = PII_RE.search(squash(text)[:2000])
    return m.group(1) if m else None


def find_wbscm_no(text: str) -> str | None:
    m = WBSCM_RE.search(squash(text))
    return m.group(1) if m else None


def find_award_date(text: str) -> date | None:
    head = "\n".join(text.splitlines()[:6])
    m = HEADER_DATE_RE.search(head)
    if m:
        return make_date(m.group(1), m.group(2), m.group(3))
    m = DATE_LABEL_RE.search(text)
    if m:
        return make_date(m.group(1), m.group(2), m.group(3))
    return None


def find_commodity_title(text: str) -> str | None:
    """A short human label for the award, taken from the first narrative line."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for i, line in enumerate(lines):
        if line.upper().startswith("PURCHASE AWARD DESCRIPTION") and i + 1 < len(lines):
            title = lines[i + 1]
            m = re.search(r"purchased\s+(.{3,90}?)\s+(?:as indicated|for distribution|products as|under)", title, re.I)
            if m:
                return m.group(1).strip()
            title = re.split(r",?\s+SOLICITATION|\s+PCA\b|\s*\(WBSCM|,?\s+WBSCM|,?\s+\d{2}-\d[A-Z0-9]{3}-", title, maxsplit=1,
                             flags=re.I)[0]
            return _clean_title(title)
    m = re.search(r"purchased\s+(.{3,90}?)\s+(?:as indicated|for distribution|as outlined)", text, re.I)
    return _clean_title(m.group(1)) if m else None


def _clean_title(title: str) -> str | None:
    title = title.strip(" ,:;.")
    if len(title) > 80 or re.match(r"^(The USDA|The following|The commodities|The Department|We would)", title, re.I):
        return None
    return title or None


def looks_international(text: str, pii: str | None, pii_regex: str | None) -> bool:
    if pii and pii_regex and re.search(pii_regex, pii):
        return True
    s = squash(text).lower()
    return any(w in s for w in INTERNATIONAL_WORDS)


def looks_cancelled(text: str, file_name: str) -> bool:
    if "cancel" in file_name.lower():
        return True
    head = squash(text[:1500]).lower()
    return any(w in head for w in ("cancellation", "iscancelled", "iscanceled", "hasbeencancelled", "hasbeencanceled",
                                   "herebycancelled", "herebycanceled", "cancelledinitsentirety", "canceledinitsentirety"))
