"""Behaviour and drift tests for scripts/pre-ship.sh (#3).

The ``shipping-work`` skill's gate is a stub until the project supplies this
file. It is a local convenience and nothing more: CI is the repo's only
correctness gate (AGENTS.md), so the one property that matters is that the
local gate never checks *less* than CI does. A gate that silently drops a step
passes a ship that CI then fails, after the push.

- **Its commands are CI's ``run:`` steps, in order.** A drift test, so adding
  a step to CI without adding it here fails.
- **It stops at the first failure.** A later step passing must not mask an
  earlier one.
- **It runs from the repo root**, whatever the caller's cwd: ``ruff check .``
  from ``tests/`` checks a subset and reports green.
"""

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "pre-ship.sh"
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def ci_commands() -> list[str]:
    """CI's ``run:`` steps across jobs, in file order, each command once."""
    runs = re.findall(r"^\s*run:\s*(.+?)\s*$", CI.read_text(), flags=re.MULTILINE)
    return list(dict.fromkeys(runs))


def script_commands() -> list[str]:
    """The ``uv`` invocations in the script, in order."""
    return [
        line.strip() for line in SCRIPT.read_text().splitlines() if line.strip().startswith("uv ")
    ]


@pytest.fixture
def fake_uv(tmp_path: Path) -> Path:
    """A ``uv`` on PATH that logs its cwd and argv, and fails when told to.

    ``FAIL_ON`` names an argv (space-joined) that exits 1. Nothing real runs,
    so the test cannot recurse into the suite that contains it.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s|%s\\n" "$PWD" "uv $*" >> "$UV_LOG"\n'
        '[ "uv $*" != "${FAIL_ON:-}" ]\n'
    )
    uv.chmod(0o755)
    return bin_dir


def run(fake_uv: Path, cwd: Path, fail_on: str = "") -> tuple[int, list[str]]:
    log = fake_uv.parent / "uv.log"
    env = {
        **os.environ,
        "PATH": f"{fake_uv}{os.pathsep}{os.environ['PATH']}",
        "UV_LOG": str(log),
        "FAIL_ON": fail_on,
    }
    proc = subprocess.run(["bash", str(SCRIPT)], cwd=cwd, env=env, capture_output=True, text=True)
    lines = log.read_text().splitlines() if log.exists() else []
    return proc.returncode, lines


def test_script_exists_and_is_executable():
    assert SCRIPT.is_file()
    assert os.stat(SCRIPT).st_mode & stat.S_IXUSR


def test_ci_has_the_steps_this_file_assumes():
    """Guards the drift test below against a parser that matches nothing."""
    assert ci_commands() == [
        "uv sync --locked",
        "uv run ruff check .",
        "uv run ruff format --check .",
        "uv run pytest",
    ]


def test_runs_exactly_what_ci_runs_in_order():
    assert script_commands() == ci_commands()


def test_runs_every_step_and_passes_when_all_pass(fake_uv):
    code, lines = run(fake_uv, REPO_ROOT)
    assert code == 0
    assert [line.split("|", 1)[1] for line in lines] == ci_commands()


@pytest.mark.parametrize("failing", range(4))
def test_stops_at_the_first_failure(fake_uv, failing):
    steps = ci_commands()
    code, lines = run(fake_uv, REPO_ROOT, fail_on=steps[failing])
    assert code != 0
    assert [line.split("|", 1)[1] for line in lines] == steps[: failing + 1]


def test_runs_from_the_repo_root_whatever_the_cwd(fake_uv):
    code, lines = run(fake_uv, REPO_ROOT / "tests")
    assert code == 0
    assert {line.split("|", 1)[0] for line in lines} == {str(REPO_ROOT)}
