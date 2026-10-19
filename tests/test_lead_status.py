"""Lead status tracking: CSV merge rules, sorting, email suppression, the sheet, persistence across days, CLI round trip."""
import copy
import csv
import sys
from datetime import date, timedelta

import pytest

from award_to_lane import lead_status
from award_to_lane.db import Database
from award_to_lane.lanes import build_lead
from award_to_lane.pipeline import sort_leads
from award_to_lane.report.call_sheet import write_csvs, write_html
from award_to_lane.report.emails import write_emails
from award_to_lane.report.summary import render_summary
from conftest import ROOT
from test_lanes_score import TODAY, make_leads

sys.path.insert(0, str(ROOT))
import run_daily  # noqa: E402

KEY = ("12-3J14-26-B-0410", "12-3J14-26-C-0001")


def write_status_csv(path, rows, header=lead_status.CSV_COLUMNS):
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
    return path


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


# -- vocabulary ---------------------------------------------------------------------------

def test_normalize_status():
    assert lead_status.normalize_status(" Booked ") == "booked"
    assert lead_status.normalize_status("skipped") == "skip" and lead_status.normalize_status("won") == "booked"
    assert lead_status.normalize_status("maybe") is None and lead_status.normalize_status(None) is None
    assert lead_status.normalize_contract("-") == "" and lead_status.normalize_contract(" 12-X ") == "12-X"


def test_parse_timestamp():
    assert lead_status.parse_timestamp("2026-10-19") < lead_status.parse_timestamp("2026-10-19T08:00:00+00:00")
    assert lead_status.parse_timestamp("2026-10-19T08:00:00Z") == lead_status.parse_timestamp("2026-10-19T08:00:00+00:00")
    assert lead_status.parse_timestamp("") is None and lead_status.parse_timestamp("yesterday") is None


# -- CSV merge rules ------------------------------------------------------------------------

def test_merge_inserts_rows_and_file_wins_on_conflict(db, tmp_path):
    db.lead_status_put(*KEY, "contacted", "left vm", "pat", "2026-10-18T09:00:00+00:00")
    path = write_status_csv(tmp_path / "s.csv", [
        [*KEY, "lost", "went with incumbent", "sam", ""],                 # same key, no timestamp: the file wins
        ["12-3J14-26-B-0433-0001", "12-3J14-26-C-0007", "Quoted", "", "", "2026-10-19"],
    ])
    assert lead_status.merge_csv(db, path) == 2
    row = db.lead_status_get(*KEY)
    assert (row["status"], row["note"], row["rep"]) == ("lost", "went with incumbent", "sam")
    assert db.lead_status_get("12-3J14-26-B-0433-0001", "12-3J14-26-C-0007")["status"] == "quoted"
    assert lead_status.merge_csv(db, path) == 0                           # idempotent: nothing changed


def test_merge_keeps_newer_table_row(db, tmp_path):
    db.lead_status_put(*KEY, "quoted", "", "pat", "2026-10-20T15:00:00+00:00")   # set by the CLI after the file was edited
    path = write_status_csv(tmp_path / "s.csv", [[*KEY, "contacted", "", "", "2026-10-19"]])
    assert lead_status.merge_csv(db, path) == 0
    assert db.lead_status_get(*KEY)["status"] == "quoted"
    write_status_csv(path, [[*KEY, "booked", "PO received", "", "2026-10-21"]])  # file edited later: file wins again
    assert lead_status.merge_csv(db, path) == 1
    row = db.lead_status_get(*KEY)
    assert row["status"] == "booked" and row["updated_at"] == "2026-10-21"


def test_merge_same_timestamp_file_wins(db, tmp_path):
    db.lead_status_put(*KEY, "contacted", "", "", "2026-10-19")
    path = write_status_csv(tmp_path / "s.csv", [[*KEY, "skip", "", "", "2026-10-19"]])
    assert lead_status.merge_csv(db, path) == 1 and db.lead_status_get(*KEY)["status"] == "skip"


def test_duplicate_keys_in_file_newest_wins(tmp_path):
    path = write_status_csv(tmp_path / "s.csv", [
        [*KEY, "booked", "", "", "2026-10-12"],
        [*KEY, "contacted", "", "", "2026-10-10"],                        # older row listed later still loses
        ["A", "-", "contacted", "first", "", ""],
        ["A", "", "quoted", "second", "", ""],                            # no timestamps: the later row wins
    ])
    rows = lead_status.read_csv(path)
    assert rows[KEY]["status"] == "booked"
    assert rows[("A", "")]["status"] == "quoted" and rows[("A", "")]["note"] == "second"


