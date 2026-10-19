"""One-screen terminal summary printed at the end of a run (ASCII only, so it renders on Windows consoles)."""
from __future__ import annotations

from datetime import date
from pathlib import Path

from ..models import Lead
from ..util import fmt_int, fmt_money


def render_summary(run_date: date, stats: dict, leads: list[Lead], out_dir: Path) -> str:
    new = [l for l in leads if l.is_new]
    d = stats.get("discover", {})
    p = stats.get("parse", {})
    lines = [
        "=" * 78,
        f" AWARD-TO-LANE  {run_date:%Y-%m-%d}",
        "=" * 78,
        f" discovery : {d.get('records', 0)} award records since {d.get('query_since', '?')}, "
        f"{d.get('new_pca', 0)} new award PDFs, {d.get('downloaded', 0)} downloaded, {d.get('download_failed', 0)} failed",
        f" parsing   : {p.get('parsed', 0)} parsed -> {p.get('ok', 0)} ok, {p.get('warnings', 0)} with warnings, {p.get('failed', 0)} failed",
        f" call sheet: {len(leads)} active leads ({len(new)} NEW), {sum(l.destinations for l in leads)} lanes, "
        f"{fmt_int(sum(l.truckloads for l in leads))} est. truckloads, {fmt_money(sum(l.dollars or 0 for l in leads))} award value",
        f" plug-ins  : sam_gov={stats.get('sam_gov', 'off')}  apollo={stats.get('apollo', 'off')}  fmcsa={stats.get('fmcsa', 'off')}"
        f"  pricing={stats.get('pricing', 'stub')}",
        " statuses  : " + ", ".join(f"{sum(1 for l in leads if l.status == s)} {s}" for s in ("booked", "quoted", "contacted", "lost", "skip")),
        f" output    : {out_dir}",
        "-" * 78,
    ]
    top = new[:10] if new else leads[:10]
    lines.append(f" {'TOP NEW LEADS' if new else 'TOP LEADS'} (score / company / plant / commodity / trucks / window opens)")
    for l in top:
        opens = f"in {l.days_until_open}d" if (l.days_until_open or 0) > 0 else ("open" if l.days_until_open is not None else "n/a")
        lines.append(f"  {l.score:5.1f}  {l.company[:28]:28} {(l.plant_city or '?')[:14]:14} {(l.plant_state or ''):2} "
                     f"{l.commodity[:26]:26} {l.truckloads:5d}  {opens}")
    if stats.get("errors"):
        lines.append("-" * 78)
        lines.append(f" ERRORS ({len(stats['errors'])}):")
        lines.extend(f"  - {e}" for e in stats["errors"][:8])
    lines.append("=" * 78)
    return "\n".join(lines)
