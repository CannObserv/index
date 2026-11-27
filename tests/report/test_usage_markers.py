"""Drift tests for ``docs/usage-markers.json``, the usage markers log (#35).

One entry per **deliberate intervention** aimed at index usage (telemetry
design, § Markers): policy wording, the reminder or health hook, a skill change
made for the purpose, and the start of collection on a host. Routine daily
skills bumps are not markers. ``docs/usage-markers.md`` is the convention.

Each entry says **where it reached, and when**: a skill change merged upstream
reaches a repo only at that repo's next vendor bump, so the merge date alone
misdates it. Reach is by ``repo`` (a cohort repo, ``deploy/cohort.json``) or,
for collection, by ``host``. ``scripts/usage_report.py`` splits a host's or
repo's periods at its reach, and leaves a repo with no reach out of the split.
"""

import json
import re
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MARKERS = REPO_ROOT / "docs" / "usage-markers.json"
CONVENTION = REPO_ROOT / "docs" / "usage-markers.md"
COHORT = REPO_ROOT / "deploy" / "cohort.json"
WHERE = {"collection", "skill", "policy", "hook"}
UTC = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d{1,9})?Z")


def _markers() -> list[dict]:
    doc = json.loads(MARKERS.read_text())
    assert set(doc) == {"markers"}, set(doc)
    return doc["markers"]


def _when(text: str) -> datetime:
    assert UTC.fullmatch(text), f"not a UTC timestamp ending Z: {text!r}"
    return datetime.fromisoformat(text)


def test_each_entry_has_exactly_the_fields_of_the_convention() -> None:
    for entry in _markers():
        assert set(entry) == {"id", "date", "what", "where", "source", "reach"}, entry
        assert re.fullmatch(r"[a-z0-9][a-z0-9-]*", entry["id"]), entry["id"]
        assert entry["where"] in WHERE, entry["where"]
        assert entry["what"].strip() and entry["source"].strip(), entry["id"]
        _when(entry["date"])


def test_ids_are_unique_and_entries_are_in_date_order() -> None:
    markers = _markers()
    ids = [m["id"] for m in markers]
    assert len(ids) == len(set(ids)), ids
    dates = [_when(m["date"]) for m in markers]
    assert dates == sorted(dates)


def test_reach_names_one_cohort_repo_or_one_host_no_earlier_than_the_change() -> None:
    repos = {r["repo"] for r in json.loads(COHORT.read_text())["repos"]}
    for entry in _markers():
        assert isinstance(entry["reach"], list), entry["id"]
        seen = set()
        for reach in entry["reach"]:
            assert set(reach) in ({"repo", "at"}, {"host", "at"}), reach
            if "repo" in reach:
                assert reach["repo"] in repos, f"{reach['repo']} isn't in deploy/cohort.json"
            else:
                assert re.fullmatch(r"[a-z0-9][a-z0-9-]*", reach["host"]), reach
            key = ("repo", reach["repo"]) if "repo" in reach else ("host", reach["host"])
            assert key not in seen, f"{entry['id']} reaches {key} twice"
            seen.add(key)
            assert _when(reach["at"]) >= _when(entry["date"]), (entry["id"], reach)


def test_the_first_entry_is_collection_on_co_index() -> None:
    """#34 posted it on #35: the first pilot event on disk."""
    first = _markers()[0]
    assert first["where"] == "collection"
    assert first["date"] == "2026-11-23T20:00:33.982Z"
    assert first["reach"] == [{"host": "co-index", "at": "2026-11-23T20:00:33.982Z"}]


def test_the_convention_is_documented_and_linked_from_agents_md() -> None:
    text = CONVENTION.read_text()
    for term in ("usage-markers.json", "deliberate", "reach", "daily", "operator"):
        assert term in text, term
    assert "docs/usage-markers.md" in (REPO_ROOT / "AGENTS.md").read_text()
