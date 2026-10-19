#!/usr/bin/env python3
"""Award-to-Lane daily entry point.

    python run_daily.py                 # normal daily run
    python run_daily.py --backfill      # re-query the full backfill window (first run does this automatically)
    python run_daily.py --reparse       # re-parse every downloaded PDF (after a parser change)
    python run_daily.py --date 2026-10-19 --skip-discover   # rebuild outputs offline for a date
    python run_daily.py status                              # list lead statuses recorded by reps
    python run_daily.py status 12-3J14-26-B-0410 12-3J14-26-C-0001 quoted --note "sent 4 lanes" --rep pat

Exit code is 0 even when individual PDFs fail; failures are logged and listed in the terminal summary.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from award_to_lane.config import Settings  # noqa: E402
from award_to_lane.lead_status import STATUSES, status_command  # noqa: E402
from award_to_lane.pipeline import run  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Turn USDA purchase awards into a freight-broker call sheet.")
    ap.add_argument("--config", default=str(Path(__file__).resolve().parent / "config.yaml"))
    ap.add_argument("--date", help="run date YYYY-MM-DD (default: today)")
    ap.add_argument("--backfill", action="store_true", help="query the full backfill window instead of the daily lookback")
    ap.add_argument("--reparse", action="store_true", help="re-parse all downloaded PDFs")
    ap.add_argument("--no-download", action="store_true", help="discover and record new award PDFs but do not download them")
    ap.add_argument("--skip-discover", action="store_true", help="do not contact the portal; rebuild outputs from the database")
    ap.add_argument("--verbose", action="store_true")
    sub = ap.add_subparsers(dest="command", metavar="command")
    st = sub.add_parser("status", help="list lead statuses, or set one: status <award_key> <contract_no> <status>",
                        description="Record what a rep did with a lead (keys are on call_sheet.csv). With no arguments, list them.")
    st.add_argument("award_key", nargs="?", help="award key from the sheet, e.g. 12-3J14-26-B-0410")
    st.add_argument("contract_no", nargs="?", help="contract number from the sheet ('-' when the lead has none)")
    st.add_argument("status", nargs="?", choices=STATUSES, help="new (un-skip), contacted, quoted, booked, lost or skip")
    st.add_argument("--note", help="free text shown on hover on the sheet (omit to keep the previous note, '' to clear)")
    st.add_argument("--rep", help="who recorded it (default: your login name)")
    args = ap.parse_args(argv)
    settings = Settings.load(args.config)
    if args.command == "status":
        return status_command(settings, args)
    run_date = date.fromisoformat(args.date) if args.date else date.today()
    try:
        run(settings, run_date, force_backfill=args.backfill, download=not args.no_download, reparse=args.reparse,
            skip_discover=args.skip_discover, log_level=logging.DEBUG if args.verbose else logging.INFO)
    except Exception as exc:  # last-resort guard so Task Scheduler sees a clean failure message
        logging.getLogger("award_to_lane").exception("run aborted")
        print(f"RUN ABORTED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
