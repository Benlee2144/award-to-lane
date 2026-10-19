"""Discovery for USDA AMS purchase awards.

The PPP portal (portal.wbscm.usda.gov/ppp) is a React app, but it is fed by an unauthenticated JSON
endpoint at /ppp/search. One GET with awdDt=YYYY-MM-DD returns every solicitation awarded since that
date, each with its document list; the award PDF is the item whose docType is "PCA". No HTML scraping
and no browser automation are needed. www.ams.usda.gov/selling-food/solicitations only links master
solicitations and schedules (and blocks non-browser user agents), so it is not polled.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote

from ...db import Database, now_iso
from ...http import PoliteSession

SOURCE = "usda_pca"
log = logging.getLogger("award_to_lane.discover")


class PppClient:
    def __init__(self, http: PoliteSession, search_url: str, doc_cat: str = "Solicitation"):
        self.http = http
        self.search_url = search_url
        self.doc_cat = doc_cat

    def _search(self, **params: str) -> list[dict]:
        data = self.http.get_json(self.search_url, params={"docCat": self.doc_cat, "latest": "true", "active": "false", **params})
        if not isinstance(data, list):
            raise ValueError(f"unexpected PPP response: {str(data)[:200]}")
        return data

    def awarded_since(self, since: date) -> list[dict]:
        return self._search(awdDt=since.isoformat())

    def published_since(self, since: date) -> list[dict]:
        return self._search(pubDt=since.isoformat())


def safe_name(text: str | None) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text or "unknown").strip("_") or "unknown"


def discover(db: Database, http: PoliteSession, settings, run_date: date, *, force_backfill: bool = False,
             download: bool = True) -> dict:
    cfg = settings.get("sources.usda_pca", {}) or {}
    client = PppClient(http, cfg.get("search_url", "https://portal.wbscm.usda.gov/ppp/search"), cfg.get("doc_cat", "Solicitation"))
    first_run = db.count_documents(SOURCE) == 0
    days = int(cfg.get("backfill_days", 365)) if (force_backfill or first_run) else int(cfg.get("lookback_days", 30))
    since = run_date - timedelta(days=days)
    stats = {"query_since": since.isoformat(), "records": 0, "new_documents": 0, "new_pca": 0, "downloaded": 0,
             "download_failed": 0}
    records = client.awarded_since(since)
    stats["records"] = len(records)
    for rec in records:
        items = ((rec.get("docLinks") or {}).get("items")) or []
        rec_meta = json.dumps({k: v for k, v in rec.items() if k != "docLinks"})
        for item in items:
            url = item.get("url")
            if not url:
                continue
            _, is_new = db.upsert_document(
                source=SOURCE, url=url, first_seen_date=run_date.isoformat(), award_key=rec.get("pii"), sol_no=rec.get("solNo"),
                doc_type=item.get("docType"), file_name=item.get("name"), posted_date=item.get("updDt"),
                prod_grp=rec.get("prodGrp"), prod_subcat=rec.get("prodSubcat"), offer_due_date=rec.get("offDt"),
                record_json=rec_meta)
            if is_new:
                stats["new_documents"] += 1
                if item.get("docType") == "PCA":
                    stats["new_pca"] += 1
    db.commit()
    log.info("PPP search since %s: %d records, %d new documents (%d award PDFs)", since, len(records),
             stats["new_documents"], stats["new_pca"])
    if download:
        stats.update(download_pending(db, http, settings))
    return stats


def download_pending(db: Database, http: PoliteSession, settings) -> dict:
    pdf_dir: Path = settings.path("paths.pdf_cache", "data/pdfs")
    downloaded = failed = 0
    for row in db.documents_to_download(SOURCE, "PCA"):
        dest = pdf_dir / safe_name(row["award_key"]) / safe_name(row["file_name"] or "award.pdf")
        try:
            sha = http.download(quote(row["url"], safe=":/%?=&"), dest, expect_pdf=True)
        except Exception as exc:  # one bad download must not stop the run
            failed += 1
            log.warning("download failed for %s: %s", row["file_name"], exc)
            db.update_document(row["id"], parse_error=f"download failed: {exc}")
        else:
            downloaded += 1
            try:
                stored = str(dest.relative_to(settings.root))
            except ValueError:
                stored = str(dest)
            db.update_document(row["id"], local_path=stored, sha256=sha, downloaded_at=now_iso(), parse_status="pending",
                               parse_error=None)
            log.info("downloaded %s (%s)", row["file_name"], row["prod_subcat"])
        db.commit()
    return {"downloaded": downloaded, "download_failed": failed}
