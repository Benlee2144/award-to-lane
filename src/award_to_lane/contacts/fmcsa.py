"""Optional FMCSA plug-in: does the awarded company run its own trucks?

The award documents never say whether the winning contractor has a private fleet, and that is the
first thing a freight broker wants to know. FMCSA's Motor Carrier Census answers it for free: every
company that operates commercial vehicles in interstate commerce (and intrastate in most states)
holds a USDOT number, and its MCS-150 filing records how it operates and how many power units it
runs. This plug-in looks each company on the call sheet up once, derives a fleet status, caches it
in SQLite (table company_fleet) and shows it as the Fleet column, a `private_fleet` flag and a
scoring penalty (scoring.penalties.private_fleet).

Source chosen (checked 2026-10-19): the Company Census File published by FMCSA on
data.transportation.gov as Socrata dataset az4n-8mr2, queried through its SODA JSON endpoint
https://data.transportation.gov/resource/az4n-8mr2.json . Why this one of the three free options:

  Motor Carrier Census (this)  No key or account; a SoQL filter returns only the rows for one
                               company, so the 4.5-million-row file (2.2 million active) is never
                               downloaded. Refreshed daily from MCMIS (the Last-Modified header
                               showed the same day). One request returns, per USDOT record:
                               legal_name, dba_name, dot_number, phy_city/phy_state, status_code
                               (A active / I inactive / P pending), carrier_operation (A interstate,
                               B intrastate hazmat, C intrastate non-hazmat), classdef (operation
                               classification, semicolon-separated: "PRIVATE PROPERTY",
                               "AUTHORIZED FOR HIRE", "EXEMPT FOR HIRE", "PRIVATE PASSENGER, ...",
                               "U. S. MAIL", "... GOVERNMENT", "OTHER-..."), power_units and
                               truck_units (totals from the MCS-150), total_drivers, fleetsize,
                               docket1prefix/docket1/docket1_status_code (MC number and its status),
                               carship (entity type: C carrier, S shipper only, B broker, F freight
                               forwarder, R registrant, I intermodal equipment provider, T cargo
                               tank) and mcs150_date (YYYYMMDD of the last filing). Field meanings
                               are from the attached "MCMIS Company Census Data Dictionary (Rev08)
                               2026-01-23". Data contact: FMCSA.CDO@dot.gov.
  QCMobile API                 Same underlying data plus safety scores, but a web key requires a
                               Login.gov developer account, the name search returns 50 carriers a
                               page without the operation classification (a second request per
                               USDOT number), and no rate limit is published.
  SAFER Company Snapshot       HTML only, one carrier per page view, meant for people; parsing it
                               at call-sheet volume is the scraping this plug-in avoids.

Limits of the data. A USDOT record is a registrant, not a corporate group: large food companies
hold one number per division or plant under slightly different names (J R SIMPLOT COMPANY three
times in Idaho; the award name "CARGILL MEAT SOLUTIONS CORPORATION" in Wichita appears as "CARGILL
MEAT LOGISTICS SOLUTIONS INC"), so a strict name match can miss a real fleet and a loose one can
pick a stranger. power_units is self-reported on the MCS-150 and goes stale (mcs150_date tells how
stale); about 1.5% of active records have power units but no classification. "AUTHORIZED FOR HIRE"
means the company holds carrier authority (it may also haul for others), not that it has no fleet.
"no fleet on record" therefore means no active record under that name, not that the company owns no
trucks; it is the normal outcome for a winner that ships purely through carriers, not an error. A
record with carrier or broker authority but zero power units (a brokerage, a shipper-only registrant)
reads "registered, no trucks".

Matching is conservative. Both names are reduced to a core name (before the first corporate suffix
and first comma, so "HP HOOD LLC SIX KIMBALL LN" and "HP HOOD LLC" both become "HP HOOD") and then
to geo.norm_key. The query is a leading-anchored SoQL `like` built from the core name's tokens
("J%R%SIMPLOT%"), which tolerates the punctuation differences between WBSCM and MCMIS spellings and
also catches a truncated award name; the active-only filter is applied server side. The rows that
come back are ranked: same core name in the award's state is an exact match (fleet status
asserted); same core name elsewhere, a DBA-name match, a prefix match for a truncated name, or a
record whose legal name merely contains all of the award name's words, is a possible match and is
shown as "unverified" with the candidate's details, never as a fleet (the contains-all-words rule also
requires the same first word, so "AMERICAN FOODS" does not pick up "NORTH AMERICAN FOODS"). When several records tie,
the one that is itself a private fleet with the most power units is kept, so a company with one
for-hire subsidiary and one private fleet reads "private fleet".

Throttling. Socrata serves anonymous requests from a shared per-IP allowance that is not published
and gives app-token requests about 1,000 per rolling hour; a free app token
(https://data.transportation.gov/profile/edit/developer_settings) goes in .env under the variable
named by plugins.fmcsa.app_token_env and is sent as the X-App-Token header. A bad token answers
HTTP 403 and an exhausted allowance HTTP 429; either stops lookups for the run (nothing is
cached), as do three failed requests. Results, including misses, are cached per company, so a
company costs one request (two when a common name hits the 50-row page and the query is narrowed
to its state) and is then re-checked after refresh_days (misses after miss_retry_days). Nothing in
this module raises into the pipeline: every failure is logged, counted in the returned stats, and
the run goes on. Off by default (plugins.fmcsa.enabled).
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from ..geo import norm_key
from ..http import HttpError, PoliteSession
from ..models import (FLAG_PRIVATE_FLEET, FLEET_FOR_HIRE, FLEET_NONE, FLEET_NOT_FOUND, FLEET_PRIVATE, FLEET_UNVERIFIED,
                      Lead)
from .sam_gov import core_name, name_key

log = logging.getLogger("award_to_lane.fmcsa")

SOURCE = "fmcsa"
SOURCE_POSSIBLE = "fmcsa~"
DEFAULT_URL = "https://data.transportation.gov/resource/az4n-8mr2.json"
SELECT = ("dot_number,legal_name,dba_name,phy_city,phy_state,status_code,carrier_operation,classdef,power_units,"
          "truck_units,total_drivers,docket1prefix,docket1,docket1_status_code,carship,mcs150_date")
PAGE_SIZE = 50
CLASS_PRIVATE = "PRIVATE PROPERTY"
CLASSES_FOR_HIRE = {"AUTHORIZED FOR HIRE", "EXEMPT FOR HIRE"}
MIN_PREFIX_KEY = 12      # a prefix match (truncated award name) needs at least this many key characters
MIN_SUBSET_TOKENS = 2    # a "legal name contains all our words" match needs at least this many words
MAX_ERRORS = 3           # failed requests before the plug-in stops for the run


class FmcsaThrottled(RuntimeError):
    """HTTP 429: the anonymous or app-token request allowance is spent for now."""


class FmcsaTokenRejected(RuntimeError):
    """HTTP 401/403: the Socrata app token is invalid."""


@dataclass
class FleetMatch:
    quality: str                 # exact | possible
    status: str                  # FLEET_PRIVATE | FLEET_FOR_HIRE | FLEET_NONE | FLEET_UNVERIFIED
    usdot: str
    legal_name: str
    dba_name: str | None
    city: str | None
    state: str | None
    classdef: str | None
    power_units: int | None
    total_drivers: int | None
    mc_number: str | None
    mcs150_date: str | None      # ISO date or None
    note: str


# -- name handling -------------------------------------------------------------------

def company_key(name: str) -> str:
    """Cache key shared with the other contact plug-ins and apollo_import.csv."""
    return norm_key(name)[:80]


def name_tokens(name: str | None) -> list[str]:
    return [t for t in re.split(r"[^A-Z0-9]+", core_name(name).upper()) if t]


def like_pattern(name: str | None) -> str | None:
    """Leading-anchored SoQL like pattern from the core name: "J.R. Simplot Co" -> "J%R%SIMPLOT%"."""
    toks = name_tokens(name)
    if not toks or len("".join(toks)) < 3:
        return None
    return "%".join(toks) + "%"        # tokens are alphanumeric, so the pattern needs no quoting or escaping


def soql_where(name: str | None, active_only: bool = True, state: str | None = None) -> str | None:
    pat = like_pattern(name)
    if not pat:
        return None
    clauses = [f"(upper(legal_name) like '{pat}' OR upper(dba_name) like '{pat}')"]
    if active_only:
        clauses.insert(0, "status_code='A'")
    if state and re.fullmatch(r"[A-Za-z]{2}", state):
        clauses.append(f"phy_state='{state.upper()}'")
    return " AND ".join(clauses)


# -- classification and matching -----------------------------------------------------

def _int(value) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _iso_date(yyyymmdd: str | None) -> str | None:
    s = re.sub(r"\D", "", yyyymmdd or "")
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 else None


def classes_of(classdef: str | None) -> set[str]:
    return {c.strip().upper() for c in (classdef or "").split(";") if c.strip()}


def classify(classdef: str | None, power_units: int | None, min_units: int = 1) -> str:
    """Fleet status of one census record, from its operation classification and MCS-150 power units."""
    units = power_units or 0
    classes = classes_of(classdef)
    if units < max(min_units, 1):
        return FLEET_NONE
    if CLASS_PRIVATE in classes:
        return FLEET_PRIVATE
    if classes & CLASSES_FOR_HIRE:
        return FLEET_FOR_HIRE
    if not classes:
        return FLEET_UNVERIFIED            # trucks on file but no classification: do not assert either way
    return FLEET_NONE                      # passenger, government, mail, "other": not a property fleet


def _row_state(row: dict) -> str:
    return (row.get("phy_state") or "").strip().upper()


def rank_rows(company: str, state: str | None, rows: list[dict]) -> list[tuple[dict, str]]:
    """Census rows that can be this company, best first, each with "exact" or "possible"."""
    ours = name_key(company)
    first_token = name_tokens(company)[:1]
    our_tokens = set(name_tokens(company))
    if len(ours) < 4:
        return []
    want_state = (state or "").strip().upper()
    ranked: list[tuple[tuple, dict, str]] = []
    for row in rows:
        legal, dba = name_key(row.get("legal_name")), name_key(row.get("dba_name"))
        legal_tokens = name_tokens(row.get("legal_name"))
        same_state = bool(want_state) and _row_state(row) == want_state
        if legal == ours and same_state:
            rank, quality = 0, "exact"
        elif legal == ours:                                             # right name, other state (or no state to check)
            rank, quality = 1, "possible"
        elif dba and dba == ours and same_state:
            rank, quality = 2, "possible"
        elif same_state and len(ours) >= MIN_PREFIX_KEY and legal.startswith(ours):     # truncated award name
            rank, quality = 3, "possible"
        elif (same_state and len(our_tokens) >= MIN_SUBSET_TOKENS and legal_tokens[:1] == first_token
              and our_tokens <= set(legal_tokens)):                                     # "CARGILL MEAT [LOGISTICS] SOLUTIONS"
            rank, quality = 4, "possible"
        else:
            continue
        ranked.append(((rank,), row, quality))
    ranked.sort(key=lambda r: r[0])
    return [(row, quality) for _, row, quality in ranked]


def pick_row(company: str, state: str | None, city: str | None, rows: list[dict], min_units: int = 1) -> tuple[dict, str] | None:
    """The record that best represents the company: among the best-ranked rows, a private fleet first, then the most
    power units, then the award's city. Several exact matches are the same company's divisions, not an ambiguity."""
    ranked = rank_rows(company, state, rows)
    if not ranked:
        return None
    best_quality = ranked[0][1]
    first_rank_rows = [row for row, q in ranked if q == best_quality]
    want_city = norm_key(city) if city else ""

    def key(row: dict):
        units = _int(row.get("power_units")) or 0
        status = classify(row.get("classdef"), units, min_units)
        return (0 if status == FLEET_PRIVATE else 1, 0 if status == FLEET_FOR_HIRE else 1,
                0 if (want_city and norm_key(row.get("phy_city")) == want_city) else 1, -units)
    return sorted(first_rank_rows, key=key)[0], best_quality


def describe(row: dict, quality: str) -> str:
    """One line for the call sheet: who the record is, how it operates, how big, how old."""
    units, drivers = _int(row.get("power_units")), _int(row.get("total_drivers"))
    name = (row.get("legal_name") or "").strip()
    if row.get("dba_name"):
        name += f" dba {row['dba_name'].strip()}"
    place = ", ".join(p for p in ((row.get("phy_city") or "").strip().title(), _row_state(row)) if p)
    classes = "; ".join(c.title() for c in sorted(classes_of(row.get("classdef")))) or "no operation classification on file"
    size = f"{units if units is not None else '?'} power units" + (f", {drivers} drivers" if drivers is not None else "")
    mc = f"{row.get('docket1prefix')} {row.get('docket1')}".strip() if row.get("docket1") else ""
    parts = [f"{'possible match: ' if quality != 'exact' else ''}{name} ({place})" if place else name, classes, size]
    if mc:
        parts.append(f"{mc}{' inactive' if row.get('docket1_status_code') == 'I' else ''}")
    if _iso_date(row.get("mcs150_date")):
        parts.append(f"MCS-150 {_iso_date(row.get('mcs150_date'))}")
    return "; ".join(parts)


def build_match(row: dict, quality: str, min_units: int = 1) -> FleetMatch:
    units = _int(row.get("power_units"))
    status = classify(row.get("classdef"), units, min_units)
    if quality != "exact":
        status = FLEET_UNVERIFIED
    mc = f"{row.get('docket1prefix') or 'MC'}-{row.get('docket1')}" if row.get("docket1") else None
    return FleetMatch(quality, status, str(row.get("dot_number") or ""), (row.get("legal_name") or "").strip(),
                      (row.get("dba_name") or "").strip() or None, (row.get("phy_city") or "").strip() or None,
                      _row_state(row) or None, row.get("classdef"), units, _int(row.get("total_drivers")), mc,
                      _iso_date(row.get("mcs150_date")), describe(row, quality))


# -- client ------------------------------------------------------------------------

class CensusClient:
    def __init__(self, base_url: str, http: PoliteSession, app_token: str | None = None, active_only: bool = True,
                 page_size: int = PAGE_SIZE):
        self.base_url = base_url
        self.http = http
        self.app_token = app_token
        self.active_only = active_only
        self.page_size = page_size

    def _get(self, params: dict) -> list[dict]:
        self.http._wait()
        self.http.requests_made += 1
        headers = {"Accept": "application/json"}
        if self.app_token:
            headers["X-App-Token"] = self.app_token
        try:
            resp = self.http.session.get(self.base_url, params=params, timeout=self.http.timeout, headers=headers)
        except requests.RequestException as exc:                     # the message may embed the URL and so the token
            raise RuntimeError(f"{type(exc).__name__} while calling data.transportation.gov") from None
        finally:
            self.http._last_request = time.monotonic()
        if resp.status_code == 429:
            raise FmcsaThrottled("HTTP 429 from data.transportation.gov: request allowance exhausted for now")
        if resp.status_code in (401, 403):
            raise FmcsaTokenRejected(f"HTTP {resp.status_code} from data.transportation.gov: app token rejected")
        if resp.status_code >= 400:
            raise HttpError(resp.status_code, self.base_url)
        data = resp.json()
        if not isinstance(data, list):                              # a SoQL error comes back as {"error": true, "message": ...}
            raise RuntimeError(str((data or {}).get("message") if isinstance(data, dict) else data)[:200])
        return data

    def search(self, company: str, state: str | None) -> list[dict]:
        where = soql_where(company, self.active_only)
        if not where:
            return []
        rows = self._get({"$select": SELECT, "$where": where, "$limit": self.page_size})
        if len(rows) >= self.page_size and state:                   # common name: narrow the page to the award's state
            rows = self._get({"$select": SELECT, "$where": soql_where(company, self.active_only, state), "$limit": self.page_size})
        return rows

    def lookup(self, company: str, state: str | None, city: str | None, min_units: int = 1) -> FleetMatch | None:
        picked = pick_row(company, state, city, self.search(company, state), min_units)
        return build_match(picked[0], picked[1], min_units) if picked else None


# -- pipeline step -------------------------------------------------------------------

def _age_days(updated_at: str | None) -> float:
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(updated_at)).total_seconds() / 86400
    except (TypeError, ValueError):
        return 1e9


def _needs_lookup(row, refresh_days: float, miss_retry_days: float) -> bool:
    if row is None:
        return True
    return _age_days(row["updated_at"]) >= (miss_retry_days if row["fleet_status"] == FLEET_NOT_FOUND else refresh_days)


def _store(db, ck: str, company: str, match: FleetMatch | None, stats: dict) -> None:
    if match is None:
        db.fleet_put(ck, company, FLEET_NOT_FOUND, SOURCE)
        stats["misses"] += 1
        log.info("FMCSA: no active census record for %s", company)
    else:
        source = SOURCE if match.quality == "exact" else SOURCE_POSSIBLE
        db.fleet_put(ck, company, match.status, source, match_quality=match.quality, usdot=match.usdot, legal_name=match.legal_name,
                     dba_name=match.dba_name, phy_city=match.city, phy_state=match.state, classdef=match.classdef,
                     power_units=match.power_units, total_drivers=match.total_drivers, mc_number=match.mc_number,
                     mcs150_date=match.mcs150_date, note=match.note)
        stats[{FLEET_PRIVATE: "private", FLEET_FOR_HIRE: "for_hire", FLEET_NONE: "no_fleet"}.get(match.status, "unverified")] += 1
        log.info("FMCSA: %s -> %s (USDOT %s, %s)", company, match.status, match.usdot, match.note)
    db.commit()


def _apply(lead: Lead, row, stats: dict) -> None:
    if row is None:
        return
    lead.fleet_status = row["fleet_status"]
    lead.usdot = row["usdot"] or None
    lead.power_units = row["power_units"]
    lead.fleet_note = row["note"] or None
    stats["companies"] += 1
    if lead.fleet_status == FLEET_PRIVATE and FLAG_PRIVATE_FLEET not in lead.flags:
        lead.flags.append(FLAG_PRIVATE_FLEET)


def enrich_leads(leads: list[Lead], db, settings, http: PoliteSession | None) -> dict:
    """Fill lead.fleet_status / usdot / power_units / fleet_note. Returns stats. Safe to call when disabled."""
    cfg = settings.get("plugins.fmcsa", {}) or {}
    enabled = bool(cfg.get("enabled"))
    stats = {"enabled": enabled, "companies": 0, "lookups": 0, "private": 0, "for_hire": 0, "no_fleet": 0, "unverified": 0,
             "misses": 0, "errors": 0, "stopped": None}
    if not enabled:
        return stats
    token_env = cfg.get("app_token_env")
    token = settings.env(token_env) if token_env else None
    client = None
    if http is not None:
        client = CensusClient(cfg.get("base_url", DEFAULT_URL), http, token, bool(cfg.get("active_only", True)))
    max_new = int(cfg.get("max_companies_per_run", 25))
    min_units = int(cfg.get("min_power_units", 1))
    refresh_days = float(cfg.get("refresh_days", 90))
    miss_retry_days = float(cfg.get("miss_retry_days", 30))
    seen: set[str] = set()
    for lead in leads:
        ck = company_key(lead.company)
        row = db.fleet_get(ck)
        if (client is not None and stats["stopped"] is None and ck not in seen and stats["lookups"] < max_new
                and _needs_lookup(row, refresh_days, miss_retry_days)):
            seen.add(ck)
            stats["lookups"] += 1
            try:
                match = client.lookup(lead.company, lead.company_state, lead.company_city, min_units)
            except (FmcsaThrottled, FmcsaTokenRejected) as exc:
                stats["errors"] += 1
                stats["stopped"] = str(exc)
                log.warning("FMCSA: %s; no further lookups this run", exc)
            except Exception as exc:                                   # a failed lookup must not stop the run
                stats["errors"] += 1
                log.warning("FMCSA lookup failed for %s: %s", lead.company, exc)
                if stats["errors"] >= MAX_ERRORS:
                    stats["stopped"] = f"{stats['errors']} failed lookups"
                    log.warning("FMCSA: %s; no further lookups this run", stats["stopped"])
            else:
                try:
                    _store(db, ck, lead.company, match, stats)
                except Exception as exc:
                    stats["errors"] += 1
                    log.warning("FMCSA: could not cache result for %s: %s", lead.company, exc)
                row = db.fleet_get(ck)
        _apply(lead, row, stats)
    if stats["lookups"]:
        log.info("FMCSA: %d lookups: %d private fleet, %d for-hire, %d registered without trucks, %d unverified, %d no record, %d errors",
                 stats["lookups"], stats["private"], stats["for_hire"], stats["no_fleet"], stats["unverified"], stats["misses"],
                 stats["errors"])
    return stats
