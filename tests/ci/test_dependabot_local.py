"""Repo-local guards on ``.github/dependabot.yml`` (#23).

Beside the skill's ``test_dependabot.py``, which is edited only in its ADAPT
block, so these live here.

**Every key is one Dependabot reads.** Dependabot rejects a whole config over
one unknown key, and says so only in its own tab: every update stops. The
skill's guards read the keys they expect, so a misspelled optional one
(``open-pull-request-limit``) passes them all. The sets below are GitHub's
Dependabot options reference, read 2026-10-30. A key it adds later fails here
first: add it.

**Every block has a 7-day cooldown.** Removing it wouldn't stop the bot. It
would bring back what #19 avoided by hand, a bump PR for a release days old,
before a yanked or hijacked one has had time to surface. Cooldown governs
version updates only, never security updates, so an advisory's fix isn't
delayed by it. ``github-actions`` supports ``default-days`` and none of the
semver-*-days keys, so ``default-days`` is the one key every block sets.
"""

from pathlib import Path

import pytest
import yaml

COOLDOWN_DAYS = 7

CONFIG = Path(__file__).resolve().parents[2] / ".github" / "dependabot.yml"

TOP_KEYS = {"version", "updates", "registries", "multi-ecosystem-groups", "enable-beta-ecosystems"}
BLOCK_KEYS = {
    "package-ecosystem",
    "directory",
    "directories",
    "schedule",
    "allow",
    "assignees",
    "commit-message",
    "cooldown",
    "exclude-paths",
    "groups",
    "ignore",
    "insecure-external-code-execution",
    "labels",
    "milestone",
    "multi-ecosystem-group",
    "open-pull-requests-limit",
    "pull-request-branch-name",
    "rebase-strategy",
    "registries",
    "target-branch",
    "vendor",
    "versioning-strategy",
}
NESTED_KEYS = {
    "schedule": {"interval", "day", "time", "timezone", "cronjob"},
    "cooldown": {
        "default-days",
        "semver-major-days",
        "semver-minor-days",
        "semver-patch-days",
        "include",
        "exclude",
    },
    "commit-message": {"prefix", "prefix-development", "include"},
}
GROUP_KEYS = {
    "applies-to",
    "dependency-type",
    "exclude-patterns",
    "group-by",
    "patterns",
    "update-types",
}


def _load() -> dict:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {} if CONFIG.is_file() else {}


def _updates() -> list[dict]:
    return _load().get("updates") or []


def _ids(block: dict) -> str:
    return str(block.get("package-ecosystem"))


def _unknown_keys(config: dict) -> list[str]:
    """Every key Dependabot doesn't read, as a dotted path."""
    bad = [k for k in config if k not in TOP_KEYS]
    for i, block in enumerate(config.get("updates") or []):
        where = f"updates[{i}]"
        bad += [f"{where}.{k}" for k in block if k not in BLOCK_KEYS]
        for key, allowed in NESTED_KEYS.items():
            bad += [f"{where}.{key}.{k}" for k in block.get(key) or {} if k not in allowed]
        for name, group in (block.get("groups") or {}).items():
            bad += [f"{where}.groups.{name}.{k}" for k in group or {} if k not in GROUP_KEYS]
    return bad


def test_there_are_blocks_to_check() -> None:
    """Guard the guard: with no blocks, the parametrized tests run zero times."""
    assert _updates(), "no update blocks in .github/dependabot.yml"


def test_every_key_is_one_dependabot_reads() -> None:
    bad = _unknown_keys(_load())
    assert not bad, (
        f"Dependabot rejects the whole config over an unknown key, and every update stops: {bad}"
    )


def test_the_key_check_reads_every_level() -> None:
    """Guard the guard: a typo at each level is caught."""
    block = {
        "package-ecosystem": "uv",
        "open-pull-request-limit": 10,
        "schedule": {"intervall": "weekly"},
        "cooldown": {"default_days": 7},
        "commit-message": {"prefx": "chore"},
        "groups": {"dev-tools": {"dependency_type": "development"}},
    }
    assert _unknown_keys({"version": 2, "update": [], "updates": [block]}) == [
        "update",
        "updates[0].open-pull-request-limit",
        "updates[0].schedule.intervall",
        "updates[0].cooldown.default_days",
        "updates[0].commit-message.prefx",
        "updates[0].groups.dev-tools.dependency_type",
    ]


@pytest.mark.parametrize("block", _updates(), ids=_ids)
def test_every_block_waits_out_a_fresh_release(block: dict) -> None:
    days = (block.get("cooldown") or {}).get("default-days")
    assert days == COOLDOWN_DAYS, (
        f"cooldown.default-days is {days!r}, expected {COOLDOWN_DAYS}: "
        "version updates would propose a release days old"
    )
