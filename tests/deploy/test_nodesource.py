"""Node from NodeSource's apt repo, for this repo's own SocratiCode client (D20, #18).

D20 admits Node 26 on co-index for one client, this repo's, installed the way
CannObserv/processor installs its Node: ``scripts/nodesource.sh`` fetches the
signing key, refuses one whose fingerprint isn't NodeSource's, and installs the
versioned source and pins. Moving the major line is a deliberate edit:
``EXPECTED_MAJOR`` here, in the same commit as the source.

- **The origin is scoped** (patching-hosts policy.md, "Two lanes"): the knob
  follows ``deb.nodesource.com``, which serves ``nsolid`` too, so everything
  from the site sits below Ubuntu's 500 and ``nodejs`` above it.
- **No store unit runs it.** ``/usr/bin/node`` is on every ``$PATH``, so
  "not on a store unit's path" means no deploy file but the installer names
  ``node``, ``npx`` or ``npm`` as a command (D20, D9).
- **Group first.** D20 takes ``exedev`` out of group ``docker`` before Node
  lands, and the installer refuses while it's still a member.

The structure tests run everywhere. The host is checked on co-index only, by
``scripts/nodesource.sh check``, where a missing install is a failure, not a skip.
"""

import os
import re
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "deploy"
SCRIPT = REPO_ROOT / "scripts" / "nodesource.sh"
SOURCES = DEPLOY / "apt-sources.list.d" / "nodesource.sources"
PINS = DEPLOY / "apt-preferences.d" / "nodesource"
KNOB = REPO_ROOT / ".skills" / "patching-hosts"
EXPECTED_MAJOR = 26
SITE = "deb.nodesource.com"
# NodeSource's repo signing key: CannObserv/watcher#296, and fetched again on
# 2026-11-06 from deb.nodesource.com/gpgkey/nodesource-repo.gpg.key.
FINGERPRINT = "6F71F525282841EEDAF851B42F59B5F99B1BE0B4"
HOST = "co-index"


def _stanzas(path: Path) -> list[dict[str, str]]:
    """deb822 stanzas, blank-line separated; ``#`` comment lines dropped."""
    body = "\n".join(ln for ln in path.read_text().splitlines() if not ln.startswith("#"))
    return [
        dict(ln.split(": ", 1) for ln in block.strip().splitlines())
        for block in body.split("\n\n")
        if block.strip()
    ]


def _script_value(name: str) -> str:
    match = re.search(rf'^{name}="([^"]*)"', SCRIPT.read_text(), re.MULTILINE)
    assert match, f"{name}= not found in {SCRIPT.name}"
    return match[1]


def test_source_is_the_pinned_major_line_signed_by_its_own_keyring() -> None:
    (source,) = _stanzas(SOURCES)
    assert source == {
        "Types": "deb",
        "URIs": f"https://{SITE}/node_{EXPECTED_MAJOR}.x",
        "Suites": "nodistro",
        "Components": "main",
        "Signed-By": _script_value("KEYRING"),
    }


def test_catch_all_holds_the_site_below_ubuntu() -> None:
    catch_all = [s for s in _stanzas(PINS) if s["Package"] == "*"]
    assert catch_all == [{"Package": "*", "Pin": f"origin {SITE}", "Pin-Priority": "100"}]


def test_nodejs_alone_is_pinned_above_ubuntu() -> None:
    """By site: the Release file's Origin is aptly's generic ``. nodistro``.
    Above universe's 500 (its nodejs 18), below 1000 (never a downgrade)."""
    named = [s for s in _stanzas(PINS) if s["Package"] != "*"]
    assert len(named) == 1
    (stanza,) = named
    assert stanza["Package"] == "nodejs"
    assert stanza["Pin"] == f"origin {SITE}"
    assert 500 < int(stanza["Pin-Priority"]) < 1000


def test_script_installs_the_tracked_files_and_checks_the_key() -> None:
    text = SCRIPT.read_text()
    assert _script_value("SOURCES") == "/etc/apt/sources.list.d/nodesource.sources"
    assert _script_value("PREF") == "/etc/apt/preferences.d/nodesource"
    assert _script_value("FINGERPRINT") == FINGERPRINT
    assert "apt-sources.list.d/nodesource.sources" in text
    assert "apt-preferences.d/nodesource" in text


def test_the_knob_follows_the_site() -> None:
    origins = [ln.split() for ln in KNOB.read_text().splitlines() if ln.startswith("origin ")]
    assert ["origin", SITE, "follow"] in origins


def test_docs_follow_the_major_line() -> None:
    runbook = (DEPLOY / "README.md").read_text()
    assert f"node_{EXPECTED_MAJOR}.x" in runbook
    assert FINGERPRINT in re.sub(r"\s", "", runbook)
    assert f"Node {EXPECTED_MAJOR}" in (REPO_ROOT / "AGENTS.md").read_text()


def test_script_parses() -> None:
    done = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def test_bare_invocation_prints_usage_naming_both_subcommands() -> None:
    done = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True)
    assert done.returncode == 2
    assert "nodesource.sh install" in done.stderr
    assert "nodesource.sh check" in done.stderr


