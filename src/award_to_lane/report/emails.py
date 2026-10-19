"""One drafted outreach email per NEW lead that no rep has touched yet (status new), written as plain text files."""
from __future__ import annotations

from pathlib import Path

from ..models import Lead
from ..util import fmt_int, slugify

TEMPLATE = """To: {to}
Subject: {subject}

Hi {greeting},

Congratulations on the USDA award under solicitation {award_key}{contract_part} for {commodity}.
Since the contract is FOB destination, I wanted to reach out before the delivery window opens
({window}) to see whether you have capacity covered for the {lane_count} {lane_word} out of {plant}.

From the award document I count roughly {truckloads} {equipment} {truck_word} across these destinations:
{lanes}

{quote_placeholder}

If it is useful, I can send a lane-by-lane quote today and hold capacity for the first loads of the window.

Best regards,
[Your name]
[Brokerage] | [phone] | [email]
"""


def draft_email(lead: Lead, top_lanes: int = 5) -> str:
    lanes = lead.lanes[:top_lanes]
    lane_lines = "\n".join(
        f"  - {lead.plant_city or '?'}, {lead.plant_state or ''} -> {l.dest_display}: {l.truckloads} load(s)"
        f"{', ~' + fmt_int(l.miles) + ' mi' if l.miles is not None else ''}" for l in lanes) or "  - (destinations not listed in the award; happy to quote once routing is set)"
    if len(lead.lanes) > top_lanes:
        lane_lines += f"\n  - ...and {len(lead.lanes) - top_lanes} more destination(s)"
    window = (f"{lead.delivery_start:%B %d} through {lead.delivery_end:%B %d, %Y}" if lead.delivery_start and lead.delivery_end
              else "per the award schedule")
    contact = lead.contacts[0] if lead.contacts else None
    return TEMPLATE.format(
        to=(contact.get("email") if contact else None) or f"[logistics contact at {lead.company}]",
        subject=f"Freight capacity for USDA award {lead.award_key} - {lead.commodity[:60]}",
        greeting=(contact.get("name", "").split(" ")[0] if contact and contact.get("name") else "there"),
        award_key=lead.award_key, contract_part=f" (contract {lead.contract_no})" if lead.contract_no else "",
        commodity=lead.commodity, window=window, lane_count=len(lead.lanes) or "upcoming",
        lane_word="lane" if len(lead.lanes) == 1 else "lanes", plant=f"{lead.plant_city or 'your plant'}, {lead.plant_state or ''}".strip(", "),
        truckloads=lead.truckloads or "the", equipment=lead.equipment, truck_word="load" if lead.truckloads == 1 else "loads",
        lanes=lane_lines,
        quote_placeholder="[QUOTE: $X,XXX all-in per load on the top lane / attach lane sheet]")


def write_emails(leads: list[Lead], out_dir: Path, top_lanes: int = 5) -> int:
    folder = out_dir / "emails"
    folder.mkdir(parents=True, exist_ok=True)
    for stale in folder.glob("*.txt"):          # the folder mirrors this run's sheet: a lead marked lost today loses its draft
        stale.unlink()
    count = 0
    for lead in leads:
        if not lead.is_new or lead.status != "new":      # contacted/quoted/booked/lost/skip: the rep is past the intro email
            continue
        name = f"{slugify(lead.award_key, 30)}_{slugify(lead.company, 40)}.txt"
        (folder / name).write_text(draft_email(lead, top_lanes), encoding="utf-8")
        count += 1
    return count
