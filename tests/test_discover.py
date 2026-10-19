from datetime import date

from award_to_lane.db import Database
from award_to_lane.sources.usda_pca.discover import SOURCE, discover

RECORD = {"solNo": "2000011515", "pii": "12-3J14-26-B-0410", "offDt": "2026-09-10", "prodGrp": "Poultry",
          "prodSubcat": "POULTRY/EGGS (TURKEY FROZEN)", "updDt": "2026-09-16", "docLinks": {"items": [
              {"name": "12-3J14-26-B-0410-PCA.pdf", "url": "https://portal.wbscm.usda.gov/ppp/doc/Solicitation/12-3J14-26-B-0410/12-3J14-26-B-0410-PCA.pdf",
               "docType": "PCA", "updDt": "2026-09-16"},
              {"name": "12-3J14-26-B-0410.pdf", "url": "https://portal.wbscm.usda.gov/ppp/doc/Solicitation/12-3J14-26-B-0410/12-3J14-26-B-0410.pdf",
               "docType": "Solicitation Preview", "updDt": "2026-09-10"}]}}


class FakeHttp:
    def __init__(self):
        self.calls = []

    def get_json(self, url, params=None):
        self.calls.append((url, params))
        return [RECORD]


def test_discover_records_documents_once(settings):
    db = Database(":memory:")
    http = FakeHttp()
    stats = discover(db, http, settings, date(2026, 10, 19), download=False)
    assert stats["records"] == 1 and stats["new_documents"] == 2 and stats["new_pca"] == 1
    assert http.calls[0][1]["awdDt"] == "2025-10-19"       # first run uses the backfill window
    stats2 = discover(db, http, settings, date(2026, 10, 20), download=False)
    assert stats2["new_documents"] == 0
    assert http.calls[1][1]["awdDt"] == "2026-09-20"       # later runs use the lookback window
    rows = db.documents_to_download(SOURCE, "PCA")
    assert len(rows) == 1 and rows[0]["award_key"] == "12-3J14-26-B-0410" and rows[0]["first_seen_date"] == "2026-10-19"
