"""Optional SAM.gov plug-in: free public entity data (website and points of contact) for awarded companies.

Every company that wins a USDA contract holds an active registration in SAM.gov, and the Entity
Management API exposes the public part of that registration. This plug-in looks each company on the
call sheet up once, fills lead.domain from the entity's website and lead.contacts from its points of
contact, and caches the result in SQLite (the same company_domains / company_contacts tables the
Apollo plug-in uses). It runs before Apollo, so Apollo (when enabled) only spends credits on
companies SAM.gov could not resolve.

What the API returns publicly (checked 2026-10-19 against https://open.gsa.gov/api/entity-api/):

  Endpoint  GET https://api.sam.gov/entity-information/v3/entities?api_key=...  v4 (Dec 2024) only
            adds an "exceeds domestic threshold" field; v3 is used here and the URL is configurable.
  Search    legalBusinessName (partial or complete value), physicalAddressProvinceOrStateCode,
            physicalAddressCity, registrationStatus (A active / E expired), samRegistered=Yes, ueiSAM,
            cageCode, includeSections=entityRegistration,coreData,pointsOfContact. Ten records per
            page (size > 10 is an error). The characters & | { } ^ \\ are rejected in parameter values.
  Response  {"totalRecords": n, "entityData": [{entityRegistration, coreData, pointsOfContact}, ...]}
    entityRegistration  ueiSAM, cageCode, legalBusinessName, dbaName, registrationStatus,
                        registrationDate, registrationExpirationDate, exclusionStatusFlag,
                        publicDisplayFlag: Y = shown publicly, N = the entity opted out of public
                        search and only a stub ("This entity has opted out of public search") is
                        returned, without address, website or points of contact.
    coreData            entityInformation.entityURL (the website; free text, often blank),
                        physicalAddress and mailingAddress (addressLine1/2, city, stateOrProvinceCode,
                        zipCode, zipCodePlus4, countryCode), businessTypes, generalInformation.
    pointsOfContact     governmentBusinessPOC, governmentBusinessAlternatePOC, electronicBusinessPOC,
                        electronicBusinessAlternatePOC, pastPerformancePOC (+ alternate), ...
                        PUBLIC fields per POC: firstName, middleInitial, lastName, title and the
                        POC's address. E-mail, US/non-US phone and fax were removed from the public
                        POC sections in v1.4 (Dec 2019) and are "FOUO" data, served only to federal
                        system accounts with the Read-FOUO role. A personal key therefore yields the
                        names and titles of the registration contacts, never an e-mail or a phone.
  Key       Not an api.data.gov key: a non-federal user signs in to SAM.gov and copies the "Public
            API Key" from https://sam.gov/workspace/profile/account-details (revealed with an
            e-mailed one-time code). Same 40-character format as api.data.gov keys; third-party
            notes say it expires after about 90 days and is regenerated on the same page.
  Quota     Default daily limits: personal key on a non-federal account with no SAM.gov role: 10
            requests/day; personal key with a SAM.gov role, or a non-federal system account:
            1,000/day; federal system account: 10,000/day. Over the limit the gateway answers
            HTTP 429 until the next calendar day; a missing or invalid key gets HTTP 403. Hence the
            small max_companies_per_run default (10), one request per company, no retries, and a
            cache that also remembers misses (retried after miss_retry_days). The bulk alternative
            is the Entity Extracts API (a daily public file), which this plug-in does not use.

Off by default (plugins.sam_gov.enabled). The key is read from the environment variable named in
plugins.sam_gov.api_key_env (default SAM_GOV_API_KEY), never from config.yaml. Nothing here raises
into the pipeline: every failure is logged, counted in the returned stats, and the run goes on.

Matching is deliberately conservative. The award PDFs carry the contractor name as typed into WBSCM:
corporate suffixes in every spelling, truncation at about 35 characters, stray address fragments
("HP HOOD LLC SIX KIMBALL LN STE 400") and the odd doubled name; the legal business name in SAM.gov
is cleaner. Both sides are reduced to a core-name key (everything before the first corporate suffix
and the first comma, normalised with geo.norm_key). An entity whose legal-name key equals ours and
whose physical address is in the award's state is an exact match (cache source "sam_gov"). Anything
looser (same key but the state could not be checked, a DBA-name match, or a prefix match for a
truncated name) is a possible match: its data is still shown, but the lead carries the flag
sam_possible_match and the cache source is "sam_gov~", the convention geo.py uses for approximate
gazetteer hits. The source of every contact is stored in company_contacts.source and in the
"source" key of each lead.contacts entry.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

from ..geo import norm_key
from ..http import HttpError, PoliteSession
from ..models import Lead

log = logging.getLogger("award_to_lane.sam_gov")

SOURCE = "sam_gov"
SOURCE_POSSIBLE = "sam_gov~"
FLAG_POSSIBLE = "sam_possible_match"
DEFAULT_URL = "https://api.sam.gov/entity-information/v3/entities"
SECTIONS = "entityRegistration,coreData,pointsOfContact"
FORBIDDEN_CHARS = "&|{}^\\"
SUFFIXES = {"INC", "INCORPORATED", "LLC", "LLP", "LP", "LTD", "LIMITED", "CO", "COMPANY", "CORP", "CORPORATION", "PLC",
            "LC", "PC", "SA", "GMBH"}
POC_ROLES = [("governmentBusinessPOC", "Govt business POC"), ("governmentBusinessAlternatePOC", "Alt govt business POC"),
             ("electronicBusinessPOC", "E-business POC"), ("electronicBusinessAlternatePOC", "Alt e-business POC")]
MIN_PREFIX_KEY = 12      # a prefix match (truncated award name) needs at least this many key characters
MAX_ERRORS = 3           # consecutive failures before the plug-in stops spending quota for the run


class SamGovQuotaExceeded(RuntimeError):
    """HTTP 429: the key's daily request allowance is spent."""


