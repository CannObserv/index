"""Where CI and every clone get their uv from (#25, #26).

**One source: ``[tool.uv] required-version``.** uv enforces it on every
command, and setup-uv reads it when the step passes no ``version`` or
``version-file`` (v10.3.3, ``WorkspaceVersionResolver``). It resolves the
range to the newest matching uv, so CI floats and a clone too old for
``uv.lock`` fails with a clear message instead of rewriting the lock. A
``version:`` on one job would be a second source, and would drift.

**The floor stays a floor, inside two bounds.** At or above the uv that wrote
``uv.lock``'s ``revision = 5``. At or below the uv Dependabot's ``uv`` updater
runs, which enforces it too: above that, every ``dev-tools`` PR fails, in
Dependabot's tab only. Raise ``DEPENDABOT_UV`` only after reading
dependabot-core's ``uv/Dockerfile``.
"""

import re
import tomllib
from pathlib import Path

import pytest
import yaml

# The uv that wrote uv.lock's revision 5 (#19), and uv's eMudhra certificate fix.
LOCK_UV = (0, 13, 5)
# dependabot-core uv/Dockerfile, read 2026-11-01 (dependabot-core#16651).
DEPENDABOT_UV = (0, 13, 5)

REPO_ROOT = Path(__file__).resolve().parents[2]
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PYPROJECT = REPO_ROOT / "pyproject.toml"

SETUP_UV = "astral-sh/setup-uv"
_FLOOR = re.compile(r"^>=\s*(\d+)\.(\d+)\.(\d+)$")


def _setup_uv_steps() -> list[dict]:
    jobs = yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]
    return [
        step
        for job in jobs.values()
        for step in job.get("steps", [])
        if str(step.get("uses", "")).split("@")[0] == SETUP_UV
    ]


def _floor(spec: str) -> tuple[int, ...] | None:
    m = _FLOOR.match(spec.strip())
    return tuple(int(g) for g in m.groups()) if m else None


def test_every_job_sets_up_uv() -> None:
    """Guard the guards: with no setup-uv step found, the rest pass on nothing."""
    assert len(_setup_uv_steps()) == 2, "expected setup-uv in both jobs (lint, test)"


@pytest.mark.parametrize("step", _setup_uv_steps(), ids=lambda s: s["uses"])
def test_ci_takes_uv_from_required_version(step: dict) -> None:
    extra = {"version", "version-file"} & set(step.get("with") or {})
    assert not extra, (
        f"setup-uv's {sorted(extra)} overrides pyproject.toml's required-version: "
        "CI and clones would get uv from two places (#25)"
    )


def test_required_version_is_a_floor_dependabot_meets() -> None:
    spec = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["tool"]["uv"].get(
        "required-version"
    )
    assert spec, "[tool.uv] required-version is unset: an old uv rewrites uv.lock (#25)"
    floor = _floor(spec)
    assert floor, f"required-version {spec!r}: write a single `>=X.Y.Z` floor, nothing tighter"
    assert floor >= LOCK_UV, f"{spec!r} admits a uv older than uv.lock's revision"
    assert floor <= DEPENDABOT_UV, (
        f"{spec!r} is above Dependabot's uv {DEPENDABOT_UV}: its dev-tools PRs would fail"
    )


def test_the_floor_reads_only_a_lower_bound() -> None:
    """Guard the guard: a pin or a ceiling isn't a floor."""
    assert _floor(">=0.13.5") == (0, 13, 5)
    assert _floor(">= 0.13.5") == (0, 13, 5)
    assert _floor("==0.13.5") is None
    assert _floor(">=0.13.5,<0.14") is None
    assert _floor("0.13.5") is None
