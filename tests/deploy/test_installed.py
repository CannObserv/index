"""Every file in ``deploy/`` is either installed on co-index as tracked, or
declared as not installed (#6).

Nothing used to check the installed copies of anything but the two long-running
units and the needrestart drop-in. On 2026-09-30, 9 of the other installed
files had drifted from ``deploy/``: comments and ``Description=`` only, but
behavioural changes would have drifted the same way, with no warning.

``INSTALL`` below is the single mapping. Two tests run everywhere, CI included:
a tracked file with no entry fails, and so does an entry with no tracked file,
so a new deploy file cannot be left out of the mapping. The parity tests run on
co-index only, where a mapped file that is missing is a failure, not a skip.
"""

import os
import shutil
import socket
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOST = "co-index"
BIN = Path("/usr/local/bin")
UNITS = Path("/etc/systemd/system")

# deploy/-relative path -> (install path, mode), or None with the reason it is
# not installed as a file.
INSTALL: dict[str, tuple[Path, int] | None] = {
    "README.md": None,  # the runbook
    "setup.sh.template": None,  # rendered once and sent to exe.dev at provisioning
    "ollama-slim/Dockerfile": None,  # built into socraticode/ollama-slim:latest
    "index-checkin.sh": (BIN / "index-checkin.sh", 0o755),
    "ollama-run.sh": (BIN / "ollama-run.sh", 0o755),
    "qdrant-cert-renew.sh": (BIN / "qdrant-cert-renew.sh", 0o755),
    "qdrant-run.sh": (BIN / "qdrant-run.sh", 0o755),
    "tailnet-bind.sh": (BIN / "tailnet-bind.sh", 0o755),
    "index-checkin.service": (UNITS / "index-checkin.service", 0o644),
    "index-checkin.timer": (UNITS / "index-checkin.timer", 0o644),
    "ollama.service": (UNITS / "ollama.service", 0o644),
    "qdrant-cert-renew.service": (UNITS / "qdrant-cert-renew.service", 0o644),
    "qdrant-cert-renew.timer": (UNITS / "qdrant-cert-renew.timer", 0o644),
    "qdrant.service": (UNITS / "qdrant.service", 0o644),
    "needrestart.conf.d/index.conf": (Path("/etc/needrestart/conf.d/index.conf"), 0o644),
}
INSTALLED = {src: dest for src, dest in INSTALL.items() if dest is not None}
LOADED_UNITS = [src for src in INSTALLED if src.endswith((".service", ".timer"))]
# A unit with [Install] is started by being enabled; one without (the timers'
# oneshot services) is started by its timer.
ENABLED_UNITS = [
    src
    for src in LOADED_UNITS
    if "[Install]" in (REPO_ROOT / "deploy" / src).read_text().splitlines()
]


def _tracked() -> set[str]:
    """``deploy/``'s tracked files, relative to it. Tracked, not globbed, so
    ``__pycache__`` and scratch files are not deploy files."""
    listed = subprocess.run(
        ["git", "ls-files", "deploy"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return {line.removeprefix("deploy/") for line in listed.splitlines()}


def _on_host() -> None:
    if socket.gethostname() != HOST:
        pytest.skip(f"not {HOST}")


def _fix(src: str) -> str:
    dest, mode = INSTALLED[src]
    cmd = f"sudo install -D -m {mode:o} deploy/{src} {dest}"
    if src in LOADED_UNITS:
        cmd += " && sudo systemctl daemon-reload"
    return cmd


def test_every_deploy_file_has_an_entry() -> None:
    assert _tracked() - INSTALL.keys() == set()


def test_every_entry_is_a_deploy_file() -> None:
    assert INSTALL.keys() - _tracked() == set()


@pytest.mark.parametrize("src", sorted(INSTALLED))
def test_installed_copy_matches_tracked(src: str) -> None:
    """Content, mode, and root ownership: a script installed 644 fails at
    ``ExecStart=``, and one run as root but writable by another user is an
    escalation."""
    _on_host()
    dest, mode = INSTALLED[src]
    try:
        installed = dest.stat()
    except FileNotFoundError:
        pytest.fail(f"{dest} not installed. Fix: {_fix(src)}")
    assert dest.read_bytes() == (REPO_ROOT / "deploy" / src).read_bytes(), f"Fix: {_fix(src)}"
    assert stat.S_IMODE(installed.st_mode) == mode, f"Fix: {_fix(src)}"
    assert installed.st_uid == 0, f"{dest} not owned by root. Fix: {_fix(src)}"


def _show(src: str, *props: str) -> dict[str, str]:
    _on_host()
    systemctl = shutil.which("systemctl")
    assert systemctl is not None
    shown = subprocess.run(
        [systemctl, "show", os.path.basename(src), "-p", ",".join(props)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return dict(ln.partition("=")[::2] for ln in shown.splitlines())


@pytest.mark.parametrize("src", LOADED_UNITS)
def test_loaded_unit_is_the_installed_one(src: str) -> None:
    """A copied unit is not a loaded one until ``systemctl daemon-reload``."""
    assert _show(src, "LoadState", "NeedDaemonReload") == {
        "LoadState": "loaded",
        "NeedDaemonReload": "no",
    }, "Fix: sudo systemctl daemon-reload"


@pytest.mark.parametrize("src", ENABLED_UNITS)
def test_enabled_unit_is_enabled_and_running(src: str) -> None:
    """Installed and loaded is not running: a timer never enabled passes
    every check above and never fires, which for the renewal timer is D14's
    silent expiry."""
    unit = os.path.basename(src)
    assert _show(src, "UnitFileState", "ActiveState") == {
        "UnitFileState": "enabled",
        "ActiveState": "active",
    }, f"Fix: sudo systemctl enable --now {unit}"