class SamGovKeyRejected(RuntimeError):
    """HTTP 401/403: missing, invalid or expired API key."""


@dataclass
class SamMatch:
    quality: str                     # exact | possible
    uei: str | None
    cage: str | None
    legal_name: str
    domain: str | None
    contacts: list[dict] = field(default_factory=list)
    opted_out: bool = False


# -- name handling ----------------------------------------------------------------

def company_key(name: str) -> str:
    """Cache key shared with the Apollo plug-in and apollo_import.csv."""
    return norm_key(name)[:80]


def _tokens(name: str) -> list[str]:
    name = re.sub(r"^.*\(\s*DESCRIPTION\s*\)\s*", "", name or "", flags=re.I)   # table header glued on by a bad parse
    name = re.sub(r"^\s*THE\s+", "", name.strip(), flags=re.I)
    name = re.sub(r",\s*THE\s*$", "", name, flags=re.I)
    toks = name.split()
    if len(toks) >= 4 and len(toks) % 2 == 0:                                    # "X FOODS X FOODS" doubled by the PDF
        half = len(toks) // 2
        if norm_key(" ".join(toks[:half])) == norm_key(" ".join(toks[half:])):
            toks = toks[:half]
    return toks


def core_name(name: str | None) -> str:
    """The name before its first corporate suffix and first comma: "HP HOOD LLC SIX KIMBALL LN" -> "HP HOOD"."""
    toks = _tokens(name or "")
    for i, tok in enumerate(toks):
        if i and re.sub(r"[^A-Z0-9]", "", tok.upper()) in SUFFIXES:
            toks = toks[:i]
            break
    core = " ".join(toks).split(",")[0]
    return re.sub(r"\s+", " ", core).strip(" .,-")


def name_key(name: str | None) -> str:
    return norm_key(core_name(name))


def query_name(name: str) -> str | None:
    """What to send as legalBusinessName: the longest run of the core name free of characters the API rejects."""
    core = core_name(name)
    segments = [s.strip(" .,-") for s in re.split("[" + re.escape(FORBIDDEN_CHARS) + "]", core)]
    best = max(segments, key=lambda s: (len(s), -segments.index(s))) if segments else ""
    best = re.sub(r"\s+", " ", best)
    return best if len(best) >= 3 else None


def clean_domain(url: str | None) -> str | None:
    if not url:
        return None
    s = url.strip().lower()
    s = re.sub(r"^[a-z][a-z0-9+.-]*://", "", s)
    s = s.split("/")[0].split("?")[0].split("#")[0].split("@")[-1].split(":")[0]
    if s.startswith("www."):
        s = s[4:]
    return s if re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", s) else None


# -- matching ----------------------------------------------------------------------

def _entity_state(entity: dict) -> str:
    addr = (entity.get("coreData") or {}).get("physicalAddress") or {}
    return (addr.get("stateOrProvinceCode") or "").upper()


