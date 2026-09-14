"""Behaviour and drift tests for scripts/install_qdrant_key.sh.

The shared SocratiCode store on ``co-index`` is gated by a single global Qdrant
``service.api_key`` (#57 D4/D14). There is no key list, no per-client identity
and no per-collection scope, so every adopting VM in the cohort holds the same
secret and a leak anywhere rotates it everywhere. That raises the cost of a
sloppy install step well above what a one-line ``echo`` deserves.

This script is the reviewed home for that step. It exists because the
alternative is four hand-transcribed copies in four adoption issues, and the
properties below are exactly the ones a transcription loses first:

- **The key arrives on stdin, never argv.** An argument is visible in ``ps`` to
  any other process on the box for as long as the call runs, and lands in shell
  history at the caller. This session leaked two credentials through a ``bash
  -x`` trace; argv is the same class of mistake with a longer tail.
- **The write is atomic and 0600 before it is visible.** A crash mid-write must
  not leave a truncated key, which presents later as a 401 that reads like a
  permissions problem rather than a corrupt file.
- **An empty stdin is an error, not an empty key.** Silently writing ``""``
  produces the same 401 from a completely different cause.
"""

import json
import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "install_qdrant_key.sh"

FAKE_KEY = "a" * 64


def run(repo_dir: Path, stdin: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), str(repo_dir)],
        input=stdin,
        capture_output=True,
        text=True,
    )


def settings(repo_dir: Path) -> Path:
    return repo_dir / ".claude" / "settings.local.json"


def test_script_exists_and_is_executable():
    assert SCRIPT.is_file()
    assert os.stat(SCRIPT).st_mode & stat.S_IXUSR


def test_creates_the_file_when_absent(tmp_path):
    result = run(tmp_path, FAKE_KEY)
    assert result.returncode == 0, result.stderr
    assert json.loads(settings(tmp_path).read_text())["env"]["QDRANT_API_KEY"] == FAKE_KEY


def test_merges_without_clobbering_existing_settings(tmp_path):
    (tmp_path / ".claude").mkdir()
    settings(tmp_path).write_text(
        json.dumps({"env": {"OTHER": "keep"}, "permissions": {"allow": ["Bash"]}})
    )
    assert run(tmp_path, FAKE_KEY).returncode == 0
    d = json.loads(settings(tmp_path).read_text())
    assert d["env"]["QDRANT_API_KEY"] == FAKE_KEY
    assert d["env"]["OTHER"] == "keep", "a sibling env value was clobbered"
    assert d["permissions"] == {"allow": ["Bash"]}, "an unrelated top-level key was dropped"


def test_replaces_an_existing_key(tmp_path):
    run(tmp_path, "old-key")
    run(tmp_path, FAKE_KEY)
    assert json.loads(settings(tmp_path).read_text())["env"]["QDRANT_API_KEY"] == FAKE_KEY


def test_strips_surrounding_whitespace(tmp_path):
    """`ssh host 'sed ...'` delivers a trailing newline; it is not part of the key."""
    assert run(tmp_path, f"  {FAKE_KEY}\n").returncode == 0
    assert json.loads(settings(tmp_path).read_text())["env"]["QDRANT_API_KEY"] == FAKE_KEY


def test_empty_stdin_is_an_error_and_writes_no_key(tmp_path):
    result = run(tmp_path, "   \n")
    assert result.returncode != 0
    assert "empty" in result.stderr.lower()
    if settings(tmp_path).exists():
        assert "QDRANT_API_KEY" not in json.loads(settings(tmp_path).read_text()).get("env", {})


def test_missing_repo_argument_is_an_error():
    result = subprocess.run(["bash", str(SCRIPT)], input=FAKE_KEY, capture_output=True, text=True)
    assert result.returncode != 0
    assert "usage" in result.stderr.lower()


def test_file_is_owner_only(tmp_path):
    run(tmp_path, FAKE_KEY)
    assert stat.S_IMODE(os.stat(settings(tmp_path)).st_mode) == 0o600


def test_leaves_no_temporary_file_behind(tmp_path):
    run(tmp_path, FAKE_KEY)
    leftovers = [
        p.name for p in (tmp_path / ".claude").iterdir() if p.name != "settings.local.json"
    ]
    assert leftovers == [], f"temporary residue: {leftovers}"


def test_reports_the_length_so_a_truncated_transfer_is_visible(tmp_path):
    """A short key 401s exactly like a wrong one. The length is the discriminator."""
    result = run(tmp_path, FAKE_KEY)
    assert "64" in result.stderr


def code() -> str:
    """The script's executable lines, with comments and blanks dropped.

    The header explains at length *why* the key stays off argv and why the
    script must never be traced. A raw text search for those phrases would
    match the comments documenting them — the same trap
    ``test_tailnet_bind.py`` calls out for its own ``ip`` probe.
    """
    return "\n".join(
        line
        for line in SCRIPT.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def test_key_never_reaches_argv():
    """Drift guard: the key must stay on stdin the whole way down.

    ``python3 -c ... "$key"`` would put it in the process table; so would any
    ``echo "$key" >`` redirect appearing in a command line. The script reads
    stdin directly inside python, so no shell variable ever holds it.
    """
    assert "$key" not in code(), "the key must never be held in a shell variable"
    assert "sys.stdin" in code(), "the key must be read from stdin inside python"


def test_does_not_trace_itself():
    """``set -x`` here is how this session leaked two credentials (#57)."""
    assert "set -x" not in code()
    assert "-x" not in code().split("set -euo pipefail")[0]
