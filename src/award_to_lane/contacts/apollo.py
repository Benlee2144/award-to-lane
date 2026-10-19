"""Optional Apollo.io plug-in: company domain lookup and logistics / transportation / supply-chain contacts.

Off by default (plugins.apollo.enabled). The API key is read from the environment variable named in
plugins.apollo.api_key_env (default APOLLO_API_KEY), never from config.yaml. Results are cached in SQLite
so a company is looked up once, not every morning. Apollo credits are only spent on companies that lack
a cached result.
"""
from __future__ import annotations

import logging

from ..geo import norm_key
from ..http import PoliteSession
from ..models import Lead

log = logging.getLogger("award_to_lane.apollo")


class ApolloClient:
    def __init__(self, api_key: str, base_url: str, http: PoliteSession):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.http = http

    def _post(self, path: str, payload: dict) -> dict:
        self.http._wait()
        resp = self.http.session.post(f"{self.base_url}{path}", json=payload, timeout=self.http.timeout,
                                      headers={"x-api-key": self.api_key, "Content-Type": "application/json",
                                               "Cache-Control": "no-cache"})
        self.http._last_request = __import__("time").monotonic()
        resp.raise_for_status()
        return resp.json()

    def find_domain(self, company: str, state: str | None = None) -> str | None:
        payload = {"q_organization_name": company, "page": 1, "per_page": 3}
        data = self._post("/mixed_companies/search", payload)
        orgs = data.get("organizations") or data.get("accounts") or []
        if state:
            for org in orgs:
                if (org.get("state") or "").upper().startswith(state.upper()) and org.get("primary_domain"):
                    return org["primary_domain"]
        for org in orgs:
            if org.get("primary_domain"):
                return org["primary_domain"]
        return None

    def find_contacts(self, domain: str, titles: list[str], limit: int = 5) -> list[dict]:
        payload = {"q_organization_domains_list": [domain], "person_titles": titles, "page": 1, "per_page": limit}
        data = self._post("/mixed_people/search", payload)
        people = data.get("people") or data.get("contacts") or []
        out = []
        for p in people[:limit]:
            out.append({"name": p.get("name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip(),
                        "title": p.get("title"), "email": p.get("email"), "phone": (p.get("phone_numbers") or [{}])[0].get("sanitized_number")
                        if p.get("phone_numbers") else None, "linkedin": p.get("linkedin_url")})
        return out


def enrich_leads(leads: list[Lead], db, settings, http: PoliteSession) -> dict:
    """Fill lead.domain and lead.contacts. Returns stats. Safe to call when the plug-in is disabled."""
    cfg = settings.get("plugins.apollo", {}) or {}
    stats = {"enabled": bool(cfg.get("enabled")), "companies": 0, "lookups": 0, "errors": 0}
    key = settings.env(cfg.get("api_key_env", "APOLLO_API_KEY"))
    client = ApolloClient(key, cfg.get("base_url", "https://api.apollo.io/api/v1"), http) if (cfg.get("enabled") and key) else None
    if cfg.get("enabled") and not key:
        log.warning("Apollo plug-in enabled but %s is not set; skipping", cfg.get("api_key_env", "APOLLO_API_KEY"))
    titles = cfg.get("titles") or ["logistics manager", "transportation manager", "supply chain manager"]
    limit = int(cfg.get("max_contacts_per_company", 5))
    max_new = int(cfg.get("max_companies_per_run", 25))
    seen: set[str] = set()
    for lead in leads:
        ck = norm_key(lead.company)[:80]
        domain = db.domain_get(ck)
        contacts = [dict(r) for r in db.contacts_get(ck)]
        if domain is None and client is not None and ck not in seen and stats["lookups"] < max_new and (lead.is_new or not contacts):
            seen.add(ck)
            stats["lookups"] += 1
            try:
                domain = client.find_domain(lead.company, lead.company_state)
                db.domain_put(ck, lead.company, domain, "apollo")
                if domain:
                    found = client.find_contacts(domain, titles, limit)
                    for c in found:
                        c.setdefault("source", "apollo")
                    # keep what another plug-in (SAM.gov) already found for this company; Apollo fills the gaps
                    contacts = [c for c in contacts if c.get("source") and c["source"] != "apollo"] + found
                    db.contacts_put(ck, contacts, "apollo")
                db.commit()
            except Exception as exc:  # a failed lookup must not stop the run
                stats["errors"] += 1
                log.warning("Apollo lookup failed for %s: %s", lead.company, exc)
        lead.domain = domain
        lead.contacts = contacts
        if domain or contacts:
            stats["companies"] += 1
    return stats
