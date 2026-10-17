"""Behaviour tests for deploy/engine-fingerprint.py, the engine's fingerprint (#11).

SocratiCode records model, prefixes and dimensions, never the engine, so an
Ollama that embeds differently looks the same to every client. The check-in
embeds a few fixed probes and compares them with the vectors the pinned
engine gave (``engine-fingerprint.json``):

- **Exit 0** when every probe's cosine is at least the threshold.
- **Exit 1** on drift, with one line naming the probes that moved. The
  check-in sends it as the ``ollama:engine`` finding's message, in JSON it
  builds by hand, so the line holds no quote or backslash.
- **Exit 2** when the probes couldn't be embedded at all: a timeout behind a
  client's batch is not evidence of a changed engine, and an unreachable
  Ollama is the check-in's ``ollama:tags`` finding already.

The probes hold uppercase and lowercase alike, so case-folding switched on or
off (0.21.2 → 0.40.5) moves at least one of them.

Ollama is a stub HTTP server in a thread; nothing leaves the test.
"""

import hashlib
import json
import math
import re
import subprocess
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "deploy" / "engine-fingerprint.py"
FIXTURE = REPO_ROOT / "deploy" / "engine-fingerprint.json"
DOCKERFILE = REPO_ROOT / "deploy" / "ollama-slim" / "Dockerfile"


def _vector(text: str) -> list[float]:
    """A unit vector that is a pure function of ``text``."""
    raw = [b - 127.5 for b in hashlib.sha512(text.encode()).digest()] * 12  # 768
    norm = math.sqrt(sum(x * x for x in raw))
    return [x / norm for x in raw]


class Engine:
    """The stub's behaviour, set per test."""

    def __init__(self) -> None:
        self.version = "0.34.0"
        self.casefold = False
        self.status = 200
        self.error = ""
        self.requests: list[dict] = []


@pytest.fixture
def engine() -> Iterator[tuple[Engine, str]]:
    state = Engine()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def _send(self, status: int, body: object) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if self.path == "/api/version":
                self._send(200, {"version": state.version})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state.requests.append(body)
            if state.status != 200:
                self._send(state.status, {"error": state.error})
                return
            texts = [t.lower() if state.casefold else t for t in body["input"]]
            self._send(200, {"embeddings": [_vector(t) for t in texts]})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield state, f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", "-I", str(SCRIPT), *args], capture_output=True, text=True, timeout=60
    )


@pytest.fixture
def captured(engine: tuple[Engine, str], tmp_path: Path) -> Path:
    _, url = engine
    out = tmp_path / "fingerprint.json"
    result = _run("capture", url, str(out))
    assert result.returncode == 0, result.stderr
    return out


def test_the_engine_that_captured_it_matches(engine: tuple[Engine, str], captured: Path) -> None:
    _, url = engine
    result = _run("check", url, str(captured))
    assert (result.returncode, result.stdout) == (0, ""), result.stderr


def test_capture_asks_as_socraticode_does(engine: tuple[Engine, str], captured: Path) -> None:
    """Same model name and truncation as SocratiCode 1.14.0's provider-ollama:
    the fingerprint is of the engine the clients use, as they use it."""
    state, _ = engine
    [request] = state.requests
    assert request["model"] == "nomic-embed-text"
    assert request["truncate"] is True
    data = json.loads(captured.read_text())
    assert data["ollama_version"] == "0.34.0"
    assert data["model"] == "nomic-embed-text"
    assert [p["text"] for p in data["probes"]] == request["input"]


def test_a_case_folding_engine_is_drift(engine: tuple[Engine, str], captured: Path) -> None:
    state, url = engine
    state.casefold, state.version = True, "0.40.5"
    result = _run("check", url, str(captured))
    assert result.returncode == 1, result.stderr
    [line] = result.stdout.splitlines()
    moved = [
        p["name"]
        for p in json.loads(captured.read_text())["probes"]
        if p["text"].lower() != p["text"]
    ]
    assert moved and all(name in line for name in moved)
    assert "0.40.5" in line and "0.34.0" in line


def test_a_wrong_dimension_is_drift(engine: tuple[Engine, str], captured: Path) -> None:
    _, url = engine
    data = json.loads(captured.read_text())
    data["probes"][0]["vector"] = data["probes"][0]["vector"][:384]
    captured.write_text(json.dumps(data))
    result = _run("check", url, str(captured))
    assert result.returncode == 1
    assert "dimension" in result.stdout


@pytest.mark.parametrize(
    "status, error", [(500, 'model "x" said \\no'), (404, "")], ids=["500", "404"]
)
def test_an_engine_that_cannot_embed_is_unknown_not_drift(
    engine: tuple[Engine, str], captured: Path, status: int, error: str
) -> None:
    state, url = engine
    state.status, state.error = status, error
    result = _run("check", url, str(captured))
    assert result.returncode == 2
    assert result.stdout == ""
    assert "unknown" in result.stderr


def test_an_unreachable_engine_is_unknown(captured: Path) -> None:
    result = _run("check", "http://127.0.0.1:9", str(captured))
    assert result.returncode == 2
    assert result.stdout == ""


def test_the_message_is_safe_in_hand_built_json(engine: tuple[Engine, str], captured: Path) -> None:
    """The check-in splices the line into JSON between quotes."""
    state, url = engine
    state.casefold, state.version = True, '0.40.5"\\x'
    result = _run("check", url, str(captured))
    assert result.returncode == 1
    assert re.fullmatch(r"[\w .,:;<>=()/+?-]+\n", result.stdout), result.stdout


def test_the_committed_fingerprint_is_of_the_pinned_engine() -> None:
    """Re-pinning without recapturing would report drift on every check-in,
    and a recapture without re-pinning would bless an unpinned engine."""
    data = json.loads(FIXTURE.read_text())
    [pinned] = re.findall(r"^FROM ollama/ollama:([\d.]+)@", DOCKERFILE.read_text(), re.M)
    assert data["ollama_version"] == pinned


def test_the_committed_probes_catch_case_folding_either_way() -> None:
    texts = [p["text"] for p in json.loads(FIXTURE.read_text())["probes"]]
    assert any(t != t.lower() for t in texts), "no probe holds an uppercase letter"
    assert any(t == t.lower() for t in texts), "no probe is all lowercase: the control"
    assert all(len(p["vector"]) == 768 for p in json.loads(FIXTURE.read_text())["probes"])
