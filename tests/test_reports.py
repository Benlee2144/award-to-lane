import csv
from datetime import date

from award_to_lane.report.apollo_import import write_apollo_import
from award_to_lane.report.call_sheet import write_csvs, write_html
from award_to_lane.report.emails import draft_email, write_emails
from award_to_lane.report.summary import render_summary
from award_to_lane.score import score_lead
from test_lanes_score import TODAY, make_leads


def test_outputs(tmp_path, turkey, canned_veg, settings):
    leads = make_leads(turkey, settings) + make_leads(canned_veg, settings, first_seen=date(2026, 10, 1))
    for l in leads:
        l.score = score_lead(l, TODAY, settings)
    html_path = write_html(leads, tmp_path, TODAY, {"discover": {}, "parse": {}})
    html = html_path.read_text(encoding="utf-8")
    assert "CARGILL MEAT SOLUTIONS CORPORATION" in html and "Omaha, NE" in html and "NEW" in html
    sheet, lanes = write_csvs(leads, tmp_path)
    rows = list(csv.DictReader(sheet.open(encoding="utf-8")))
    assert len(rows) == len(leads) and rows[0]["company"] and rows[0]["truckloads"]
    lane_rows = list(csv.DictReader(lanes.open(encoding="utf-8")))
    assert len(lane_rows) == sum(l.destinations for l in leads)
    n = write_emails(leads, tmp_path)
    assert n == sum(1 for l in leads if l.is_new) == 2
    body = draft_email(leads[0])
    assert "12-3J14-26-B-0410" in body and "[QUOTE" in body and "November 01 through November 15, 2026" in body
    imp = write_apollo_import(leads, tmp_path)
    companies = list(csv.DictReader(imp.open(encoding="utf-8")))
    assert {c["Company"] for c in companies} == {l.company for l in leads}
    text = render_summary(TODAY, {"discover": {}, "parse": {}}, leads, tmp_path)
    assert "active leads" in text and text.isascii()
