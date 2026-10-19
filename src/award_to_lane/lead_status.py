"""Lead status tracking: what a rep did with a lead, so the call sheet can show it the next morning.

A status is keyed by award_key + contract_no (the two columns that identify a lead on call_sheet.csv) and is one of
STATUSES. It lives in two places that are merged on every run:

* the ``lead_status`` table in SQLite (db.py), written by ``run_daily.py status ...``;
* ``data/lead_status.csv`` (``paths.lead_status``), a plain file a rep can edit in Excel or Notepad with the same
  columns: award_key, contract_no, status, note, rep, updated_at.

Merge rules (``merge_csv``): the newest ``updated_at`` wins; on a tie, or when the file row has no ``updated_at``,
the file wins. A key listed twice in the file resolves the same way (newest, else the later row). The CLI also
rewrites the file (one row per lead), so the file is a complete, readable record even when the database is lost.

Statuses survive across days because the table and file outlive the run: ``attach`` looks each lead up by key on
every run until the lead drops off the sheet when its delivery window closes.
"""
from __future__ import annotations

import csv
import logging
from datetime import date, datetime, timezone
from pathlib import Path

from .db import Database, now_iso
from .models import Lead

STATUSES = ("new", "contacted", "quoted", "booked", "lost", "skip")
STATUS_ALIASES = {"skipped": "skip", "won": "booked", "dead": "lost"}
TOP_STATUSES = ("booked", "quoted")      # sort at the very top of the sheet, with NEW
DEAD_STATUSES = ("lost", "skip")         # sink to the bottom, greyed out, hideable
CSV_COLUMNS = ["award_key", "contract_no", "status", "note", "rep", "updated_at"]
CONTRACT_PLACEHOLDER = "-"               # CLI spelling for a lead with no contract number

log = logging.getLogger("award_to_lane.lead_status")


def normalize_status(value: str | None) -> str | None:
    """'Booked ' -> 'booked', 'skipped' -> 'skip'; None for anything outside STATUSES."""
    s = (value or "").strip().lower()
    s = STATUS_ALIASES.get(s, s)
    return s if s in STATUSES else None


def normalize_contract(value: str | None) -> str:
    v = (value or "").strip()
    return "" if v == CONTRACT_PLACEHOLDER else v


def parse_timestamp(value: str | None) -> datetime | None:
    """ISO date or date-time from the CSV or the table; naive values are taken as UTC so they compare."""
    v = (value or "").strip()
    if not v:
        return None
    try:
        ts = datetime.fromisoformat(v.replace("Z", "+00:00")) if len(v) > 10 else datetime.combine(date.fromisoformat(v), datetime.min.time())
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


# -- the CSV file ---------------------------------------------------------------------

def read_csv(path: Path) -> dict[tuple[str, str], dict]:
    """One row per lead key from the file, newest updated_at winning (the later row on a tie). Bad rows are logged
    and skipped; a missing file is an empty dict."""
    rows: dict[tuple[str, str], dict] = {}
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            return rows
        reader.fieldnames = [(f or "").strip().lower() for f in reader.fieldnames]
        for n, raw in enumerate(reader, start=2):
            award_key = (raw.get("award_key") or "").strip()
            status = normalize_status(raw.get("status"))
            if not award_key or status is None:
                log.warning("%s line %d skipped: award_key %r status %r", path.name, n, award_key, raw.get("status"))
                continue
            row = {"award_key": award_key, "contract_no": normalize_contract(raw.get("contract_no")), "status": status,
                   "note": (raw.get("note") or "").strip(), "rep": (raw.get("rep") or "").strip(),
                   "updated_at": (raw.get("updated_at") or "").strip()}
            if row["updated_at"] and parse_timestamp(row["updated_at"]) is None:
                log.warning("%s line %d: unreadable updated_at %r ignored", path.name, n, row["updated_at"])
                row["updated_at"] = ""
            key = (row["award_key"], row["contract_no"])
            if key in rows and _newer(rows[key]["updated_at"], row["updated_at"]):
                continue
            rows[key] = row
    return rows


