"""The daily run: discover -> parse -> leads -> score -> plug-ins -> outputs -> summary."""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

from .config import Settings
from .contacts import apollo, fmcsa, sam_gov
from .db import Database
from .geo import Gazetteer, Geocoder, NominatimClient
from .http import PoliteSession
from . import lead_status
from .models import Lead
from .pricing.base import apply_rates, get_provider
from .report.apollo_import import write_apollo_import
from .report.call_sheet import write_csvs, write_html
from .report.emails import write_emails
from .report.summary import render_summary
from .score import score_lead
from .sources.base import RunContext, all_sources
from .util import setup_logging


def sort_leads(leads: list[Lead]) -> list[Lead]:
    """Top group: booked, then quoted, then NEW (each by score, highest first). Middle: everything else by days until
    the window opens, a contacted lead after the untouched leads with the same days. Bottom: lost and skipped, in the
    same order (still on the sheet, greyed out, so a rep can un-skip one)."""
    def key(l: Lead):
        status = l.status or "new"
        if status in lead_status.TOP_STATUSES:
            return (0, lead_status.TOP_STATUSES.index(status), 0, -l.score)
        if l.is_new and status == "new":
            return (0, len(lead_status.TOP_STATUSES), 0, -l.score)
        opens = l.days_until_open if l.days_until_open is not None else 10_000
        return (2 if status in lead_status.DEAD_STATUSES else 1, max(opens, 0), 0 if status == "new" else 1, -l.score)
    return sorted(leads, key=key)


def run(settings: Settings, run_date: date | None = None, *, force_backfill: bool = False, download: bool = True,
        reparse: bool = False, skip_discover: bool = False, log_level: int = logging.INFO) -> dict:
    run_date = run_date or date.today()
    log = setup_logging(settings.path("paths.logs", "logs"), log_level)
    log.info("run started for %s (config %s)", run_date, settings.config_path)
    db = Database(settings.path("paths.database", "data/award_to_lane.sqlite"))
    http = PoliteSession(settings.get("http.user_agent", "AwardToLane/0.1"), settings.get("http.min_seconds_between_requests", 3),
                         settings.get("http.timeout_seconds", 60), settings.get("http.max_retries", 3),
                         referer="https://portal.wbscm.usda.gov/ppp")
    geocoder = _make_geocoder(settings, db)
    ctx = RunContext(settings=settings, db=db, http=http, geocoder=geocoder, run_date=run_date, log=log)
    run_id = db.start_run(run_date.isoformat())
    stats: dict = {"run_date": run_date.isoformat(), "discover": {}, "parse": {}, "errors": []}
    leads: list[Lead] = []
    for source in all_sources():
        if not source.enabled(settings):
            log.info("source %s disabled", source.name)
            continue
        if not skip_discover:
            try:
                stats["discover"][source.name] = source.discover(ctx, force_backfill=force_backfill, download=download)
            except Exception as exc:
                log.exception("discovery failed for %s", source.name)
                stats["errors"].append(f"{source.name} discovery: {exc}")
        try:
            stats["parse"][source.name] = source.parse_pending(ctx, reparse=reparse)
        except Exception as exc:
            log.exception("parsing failed for %s", source.name)
            stats["errors"].append(f"{source.name} parse: {exc}")
        try:
            leads.extend(source.build_leads(ctx))
        except Exception as exc:
            log.exception("lead building failed for %s", source.name)
            stats["errors"].append(f"{source.name} leads: {exc}")
    stats["statuses"] = lead_status.attach(leads, db, settings.path("paths.lead_status", "data/lead_status.csv"))
    for lead in leads:
        lead.score = score_lead(lead, run_date, settings)
    leads = sort_leads(leads)

    sam_stats = sam_gov.enrich_leads(leads, db, settings, http)          # free public data first ...
    stats["sam_gov"] = "on" if sam_stats["enabled"] else "off"
    apollo_stats = apollo.enrich_leads(leads, db, settings, http)        # ... so Apollo only fills the gaps
    stats["apollo"] = "on" if apollo_stats["enabled"] else "off"
    fmcsa_stats = fmcsa.enrich_leads(leads, db, settings, http)          # private fleet? (free FMCSA census)
    stats["fmcsa"] = "on" if fmcsa_stats["enabled"] else "off"
    if fmcsa_stats["companies"]:                                         # a confirmed private fleet lowers the score
        for lead in leads:
            lead.score = score_lead(lead, run_date, settings)
        leads = sort_leads(leads)
    provider = get_provider(settings, db, http)
    stats["pricing"] = provider.name
    stats["rates_quoted"] = apply_rates(leads, provider)

    out_dir: Path = settings.path("paths.output", "output") / run_date.isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)
    flat = {"discover": _flatten(stats["discover"]), "parse": _flatten(stats["parse"]), "sam_gov": stats["sam_gov"],
            "apollo": stats["apollo"], "fmcsa": stats["fmcsa"], "pricing": stats["pricing"], "errors": stats["errors"]}
    write_html(leads, out_dir, run_date, flat)
    write_csvs(leads, out_dir)
    write_apollo_import(leads, out_dir)
    stats["emails"] = write_emails(leads, out_dir, int(settings.get("call_sheet.top_lanes_in_email", 5)))
    stats["leads"] = len(leads)
    stats["new_leads"] = sum(1 for l in leads if l.is_new)
    stats["output_dir"] = str(out_dir)
    db.finish_run(run_id, stats)
    summary = render_summary(run_date, flat, leads, out_dir)
    (out_dir / "summary.txt").write_text(summary, encoding="utf-8")
    print(summary)
    log.info("run finished: %d leads (%d new), %d emails", stats["leads"], stats["new_leads"], stats["emails"])
    db.close()
    return stats


def _flatten(per_source: dict) -> dict:
    merged: dict = {}
    for src_stats in per_source.values():
        for k, v in src_stats.items():
            merged[k] = (merged.get(k, 0) + v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v
    return merged


def _make_geocoder(settings: Settings, db: Database) -> Geocoder | None:
    csv_path = settings.path("lanes.geocoder.gazetteer_csv", "data/us_places.csv")
    if not csv_path.exists():
        logging.getLogger("award_to_lane").warning("gazetteer %s missing; miles will be blank", csv_path)
        return None
    nominatim = None
    if settings.get("lanes.geocoder.nominatim_fallback", True):
        nominatim = NominatimClient(settings.get("lanes.geocoder.nominatim_url", "https://nominatim.openstreetmap.org/search"),
                                    settings.get("http.user_agent", "AwardToLane/0.1"),
                                    float(settings.get("lanes.geocoder.nominatim_min_seconds_between_requests", 1.1)))
    return Geocoder(Gazetteer(csv_path), db, nominatim)
