"""SQLite persistence. Plain sqlite3, schema created on open, no ORM."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .models import Award

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY, run_date TEXT, started_at TEXT, finished_at TEXT, summary_json TEXT);
CREATE TABLE IF NOT EXISTS documents (
  id INTEGER PRIMARY KEY, source TEXT NOT NULL, award_key TEXT, sol_no TEXT, doc_type TEXT, file_name TEXT,
  url TEXT NOT NULL UNIQUE, posted_date TEXT, prod_grp TEXT, prod_subcat TEXT, offer_due_date TEXT,
  record_json TEXT, local_path TEXT, sha256 TEXT, downloaded_at TEXT, first_seen_date TEXT,
  parse_status TEXT, parse_error TEXT, parsed_at TEXT);
CREATE INDEX IF NOT EXISTS idx_documents_source_type ON documents(source, doc_type);
CREATE TABLE IF NOT EXISTS awards (
  id INTEGER PRIMARY KEY, document_id INTEGER UNIQUE REFERENCES documents(id) ON DELETE CASCADE,
  source TEXT, award_key TEXT, sol_no TEXT, pii TEXT, family TEXT, commodity TEXT, commodity_title TEXT,
  award_date TEXT, delivery_start TEXT, delivery_end TEXT, num_offers INTEGER, total_qty REAL, total_uom TEXT,
  total_dollars REAL, is_international INTEGER, is_blanket INTEGER, is_cancelled INTEGER, parse_status TEXT,
  warnings_json TEXT, unparsed_json TEXT);
CREATE TABLE IF NOT EXISTS contracts (
  id INTEGER PRIMARY KEY, award_id INTEGER REFERENCES awards(id) ON DELETE CASCADE, contract_no TEXT,
  contractor_name TEXT, contractor_address TEXT, contractor_city TEXT, contractor_state TEXT, contractor_zip TEXT,
  business_types TEXT, plant_city TEXT, plant_state TEXT, inv_no TEXT, total_qty REAL, total_uom TEXT,
  total_dollars REAL, flags_json TEXT);
CREATE TABLE IF NOT EXISTS line_items (
  id INTEGER PRIMARY KEY, contract_id INTEGER REFERENCES contracts(id) ON DELETE CASCADE, award_id INTEGER,
  bid_item TEXT, material TEXT, dest_raw TEXT, dest_city TEXT, dest_state TEXT, qty REAL, uom TEXT, price REAL,
  page INTEGER, flags_json TEXT);
CREATE INDEX IF NOT EXISTS idx_line_items_contract ON line_items(contract_id);
CREATE TABLE IF NOT EXISTS geocode_cache (
  key TEXT PRIMARY KEY, lat REAL, lon REAL, matched_name TEXT, source TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS company_domains (
  company_key TEXT PRIMARY KEY, company TEXT, domain TEXT, source TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS company_contacts (
  id INTEGER PRIMARY KEY, company_key TEXT, name TEXT, title TEXT, email TEXT, phone TEXT, linkedin TEXT,
  source TEXT, fetched_at TEXT);
CREATE INDEX IF NOT EXISTS idx_contacts_company ON company_contacts(company_key);
CREATE TABLE IF NOT EXISTS rate_cache (key TEXT PRIMARY KEY, json TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS company_fleet (
  company_key TEXT PRIMARY KEY, company TEXT, fleet_status TEXT, match_quality TEXT, usdot TEXT, legal_name TEXT,
  dba_name TEXT, phy_city TEXT, phy_state TEXT, classdef TEXT, power_units INTEGER, total_drivers INTEGER, mc_number TEXT,
  mcs150_date TEXT, note TEXT, source TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS lead_status (
  award_key TEXT NOT NULL, contract_no TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, note TEXT, rep TEXT,
  updated_at TEXT NOT NULL, PRIMARY KEY (award_key, contract_no));
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Database:
    def __init__(self, path: Path | str):
        path = Path(path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)

    # -- generic -----------------------------------------------------------------
    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, tuple(params)).fetchone()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, tuple(params))

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- documents ---------------------------------------------------------------
    def upsert_document(self, *, source: str, url: str, first_seen_date: str, **fields: Any) -> tuple[int, bool]:
        """Insert a document if its URL is new. Returns (document_id, is_new). Existing rows keep their data
        except for the listing metadata, which is refreshed."""
        row = self.one("SELECT id FROM documents WHERE url = ?", (url,))
        refresh = {k: fields[k] for k in ("posted_date", "prod_grp", "prod_subcat", "offer_due_date", "record_json") if k in fields}
        if row:
            if refresh:
                sets = ", ".join(f"{k} = ?" for k in refresh)
                self.execute(f"UPDATE documents SET {sets} WHERE id = ?", (*refresh.values(), row["id"]))
            return int(row["id"]), False
        cols = ["source", "url", "first_seen_date", *fields.keys()]
        cur = self.execute(
            f"INSERT INTO documents ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
            (source, url, first_seen_date, *fields.values()))
        return int(cur.lastrowid), True

    def update_document(self, doc_id: int, **fields: Any) -> None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE documents SET {sets} WHERE id = ?", (*fields.values(), doc_id))

    def documents_to_download(self, source: str, doc_type: str) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM documents WHERE source = ? AND doc_type = ? AND local_path IS NULL "
                          "ORDER BY posted_date", (source, doc_type))

    def documents_to_parse(self, source: str, doc_type: str, reparse: bool = False) -> list[sqlite3.Row]:
        cond = "" if reparse else " AND (parse_status IS NULL OR parse_status = 'pending')"
        return self.query(f"SELECT * FROM documents WHERE source = ? AND doc_type = ? AND local_path IS NOT NULL{cond} "
                          "ORDER BY posted_date", (source, doc_type))

    def count_documents(self, source: str) -> int:
        row = self.one("SELECT COUNT(*) AS n FROM documents WHERE source = ?", (source,))
        return int(row["n"]) if row else 0

    # -- awards ------------------------------------------------------------------
    def replace_award(self, document_id: int, source: str, award: Award) -> int:
        self.execute("DELETE FROM awards WHERE document_id = ?", (document_id,))
        cur = self.execute(
            "INSERT INTO awards (document_id, source, award_key, sol_no, pii, family, commodity, commodity_title, award_date,"
            " delivery_start, delivery_end, num_offers, total_qty, total_uom, total_dollars, is_international, is_blanket,"
            " is_cancelled, parse_status, warnings_json, unparsed_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (document_id, source, award.award_key, award.sol_no, award.pii, award.family, award.commodity,
             award.commodity_title, _iso(award.award_date), _iso(award.delivery_start), _iso(award.delivery_end),
             award.num_offers, award.total_qty, award.total_uom, award.total_dollars, int(award.is_international),
             int(award.is_blanket), int(award.is_cancelled), award.parse_status, json.dumps(award.warnings),
             json.dumps(award.unparsed_lines[:200])))
        award_id = int(cur.lastrowid)
        for c in award.contracts:
            ccur = self.execute(
                "INSERT INTO contracts (award_id, contract_no, contractor_name, contractor_address, contractor_city,"
                " contractor_state, contractor_zip, business_types, plant_city, plant_state, inv_no, total_qty, total_uom,"
                " total_dollars, flags_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (award_id, c.contract_no, c.contractor_name, c.contractor_address, c.contractor_city, c.contractor_state,
                 c.contractor_zip, c.business_types, c.plant_city, c.plant_state, c.inv_no, c.total_qty, c.total_uom,
                 c.total_dollars, json.dumps(c.flags)))
            contract_id = int(ccur.lastrowid)
            self.conn.executemany(
                "INSERT INTO line_items (contract_id, award_id, bid_item, material, dest_raw, dest_city, dest_state, qty,"
                " uom, price, page, flags_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [(contract_id, award_id, li.bid_item, li.material, li.dest_raw, li.dest_city, li.dest_state, li.qty,
                  li.uom, li.price, li.page, json.dumps(li.flags)) for li in c.line_items])
        return award_id

    def awards_with_documents(self, source: str) -> list[sqlite3.Row]:
        return self.query(
            "SELECT a.*, d.posted_date, d.prod_grp, d.prod_subcat, d.url AS pdf_url, d.first_seen_date, d.file_name "
            "FROM awards a JOIN documents d ON d.id = a.document_id WHERE a.source = ?", (source,))

    def contracts_for_award(self, award_id: int) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM contracts WHERE award_id = ? ORDER BY id", (award_id,))

    def line_items_for_contract(self, contract_id: int) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM line_items WHERE contract_id = ? ORDER BY id", (contract_id,))

    # -- caches ------------------------------------------------------------------
    def geocode_get(self, key: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM geocode_cache WHERE key = ?", (key,))

    def geocode_put(self, key: str, lat: float | None, lon: float | None, matched_name: str | None, source: str) -> None:
        self.execute("INSERT OR REPLACE INTO geocode_cache (key, lat, lon, matched_name, source, updated_at) VALUES (?,?,?,?,?,?)",
                     (key, lat, lon, matched_name, source, now_iso()))

    def domain_get(self, company_key: str) -> str | None:
        row = self.one("SELECT domain FROM company_domains WHERE company_key = ?", (company_key,))
        return row["domain"] if row else None

    def domain_put(self, company_key: str, company: str, domain: str | None, source: str) -> None:
        self.execute("INSERT OR REPLACE INTO company_domains (company_key, company, domain, source, updated_at) VALUES (?,?,?,?,?)",
                     (company_key, company, domain, source, now_iso()))

    def contacts_get(self, company_key: str) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM company_contacts WHERE company_key = ? ORDER BY id", (company_key,))

    def contacts_put(self, company_key: str, contacts: list[dict], source: str) -> None:
        self.execute("DELETE FROM company_contacts WHERE company_key = ?", (company_key,))
        self.conn.executemany(
            "INSERT INTO company_contacts (company_key, name, title, email, phone, linkedin, source, fetched_at) VALUES (?,?,?,?,?,?,?,?)",
            [(company_key, c.get("name"), c.get("title"), c.get("email"), c.get("phone"), c.get("linkedin"),
              c.get("source") or source, now_iso()) for c in contacts])

    def fleet_get(self, company_key: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM company_fleet WHERE company_key = ?", (company_key,))

    def fleet_put(self, company_key: str, company: str, fleet_status: str, source: str, **fields: Any) -> None:
        """Cache one FMCSA census result per company (also misses, as fleet_status 'no fleet on record' and no record fields)."""
        cols = ["company_key", "company", "fleet_status", "source", "updated_at", *fields.keys()]
        self.execute(f"INSERT OR REPLACE INTO company_fleet ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                     (company_key, company, fleet_status, source, now_iso(), *fields.values()))

    def cache_get(self, key: str, max_age_hours: float) -> Any | None:
        row = self.one("SELECT json, updated_at FROM rate_cache WHERE key = ?", (key,))
        if not row:
            return None
        age = datetime.now(timezone.utc) - datetime.fromisoformat(row["updated_at"])
        if age.total_seconds() > max_age_hours * 3600:
            return None
        return json.loads(row["json"])

    def cache_put(self, key: str, value: Any) -> None:
        self.execute("INSERT OR REPLACE INTO rate_cache (key, json, updated_at) VALUES (?,?,?)", (key, json.dumps(value), now_iso()))

    # -- lead status (what a rep did with a lead; see lead_status.py) ------------
    def lead_status_all(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM lead_status ORDER BY updated_at DESC, award_key, contract_no")

    def lead_status_get(self, award_key: str, contract_no: str | None) -> sqlite3.Row | None:
        return self.one("SELECT * FROM lead_status WHERE award_key = ? AND contract_no = ?", (award_key, contract_no or ""))

    def lead_status_put(self, award_key: str, contract_no: str | None, status: str, note: str | None, rep: str | None,
                        updated_at: str | None = None) -> None:
        self.execute("INSERT OR REPLACE INTO lead_status (award_key, contract_no, status, note, rep, updated_at) VALUES (?,?,?,?,?,?)",
                     (award_key, contract_no or "", status, note or "", rep or "", updated_at or now_iso()))

    def contract_lookup(self, award_key: str, contract_no: str | None) -> sqlite3.Row | None:
        """The company behind a lead key, for the status CLI's confirmation line (None when the key is not on file)."""
        return self.one("SELECT c.contractor_name AS company, c.plant_city, c.plant_state, a.commodity, a.delivery_end "
                        "FROM contracts c JOIN awards a ON a.id = c.award_id WHERE a.award_key = ? AND COALESCE(c.contract_no, '') = ?",
                        (award_key, contract_no or ""))

    # -- runs --------------------------------------------------------------------
    def start_run(self, run_date: str) -> int:
        cur = self.execute("INSERT INTO runs (run_date, started_at) VALUES (?, ?)", (run_date, now_iso()))
        self.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, summary: dict) -> None:
        self.execute("UPDATE runs SET finished_at = ?, summary_json = ? WHERE id = ?", (now_iso(), json.dumps(summary, default=str), run_id))
        self.commit()


def _iso(d) -> str | None:
    return d.isoformat() if d else None
