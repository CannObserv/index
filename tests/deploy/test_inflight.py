"""Behaviour tests for deploy/index-inflight.sh, the patching knob's ``inflight`` (#9).

``patching-hosts`` refuses a step unless every ``inflight`` command prints
``0`` and exits 0. Nothing on co-index sees an index run, its own (D20)
included, except Qdrant: its access log records every request. The script
counts the **writes** in the last five minutes.

- **From the journal, not ``docker logs`` (#18).** ``qdrant-run.sh`` runs
  ``docker run`` attached, so Qdrant's log is ``qdrant.service``'s journal,
  which ``exedev`` reads through group ``adm``. The knob's commands never run
  as root, and D20 takes ``exedev`` out of group ``docker``. Measured
  2026-11-06 over 2 h: ``journalctl -o cat`` and ``docker logs`` printed the
  same 1441 lines, and the write pattern counted 641 in each.
- **Writes, not reads.** A search or a retrieve is ``POST`` too, and a session
  searching is not an index run. ``PUT``, ``PATCH`` and ``DELETE`` are writes,
  and so are the ``POST`` point endpoints that change points.
- **Fail closed.** When the journal can't be read, whether work is in flight
  is unknown: the script exits non-zero and prints no count, and the gate
  refuses. So too when journald reports it dropped the unit's lines (its rate
  limit): a dropped write would read as 0.
- **A proxy.** A run pauses between embedding batches, so five quiet minutes
  are evidence, not proof.
- **A silent log is not an idle one.** Qdrant doesn't log ``/readyz``, and
  above INFO it logs no request at all, so an empty window could mean either.
  The script first makes one unauthenticated request, which Qdrant logs as a
  401, carrying a unique ``User-Agent``. Until that marker is in the log, the
  count means nothing, and the script fails closed.

``journalctl`` and ``curl`` are stubbed on ``PATH``; nothing reaches the real
journal or Qdrant. The ``curl`` stub plays Qdrant's logger: it appends the
request's log line to what ``journalctl`` prints, unless ``QDRANT_LOGS=0``.

``fixtures/qdrant-journal.txt`` is ``journalctl -u qdrant.service -o cat``
on co-index (2026-11-03..06): the restart of 2026-11-03T17:59Z, then one line
for each request shape the store served in three days. Edited for a public
repo only: the tailnet address is ``100.64.0.1``, and other projects'
collection names are ``index``'s. Request lines, statuses and agents are as
logged.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "deploy" / "index-inflight.sh"
JOURNAL = Path(__file__).parent / "fixtures" / "qdrant-journal.txt"


def _line(method: str, path: str, status: int = 200, http: str = "1.1") -> str:
    return (
        "2026-10-08T16:07:04.357383Z  INFO actix_web::middleware::logger: 172.17.0.1 "
        f'"{method} {path} HTTP/{http}" {status} 74 "-" "qdrant-js/1.18.0" 0.004820'
    )


def _stub(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(0o755)


@pytest.fixture
def stubs(tmp_path: Path) -> Path:
    """``journalctl`` logs its argv, prints ``$LOG`` and exits ``$RC``. ``curl``
    logs its argv and, as Qdrant would, appends the request's line to
    ``$LOG`` with its ``-A`` user agent, then exits ``$CURL_RC``."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub(
        bin_dir / "journalctl", 'printf "%s\\n" "$*" >> "$ARGV_LOG"\ncat "$LOG"\nexit "${RC:-0}"\n'
    )
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
    stubs: Path, lines: list[str], rc: int = 0, logs: bool = True, curl_rc: int = 0
) -> subprocess.CompletedProcess[str]:
    log = stubs / "qdrant.log"
    log.write_text("".join(f"{ln}\n" for ln in lines))
    env = {
        **os.environ,
        "PATH": f"{stubs / 'bin'}:{os.environ['PATH']}",
        "LOG": str(log),
        "ARGV_LOG": str(stubs / "argv"),
        "CURL_LOG": str(stubs / "curl"),
        "RC": str(rc),
        "QDRANT_LOGS": "1" if logs else "0",
        "CURL_RC": str(curl_rc),
    }
    return subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env)


