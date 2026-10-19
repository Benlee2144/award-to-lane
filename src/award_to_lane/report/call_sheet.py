"""call_sheet.html, call_sheet.csv and lanes.csv."""
from __future__ import annotations

import csv
import html
from datetime import date
from pathlib import Path

from ..lead_status import is_dead
from ..models import FLEET_FOR_HIRE, FLEET_PRIVATE, FLEET_UNVERIFIED, Lead
from ..util import fmt_int, fmt_money

LEAD_COLUMNS = ["new", "status", "score", "company", "company_city", "company_state", "plant_city", "plant_state", "commodity", "equipment",
                "destinations", "total_qty", "total_uom", "total_pounds", "truckloads", "dollars", "delivery_start", "delivery_end",
                "days_until_open", "days_until_close", "award_date", "posted_date", "num_offers", "award_key", "contract_no",
                "status_note", "status_rep", "status_updated_at", "domain", "fleet_status", "usdot", "power_units", "fleet_note", "flags", "lanes", "pdf_url", "source"]
LANE_COLUMNS = ["award_key", "contract_no", "company", "origin_city", "origin_state", "dest_display", "dest_city", "dest_state",
                "drops", "qty", "uom", "pounds", "pounds_estimated", "truckloads", "equipment", "miles", "rate_low", "rate_high",
                "rate_unit", "rate_source", "flags"]


def lead_row(lead: Lead) -> dict:
    return {"new": "NEW" if lead.is_new else "", "status": lead.status or "new", "score": lead.score, "company": lead.company, "company_city": lead.company_city,
            "company_state": lead.company_state, "plant_city": lead.plant_city, "plant_state": lead.plant_state,
            "commodity": lead.commodity, "equipment": lead.equipment, "destinations": lead.destinations,
            "total_qty": round(lead.total_qty, 3), "total_uom": lead.total_uom, "total_pounds": lead.total_pounds,
            "truckloads": lead.truckloads, "dollars": round(lead.dollars, 2) if lead.dollars else "", "delivery_start": lead.delivery_start,
            "delivery_end": lead.delivery_end, "days_until_open": lead.days_until_open, "days_until_close": lead.days_until_close,
            "award_date": lead.award_date, "posted_date": lead.posted_date, "num_offers": lead.num_offers, "award_key": lead.award_key,
            "contract_no": lead.contract_no, "status_note": lead.status_note or "", "status_rep": lead.status_rep or "",
            "status_updated_at": (lead.status_updated_at or "")[:10], "domain": lead.domain or "", "fleet_status": lead.fleet_status or "",
            "usdot": lead.usdot or "", "power_units": lead.power_units if lead.power_units is not None else "",
            "fleet_note": lead.fleet_note or "", "flags": ";".join(lead.flags),
            "lanes": " | ".join(f"{l.dest_display} x{l.truckloads}" for l in lead.lanes), "pdf_url": lead.pdf_url, "source": lead.source}


def write_csvs(leads: list[Lead], out_dir: Path) -> tuple[Path, Path]:
    sheet = out_dir / "call_sheet.csv"
    with sheet.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=LEAD_COLUMNS)
        w.writeheader()
        for lead in leads:
            w.writerow(lead_row(lead))
    lanes = out_dir / "lanes.csv"
    with lanes.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=LANE_COLUMNS)
        w.writeheader()
        for lead in leads:
            for l in lead.lanes:
                w.writerow({"award_key": lead.award_key, "contract_no": lead.contract_no, "company": lead.company,
                            "origin_city": l.origin_city, "origin_state": l.origin_state, "dest_display": l.dest_display,
                            "dest_city": l.dest_city, "dest_state": l.dest_state, "drops": l.drops, "qty": round(l.qty, 3), "uom": l.uom,
                            "pounds": l.pounds, "pounds_estimated": int(l.pounds_estimated), "truckloads": l.truckloads,
                            "equipment": l.equipment, "miles": l.miles, "rate_low": l.rate.low if l.rate else "",
                            "rate_high": l.rate.high if l.rate else "", "rate_unit": l.rate.unit if l.rate else "",
                            "rate_source": l.rate.source if l.rate else "", "flags": ";".join(l.flags)})
    return sheet, lanes


