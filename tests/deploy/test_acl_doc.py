"""Drift tests for the documented ACL, in the runbook and the design record (#7).

**The check-in rule names the host the check-in posts to.** D16 moved the
check-in from notifier to co-status, and ``index-checkin.sh`` followed, but
both ACL blocks kept ``tag:index -> tag:notifier:9000`` from the cutover
(notifier#83, 2026-09-28) until #7, while the live policy carried both edges. Only the rule's ``dst`` tracks the script, so
this reads the host from the script rather than pinning ``status`` again. It
relies on the cohort's convention that a node's tag is its host name
(``status`` is ``tag:status``); this asserts the docs, not that convention.

**D17 is the store's client list of record, and both blocks agree with it.**
The live policy opened ``:6333,11434`` to every cohort VM while the docs named
four.

**Build-phase scaffolding is a block of its own.** D13's ``:22`` edge and
``ssh`` block are removed at the end of Phase 3; in the same block as the
steady state, a re-provision recreates them as if current.

These read documents, not the tailnet: the live policy is not readable from
this host. A node's packet filter shows only its inbound rules, as IPs.
"""

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
README = REPO_ROOT / "deploy" / "README.md"
DESIGN = REPO_ROOT / "docs" / "plans" / "2026-09-11-shared-qdrant-vm-design.md"
CHECKIN = REPO_ROOT / "deploy" / "index-checkin.sh"
DOCS = [README, DESIGN]
BUILD_PHASE = "BUILD PHASE ONLY"
STORE_DST = "tag:index:6333,11434"


def _blocks(doc: Path) -> list[tuple[str, dict]]:
    """Every ACL policy in ``doc``: its raw text, and the JSON it parses to.

    A ``jsonc`` block without an ``acls`` key is some other example, not a policy.
    """
    blocks = re.findall(r"```jsonc\n(.*?)```", doc.read_text(), flags=re.S)
    parsed = [(raw, json.loads(re.sub(r"//[^\n]*", "", raw))) for raw in blocks]
    return [(raw, policy) for raw, policy in parsed if "acls" in policy]


def _steady(doc: Path) -> dict:
    """``doc``'s one steady-state policy block."""
    steady = [policy for raw, policy in _blocks(doc) if BUILD_PHASE not in raw]
    assert len(steady) == 1, f"{doc.name}: {len(steady)} steady-state ACL blocks"
    return steady[0]


def _checkin_host() -> str:
    hosts = set(re.findall(r"http://([a-z0-9-]+):9000/", CHECKIN.read_text()))
    assert len(hosts) == 1, f"index-checkin.sh posts to {sorted(hosts)}"
    return hosts.pop()


def _d17_clients() -> set[str]:
    rows = (ln for ln in DESIGN.read_text().splitlines() if ln.startswith("| **D17**"))
    row = next(rows, None)
    assert row is not None, "the design record has no D17 row"
    clients = set(re.findall(r"`(tag:[a-z0-9-]+)`", row)) - {"tag:index"}
    assert clients, "D17 names no store clients"
    return clients


@pytest.mark.parametrize("doc", DOCS, ids=lambda d: d.name)
def test_checkin_rule_targets_the_checkin_host(doc: Path) -> None:
    rules = [r for r in _steady(doc)["acls"] if "tag:index" in r["src"]]
    assert [r["dst"] for r in rules] == [[f"tag:{_checkin_host()}:9000"]]


@pytest.mark.parametrize("doc", DOCS, ids=lambda d: d.name)
def test_store_clients_are_d17s(doc: Path) -> None:
    rules = [r for r in _steady(doc)["acls"] if r["dst"] == [STORE_DST]]
    assert len(rules) == 1
    assert set(rules[0]["src"]) == _d17_clients()


@pytest.mark.parametrize("doc", DOCS, ids=lambda d: d.name)
def test_steady_state_has_no_build_phase_scaffolding(doc: Path) -> None:
    policy = _steady(doc)
    assert "ssh" not in policy
    assert not [d for r in policy["acls"] for d in r["dst"] if d.endswith(":22")]


@pytest.mark.parametrize("doc", DOCS, ids=lambda d: d.name)
def test_build_phase_edge_is_its_own_block(doc: Path) -> None:
    build = [policy for raw, policy in _blocks(doc) if BUILD_PHASE in raw]
    assert len(build) == 1
    assert build[0]["ssh"]
    assert [r["dst"] for r in build[0]["acls"]] == [["tag:index:22"]]
