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


def parse_runs(workflow: str) -> list[str]:
    """A workflow's ``run:`` steps across jobs, in file order, each command once."""
    runs = re.findall(r"^\s*(?:-\s+)?run:\s*(.+?)\s*$", workflow, flags=re.MULTILINE)
    return list(dict.fromkeys(runs))


def ci_commands() -> list[str]:
    return parse_runs(CI.read_text())


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


def test_the_ci_parser_reads_what_ci_runs():
    """Guards the drift test below against a parser that reads nothing, or reads wrong.

    It reads one-line ``run:`` steps only. A block scalar (``run: |``) would
    come back as a bare ``|``, so one fails here, naming the cause.
    """
    steps = ci_commands()
    assert "uv run pytest" in steps
    assert not [s for s in steps if s[0] in "|>"], "block-scalar run: steps are not parsed"


def test_the_parser_reads_both_step_forms():
    """A shorthand ``- run:`` step missed here would be a CI step the gate skips."""
    workflow = (
        "    steps:\n"
        "      - name: named\n"
        "        run: uv run a\n"
        "      - run: uv run b\n"
        "      - run: |\n"
        "          echo c\n"
    )
    assert parse_runs(workflow) == ["uv run a", "uv run b", "|"]


def test_runs_exactly_what_ci_runs_in_order():
    assert script_commands() == ci_commands()


def test_runs_every_step_and_passes_when_all_pass(fake_uv):
    code, lines = run(fake_uv, REPO_ROOT)
    assert code == 0
    assert [line.split("|", 1)[1] for line in lines] == ci_commands()


@pytest.mark.parametrize("failing", ci_commands())
def test_stops_at_the_first_failure(fake_uv, failing):
    steps = ci_commands()
    code, lines = run(fake_uv, REPO_ROOT, fail_on=failing)
    assert code != 0
    assert [line.split("|", 1)[1] for line in lines] == steps[: steps.index(failing) + 1]


def test_runs_from_the_repo_root_whatever_the_cwd(fake_uv):
    code, lines = run(fake_uv, REPO_ROOT / "tests")
    assert code == 0
    assert {line.split("|", 1)[0] for line in lines} == {str(REPO_ROOT)}
