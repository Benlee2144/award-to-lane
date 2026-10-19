import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from award_to_lane.config import Settings  # noqa: E402
from award_to_lane.sources.usda_pca.parse import parse_pdf  # noqa: E402

SAMPLES = ROOT / "samples"


@pytest.fixture(scope="session")
def settings():
    return Settings.load(ROOT / "config.yaml")


@pytest.fixture(scope="session")
def sample_index():
    return {r["file"]: r for r in json.loads((SAMPLES / "_index.json").read_text())}


def parse_sample(name: str, sample_index: dict):
    m = sample_index.get(name, {})
    meta = {"award_key": m.get("pii"), "pii": m.get("pii"), "sol_no": m.get("solNo"), "prod_subcat": m.get("prodSubcat"),
            "prod_grp": m.get("prodGrp")} if m else {}
    path = SAMPLES / name if (SAMPLES / name).exists() else SAMPLES / "odd" / name
    return parse_pdf(path, meta=meta, international_pii_regex=r"^\d\d-\dN")


@pytest.fixture(scope="session")
def turkey(sample_index):
    return parse_sample("12-3J14-26-B-0410-PCA.pdf", sample_index)


@pytest.fixture(scope="session")
def canned_veg(sample_index):
    return parse_sample("12-3J14-26-B-0433-0001-PCA.pdf", sample_index)