def write_csv(path: Path, rows: dict[tuple[str, str], dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for key in sorted(rows):
            w.writerow({c: rows[key].get(c, "") for c in CSV_COLUMNS})


def _newer(a: str | None, b: str | None) -> bool:
    """True when timestamp a is strictly newer than b (a missing timestamp is never newer)."""
    ta, tb = parse_timestamp(a), parse_timestamp(b)
    return ta is not None and (tb is None or ta > tb)


def _same(row: dict, existing) -> bool:
    return (row["status"] == existing["status"] and (row.get("note") or "") == (existing["note"] or "")
            and (row.get("rep") or "") == (existing["rep"] or ""))


def merge_csv(db: Database, path: Path) -> int:
    """Fold the file into the table. Returns the number of table rows written."""
    written = 0
    for (award_key, contract_no), row in read_csv(path).items():
        existing = db.lead_status_get(award_key, contract_no)
        if existing is not None:
            if row["updated_at"] and _newer(existing["updated_at"], row["updated_at"]):
                continue                                   # set later by the CLI: the table is more recent
            if _same(row, existing):
                continue                                   # nothing to do, keep the table's timestamp
        db.lead_status_put(award_key, contract_no, row["status"], row["note"], row["rep"], row["updated_at"] or None)
        written += 1
    if written:
        db.commit()
        log.info("lead_status.csv: %d status(es) merged into the database", written)
    return written


# -- leads ----------------------------------------------------------------------------

def attach(leads: list[Lead], db: Database, csv_path: Path | None = None) -> dict[str, int]:
    """Merge the file (when given) and stamp every lead with its status. Returns a count per status."""
    if csv_path is not None:
        merge_csv(db, csv_path)
    table = {(r["award_key"], r["contract_no"]): r for r in db.lead_status_all()}
    counts = {s: 0 for s in STATUSES}
    for lead in leads:
        row = table.get((lead.award_key, lead.contract_no or ""))
        if row is None:
            lead.status, lead.status_note, lead.status_rep, lead.status_updated_at = "new", None, None, None
        else:
            lead.status = row["status"]
            lead.status_note, lead.status_rep = row["note"] or None, row["rep"] or None
            lead.status_updated_at = row["updated_at"]
        counts[lead.status] += 1
    return counts


def is_dead(lead: Lead) -> bool:
    return lead.status in DEAD_STATUSES


# -- CLI (run_daily.py status ...) ------------------------------------------------------

def set_status(db: Database, csv_path: Path, award_key: str, contract_no: str | None, status: str, *, note: str | None = None,
               rep: str | None = None, now: str | None = None) -> dict:
    """Record a status in the table and the file. A note that is not given keeps the previous one; '' clears it.
    Returns the stored row plus 'previous' (the old status or None) and 'company' (None when the key is not on file)."""
    normalized = normalize_status(status)
    if normalized is None:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}, not {status!r}")
    award_key, contract_no = award_key.strip(), normalize_contract(contract_no)
    merge_csv(db, csv_path)
    existing = db.lead_status_get(award_key, contract_no)
    if note is None and existing is not None:
        note = existing["note"]
    row = {"award_key": award_key, "contract_no": contract_no, "status": normalized, "note": (note or "").strip(),
           "rep": (rep or "").strip(), "updated_at": now or now_iso()}
    db.lead_status_put(award_key, contract_no, row["status"], row["note"], row["rep"], row["updated_at"])
    db.commit()
    file_rows = read_csv(csv_path)
    file_rows[(award_key, contract_no)] = row
    try:
        write_csv(csv_path, file_rows)
    except OSError as exc:                              # file open in Excel: the table is newer and wins at the next merge
        log.warning("could not update %s (%s); the status is saved in the database", csv_path, exc)
    hit = db.contract_lookup(award_key, contract_no)
    return {**row, "previous": existing["status"] if existing else None, "company": hit["company"] if hit else None}


def list_statuses(db: Database, csv_path: Path, award_key: str | None = None, contract_no: str | None = None) -> list[dict]:
    """Current statuses (file merged first), newest first, with the company name where the key is on file."""
    merge_csv(db, csv_path)
    out = []
    for r in db.lead_status_all():
        if award_key and r["award_key"] != award_key.strip():
            continue
        if contract_no is not None and r["contract_no"] != normalize_contract(contract_no):
            continue
        hit = db.contract_lookup(r["award_key"], r["contract_no"])
        out.append({**dict(r), "company": hit["company"] if hit else None,
                    "delivery_end": hit["delivery_end"] if hit else None})
    return out


def format_rows(rows: list[dict], today: date | None = None) -> str:
    """Fixed-width ASCII listing for the terminal (Windows consoles included)."""
    if not rows:
        return "no lead statuses recorded (run_daily.py status <award_key> <contract_no> <status>)"
    today = today or date.today()
    lines = [f"{'STATUS':9} {'UPDATED':10} {'REP':10} {'AWARD':18} {'CONTRACT':18} COMPANY / NOTE"]
    for r in rows:
        updated = (r.get("updated_at") or "")[:10]
        company = r.get("company") or "(not on file)"
        end = r.get("delivery_end")
        if end and date.fromisoformat(end) < today:
            company += " (window closed)"
        line = f"{r['status']:9} {updated:10} {(r.get('rep') or '')[:10]:10} {r['award_key'][:18]:18} {(r['contract_no'] or '-')[:18]:18} {company[:40]}"
        if r.get("note"):
            line += f"\n{'':50} note: {r['note']}"
        lines.append(line)
    return "\n".join(lines)


def status_command(settings, args, out=print) -> int:
    """argparse handler: no key -> list; key + contract -> show; key + contract + status -> set."""
    db = Database(settings.path("paths.database", "data/award_to_lane.sqlite"))
    csv_path = settings.path("paths.lead_status", "data/lead_status.csv")
    try:
        if args.status:
            if not args.award_key or args.contract_no is None:
                out("usage: run_daily.py status <award_key> <contract_no> <status> [--note ...] [--rep ...]")
                return 2
            rep = args.rep if args.rep is not None else _default_rep()
            r = set_status(db, csv_path, args.award_key, args.contract_no, args.status, note=args.note, rep=rep)
            who = r["company"] or "(no lead with this key on file; check award_key and contract_no on call_sheet.csv)"
            was = f" (was {r['previous']})" if r["previous"] and r["previous"] != r["status"] else ""
            out(f"{r['award_key']} / {r['contract_no'] or '-'}: {r['status']}{was} - {who}")
            if r["note"]:
                out(f"  note: {r['note']}")
            out(f"  saved to the database and {csv_path.name}; the next run reflects it")
            return 0
        out(format_rows(list_statuses(db, csv_path, args.award_key, args.contract_no)))
        return 0
    finally:
        db.close()


def _default_rep() -> str:
    try:
        import getpass
        return getpass.getuser()
    except Exception:  # no login name available (service accounts)
        return ""