CSS = """
:root{--ink:#1b2430;--muted:#5d6b7a;--line:#d9e0e7;--new:#0b6e4f;--newbg:#e4f5ee;--warn:#9a5b00;--warnbg:#fff4df;--head:#f3f6f9;--acc:#2156a5}
*{box-sizing:border-box}body{font:14px/1.4 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:var(--ink);margin:24px;background:#fff}
h1{font-size:22px;margin:0 0 4px}.sub{color:var(--muted);margin:0 0 16px}.kpis{display:flex;gap:18px;flex-wrap:wrap;margin:0 0 18px}
.kpi{border:1px solid var(--line);border-radius:8px;padding:8px 14px;min-width:120px}.kpi b{display:block;font-size:20px}.kpi span{color:var(--muted);font-size:12px}
table{border-collapse:collapse;width:100%}th,td{padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top;text-align:left}
th{background:var(--head);font-size:12px;text-transform:uppercase;letter-spacing:.03em;color:var(--muted);position:sticky;top:0}
td.num,th.num{text-align:right;white-space:nowrap}tr.lead.new td{background:var(--newbg)}.tag{display:inline-block;border-radius:4px;padding:1px 6px;font-size:11px;font-weight:600}
.tag.new{background:var(--new);color:#fff}.tag.flag{background:var(--warnbg);color:var(--warn);margin:1px 2px 0 0;font-weight:500}
.tag.fleet{background:#fbe3e3;color:#9b1c1c}.tag.hire{background:#e6eefb;color:var(--acc)}.tag.unsure{background:var(--warnbg);color:var(--warn)}
.tag.st{cursor:default}.tag.st-booked{background:var(--new);color:#fff}.tag.st-quoted{background:#e6eefb;color:var(--acc)}
.tag.st-contacted{background:#eef1f4;color:var(--muted)}.tag.st-lost{background:#fbe3e3;color:#9b1c1c}.tag.st-skip{background:#eef1f4;color:var(--muted);text-decoration:line-through}
tr.lead.dead td,tr.lead.dead+tr.lanes td{opacity:.45}tr.lead.dead td.company,tr.lead.dead .company{font-weight:500}
.score{font-weight:700;color:var(--acc)}.company{font-weight:600}.small{color:var(--muted);font-size:12px}
tr.lanes td{padding:0 8px 10px 8px;background:#fbfcfd}details summary{cursor:pointer;color:var(--acc);font-size:12px;padding:6px 0}
table.lanes{width:auto;min-width:60%;font-size:12px;margin:4px 0 0 18px}table.lanes th{position:static;background:#fff}
.toolbar{margin:0 0 10px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}.toolbar button,.toolbar input[type=text]{font:inherit;padding:5px 10px;border:1px solid var(--line);border-radius:6px;background:#fff}
.toolbar label{font-size:12px;color:var(--muted);display:inline-flex;gap:4px;align-items:center;cursor:pointer}
@media print{body{margin:8mm;font-size:11px}.toolbar{display:none}th{position:static}tr.lead{break-inside:avoid}details summary{display:none}details>*{display:block}}
"""

SCRIPT = """
function toggleAll(open){document.querySelectorAll('details').forEach(d=>d.open=open)}
window.addEventListener('beforeprint',()=>toggleAll(true));
function filterRows(){const q=document.getElementById('q').value.toLowerCase();const hide=document.getElementById('hidedead').checked;
document.querySelectorAll('tr.lead').forEach(tr=>{const show=tr.innerText.toLowerCase().includes(q)&&!(hide&&tr.classList.contains('dead'));tr.style.display=show?'':'none';const ln=tr.nextElementSibling;if(ln)ln.style.display=show?'':'none'})}
"""


def _esc(v) -> str:
    return html.escape("" if v is None else str(v))


def _window(lead: Lead) -> str:
    if not lead.delivery_start:
        return "<span class='small'>not stated</span>"
    return f"{lead.delivery_start:%b %d, %Y} &ndash; {lead.delivery_end:%b %d, %Y}" if lead.delivery_end else f"{lead.delivery_start:%b %d, %Y}"


def _opens(lead: Lead) -> str:
    d = lead.days_until_open
    if d is None:
        return ""
    if d > 0:
        return f"in {d} d"
    close = lead.days_until_close
    return f"open, {close} d left" if close is not None else "open"


CONTACT_SOURCES = {"sam_gov": "SAM.gov", "sam_gov~": "SAM.gov, possible match", "apollo": "Apollo"}