def match_entity(company: str, state: str | None, entities: list[dict]) -> tuple[dict, str] | None:
    """Pick the entity for an award company. Returns (entity, "exact" | "possible") or None."""
    ours = name_key(company)
    if len(ours) < 4:
        return None
    want_state = (state or "").upper()
    ranked: list[tuple[tuple, dict, str]] = []
    for entity in entities:
        reg = entity.get("entityRegistration") or {}
        legal, dba = name_key(reg.get("legalBusinessName")), name_key(reg.get("dbaName"))
        e_state = _entity_state(entity)
        same_state = bool(want_state) and e_state == want_state
        if legal == ours and same_state:
            rank, quality = 0, "exact"
        elif legal == ours and not e_state:                     # opted-out stub or state unknown on either side
            rank, quality = 1, "possible"
        elif legal == ours and not want_state:
            rank, quality = 1, "possible"
        elif dba and dba == ours and same_state:
            rank, quality = 2, "possible"
        elif same_state and len(ours) >= MIN_PREFIX_KEY and legal.startswith(ours):
            rank, quality = 3, "possible"
        else:
            continue
        info = (entity.get("coreData") or {}).get("entityInformation") or {}
        tie = (0 if reg.get("registrationStatus") == "A" else 1, 0 if clean_domain(info.get("entityURL")) else 1,
               abs(len(legal) - len(ours)))
        ranked.append(((rank, *tie), entity, quality))
    if not ranked:
        return None
    ranked.sort(key=lambda r: r[0])
    return ranked[0][1], ranked[0][2]


def extract_contacts(entity: dict, source: str = SOURCE) -> list[dict]:
    """Registration points of contact as call-sheet contacts. Public data has names and titles only."""
    pocs = entity.get("pointsOfContact") or {}
    out: list[dict] = []
    seen: set[str] = set()
    for key, label in POC_ROLES:
        p = pocs.get(key) or {}
        name = " ".join(x.strip() for x in (p.get("firstName"), p.get("middleInitial"), p.get("lastName")) if x and x.strip())
        if not name or norm_key(name) in seen:
            continue
        seen.add(norm_key(name))
        title = (p.get("title") or "").strip()
        out.append({"name": name, "title": f"{title}, {label}" if title else label,
                    "email": (p.get("email") or "").strip() or None,                       # FOUO: blank on public keys
                    "phone": (p.get("usPhone") or p.get("nonUSPhone") or "").strip() or None,
                    "linkedin": None, "source": source})
    return out


# -- client ------------------------------------------------------------------------

class SamGovClient:
    def __init__(self, api_key: str, base_url: str, http: PoliteSession, active_only: bool = True):
        self.api_key = api_key
        self.base_url = base_url
        self.http = http
        self.active_only = active_only

    def _get(self, params: dict) -> dict:
        self.http._wait()
        self.http.requests_made += 1
        try:
            resp = self.http.session.get(self.base_url, params={**params, "api_key": self.api_key}, timeout=self.http.timeout,
                                         headers={"Accept": "application/json"})
        except requests.RequestException as exc:                     # message may embed the URL and so the key
            raise RuntimeError(f"{type(exc).__name__} while calling SAM.gov") from None
        finally:
            self.http._last_request = time.monotonic()
        if resp.status_code == 429:
            raise SamGovQuotaExceeded("HTTP 429 from SAM.gov: daily request quota exhausted")
        if resp.status_code in (401, 403):
            raise SamGovKeyRejected(f"HTTP {resp.status_code} from SAM.gov: API key missing, invalid or expired")
        if resp.status_code >= 400:
            raise HttpError(resp.status_code, self.base_url)
        return resp.json()

    def search(self, legal_name: str, state: str | None = None) -> list[dict]:
        params = {"legalBusinessName": legal_name, "samRegistered": "Yes", "includeSections": SECTIONS, "page": 0, "size": 10}
        if self.active_only:
            params["registrationStatus"] = "A"
        if state:
            params["physicalAddressProvinceOrStateCode"] = state.upper()
        data = self._get(params)
        return list(data.get("entityData") or [])

    def lookup(self, company: str, state: str | None) -> SamMatch | None:
        q = query_name(company)
        if not q:
            return None
        picked = match_entity(company, state, self.search(q, state))
        if picked is None:
            return None
        entity, quality = picked
        reg = entity.get("entityRegistration") or {}
        info = (entity.get("coreData") or {}).get("entityInformation") or {}
        opted_out = (reg.get("publicDisplayFlag") or "Y").upper() == "N"
        source = SOURCE if quality == "exact" else SOURCE_POSSIBLE
        return SamMatch(quality, reg.get("ueiSAM"), reg.get("cageCode"), reg.get("legalBusinessName") or "",
                        None if opted_out else clean_domain(info.get("entityURL")),
                        [] if opted_out else extract_contacts(entity, source), opted_out)


# -- pipeline step -------------------------------------------------------------------

def _cached(db, ck: str):
    return db.one("SELECT domain, source, updated_at FROM company_domains WHERE company_key = ?", (ck,))


