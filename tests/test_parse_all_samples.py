import pytest

from conftest import SAMPLES, parse_sample

SAP_FILES = sorted(p.name for p in SAMPLES.glob("*.pdf") if "0175" not in p.name)


@pytest.mark.parametrize("name", SAP_FILES)
def test_every_sap_sample_reconciles(name, sample_index):
    award = parse_sample(name, sample_index)
    assert award.family == "sap_details"
    assert award.parse_status == "ok", award.warnings
    assert award.contracts
    rows = sum(li.qty or 0 for c in award.contracts for li in c.line_items)
    assert award.total_qty is not None and abs(rows - award.total_qty) < 0.01
    assert not award.unparsed_lines
    for c in award.contracts:
        assert c.contractor_name and c.contract_no and c.plant_city
        got = sum(li.qty or 0 for li in c.line_items)
        assert c.total_qty is not None and abs(got - c.total_qty) < 0.01
    if not award.is_international:
        assert award.delivery_start and award.delivery_end
        for c in award.contracts:
            for li in c.line_items:
                # a few awards use a destination code (e.g. "O-JACK") instead of a city: it must be flagged, never dropped
                assert li.dest_state or ("dest_state_missing" in li.flags and "-" in li.dest_raw)


def test_older_report_version_with_price_on_same_line(sample_index):
    award = parse_sample("PCA_-2000011271.pdf", sample_index)
    assert award.parse_status == "ok" and award.total_qty == 12757776.12
    assert sum(len(c.line_items) for c in award.contracts) == 401
    assert all(li.price is not None for c in award.contracts for li in c.line_items)


def test_international_award_is_flagged(sample_index):
    award = parse_sample("12-1NFP-26-B-0022-0001-PCA.pdf", sample_index)
    assert award.is_international and award.total_uom == "MT"
    assert all("dest_state_missing" in li.flags for c in award.contracts for li in c.line_items)


def test_blanket_award(sample_index):
    award = parse_sample("12-3J14-26-B-0175-0002-PCA.pdf", sample_index)
    assert award.family == "blanket" and award.is_blanket
    names = [c.contractor_name for c in award.contracts]
    assert "Burnette Foods Inc" in names and "Seneca Foods" in names
    burnette = award.contracts[names.index("Burnette Foods Inc")]
    assert (burnette.contract_no, burnette.contractor_city, burnette.contractor_state) == ("4400002570", "Elk Rapids", "MI")
    assert len(burnette.line_items) == 3 and all("no_destination" in li.flags for li in burnette.line_items)
    assert award.total_dollars == 2332075.30 and award.total_uom == "CS"


def test_generic_and_scanned(sample_index):
    potatoes = parse_sample("2000011198__BulkPotatoesForFurtherProcessing_PCAReport_Final.pdf", sample_index)
    assert potatoes.family == "generic" and potatoes.parse_status == "warnings"
    assert potatoes.delivery_start.isoformat() == "2026-10-01"
    holm = potatoes.contracts[0]
    assert holm.contractor_name.startswith("HOLM") and holm.contractor_city == "Idaho Falls"
    assert [(li.dest_city, li.dest_state) for li in holm.line_items][:2] == [("Pasco", "WA"), ("Blackfoot", "ID")]
    cancel = parse_sample("12-3J14-26-B-0283-PCA_Cancellation.pdf", sample_index)
    assert cancel.is_cancelled and not cancel.contracts
