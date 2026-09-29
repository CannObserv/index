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
- **The target must be git-ignored.** CannObserv/broker#17 asserted that
  ``.claude/settings.local.json`` "is git-ignored"; in CannObserv/broker it was
  not, and broker is a **public** repo. Four of five cohort repos carried the
  rule, so the assertion read as true right up until the one exception put the
  cohort's single key one ``git add -A`` from GitHub (notifier#68).

  The check is ``git check-ignore``, whatever the *source* of the rule — a
  global ``core.excludesfile`` genuinely does prevent a commit from this VM,
  which is all the installer is responsible for. Pinning the rule to the repo's
  own ``.gitignore`` is the repo's job, and is what broker#18 does; conflating
  the two would make the installer refuse a target that is in fact safe here.
"""

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "install_qdrant_key.sh"

FAKE_KEY = "a" * 64


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git work tree that ignores the target — the only shape the script accepts.

    Every happy-path case needs this now. A bare ``tmp_path`` is not a work
    tree, which the script refuses, and that refusal is itself under test in
    ``test_refuses_a_target_outside_a_git_work_tree``.
    """
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text(".claude/settings.local.json\n")
    return tmp_path


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


def test_creates_the_file_when_absent(repo):
    result = run(repo, FAKE_KEY)
    assert result.returncode == 0, result.stderr
    assert json.loads(settings(repo).read_text())["env"]["QDRANT_API_KEY"] == FAKE_KEY


def test_merges_without_clobbering_existing_settings(repo):
    (repo / ".claude").mkdir()
    settings(repo).write_text(
        json.dumps({"env": {"OTHER": "keep"}, "permissions": {"allow": ["Bash"]}})
    )
    assert run(repo, FAKE_KEY).returncode == 0
    d = json.loads(settings(repo).read_text())
    assert d["env"]["QDRANT_API_KEY"] == FAKE_KEY
    assert d["env"]["OTHER"] == "keep", "a sibling env value was clobbered"
    assert d["permissions"] == {"allow": ["Bash"]}, "an unrelated top-level key was dropped"


def test_replaces_an_existing_key(repo):
    run(repo, "old-key")
    run(repo, FAKE_KEY)
    assert json.loads(settings(repo).read_text())["env"]["QDRANT_API_KEY"] == FAKE_KEY


def test_strips_surrounding_whitespace(repo):
    """`ssh host 'sed ...'` delivers a trailing newline; it is not part of the key."""
    assert run(repo, f"  {FAKE_KEY}\n").returncode == 0
    assert json.loads(settings(repo).read_text())["env"]["QDRANT_API_KEY"] == FAKE_KEY


def test_empty_stdin_is_an_error_and_writes_no_key(repo):
    result = run(repo, "   \n")
    assert result.returncode != 0
    assert "empty" in result.stderr.lower()
    if settings(repo).exists():
        assert "QDRANT_API_KEY" not in json.loads(settings(repo).read_text()).get("env", {})


def test_missing_repo_argument_is_an_error():
    result = subprocess.run(["bash", str(SCRIPT)], input=FAKE_KEY, capture_output=True, text=True)
    assert result.returncode != 0
    assert "usage" in result.stderr.lower()


def test_file_is_owner_only(repo):
    run(repo, FAKE_KEY)
    assert stat.S_IMODE(os.stat(settings(repo)).st_mode) == 0o600


def test_leaves_no_temporary_file_behind(repo):
    run(repo, FAKE_KEY)
    leftovers = [p.name for p in (repo / ".claude").iterdir() if p.name != "settings.local.json"]
    assert leftovers == [], f"temporary residue: {leftovers}"


def test_reports_the_length_so_a_truncated_transfer_is_visible(repo):
    """A short key 401s exactly like a wrong one. The length is the discriminator."""
    result = run(repo, FAKE_KEY)
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


def test_refuses_a_target_git_would_commit(tmp_path):
    """The notifier#68 case: a work tree with no rule for the target.

    CannObserv/broker was exactly this, and public. Writing here puts the
    cohort's single Qdrant key one ``git add -A`` from GitHub, and per #57 a
    leak anywhere is a rotation on every cohort VM with no overlap window.
    """
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    result = run(tmp_path, FAKE_KEY)
    assert result.returncode != 0
    assert "not ignored" in result.stderr.lower()
    assert ".gitignore" in result.stderr, "the refusal must name the remedy"
    assert not settings(tmp_path).exists(), "refused, so nothing may be written"


def test_refuses_a_target_already_tracked_by_git(tmp_path):
    """A tracked file is a *different* incident, and needs a different remedy.

    ``.gitignore`` does not apply to tracked paths, so adding the rule fixes
    nothing here — the key may already be in history, which makes this a
    rotation rather than a config fix. Blending it into the "not ignored"
    message would send the operator to the wrong remedy.
    """
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text(".claude/settings.local.json\n")
    (tmp_path / ".claude").mkdir()
    settings(tmp_path).write_text("{}\n")
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "-f", ".claude/settings.local.json"], check=True
    )
    result = run(tmp_path, FAKE_KEY)
    assert result.returncode != 0
    assert "tracked" in result.stderr.lower()
    assert "rotate" in result.stderr.lower(), "a tracked key is a rotation, not a config fix"
    assert json.loads(settings(tmp_path).read_text()) == {}, "refused, so nothing may be written"


def test_refuses_a_target_outside_a_git_work_tree(tmp_path):
    """``git check-ignore`` exits 128 here, which is not the same as 'not ignored'."""
    result = run(tmp_path, FAKE_KEY)
    assert result.returncode != 0
    assert "work tree" in result.stderr.lower()
    assert not settings(tmp_path).exists()


def test_a_global_excludesfile_is_accepted(tmp_path):
    """The installer asks 'would git commit this here', not 'whose rule is it'.

    A global ``core.excludesfile`` genuinely prevents a commit from this VM,
    which is the whole of the installer's responsibility. Pinning the rule to
    the repo's own ``.gitignore`` is the repo's job (broker#18); enforcing it
    here would refuse a target that is actually safe.
    """
    excludes = tmp_path / "global-excludes"
    excludes.write_text(".claude/settings.local.json\n")
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    subprocess.run(
        ["git", "-C", str(work), "config", "core.excludesfile", str(excludes)], check=True
    )
    result = run(work, FAKE_KEY)
    assert result.returncode == 0, result.stderr
    assert json.loads(settings(work).read_text())["env"]["QDRANT_API_KEY"] == FAKE_KEY


def test_refusal_leaves_no_directory_behind(tmp_path):
    """The check runs before ``mkdir -p``: a refused install creates nothing."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    run(tmp_path, FAKE_KEY)
    assert not (tmp_path / ".claude").exists(), "refused, but .claude was created anyway"
