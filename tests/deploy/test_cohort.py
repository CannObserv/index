"""Drift tests for ``deploy/cohort.json``, the cohort's one repo list (#38).

The link check (``link-check.py``) reads every cohort repo's committed
``.socraticode.json``, so it needs each repo by name. D17 lists the store's
clients by tailnet tag, and tags and repos differ in places:
``tag:observo-primary`` is the observo repo, ``tag:processor`` is node
``co-processor``. So the list pairs each repo with its tag, and this holds its
tags to D17's row. ``test_acl_doc.py`` holds that row to the peers ``co-index``
sees, so the three agree. ``index`` is listed too, as ``tag:index``: the store
is its own client (D20), and not one of D17's.

**The private repos are hand-listed** (``hand_listed``), never fetched with a
token: ``.env``'s tokens are for issues (D18), and a root unit reading them
would amend it. A hand-listed entry goes stale silently, so it carries when
and where it was read. ``config`` is ``null`` for a repo with no
``.socraticode.json``.

The telemetry design names the private repos. It named ``status``, public
since, until #38, so this holds its line to the list.
"""

import json
import re
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
COHORT = REPO_ROOT / "deploy" / "cohort.json"
DESIGN = REPO_ROOT / "docs" / "plans" / "2026-09-11-shared-qdrant-vm-design.md"
TELEMETRY = REPO_ROOT / "docs" / "plans" / "2026-11-04-index-usage-telemetry-design.md"


def _cohort() -> dict:
    return json.loads(COHORT.read_text())


def _repos() -> list[dict]:
    return _cohort()["repos"]


def _d17_clients() -> set[str]:
    rows = [ln for ln in DESIGN.read_text().splitlines() if ln.startswith("| **D17**")]
    assert len(rows) == 1, "the design record has no single D17 row"
    clients = set(re.findall(r"`(tag:[a-z0-9-]+)`", rows[0])) - {"tag:index"}
    assert clients, "D17 names no store clients"
    return clients


def test_the_tags_are_d17s_clients_and_the_store() -> None:
    tags = [r["tag"] for r in _repos()]
    assert len(tags) == len(set(tags)), "a tag listed twice"
    assert set(tags) - {"tag:index"} == _d17_clients()
    assert "tag:index" in tags


def test_each_repo_is_listed_once_by_its_name() -> None:
    names = [r["repo"] for r in _repos()]
    assert len(names) == len(set(names))
    assert all(re.fullmatch(r"[a-z0-9][a-z0-9-]*", n) for n in names), names
    assert {"index", "observo", "processor"} <= set(names), "tag and repo names differ"


def test_the_configs_are_fetched_anonymously_from_raw() -> None:
    """raw.githubusercontent.com has no anonymous rate limit; api.github.com
    allows 60 calls an hour per address, which a 10-minute timer would spend."""
    assert _cohort()["raw"] == "https://raw.githubusercontent.com/CannObserv"


def test_only_a_private_repo_is_hand_listed_and_it_says_when() -> None:
    for entry in _repos():
        assert set(entry) <= {"repo", "tag", "private", "hand_listed"}, entry
        if not entry.get("private"):
            assert "hand_listed" not in entry, f"{entry['repo']} is fetched"
            continue
        hand = entry["hand_listed"]
        assert set(hand) == {"as_of", "source", "config"}, entry["repo"]
        date.fromisoformat(hand["as_of"])
        assert hand["source"]
        config = hand["config"]
        if config is not None:
            assert isinstance(config["projectId"], str)
            assert all(isinstance(p, str) for p in config.get("linkedProjects", []))


def test_the_telemetry_design_names_the_private_repos_as_listed() -> None:
    """#38: it said ``status`` was private; ``status`` went public."""
    paragraphs = (" ".join(p.split()) for p in TELEMETRY.read_text().split("\n\n"))
    [line] = [p for p in paragraphs if "Private repos (" in p]
    named = set(re.findall(r"`([a-z0-9-]+)`", line.split("Private repos (", 1)[1].split(")")[0]))
    assert named == {r["repo"] for r in _repos() if r.get("private")}
