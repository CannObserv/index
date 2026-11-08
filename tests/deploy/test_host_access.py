"""What ``exedev`` may do on co-index, as host state (D20, #18).

D20 takes ``exedev`` out of group ``docker`` before Node lands: the group is
root-equivalent, and without it a SocratiCode session that fell back to
managed mode fails at ``docker info``, before it pulls or starts a container.
The patching knob's ``inflight`` reads Qdrant's log from the journal instead,
so ``exedev`` must still read the system journal (group ``adm``).

Both are host state, so CI can't see them lapse (D20's accepted limits). They
run on co-index only, as ``exedev``, where each failure names its fix.
"""

import getpass
import grp
import os
import socket
import subprocess

import pytest

HOST = "co-index"
USER = "exedev"


@pytest.fixture(autouse=True)
def _as_exedev_on_co_index() -> None:
    if socket.gethostname() != HOST:
        pytest.skip(f"not {HOST}")
    if getpass.getuser() != USER:
        pytest.fail(f"run as {USER}: these check what its sessions can do")


def test_exedev_is_not_in_group_docker() -> None:
    assert USER not in grp.getgrnam("docker").gr_mem, f"Fix: sudo gpasswd -d {USER} docker"


def test_this_session_is_not_in_group_docker() -> None:
    """A session keeps the groups it started with: one from before the change
    still reaches the daemon."""
    assert grp.getgrnam("docker").gr_gid not in os.getgroups(), (
        "this session predates the group change. "
        "Fix: restart the VS Code server, then check `id -nG`"
    )


def _holders(ps_lines: str, gid: int) -> list[str]:
    """The pids among ``ps -o pid=,supgid=`` lines whose groups include ``gid``."""
    return [
        pid
        for pid, _, groups in (ln.strip().partition(" ") for ln in ps_lines.splitlines())
        if str(gid) in groups.strip().split(",")
    ]


def test_holders_reads_ps_by_whole_gid() -> None:
    measured = "  312 4,24,27,107,1000\n82888 107,1000\n 401 4,1070,1000\n"
    assert _holders(measured, 107) == ["312", "82888"]


def test_no_exedev_process_holds_group_docker() -> None:
    """Leaving the group changes the database, not a running process. On
    2026-11-06, with the database clean, the VS Code server (a window reload
    doesn't restart it), an orphaned VS Code CLI and the user manager (linger,
    up since boot) all still held it, and so would all they started."""
    gid = grp.getgrnam("docker").gr_gid
    listed = subprocess.run(
        ["ps", "-u", USER, "-o", "pid=,supgid="], capture_output=True, text=True, check=True
    ).stdout
    assert _holders(listed, gid) == [], (
        'still in group docker. Fix: deploy/README.md, "Node": restart the VS Code server '
        "and the user manager"
    )


@pytest.mark.parametrize("unit", ["qdrant.service", "ollama.service"])
def test_exedev_reads_the_stores_journals(unit: str) -> None:
    """``index-inflight.sh`` reads ``qdrant.service``'s and, since #41,
    ``ollama.service``'s journal as the knob user. Without ``adm``,
    ``exedev``'s ``journalctl`` reads only its own user journal: it exits 0
    and prints nothing, so the exit status alone proves nothing, and the gate
    fails closed on every window."""
    done = subprocess.run(
        ["journalctl", "-u", unit, "-n", "1", "-o", "cat", "--no-pager"],
        capture_output=True,
        text=True,
    )
    assert (done.returncode, bool(done.stdout.strip())) == (0, True), (
        f"{done.stderr.strip()} Fix: sudo usermod -aG adm {USER}, then restart the VS Code server"
    )
