"""Feature 12: every evidence path in docs/ps_compliance.md exists, every row has an allowed status,
and no row claims more than the artifacts support (validation rows can't be IMPLEMENTED without
an observed dataset)."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "ps_compliance.md"
STATUSES = {"IMPLEMENTED", "PARTIAL", "BLOCKED", "NOT IMPLEMENTED"}


def _rows():
    text = DOC.read_text(encoding="utf-8")
    block = text.split("<!-- matrix:start -->", 1)[1].split("<!-- matrix:end -->", 1)[0]
    rows = []
    for line in block.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 5 and cells[0].isdigit():
            rows.append(cells)
    return rows


def test_matrix_has_rows_with_allowed_statuses():
    rows = _rows()
    assert len(rows) >= 10
    for row in rows:
        assert row[3] in STATUSES, row


def test_every_evidence_path_exists():
    for row in _rows():
        paths = re.findall(r"`([^`]+)`", row[2])
        assert paths, f"row {row[0]} has no evidence path"
        for p in paths:
            assert (ROOT / p).exists(), f"row {row[0]}: missing evidence {p}"


def test_validation_is_not_claimed_without_observed_data():
    observed = list((ROOT / "data").glob("*/observed/flood_extent_*.geojson")) if (ROOT / "data").is_dir() else []
    for row in _rows():
        if "validation" in row[1].lower() and not observed:
            assert row[3] != "IMPLEMENTED", row