def test_bad_rows_are_skipped_and_missing_file_is_fine(db, tmp_path, caplog):
    assert lead_status.merge_csv(db, tmp_path / "absent.csv") == 0
    path = write_status_csv(tmp_path / "s.csv", [
        [*KEY, "maybe", "", "", ""],                                      # unknown status
        ["", "C", "lost", "", "", ""],                                    # no key
        ["B", "C", "lost", "", "", "not a date"],                         # bad timestamp: kept, timestamp dropped
    ], header=["Award_Key", " contract_no", "STATUS", "note", "rep", "updated_at"])  # header case/space tolerant
    assert lead_status.merge_csv(db, path) == 1
    assert db.lead_status_get("B", "C")["status"] == "lost"
    assert "line 2 skipped" in caplog.text and "line 3 skipped" in caplog.text and "unreadable updated_at" in caplog.text


# -- leads: attach, persistence, sorting ---------------------------------------------------

def test_attach_and_status_survives_across_days(db, turkey, settings):
    cargill, vpg = make_leads(turkey, settings)
    db.lead_status_put(cargill.award_key, cargill.contract_no, "quoted", "4 lanes sent", "pat")
    counts = lead_status.attach([cargill, vpg], db)
    assert (cargill.status, cargill.status_note, cargill.status_rep) == ("quoted", "4 lanes sent", "pat")
    assert vpg.status == "new" and vpg.status_note is None and counts["quoted"] == 1 and counts["new"] == 1
    later = TODAY + timedelta(days=9)                                    # the sheet rebuilt on another day
    rebuilt = build_lead(turkey, turkey.contracts[0], source="usda_pca", posted_date=date(2026, 10, 18), first_seen=TODAY,
                         pdf_url=None, geocoder=None, settings=settings, today=later)
    assert not rebuilt.is_new
    lead_status.attach([rebuilt], db)
    assert rebuilt.status == "quoted" and rebuilt.status_note == "4 lanes sent"


def test_sort_order(turkey, settings):
    base = make_leads(turkey, settings)[0]

    def lead(name, *, status, is_new=False, opens=20, score=50.0):
        l = copy.deepcopy(base)
        l.company, l.status, l.is_new, l.days_until_open, l.score = name, status, is_new, opens, score
        return l

    leads = [
        lead("lost", status="lost", opens=1, score=99),
        lead("untouched-10", status="new", opens=10, score=80),
        lead("contacted-5", status="contacted", opens=5, score=95),
        lead("new-low", status="new", is_new=True, score=20),
        lead("skip", status="skip", opens=0, score=99),
        lead("untouched-5", status="new", opens=5, score=10),
        lead("quoted", status="quoted", opens=40, score=5),
        lead("new-high", status="new", is_new=True, score=70),
        lead("booked", status="booked", opens=60, score=1),
        lead("new-but-contacted", status="contacted", is_new=True, opens=13, score=100),
    ]
    order = [l.company for l in sort_leads(leads)]
    assert order == ["booked", "quoted", "new-high", "new-low",                 # top group: booked, quoted, NEW by score
                     "untouched-5", "contacted-5", "untouched-10", "new-but-contacted",  # by days until open, contacted after untouched
                     "skip", "lost"]                                            # dead last, same ordering


# -- outputs -----------------------------------------------------------------------------

def test_emails_skip_touched_leads(turkey, settings, tmp_path):
    cargill, vpg = make_leads(turkey, settings)
    assert cargill.is_new and vpg.is_new
    assert write_emails([cargill, vpg], tmp_path) == 2
    cargill.status = "lost"
    assert write_emails([cargill, vpg], tmp_path) == 1                 # the same-day rerun drops yesterday's draft too
    names = [p.name for p in (tmp_path / "emails").iterdir()]
    assert len(names) == 1 and "virginia" in names[0] and "cargill" not in names[0]
    for status in ("contacted", "quoted", "booked", "skip"):
        vpg.status = status
        assert write_emails([vpg], tmp_path / status) == 0
    vpg.status = "new"
    assert write_emails([vpg], tmp_path / "again") == 1


def test_call_sheet_shows_status(turkey, settings, tmp_path):
    cargill, vpg = make_leads(turkey, settings)
    cargill.status, cargill.status_note, cargill.status_rep, cargill.status_updated_at = "quoted", "sent 4 lanes <today>", "pat", "2026-10-19T10:00:00+00:00"
    vpg.status, vpg.status_note = "lost", None
    html = write_html([cargill, vpg], tmp_path, TODAY, {"discover": {}, "parse": {}}).read_text(encoding="utf-8")
    assert ">Status</th>" in html and "id='hidedead'" in html and "hide lost/skipped" in html
    assert "<span class='tag st st-quoted' title='sent 4 lanes &lt;today&gt; &middot; pat &middot; 2026-10-19'>quoted</span>" in html
    assert "class='lead new dead' data-status='lost'" in html and "st-lost" in html
    assert "class='lead new' data-status='quoted'" in html
    assert html.count("colspan='16'") == 2
    sheet, _ = write_csvs([cargill, vpg], tmp_path)
    rows = list(csv.DictReader(sheet.open(encoding="utf-8")))
    assert [r["status"] for r in rows] == ["quoted", "lost"]
    assert rows[0]["status_note"] == "sent 4 lanes <today>" and rows[0]["status_rep"] == "pat" and rows[0]["status_updated_at"] == "2026-10-19"
    text = render_summary(TODAY, {"discover": {}, "parse": {}}, [cargill, vpg], tmp_path)
    assert "0 booked, 1 quoted, 0 contacted, 1 lost, 0 skip" in text