def _age_days(updated_at: str | None) -> float:
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(updated_at)).total_seconds() / 86400
    except (TypeError, ValueError):
        return 1e9


def _needs_lookup(row, contacts: list[dict], retry_days: float) -> bool:
    if row is None:
        return True
    if row["domain"] or contacts:
        return False
    return _age_days(row["updated_at"]) >= retry_days          # a cached miss, old enough to try again


def _store(db, ck: str, company: str, match: SamMatch | None, max_contacts: int, stats: dict) -> None:
    if match is None:
        db.domain_put(ck, company, None, SOURCE)
        stats["misses"] += 1
        log.info("SAM.gov: no match for %s", company)
    else:
        source = SOURCE if match.quality == "exact" else SOURCE_POSSIBLE
        db.domain_put(ck, company, match.domain, source)
        contacts = match.contacts[:max_contacts]
        if contacts:
            db.contacts_put(ck, contacts, source)
        stats["matched" if match.quality == "exact" else "possible"] += 1
        log.info("SAM.gov: %s match for %s -> %s (UEI %s, CAGE %s) site=%s pocs=%d%s", match.quality, company, match.legal_name,
                 match.uei, match.cage, match.domain or "-", len(contacts), " (opted out of public display)" if match.opted_out else "")
    db.commit()


def _apply(lead: Lead, row, contacts: list[dict], stats: dict) -> None:
    if row is None:
        return
    if row["domain"] and not lead.domain:
        lead.domain = row["domain"]
    if contacts and not lead.contacts:
        lead.contacts = contacts
    source = row["source"] or ""
    if source.startswith(SOURCE) and (row["domain"] or contacts):
        stats["companies"] += 1
        if source == SOURCE_POSSIBLE and FLAG_POSSIBLE not in lead.flags:
            lead.flags.append(FLAG_POSSIBLE)


def enrich_leads(leads: list[Lead], db, settings, http: PoliteSession | None) -> dict:
    """Fill lead.domain and lead.contacts from SAM.gov. Returns stats. Safe to call when the plug-in is disabled."""
    cfg = settings.get("plugins.sam_gov", {}) or {}
    enabled = bool(cfg.get("enabled"))
    stats = {"enabled": enabled, "companies": 0, "lookups": 0, "matched": 0, "possible": 0, "misses": 0, "errors": 0, "stopped": None}
    if not enabled:
        return stats
    key_env = cfg.get("api_key_env", "SAM_GOV_API_KEY")
    key = settings.env(key_env)
    client = None
    if not key:
        log.warning("SAM.gov plug-in enabled but %s is not set; skipping lookups", key_env)
    elif http is not None:
        client = SamGovClient(key, cfg.get("base_url", DEFAULT_URL), http, bool(cfg.get("active_only", True)))
    max_new = int(cfg.get("max_companies_per_run", 10))
    max_contacts = int(cfg.get("max_contacts_per_company", 4))
    retry_days = float(cfg.get("miss_retry_days", 30))
    seen: set[str] = set()
    for lead in leads:
        ck = company_key(lead.company)
        row = _cached(db, ck)
        contacts = [dict(r) for r in db.contacts_get(ck)]
        if (client is not None and stats["stopped"] is None and ck not in seen and stats["lookups"] < max_new
                and _needs_lookup(row, contacts, retry_days)):
            seen.add(ck)
            stats["lookups"] += 1
            try:
                match = client.lookup(lead.company, lead.company_state)
            except (SamGovQuotaExceeded, SamGovKeyRejected) as exc:
                stats["errors"] += 1
                stats["stopped"] = str(exc)
                log.warning("SAM.gov: %s; no further lookups this run", exc)
            except Exception as exc:                                   # a failed lookup must not stop the run
                stats["errors"] += 1
                log.warning("SAM.gov lookup failed for %s: %s", lead.company, exc)
                if stats["errors"] >= MAX_ERRORS:
                    stats["stopped"] = f"{stats['errors']} failed lookups"
                    log.warning("SAM.gov: %s; no further lookups this run", stats["stopped"])
            else:
                try:
                    _store(db, ck, lead.company, match, max_contacts, stats)
                except Exception as exc:
                    stats["errors"] += 1
                    log.warning("SAM.gov: could not cache result for %s: %s", lead.company, exc)
                row = _cached(db, ck)
                contacts = [dict(r) for r in db.contacts_get(ck)]
        _apply(lead, row, contacts, stats)
    if stats["lookups"]:
        log.info("SAM.gov: %d lookups, %d exact, %d possible, %d misses, %d errors", stats["lookups"], stats["matched"],
                 stats["possible"], stats["misses"], stats["errors"])
    return stats
