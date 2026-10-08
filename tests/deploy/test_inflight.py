"""Behaviour tests for deploy/index-inflight.sh, the patching knob's ``inflight`` (#9).

``patching-hosts`` refuses a step unless every ``inflight`` command prints
``0`` and exits 0. Clients index from their own nodes (D11), so nothing on
co-index sees a client's run, except Qdrant: its access log records every
request. The script counts the **writes** in the last five minutes.

- **Writes, not reads.** A search or a retrieve is ``POST`` too, and a session
  searching is not an index run. ``PUT``, ``PATCH`` and ``DELETE`` are writes,
  and so are the ``POST`` point endpoints that change points.
- **Fail closed.** When ``docker logs`` fails, whether work is in flight is
  unknown: the script exits non-zero and prints no count, and the gate refuses.
- **A proxy.** A run pauses between embedding batches, so five quiet minutes
  are evidence, not proof.
- **A silent log is not an idle one.** Qdrant doesn't log ``/readyz``, and
  above INFO it logs no request at all, so an empty window could mean either.
  The script first makes one unauthenticated request, which Qdrant logs as a
  401, carrying a unique ``User-Agent``. Until that marker is in the log, the
  count means nothing, and the script fails closed.

``docker`` and ``curl`` are stubbed on ``PATH``; nothing reaches a real daemon
or Qdrant. The ``curl`` stub plays Qdrant's logger: it appends the request's
log line to what ``docker logs`` prints, unless ``QDRANT_LOGS=0``.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "deploy" / "index-inflight.sh"


def _line(method: str, path: str, status: int = 200, http: str = "1.1") -> str:
    return (
        "2026-10-08T16:07:04.357383Z  INFO actix_web::middleware::logger: 172.17.0.1 "
        f'"{method} {path} HTTP/{http}" {status} 74 "-" "qdrant-js/1.18.0" 0.004820'
    )


def _stub(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(0o755)


@pytest.fixture
def docker(tmp_path: Path) -> Path:
    """``docker`` logs its argv, prints ``$LOG`` and exits ``$RC``. ``curl``
    logs its argv and, as Qdrant would, appends the request's line to
    ``$LOG`` with its ``-A`` user agent, then exits ``$CURL_RC``."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub(bin_dir / "docker", 'printf "%s\\n" "$*" >> "$ARGV_LOG"\ncat "$LOG"\nexit "${RC:-0}"\n')
    _stub(
        bin_dir / "curl",
        'printf "%s\\n" "$*" >> "$CURL_LOG"\n'
        "ua= prev=\n"
        'for a in "$@"; do [[ $prev == -A ]] && ua=$a; prev=$a; done\n'
        "if [[ $QDRANT_LOGS == 1 && ${CURL_RC:-0} == 0 ]]; then\n"
        '  printf \'%s "GET /collections HTTP/2.0" 401 56 "-" "%s" 0.000036\\n\' \\\n'
        '    "2026-10-08T18:10:47.400985Z  INFO actix_web::middleware::logger: 100.64.0.1" \\\n'
        '    "$ua" >> "$LOG"\n'
        "fi\n"
        'exit "${CURL_RC:-0}"\n',
    )
    return tmp_path


def _run(
    docker: Path, lines: list[str], rc: int = 0, logs: bool = True, curl_rc: int = 0
) -> subprocess.CompletedProcess[str]:
    log = docker / "qdrant.log"
    log.write_text("".join(f"{ln}\n" for ln in lines))
    env = {
        **os.environ,
        "PATH": f"{docker / 'bin'}:{os.environ['PATH']}",
        "LOG": str(log),
        "ARGV_LOG": str(docker / "argv"),
        "CURL_LOG": str(docker / "curl"),
        "RC": str(rc),
        "QDRANT_LOGS": "1" if logs else "0",
        "CURL_RC": str(curl_rc),
    }
    return subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env)


def test_reads_the_last_five_minutes_of_qdrants_log(docker: Path) -> None:
    _run(docker, [])
    assert (docker / "argv").read_text() == "logs --since 5m qdrant\n"


def test_an_empty_log_prints_zero(docker: Path) -> None:
    done = _run(docker, [])
    assert (done.returncode, done.stdout) == (0, "0\n")


WRITES = [
    ("PUT", "/collections/c/points?wait=true"),
    ("PUT", "/collections/c/points"),
    ("POST", "/collections/c/points/delete?wait=true"),
    ("POST", "/collections/c/points/payload"),
    ("PUT", "/collections/c/points/payload"),
    ("POST", "/collections/c/points/payload/delete"),
    ("POST", "/collections/c/points/payload/clear"),
    ("PUT", "/collections/c/points/vectors"),
    ("POST", "/collections/c/points/vectors/delete"),
    ("POST", "/collections/c/points/batch"),
    ("PUT", "/collections/c"),
    ("PATCH", "/collections/c"),
    ("DELETE", "/collections/c"),
    ("PUT", "/collections/c/index"),
]


