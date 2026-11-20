"""The pinned collector, started for a test the way ``otelcol-run.sh`` starts it (D21, #33).

The run script's own ``docker run`` words are reused, so the image, the
config and every hardening flag under test are the ones that run. Three
things change, and only these: the name, the publish (loopback, a port Docker
picks) and the two host paths (the tracked config, and a scratch data
directory). The mode option of a mount is kept. ``--rm`` is dropped, so a
collector that exits at its start still has logs to show.

**No mount may reach a host path.** On co-index the real directory is
``/var/lib/otelcol``, with the cohort's data in it. If the run script's
mounts changed and this didn't follow, the test container would mount it
read-write as root and write fixtures into it. ``adapt`` refuses any mount
whose source isn't the scratch directory or a file in this checkout, and any
mount spelled in a form it doesn't rewrite.

Docker is reached plainly where the user may (CI), and through ``sudo -n`` on
co-index, where ``exedev`` is out of group ``docker`` (D20). The data
directory is root's, mode 700, as on the host: the container's root has no
capability to write anywhere else, so its files are read back through
``sudo`` too. With no Docker the tests skip, except in CI and on co-index,
where that would leave the pipeline unproven.
"""

import calendar
import getpass
import json
import os
import re
import shlex
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "deploy"
RUN_SCRIPT = DEPLOY / "otelcol-run.sh"
CONFIG = DEPLOY / "otelcol.yaml"
HOST = "co-index"
HOST_CONFIG = "/usr/local/share/co-index/otelcol.yaml"
DATA_DIR = "/var/lib/otelcol"
PORT = 4318


def docker_run_words(script: Path) -> list[str]:
    """``script``'s ``exec docker run`` statement as words, continuation lines
    joined and the comments above it ignored."""
    text = script.read_text()
    start = re.search(r"^exec docker run", text, flags=re.M)  # never one in a comment
    assert start is not None, f"{script.name} has no `exec docker run` statement"
    statement = text[start.start() :].replace("\\\n", " ")
    return shlex.split(statement.splitlines()[0])


def options(words: list[str], flag: str) -> list[str]:
    """Every value ``flag`` is given in a ``docker run`` word list."""
    return [words[i + 1] for i, w in enumerate(words) if w == flag]


def _must_run() -> bool:
    return bool(os.environ.get("CI")) or socket.gethostname() == HOST


def _works(cmd: list[str]) -> bool:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=30).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def docker() -> list[str]:
    """The command that reaches Docker here."""
    for cmd in (["docker"], ["sudo", "-n", "docker"]):
        if _works([*cmd, "info"]) and _works(["sudo", "-n", "true"]):
            return cmd
    if _must_run():
        pytest.fail("no Docker, or no passwordless sudo, where the pipeline must be proven")
    pytest.skip("no Docker, or no passwordless sudo, on this host")


def _sudo(*cmd: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["sudo", "-n", *cmd], capture_output=True, text=True, check=check)