def test_reads_the_last_five_minutes_of_qdrants_journal(stubs: Path) -> None:
    """The unit's journal, messages only: ``-o cat`` prints each line as
    Qdrant wrote it, as ``docker logs`` did. Never through ``docker`` (D20)."""
    _run(stubs, [])
    assert (stubs / "argv").read_text() == "-u qdrant.service --since -5min -o cat --no-pager\n"


def test_never_calls_docker() -> None:
    code = [ln for ln in SCRIPT.read_text().splitlines() if not ln.lstrip().startswith("#")]
    assert not [ln for ln in code if re.search(r"\bdocker\b", ln)]


def test_counts_the_writes_in_the_real_journal(stubs: Path) -> None:
    """The fixture's four writes: the metadata upsert, a payload index, a
    points delete and a collection create. Its reads, a retrieve by id
    included, and the restart's lines count nothing."""
    done = _run(stubs, JOURNAL.read_text().splitlines())
    assert (done.returncode, done.stdout) == (0, "4\n")


def test_the_real_marker_line_is_the_marker(stubs: Path) -> None:
    """The fixture's first line is an earlier run's marker as the journal
    holds it: the stub's line for this run must take the same shape."""
    first = JOURNAL.read_text().splitlines()[0]
    agent = re.search(r'"(index-inflight/[^"]+)"', first)
    assert agent is not None
    _run(stubs, [])
    stub_line = (stubs / "qdrant.log").read_text().splitlines()[-1]
    shape = re.compile(
        r"^\S+  INFO actix_web::middleware::logger: \S+ "
        r'"GET /collections HTTP/2\.0" 401 56 "-" "index-inflight/\d+\.\d+" \d\.\d+$'
    )
    assert shape.match(first)
    assert shape.match(stub_line)


# journald's report, as it printed one for a transient unit on co-index
# (2026-11-06, ``LogRateLimitBurst=5``), with this unit's name.
SUPPRESSED = "Suppressed 29 messages from qdrant.service"


def test_fails_closed_when_journald_dropped_qdrants_lines(stubs: Path) -> None:
    """journald drops a unit's lines past its rate limit and reports how
    many when the next one arrives, as the unit's own entry. The marker is at
    the latest that next line, so a drop in the window is reported before it."""
    done = _run(stubs, [_line("PUT", "/collections/c/points"), SUPPRESSED])
    assert done.returncode != 0
    assert done.stdout == ""
    assert "suppressed" in done.stderr


@pytest.mark.parametrize(
    "line",
    [
        "Suppressed 29 messages from ollama.service",
        _line("GET", "/collections").replace("qdrant-js/1.18.0", SUPPRESSED),
    ],
)
def test_another_report_is_not_a_drop(stubs: Path, line: str) -> None:
    """Another unit's report isn't this one's, and a client can't fake one
    from its user agent: only journald's own line, whole, counts."""
    done = _run(stubs, [line])
    assert (done.returncode, done.stdout) == (0, "0\n")


def test_an_empty_log_prints_zero(stubs: Path) -> None:
    done = _run(stubs, [])
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
def test_a_write_is_counted(stubs: Path, method: str, path: str) -> None:
    done = _run(stubs, [_line(method, path)])
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
def test_a_read_is_not_counted(stubs: Path, method: str, path: str) -> None:
    done = _run(stubs, [_line(method, path)])
    assert (done.returncode, done.stdout) == (0, "0\n")


@pytest.mark.parametrize("http", ["1.1", "2.0"])
def test_the_protocol_version_does_not_matter(stubs: Path, http: str) -> None:
    """qdrant-js speaks HTTP/1.1, but a request over TLS can be HTTP/2: curl's
    on co-index is logged as ``HTTP/2.0`` (measured 2026-10-08)."""
    lines = [
        _line("PUT", "/collections/c/points", http=http),
        _line("POST", "/collections/c/points/delete", http=http),
        _line("POST", "/collections/c/points/search", http=http),
    ]
    done = _run(stubs, lines)
    assert (done.returncode, done.stdout) == (0, "2\n")