@pytest.mark.parametrize(("method", "path"), WRITES)
def test_a_write_is_counted(docker: Path, method: str, path: str) -> None:
    done = _run(docker, [_line(method, path)])
    assert (done.returncode, done.stdout) == (0, "1\n")


READS = [
    ("POST", "/collections/socraticode_metadata/points"),  # retrieve by id
    ("POST", "/collections/c/points/search"),
    ("POST", "/collections/c/points/search/batch"),
    ("POST", "/collections/c/points/query"),
    ("POST", "/collections/c/points/query/batch"),
    ("POST", "/collections/c/points/scroll"),
    ("POST", "/collections/c/points/count"),
    ("POST", "/collections/c/points/recommend"),
    ("POST", "/collections/c/snapshots"),  # the run's own recovery point
    ("POST", "/snapshots"),
    ("GET", "/collections"),
    ("GET", "/readyz"),
]


@pytest.mark.parametrize(("method", "path"), READS)
def test_a_read_is_not_counted(docker: Path, method: str, path: str) -> None:
    done = _run(docker, [_line(method, path)])
    assert (done.returncode, done.stdout) == (0, "0\n")


@pytest.mark.parametrize("http", ["1.1", "2.0"])
def test_the_protocol_version_does_not_matter(docker: Path, http: str) -> None:
    """qdrant-js speaks HTTP/1.1, but a request over TLS can be HTTP/2: curl's
    on co-index is logged as ``HTTP/2.0`` (measured 2026-10-08)."""
    lines = [
        _line("PUT", "/collections/c/points", http=http),
        _line("POST", "/collections/c/points/delete", http=http),
        _line("POST", "/collections/c/points/search", http=http),
    ]
    done = _run(docker, lines)
    assert (done.returncode, done.stdout) == (0, "2\n")


def test_counts_every_write_among_other_lines(docker: Path) -> None:
    lines = [
        "2026-10-08T16:00:00Z  INFO qdrant: Qdrant HTTP listening on 6333",
        _line("PUT", "/collections/c/points?wait=true"),
        _line("POST", "/collections/c/points/search"),
        _line("POST", "/collections/c/points/delete"),
        # A failed write is still a client at work.
        _line("PUT", "/collections/c/points", status=500),
    ]
    done = _run(docker, lines)
    assert (done.returncode, done.stdout) == (0, "3\n")


def test_fails_closed_when_docker_logs_fails(docker: Path) -> None:
    """``docker logs`` on a missing container prints its error and exits 1.
    No count is printed, so the gate can't read an error as zero."""
    done = _run(docker, ["Error response from daemon: No such container: qdrant"], rc=1)
    assert done.returncode != 0
    assert done.stdout == ""
    assert "unknown" in done.stderr


def test_fails_closed_without_docker(tmp_path: Path) -> None:
    env = {**os.environ, "PATH": str(tmp_path)}
    done = subprocess.run(["/usr/bin/bash", str(SCRIPT)], capture_output=True, text=True, env=env)
    assert done.returncode != 0
    assert done.stdout == ""


def test_fails_closed_when_grep_fails(docker: Path) -> None:
    """grep exits 1 for no match, which is a count of 0, and 2 for an error.
    An error must not leave the script exiting 0 with nothing printed: the
    gate would refuse, but say the work hadn't drained."""
    # Fails the count alone, so the marker check before it still passes.
    _stub(
        docker / "bin" / "grep",
        "[[ $1 == -cE ]] && { echo 'grep: boom' >&2; exit 2; }\n"
        f'exec {shutil.which("grep")} "$@"\n',
    )
    done = _run(docker, [_line("PUT", "/collections/c/points")])
    assert done.returncode != 0
    assert done.stdout == ""
    assert "grep failed" in done.stderr


def test_its_own_marker_request_is_unauthenticated_and_to_the_clients_url(docker: Path) -> None:
    """No key: the knob's commands run as the invoking user, who can't read
    it, and a 401 is logged like any request. The name, not the address:
    Qdrant's certificate covers only the MagicDNS name (D14)."""
    _run(docker, [])
    (call,) = (docker / "curl").read_text().splitlines()
    assert "https://index.taild0fb76.ts.net:6333/collections" in call.split()
    assert "api-key" not in call.lower()
    assert re.search(r"-A index-inflight/\S+", call)


def test_its_own_marker_request_is_not_counted(docker: Path) -> None:
    done = _run(docker, [_line("PUT", "/collections/c/points")])
    assert (done.returncode, done.stdout) == (0, "1\n")


def test_fails_closed_when_qdrant_logs_no_requests(docker: Path) -> None:
    """Qdrant above INFO: no write is logged, and neither is the marker. An
    empty window then means nothing, and the gate must not read it as 0."""
    done = _run(docker, [], logs=False)
    assert done.returncode != 0
    assert done.stdout == ""
    assert "access log" in done.stderr


def test_fails_closed_when_qdrant_is_unreachable(docker: Path) -> None:
    done = _run(docker, [], curl_rc=7)
    assert done.returncode != 0
    assert done.stdout == ""
    assert "unknown" in done.stderr