def _contact_line(c: dict) -> str:
    """"Pat Example (Contracts Manager, Govt business POC) [SAM.gov]" - the source tells the rep what kind of contact it is."""
    line = f"{c.get('name')} ({c.get('title')}) {c.get('email') or c.get('phone') or ''}".strip()
    source = c.get("source")
    return f"{line} [{CONTACT_SOURCES.get(source, source)}]" if source else line


FLEET_TAGS = {FLEET_PRIVATE: "fleet", FLEET_FOR_HIRE: "hire", FLEET_UNVERIFIED: "unsure"}


def _fleet_cell(lead: Lead) -> str:
    """Fleet column: the status as a tag (or muted text), then USDOT and power units; the full note is the tooltip."""
    if not lead.fleet_status:
        return ""
    css = FLEET_TAGS.get(lead.fleet_status)
    label = f"<span class='tag {css}'>{_esc(lead.fleet_status)}</span>" if css else f"<span class='small'>{_esc(lead.fleet_status)}</span>"
    detail = " &middot; ".join(p for p in ((f"{lead.power_units} units" if lead.power_units is not None else ""),
                                            (f"USDOT {_esc(lead.usdot)}" if lead.usdot else "")) if p)
    title = f" title='{_esc(lead.fleet_note)}'" if lead.fleet_note else ""
    return f"<div{title}>{label}{('<div class=' + chr(39) + 'small' + chr(39) + '>' + detail + '</div>') if detail else ''}</div>"


def _status_cell(lead: Lead) -> str:
    """Status column: a coloured tag for anything a rep recorded; the note, rep and date are the tooltip."""
    if not lead.status or lead.status == "new":
        return ""
    detail = " &middot; ".join(p for p in (_esc(lead.status_note), _esc(lead.status_rep), _esc((lead.status_updated_at or "")[:10])) if p)
    title = f" title='{detail}'" if detail else ""
    return f"<span class='tag st st-{_esc(lead.status)}'{title}>{_esc(lead.status)}</span>"


def _lane_rows(lead: Lead) -> str:
    rows = []
    for l in lead.lanes:
        rate = f"${l.rate.low:,.0f}-{l.rate.high:,.0f} {l.rate.unit}" if (l.rate and l.rate.low is not None) else ""
        flags = " ".join(f"<span class='tag flag'>{_esc(f)}</span>" for f in l.flags if f not in ("weight_estimated",))
        est = "~" if l.pounds_estimated else ""
        rows.append(f"<tr><td>{_esc(l.dest_display)}</td><td class='num'>{l.drops}</td><td class='num'>{fmt_int(l.qty)} {_esc(l.uom)}</td>"
                    f"<td class='num'>{est}{fmt_int(l.pounds)}</td><td class='num'>{l.truckloads}</td><td class='num'>{fmt_int(l.miles) if l.miles is not None else '?'}</td>"
                    f"<td>{_esc(l.equipment)}</td><td>{rate}</td><td>{flags}</td></tr>")
    return "".join(rows)


