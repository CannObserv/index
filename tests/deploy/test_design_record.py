"""The design record holds every decision, and the guidelines say which (#33).

D21 was reserved for #33 while D22 landed first (#40), so for a while the
record's range had a hole and ``AGENTS.md`` said so. A decision that exists
only in an issue, or a range that names a row the record lacks, sends a
reader to the wrong place.
"""

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DESIGN = REPO_ROOT / "docs" / "plans" / "2026-09-11-shared-qdrant-vm-design.md"
RANGE = re.compile(r"D0–D(\d+)")


def _rows() -> list[int]:
    return [int(n) for n in re.findall(r"^\| \*\*D(\d+)\*\*", DESIGN.read_text(), flags=re.M)]


def test_every_decision_has_one_row() -> None:
    rows = _rows()
    assert sorted(rows) == list(range(max(rows) + 1))


def test_d21_is_the_collector() -> None:
    row = next(ln for ln in DESIGN.read_text().splitlines() if ln.startswith("| **D21**"))
    assert "OTLP" in row and ":4318" in row and "CannObserv/index#33" in row
    assert "no ingest auth" in row.lower()


def test_the_guidelines_name_the_whole_range() -> None:
    last = max(_rows())
    for doc in ("AGENTS.md", "docs/SOCRATICODE.md"):
        text = (REPO_ROOT / doc).read_text()
        assert RANGE.findall(text) == [str(last)], doc
        assert "D21 is #33" not in text, doc


def test_the_manifest_describes_the_whole_range() -> None:
    manifest = json.loads((REPO_ROOT / ".socraticodecontextartifacts.json").read_text())
    [plans] = [a for a in manifest["artifacts"] if a["name"] == "design-plans"]
    assert re.findall(r"D0-D(\d+)", plans["description"]) == [str(max(_rows()))]