def _stub_install_env(tmp_path: Path, members: str, procs: str = "") -> dict[str, str]:
    """``id`` says root, ``getent`` prints group ``docker`` (gid 107) with
    ``members``, ``ps`` prints ``procs`` as exedev's ``pid supgid`` lines, and
    every other tool the install reaches fails loudly, so a refusal is proven
    to come before any fetch or write."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (tmp_path / "procs").write_text(procs)
    stubs = {
        "id": 'if [ "$1" = -u ]; then echo 0; else exec /usr/bin/id "$@"; fi',
        "getent": f'[ "$1 $2" = "group docker" ] && echo "docker:x:107:{members}"',
        "ps": f'[ "$*" = "-u exedev -o pid=,supgid=" ] || exit 9\ncat "{tmp_path / "procs"}"',
    }
    for tool in ("curl", "gpg", "install", "apt-get", "choom"):
        stubs[tool] = f'echo "{tool} reached" >&2; exit 99'
    for name, body in stubs.items():
        (bin_dir / name).write_text(f"#!/usr/bin/env bash\n{body}\n")
        (bin_dir / name).chmod(0o755)
    return {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "SUDO_USER": "exedev"}


@pytest.mark.parametrize("members", ["exedev", "someone,exedev"])
def test_install_refuses_while_exedev_is_in_group_docker(tmp_path: Path, members: str) -> None:
    env = _stub_install_env(tmp_path, members)
    done = subprocess.run(["bash", str(SCRIPT), "install"], capture_output=True, text=True, env=env)
    assert done.returncode == 2
    assert "group docker" in done.stderr
    assert "reached" not in done.stderr


# 2026-11-06: the group database was clean, and these still held gid 107: the
# VS Code server (a window reload doesn't restart it), an orphaned VS Code CLI
# agent host, and the user manager, which linger keeps up since boot.
STALE = "  312 4,24,27,107,1000\n82888 107,1000\n"


@pytest.mark.parametrize("procs", [STALE, "  312 107\n"])
def test_install_refuses_while_an_exedev_process_holds_the_group(
    tmp_path: Path, procs: str
) -> None:
    """Leaving the group changes the database, not a running process: one
    started before keeps the gid, and so does everything it starts."""
    env = _stub_install_env(tmp_path, "", procs)
    done = subprocess.run(["bash", str(SCRIPT), "install"], capture_output=True, text=True, env=env)
    assert done.returncode == 2
    assert "still hold group docker" in done.stderr
    assert "312" in done.stderr
    assert "reached" not in done.stderr


def test_install_passes_the_group_guard_once_exedev_is_out(tmp_path: Path) -> None:
    """Out of the group, every process included, the install goes on to
    fetch the key, which the stub fails. A gid that merely contains 107's
    digits is another group."""
    env = _stub_install_env(tmp_path, "", "  312 4,1070,1000\n 400 1107,4\n")
    done = subprocess.run(["bash", str(SCRIPT), "install"], capture_output=True, text=True, env=env)
    assert "curl reached" in done.stderr


# Executables the store's units and scripts would run. The installer is in
# scripts/, never installed; comments and the runbook are prose, and apt's
# files name packages, not commands.
NODE = re.compile(r"(^|[\s;&|(`$\"'/])(node|nodejs|npx|npm)([\s;&|)`\"]|'(?!s\b)|$)")


def _tracked_deploy_files() -> list[str]:
    listed = subprocess.run(
        ["git", "ls-files", "deploy"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    return [f for f in listed if f != "deploy/README.md" and not f.startswith("deploy/apt-")]


@pytest.mark.parametrize("path", _tracked_deploy_files())
def test_no_store_file_runs_node(path: str) -> None:
    code = [
        ln
        for ln in (REPO_ROOT / path).read_text().splitlines()
        if not ln.lstrip().startswith(("#", "Explanation:"))
    ]
    assert [ln for ln in code if NODE.search(ln)] == []


def test_the_node_pattern_finds_a_call() -> None:
    assert NODE.search("ExecStart=/usr/bin/node server.js")
    assert NODE.search('exec npx -y socraticode "$@"')
    assert NODE.search("sh -c 'node'")
    assert not NODE.search("tailnet-bind.sh resolves this node's address")


def _policy(tmp_path: Path, *packages: str) -> str:
    apt_cache = shutil.which("apt-cache")
    if apt_cache is None:
        pytest.skip("apt-cache not available on this host")
    parts = tmp_path / "preferences.d"
    parts.mkdir()
    shutil.copy(PINS, parts / "nodesource")
    done = subprocess.run(
        [
            apt_cache,
            "-o",
            "Dir::Etc::Preferences=/dev/null",
            "-o",
            f"Dir::Etc::PreferencesParts={parts}",
            "policy",
            *packages,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert done.stderr == ""
    return done.stdout


def test_apt_parses_the_pins_cleanly(tmp_path: Path) -> None:
    _policy(tmp_path)


def test_the_host_matches_the_tracked_setup() -> None:
    """``nodesource.sh check``: key, source and pins as tracked, the
    candidate on the pinned line from NodeSource, and ``node`` the package's."""
    if socket.gethostname() != HOST:
        pytest.skip(f"not {HOST}")
    done = subprocess.run(["bash", str(SCRIPT), "check"], capture_output=True, text=True)
    assert done.returncode == 0, f"{done.stderr}Fix: sudo bash scripts/nodesource.sh install"
