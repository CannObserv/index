"""Pin the cooldown on every ``.github/dependabot.yml`` block (#23).

Repo-local, beside the skill's ``test_dependabot.py``, which is edited only in
its ADAPT block. Removing the cooldown wouldn't stop the bot. It would bring
back what #19 avoided by hand, a bump PR for a release days old, before a
yanked or hijacked one has had time to surface.

Cooldown governs version updates only, never security updates (GitHub's
Dependabot options reference), so an advisory's fix isn't delayed by it.
``github-actions`` supports ``default-days`` and none of the semver-*-days
keys, so ``default-days`` is the one key every block sets.
"""

from pathlib import Path

import pytest
import yaml

COOLDOWN_DAYS = 7

CONFIG = Path(__file__).resolve().parents[2] / ".github" / "dependabot.yml"


def _updates() -> list[dict]:
    if not CONFIG.is_file():
        return []
    return (yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}).get("updates") or []


def test_there_are_blocks_to_check() -> None:
    """Guard the guard: with no blocks, the parametrized test below runs zero times."""
    assert _updates(), "no update blocks in .github/dependabot.yml"


@pytest.mark.parametrize("block", _updates(), ids=lambda b: str(b.get("package-ecosystem")))
def test_every_block_waits_out_a_fresh_release(block: dict) -> None:
    days = (block.get("cooldown") or {}).get("default-days")
    assert days == COOLDOWN_DAYS, (
        f"cooldown.default-days is {days!r}, expected {COOLDOWN_DAYS}: "
        "version updates would propose a release days old"
    )