def test_counts_every_write_among_other_lines(stubs: Path) -> None:
    lines = [
        "2026-10-08T16:00:00Z  INFO qdrant: Qdrant HTTP listening on 6333",
        _line("PUT", "/collections/c/points?wait=true"),
        _line("POST", "/collections/c/points/search"),
        _line("POST", "/collections/c/points/delete"),
        # A failed write is still a client at work.
        _line("PUT", "/collections/c/points", status=500),
    ]
    done = _run(stubs, lines)
    assert (done.returncode, done.stdout) == (0, "3\n")


def test_fails_closed_when_journalctl_fails(stubs: Path) -> None:
    """A user with no journal it may read at all gets this, and exit 1
    (measured as ``nobody``, 2026-11-06). ``exedev`` without ``adm`` still
    has its own: that case is ``test_an_empty_journal_names_the_group``. No
    count is printed, so the gate can't read an error as zero."""
    done = _run(stubs, ["No journal files were opened due to insufficient permissions."], rc=1)
    assert done.returncode != 0
    assert done.stdout == ""
    assert "unknown" in done.stderr


def test_an_empty_journal_names_the_group(stubs: Path) -> None:
    """Without group ``adm``, ``exedev`` still has its own user journal, so
    ``journalctl -u qdrant.service`` exits 0 and prints nothing (measured
    2026-11-06, ``setpriv --clear-groups``). That fails closed on the marker,
    and the message must point at the group, not at Qdrant's log level."""
    done = _run(stubs, [], logs=False)
    assert done.returncode != 0
    assert done.stdout == ""
    assert "group adm" in done.stderr


def test_fails_closed_without_journalctl(tmp_path: Path) -> None:
    env = {**os.environ, "PATH": str(tmp_path)}
    done = subprocess.run(["/usr/bin/bash", str(SCRIPT)], capture_output=True, text=True, env=env)
    assert done.returncode != 0
    assert done.stdout == ""


@pytest.mark.parametrize("flags", ["-cE", "-qxE"])
def test_fails_closed_when_grep_fails(stubs: Path, flags: str) -> None:
    """grep exits 1 for no match, which is a count of 0 or no drop, and 2 for
    an error. An error must not leave the script exiting 0 with nothing
    printed, nor read as no drop."""
    # Fails one grep alone, so the marker check before it still passes.
    _stub(
        stubs / "bin" / "grep",
        f"[[ $1 == {flags} ]] && {{ echo 'grep: boom' >&2; exit 2; }}\n"
        f'exec {shutil.which("grep")} "$@"\n',
    )
    done = _run(stubs, [_line("PUT", "/collections/c/points")])
    assert done.returncode != 0
    assert done.stdout == ""
    assert "grep failed" in done.stderr


def test_its_own_marker_request_is_unauthenticated_and_to_the_clients_url(stubs: Path) -> None:
    """No key: the knob's commands run as the invoking user, who can't read
    it, and a 401 is logged like any request. The name, not the address:
    Qdrant's certificate covers only the MagicDNS name (D14)."""
    _run(stubs, [])
    (call,) = (stubs / "curl").read_text().splitlines()
    assert "https://index.taild0fb76.ts.net:6333/collections" in call.split()
    assert "api-key" not in call.lower()
    assert re.search(r"-A index-inflight/\S+", call)


def test_its_own_marker_request_is_not_counted(stubs: Path) -> None:
    done = _run(stubs, [_line("PUT", "/collections/c/points")])
    assert (done.returncode, done.stdout) == (0, "1\n")


def test_fails_closed_when_qdrant_logs_no_requests(stubs: Path) -> None:
    """Qdrant above INFO: no write is logged, and neither is the marker. An
    empty window then means nothing, and the gate must not read it as 0."""
    warn = "2026-10-08T16:00:00Z  WARN qdrant: a line above INFO"
    done = _run(stubs, [warn], logs=False)
    assert done.returncode != 0
    assert done.stdout == ""
    assert "access log" in done.stderr


def test_fails_closed_when_qdrant_is_unreachable(stubs: Path) -> None:
    done = _run(stubs, [], curl_rc=7)
    assert done.returncode != 0
    assert done.stdout == ""
    assert "unknown" in done.stderr