class Collector:
    """A running collector: where to send, and what it wrote."""

    def __init__(self, port: int, data: Path, name: str, docker_cmd: list[str]) -> None:
        self.url = f"http://127.0.0.1:{port}"
        self.data = data
        self.name = name
        self._docker = docker_cmd

    def post(self, signal: str, body: dict) -> tuple[int, str]:
        """One OTLP/HTTP JSON export; the status and the answer's text."""
        request = urllib.request.Request(
            f"{self.url}/v1/{signal}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as answer:
                return answer.status, answer.read().decode()
        except urllib.error.HTTPError as refused:
            return refused.code, refused.read().decode()

    def files(self, sub: str = "") -> list[str]:
        """The regular files in the data directory, or in ``sub`` under it."""
        found = _sudo(
            "find", str(self.data / sub), "-maxdepth", "1", "-type", "f", "-printf", "%f\\n"
        )
        return found.stdout.split()

    def raw(self, name: str) -> str:
        """``name``'s text in the data directory, or nothing if it isn't
        there. A file that is there and can't be read is a failure: read as
        empty, it would pass every "nothing was written" scan."""
        if _sudo("test", "-e", str(self.data / name), check=False).returncode != 0:
            return ""
        return _sudo("cat", str(self.data / name)).stdout

    def lines(self, name: str) -> list[dict]:
        return [json.loads(line) for line in self.raw(name).splitlines()]

    def wait_for(self, name: str, marker: str, seconds: float = 20) -> None:
        """Until ``marker`` is in ``name``. The exporter writes each export as
        it arrives, so the last one sent is the one to wait for."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if marker in self.raw(name):
                return
            time.sleep(0.2)
        pytest.fail(f"{marker!r} never reached {name}. {self.logs()[-2000:]}")

    def logs(self) -> str:
        shown = subprocess.run([*self._docker, "logs", self.name], capture_output=True, text=True)
        return shown.stdout + shown.stderr


MOUNT_FLAGS = ("--volume", "-v", "--mount", "--tmpfs", "--volumes-from")


def adapt(words: list[str], name: str, data: Path) -> list[str]:
    """``docker run`` arguments for a test, from the run script's own words
    (past ``exec docker run``): see the module's docstring."""
    swaps = {"otelcol": name, f"${{ADDR}}:{PORT}:{PORT}": f"127.0.0.1::{PORT}"}
    paths = {HOST_CONFIG: str(CONFIG), DATA_DIR: str(data)}
    args = []
    for word in words:
        if word == "--rm":
            continue
        word = swaps.get(word, word)
        source, colon, rest = word.partition(":")
        if colon and source in paths:
            word = f"{paths[source]}:{rest}"
        args.append(word)
    assert name in args and f"127.0.0.1::{PORT}" in args, "the run statement was not adapted"
    other = [w for w in args if w.startswith(MOUNT_FLAGS) and w != "--volume"]
    assert not other, f"a mount this helper does not rewrite: {other}"
    for source in (v.partition(":")[0] for v in options(args, "--volume")):
        inside = Path(source) == data or REPO_ROOT in Path(source).parents
        assert inside, f"the test container would mount the host's {source}"
    return args


def _reap_stale(docker_cmd: list[str], minutes: int = 30) -> None:
    """A pytest that was killed leaves its container running, 256 MiB of
    this host's 8 GB, and a root-owned directory its own cleanup can't
    remove. Nothing else would ever take them. Only what is older than any
    run: a second session's live container is left alone."""
    listed = subprocess.run(
        [*docker_cmd, "ps", "-a", "--filter", "name=otelcol-test-", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
    ).stdout.split()
    for name in listed:
        created = subprocess.run(
            [*docker_cmd, "inspect", "--format", "{{.Created}}", name],
            capture_output=True,
            text=True,
        ).stdout.strip()
        try:
            # .Created is UTC, to the nanosecond.
            age = time.time() - calendar.timegm(time.strptime(created[:19], "%Y-%m-%dT%H:%M:%S"))
        except ValueError:
            continue
        if age > minutes * 60:
            subprocess.run([*docker_cmd, "rm", "-f", name], capture_output=True)
    scratch = Path(tempfile.gettempdir()) / f"pytest-of-{getpass.getuser()}"
    if scratch.is_dir():
        _sudo(
            "find", str(scratch), "-maxdepth", "3", "-type", "d", "-name", "otelcol-data",
            "-user", "root", "-mmin", f"+{minutes}", "-prune", "-exec", "rm", "-rf", "{}", "+",
            check=False,
        )  # fmt: skip


@contextmanager
def running(tmp: Path, planted: dict[str, str] | None = None) -> Iterator[Collector]:
    """The collector, up and answering, with ``planted`` files already in its
    data directory. Stopped, and its root-owned directory removed, on exit."""
    docker_cmd = docker()
    _reap_stale(docker_cmd)
    data = tmp / "otelcol-data"
    name = f"otelcol-test-{uuid.uuid4().hex[:12]}"
    args = adapt(docker_run_words(RUN_SCRIPT)[3:], name, data)
    try:
        _sudo("install", "-d", "-m", "700", "-o", "root", "-g", "root", str(data))
        for planted_name, content in (planted or {}).items():
            target = data / planted_name
            _sudo("install", "-d", "-m", "700", str(target.parent))
            _sudo("sh", "-c", 'printf %s "$1" > "$2"', "sh", content, str(target))
        started = subprocess.run([*docker_cmd, "run", "-d", *args], capture_output=True, text=True)
        assert started.returncode == 0, started.stderr
        collector = Collector(0, data, name, docker_cmd)
        collector.url = f"http://127.0.0.1:{_published(collector)}"
        _await_ready(collector)
        yield collector
    finally:
        subprocess.run([*docker_cmd, "rm", "-f", name], capture_output=True)
        _sudo("rm", "-rf", str(data), check=False)


def _published(collector: Collector) -> int:
    """The loopback port Docker picked. A container that already exited has
    none, and says why in its logs: a config this image rejects, most often."""
    shown = subprocess.run(
        [*collector._docker, "port", collector.name, f"{PORT}/tcp"], capture_output=True, text=True
    )
    if shown.returncode != 0 or not shown.stdout.strip():
        pytest.fail(f"the collector is not running. Its logs: {collector.logs()[-3000:]}")
    return int(shown.stdout.splitlines()[0].rsplit(":", 1)[1])


def _await_ready(collector: Collector, seconds: float = 30) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if collector.post("logs", {})[0] == 200:
                return
        except OSError:
            pass
        state = subprocess.run(
            [*collector._docker, "inspect", "--format", "{{.State.Running}}", collector.name],
            capture_output=True,
            text=True,
        ).stdout.strip()
        if state != "true":
            break  # it exited: say why now, not after the wait
        time.sleep(0.3)
    pytest.fail(f"the collector never answered. {collector.logs()[-3000:]}")
