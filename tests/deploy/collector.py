"""The pinned collector, started for a test the way ``otelcol-run.sh`` starts it (D21, #33).

The run script's own ``docker run`` words are reused, so the image, the
config and every hardening flag under test are the ones that run. Three
things change, and only these: the name, the publish (loopback, a port Docker
picks) and the two host paths (the tracked config, and a scratch data
directory). The mode option of a mount is kept.

Docker is reached plainly where the user may (CI), and through ``sudo -n`` on
co-index, where ``exedev`` is out of group ``docker`` (D20). The data
directory is root's, mode 700, as on the host: the container's root has no
capability to write anywhere else, so its files are read back through
``sudo`` too. With no Docker the tests skip, except in CI and on co-index,
where that would leave the pipeline unproven.
"""

import json
import os
import shlex
import socket
import subprocess
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
    statement = text[text.index("exec docker run") :].replace("\\\n", " ")
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

    def files(self) -> list[str]:
        return _sudo("ls", "-A", str(self.data)).stdout.split()

    def raw(self, name: str) -> str:
        """``name``'s text in the data directory, or nothing if it isn't there."""
        read = _sudo("cat", str(self.data / name), check=False)
        return read.stdout if read.returncode == 0 else ""

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


@contextmanager
def running(tmp: Path, planted: dict[str, str] | None = None) -> Iterator[Collector]:
    """The collector, up and answering, with ``planted`` files already in its
    data directory. Stopped, and its root-owned directory removed, on exit."""
    docker_cmd = docker()
    data = tmp / "otelcol-data"
    name = f"otelcol-test-{uuid.uuid4().hex[:12]}"
    words = docker_run_words(RUN_SCRIPT)[3:]  # past `exec docker run`
    swaps = {"otelcol": name, f"${{ADDR}}:{PORT}:{PORT}": f"127.0.0.1::{PORT}"}
    paths = {HOST_CONFIG: str(CONFIG), DATA_DIR: str(data)}
    args = []
    for word in words:
        word = swaps.get(word, word)
        source, colon, rest = word.partition(":")
        if colon and source in paths:
            word = f"{paths[source]}:{rest}"
        args.append(word)
    assert name in args and f"127.0.0.1::{PORT}" in args, "the run statement was not adapted"
    try:
        _sudo("install", "-d", "-m", "700", "-o", "root", "-g", "root", str(data))
        for planted_name, content in (planted or {}).items():
            _sudo("sh", "-c", 'printf %s "$1" > "$2"', "sh", content, str(data / planted_name))
        started = subprocess.run([*docker_cmd, "run", "-d", *args], capture_output=True, text=True)
        assert started.returncode == 0, started.stderr
        collector = Collector(_published(docker_cmd, name), data, name, docker_cmd)
        _await_ready(collector)
        yield collector
    finally:
        subprocess.run([*docker_cmd, "rm", "-f", name], capture_output=True)
        _sudo("rm", "-rf", str(data), check=False)


def _published(docker_cmd: list[str], name: str) -> int:
    shown = subprocess.run(
        [*docker_cmd, "port", name, f"{PORT}/tcp"], capture_output=True, text=True, check=True
    ).stdout
    return int(shown.splitlines()[0].rsplit(":", 1)[1])


def _await_ready(collector: Collector, seconds: float = 30) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if collector.post("logs", {})[0] == 200:
                return
        except OSError:
            pass
        time.sleep(0.3)
    pytest.fail(f"the collector never answered. {collector.logs()[-3000:]}")