def write_html(leads: list[Lead], out_dir: Path, run_date: date, stats: dict) -> Path:
    new = [l for l in leads if l.is_new]
    kpis = [(len(leads), "active leads"), (len(new), "new today"), (sum(1 for l in leads if l.status == "booked"), "booked"),
            (sum(1 for l in leads if l.status == "quoted"), "quoted"), (sum(l.truckloads for l in leads), "est. truckloads"),
            (sum(l.destinations for l in leads), "lanes"), (fmt_money(sum(l.dollars or 0 for l in leads)), "award value on sheet")]
    parts = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>",
             f"<title>Award-to-Lane call sheet {run_date:%Y-%m-%d}</title><style>{CSS}</style></head><body>",
             f"<h1>Award-to-Lane call sheet &mdash; {run_date:%A, %B %d, %Y}</h1>",
             "<p class='sub'>USDA AMS purchase awards whose delivery window has not closed. FOB destination: the awarded company pays the freight. "
             "Booked and quoted leads first, then NEW leads (by score), then everything else by days until the delivery window opens; "
             "lost and skipped leads sink to the bottom, greyed out. Hover a status for the rep's note. "
             "Record a status with <code>run_daily.py status &lt;award_key&gt; &lt;contract_no&gt; &lt;status&gt;</code> or in data/lead_status.csv.</p>",
             "<div class='kpis'>" + "".join(f"<div class='kpi'><b>{_esc(v)}</b><span>{k}</span></div>" for v, k in kpis) + "</div>",
             "<div class='toolbar'><input id='q' type='text' placeholder='filter: company, state, commodity, status...' oninput='filterRows()' size='40'>"
             "<label><input type='checkbox' id='hidedead' onchange='filterRows()'> hide lost/skipped</label>"
             "<button onclick='toggleAll(true)'>Expand all lanes</button><button onclick='toggleAll(false)'>Collapse</button>"
             "<button onclick='window.print()'>Print</button></div>",
             "<table><thead><tr><th></th><th title='What a rep did with the lead (run_daily.py status ...)'>Status</th><th class='num'>Score</th><th>Company</th><th>Plant / origin</th><th>Commodity</th><th>Equip</th>"
             "<th class='num'>Dests</th><th class='num'>Total lb</th><th class='num'>Trucks</th><th>Delivery window</th><th>Opens</th>"
             "<th class='num'>Bids</th><th title='FMCSA census: does the company run its own trucks? (plugins.fmcsa)'>Fleet</th>"
             "<th>Flags</th><th>PDF</th></tr></thead><tbody>"]
    for lead in leads:
        flags = " ".join(f"<span class='tag flag'>{_esc(f)}</span>" for f in lead.flags)
        est = "~" if any(l.pounds_estimated for l in lead.lanes) else ""
        pdf = f"<a href='{_esc(lead.pdf_url)}' target='_blank'>PCA</a>" if lead.pdf_url else ""
        contacts = ""
        if lead.contacts:
            contacts = "<div class='small'>" + "; ".join(_esc(_contact_line(c)) for c in lead.contacts[:3]) + "</div>"
        parts.append(
            f"<tr class='lead{' new' if lead.is_new else ''}{' dead' if is_dead(lead) else ''}' data-status='{_esc(lead.status)}'>"
            f"<td>{'<span class=\"tag new\">NEW</span>' if lead.is_new else ''}</td><td>{_status_cell(lead)}</td>"
            f"<td class='num score'>{lead.score:.0f}</td>"
            f"<td><span class='company'>{_esc(lead.company)}</span><div class='small'>{_esc(lead.company_city or '')}{', ' if lead.company_city and lead.company_state else ''}{_esc(lead.company_state or '')}"
            f"{(' &middot; ' + _esc(lead.domain)) if lead.domain else ''} &middot; {_esc(lead.contract_no or lead.award_key)}</div>{contacts}</td>"
            f"<td>{_esc(lead.plant_city or '?')}, {_esc(lead.plant_state or '')}</td><td>{_esc(lead.commodity)}</td><td>{_esc(lead.equipment)}</td>"
            f"<td class='num'>{lead.destinations}</td><td class='num'>{est}{fmt_int(lead.total_pounds)}</td><td class='num'>{lead.truckloads}</td>"
            f"<td>{_window(lead)}</td><td>{_opens(lead)}</td><td class='num'>{_esc(lead.num_offers if lead.num_offers is not None else '')}</td>"
            f"<td>{_fleet_cell(lead)}</td><td>{flags}</td><td>{pdf}</td></tr>")
        if lead.lanes:
            parts.append(f"<tr class='lanes'><td colspan='16'><details><summary>{len(lead.lanes)} lane(s) from {_esc(lead.plant_city or '?')}, {_esc(lead.plant_state or '')}"
                         f" &mdash; {fmt_int(lead.total_qty)} {_esc(lead.total_uom)} &middot; {fmt_money(lead.dollars)}</summary>"
                         "<table class='lanes'><thead><tr><th>Destination</th><th class='num'>Drops</th><th class='num'>Qty</th><th class='num'>Lb</th>"
                         "<th class='num'>Trucks</th><th class='num'>Miles</th><th>Equip</th><th>Rate ref</th><th></th></tr></thead>"
                         f"<tbody>{_lane_rows(lead)}</tbody></table></details></td></tr>")
        else:
            parts.append("<tr class='lanes'><td colspan='16'><span class='small'>No destinations listed in the award document.</span></td></tr>")
    parts.append("</tbody></table>")
    parts.append(f"<p class='small'>Generated {run_date:%Y-%m-%d}. Discovery: {_esc(stats.get('discover', {}))}. Parse: {_esc(stats.get('parse', {}))}. "
                 "Pounds marked ~ are estimated from pack size. Miles are straight-line distance between city centroids times a road factor.</p>")
    parts.append(f"<script>{SCRIPT}</script></body></html>")
    path = out_dir / "call_sheet.html"
    path.write_text("\n".join(parts), encoding="utf-8")
    return path