# -- CLI --------------------------------------------------------------------------------

def make_config(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("paths:\n  database: status.sqlite\n  lead_status: lead_status.csv\n", encoding="utf-8")
    return cfg


def test_cli_round_trip(tmp_path, turkey, settings, capsys):
    cfg = make_config(tmp_path)
    db = Database(tmp_path / "status.sqlite")                          # put the turkey award on file so the CLI can name the company
    doc_id, _ = db.upsert_document(source="usda_pca", url="http://x/pca.pdf", first_seen_date="2026-10-19", doc_type="PCA")
    db.replace_award(doc_id, "usda_pca", turkey)
    db.commit()
    db.close()
    key = (turkey.award_key, turkey.contracts[0].contract_no)

    assert run_daily.main(["--config", str(cfg), "status"]) == 0
    assert "no lead statuses recorded" in capsys.readouterr().out

    assert run_daily.main(["--config", str(cfg), "status", *key, "quoted", "--note", "sent 4 lanes", "--rep", "pat"]) == 0
    out = capsys.readouterr().out
    assert "quoted" in out and "CARGILL MEAT SOLUTIONS CORPORATION" in out and "sent 4 lanes" in out

    assert run_daily.main(["--config", str(cfg), "status"]) == 0
    listing = capsys.readouterr().out
    assert "quoted" in listing and key[0] in listing and "pat" in listing and "note: sent 4 lanes" in listing

    rows = list(csv.DictReader((tmp_path / "lead_status.csv").open(encoding="utf-8")))   # the file mirrors the table
    assert len(rows) == 1 and rows[0]["status"] == "quoted" and rows[0]["note"] == "sent 4 lanes" and rows[0]["rep"] == "pat"
    assert rows[0]["updated_at"][:10] == date.today().isoformat()

    assert run_daily.main(["--config", str(cfg), "status", *key, "booked"]) == 0            # note kept when not given
    assert "(was quoted)" in capsys.readouterr().out
    db = Database(tmp_path / "status.sqlite")
    row = db.lead_status_get(*key)
    assert row["status"] == "booked" and row["note"] == "sent 4 lanes"
    cargill, vpg = make_leads(turkey, settings)                                             # ... and the next sheet reflects it
    lead_status.attach([cargill, vpg], db, tmp_path / "lead_status.csv")
    assert cargill.status == "booked" and vpg.status == "new"
    db.close()

    assert run_daily.main(["--config", str(cfg), "status", *key, "new", "--note", ""]) == 0   # un-skip / reset clears the note
    assert "(was booked)" in capsys.readouterr().out
    db = Database(tmp_path / "status.sqlite")
    assert db.lead_status_get(*key)["note"] == "" and db.lead_status_get(*key)["status"] == "new"
    db.close()

    assert run_daily.main(["--config", str(cfg), "status", "12-UNKNOWN", "-", "skip"]) == 0  # no contract number, key not on file
    out = capsys.readouterr().out
    assert "no lead with this key on file" in out
    assert run_daily.main(["--config", str(cfg), "status", "12-UNKNOWN", "-"]) == 0
    assert "skip" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        run_daily.main(["--config", str(cfg), "status", *key, "maybe"])
    assert run_daily.main(["--config", str(cfg), "status", key[0]]) == 0                   # award-only filter lists its leads
    assert key[0] in capsys.readouterr().out


def test_cli_file_edit_then_cli_then_file(tmp_path, capsys):
    """A rep edits the file, another uses the CLI, then the file is edited again: the latest action wins each time."""
    cfg = make_config(tmp_path)
    path = write_status_csv(tmp_path / "lead_status.csv", [[*KEY, "contacted", "", "", "2026-10-18"]])
    assert run_daily.main(["--config", str(cfg), "status", *KEY, "quoted"]) == 0            # merges the file, then sets
    assert "(was contacted)" in capsys.readouterr().out
    rows = lead_status.read_csv(path)
    assert rows[KEY]["status"] == "quoted"
    far_future = (date.today() + timedelta(days=1)).isoformat()
    write_status_csv(path, [[*KEY, "lost", "picked a carrier", "", far_future]])             # hand edit, dated after the CLI
    db = Database(tmp_path / "status.sqlite")
    assert lead_status.merge_csv(db, path) == 1 and db.lead_status_get(*KEY)["status"] == "lost"
    db.close()
