"""Behaviour tests for deploy/index-inflight.sh, the patching knob's ``inflight`` (#9, #41).

``patching-hosts`` refuses a step unless every ``inflight`` command prints
``0`` and exits 0. Nothing on co-index sees an index run, its own (D20)
included, except the store: Qdrant's access log records every request, and
Ollama's every embed. The script counts Qdrant's **writes** and Ollama's
**batches** in the last five minutes.

- **Ollama's batches (#41).** A run can embed for over five minutes between
  upserts: on 2026-11-08 an archiver run embedded from 19:14 to 19:48 UTC,
  1 to 2.5 min a batch, and Qdrant's writes read 0 at 19:17:55. Ollama runs
  one request at a time (``n_slots = 1``), and llama-server logs each input
  as it starts it (``launch_slot_ … processing task``), so the inputs between
  two ``/api/embed`` request lines are one request's. A batch is a request of
  10 inputs or more, or one that took 10 s or more; inputs past the last
  request line, 10 or more, are a batch still running; 50 inputs in the
  window from smaller requests, when nothing else counted, are one. Measured over 7 days
  (2,379 requests): a search embeds 1 input, the engine fingerprint (D19)
  embeds 5, every 10 min, in about 1.1 s, and neither ever crossed either
  line. Counted that way, the gate missed 1.6% of index-run time, all of it
  in a run's first minute.

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
journal, Qdrant or Ollama. The ``curl`` stub plays both loggers: it appends
the request's log line to the unit's journal, unless ``QDRANT_LOGS=0`` or
``OLLAMA_LOGS=0``.

``fixtures/qdrant-journal.txt`` is ``journalctl -u qdrant.service -o cat``
on co-index (2026-11-03..06): the restart of 2026-11-03T17:59Z, then one line
for each request shape the store served in three days. Edited for a public
repo only: the tailnet address is ``100.64.0.1``, and other projects'
collection names are ``index``'s. Request lines, statuses and agents are as
logged.

``fixtures/ollama-journal.txt`` is ``journalctl -u ollama.service -o cat``
on co-index, four real slices put together: an earlier run's marker
(2026-11-08), the engine fingerprint's request as the model finished loading
(2026-11-08, its load dump cut), a client's 28-input batch of 16.9 s
(2026-11-06), and a request a client gave up on after 30 s (2026-11-08, a
400). Edited for a public repo only: tailnet addresses are ``100.64.0.1``.
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
OLLAMA_JOURNAL = Path(__file__).parent / "fixtures" / "ollama-journal.txt"


def _line(method: str, path: str, status: int = 200, http: str = "1.1") -> str:
    return (
        "2026-10-08T16:07:04.357383Z  INFO actix_web::middleware::logger: 172.17.0.1 "
        f'"{method} {path} HTTP/{http}" {status} 74 "-" "qdrant-js/1.18.0" 0.004820'
    )


def _gin(method: str, path: str, took: str = "1.102474403s", status: int = 200) -> str:
    """A request line as Ollama's gin logger writes it."""
    return (
        f"[GIN] 2026/11/08 - 22:48:52 | {status} | {took:>13} |    100.64.0.1 | "
        f'{method:<8} "{path}"'
    )


def _task(n: int) -> str:
    """llama-server's line as it starts one input."""
    return f"slot launch_slot_: id  0 | task {n} | processing task, is_child = 0"


def _embed(inputs: int, took: str = "1.102474403s", status: int = 200) -> list[str]:
    """One /api/embed request: its inputs' lines, then its request line."""
    return [_task(2 * i) for i in range(inputs)] + [_gin("POST", "/api/embed", took, status)]


def _stub(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(0o755)


@pytest.fixture
def stubs(tmp_path: Path) -> Path:
    """``journalctl`` logs its argv, prints ``$LOG`` for ``qdrant.service`` or
    ``$OLOG`` for ``ollama.service``, and exits ``$RC`` (``$ORC``). ``curl``
    logs its argv and plays the server it's sent to: Qdrant appends the
    request's line to ``$LOG`` with its ``-A`` user agent, and exits
    ``$CURL_RC``; Ollama appends its 404 for the path to ``$OLOG``, and exits
    ``$OCURL_RC``."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub(
        bin_dir / "journalctl",
        'printf "%s\\n" "$*" >> "$ARGV_LOG"\n'
        'case " $* " in\n'
        '  *" ollama.service "*) cat "$OLOG"; exit "${ORC:-0}" ;;\n'
        '  *) cat "$LOG"; exit "${RC:-0}" ;;\n'
        "esac\n",
    )
    _stub(
        bin_dir / "curl",
        'printf "%s\\n" "$*" >> "$CURL_LOG"\n'
        "ua= prev= url=\n"
        'for a in "$@"; do [[ $prev == -A ]] && ua=$a; [[ $a == http* ]] && url=$a; prev=$a; done\n'
        "if [[ $url == *:11434/* ]]; then\n"
        "  if [[ $OLLAMA_LOGS == 1 && ${OCURL_RC:-0} == 0 ]]; then\n"
        "    printf '[GIN] 2026/11/08 - 23:07:04 | 404 |     176.367µs |    100.64.0.1 | "
        'GET      "%s"\\n\' \\\n'
        '      "/${url#*:11434/}" >> "$OLOG"\n'
        "  fi\n"
        '  exit "${OCURL_RC:-0}"\n'
        "fi\n"
        "if [[ $QDRANT_LOGS == 1 && ${CURL_RC:-0} == 0 ]]; then\n"
        '  printf \'%s "GET /collections HTTP/2.0" 401 56 "-" "%s" 0.000036\\n\' \\\n'
        '    "2026-10-08T18:10:47.400985Z  INFO actix_web::middleware::logger: 100.64.0.1" \\\n'
        '    "$ua" >> "$LOG"\n'
        "fi\n"
        'exit "${CURL_RC:-0}"\n',
    )
    return tmp_path


def _run(
    stubs: Path,
    lines: list[str],
    rc: int = 0,
    logs: bool = True,
    curl_rc: int = 0,
    ollama: list[str] | None = None,
    orc: int = 0,
    ollama_logs: bool = True,
    ocurl_rc: int = 0,
) -> subprocess.CompletedProcess[str]:
    env = _run_env(stubs, lines, ollama, rc, logs, curl_rc, orc, ollama_logs, ocurl_rc)
    return subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env)


def _run_env(
    stubs: Path,
    lines: list[str],
    ollama: list[str] | None = None,
    rc: int = 0,
    logs: bool = True,
    curl_rc: int = 0,
    orc: int = 0,
    ollama_logs: bool = True,
    ocurl_rc: int = 0,
) -> dict[str, str]:
    log = stubs / "qdrant.log"
    log.write_text("".join(f"{ln}\n" for ln in lines))
    olog = stubs / "ollama.log"
    olog.write_text("".join(f"{ln}\n" for ln in ollama or []))
    env = {
        **os.environ,
        "PATH": f"{stubs / 'bin'}:{os.environ['PATH']}",
        "LOG": str(log),
        "OLOG": str(olog),
        "ARGV_LOG": str(stubs / "argv"),
        "CURL_LOG": str(stubs / "curl"),
        "RC": str(rc),
        "ORC": str(orc),
        "QDRANT_LOGS": "1" if logs else "0",
        "OLLAMA_LOGS": "1" if ollama_logs else "0",
        "CURL_RC": str(curl_rc),
        "OCURL_RC": str(ocurl_rc),
    }
    return env


def test_reads_the_last_five_minutes_of_both_journals(stubs: Path) -> None:
    """Each unit's journal, messages only: ``-o cat`` prints each line as
    the server wrote it, as ``docker logs`` did. Never through ``docker`` (D20)."""
    _run(stubs, [])
    assert (stubs / "argv").read_text().splitlines() == [
        "-u qdrant.service --since -5min -o cat --no-pager",
        "-u ollama.service --since -5min -o cat --no-pager",
    ]


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
    (call,) = [c for c in (stubs / "curl").read_text().splitlines() if ":6333" in c]
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


# --- Ollama's batches (#41) ---


def test_counts_the_batches_in_the_real_journal(stubs: Path) -> None:
    """The fixture's 28-input batch, and the request a client gave up on
    after 30 s. The engine fingerprint's 5 inputs in 1.1 s, the marker and
    ``/api/tags`` count nothing."""
    done = _run(stubs, [], ollama=OLLAMA_JOURNAL.read_text().splitlines())
    assert (done.returncode, done.stdout) == (0, "2\n")


def test_the_helpers_write_ollamas_real_lines() -> None:
    """So the tests below test the format Ollama writes."""
    real = OLLAMA_JOURNAL.read_text().splitlines()
    assert _task(10) in real
    assert _gin("POST", "/api/embed") in real


def test_the_real_marker_line_is_ollamas_marker(stubs: Path) -> None:
    first = OLLAMA_JOURNAL.read_text().splitlines()[0]
    _run(stubs, [])
    stub_line = (stubs / "ollama.log").read_text().splitlines()[-1]
    shape = re.compile(
        r'^\[GIN\] \S+ - \S+ \| 404 \| +\S+ \| +\S+ \| GET +"/index-inflight/\d+\.\d+"$'
    )
    assert shape.match(first)
    assert shape.match(stub_line)


@pytest.mark.parametrize(
    ("inputs", "took", "count"),
    [
        (1, "87.715943ms", 0),  # a search
        (5, "1.102474403s", 0),  # the engine fingerprint
        (9, "9.98s", 0),
        (10, "1.5s", 1),  # by its inputs
        (2, "10s", 1),  # by its time
        (28, "16.879952533s", 1),
        (40, "1m20s", 1),
    ],
)
def test_a_batch_is_ten_inputs_or_ten_seconds(
    stubs: Path, inputs: int, took: str, count: int
) -> None:
    done = _run(stubs, [], ollama=_embed(inputs, took))
    assert (done.returncode, done.stdout) == (0, f"{count}\n")


def test_searches_never_add_up_to_a_batch(stubs: Path) -> None:
    """Inputs are counted per request: six searches and six fingerprints in
    the window are 36 inputs, and no batch."""
    lines = [ln for _ in range(6) for ln in _embed(1, "90ms") + _embed(5)]
    done = _run(stubs, [], ollama=lines)
    assert (done.returncode, done.stdout) == (0, "0\n")


def test_a_batch_still_running_counts(stubs: Path) -> None:
    """Its request line comes when it ends: ten inputs since the last one
    are a batch, before it has one."""
    done = _run(stubs, [], ollama=_embed(5) + [_task(100 + i) for i in range(10)])
    assert (done.returncode, done.stdout) == (0, "1\n")


def test_nine_inputs_running_are_not_yet_a_batch(stubs: Path) -> None:
    done = _run(stubs, [], ollama=[_task(i) for i in range(9)])
    assert (done.returncode, done.stdout) == (0, "0\n")


def test_a_request_a_client_gave_up_on_still_counts(stubs: Path) -> None:
    """A 400 after 30 s: a batch was ahead of it, whatever its status."""
    done = _run(stubs, [], ollama=_embed(1, "30.059689033s", status=400))
    assert (done.returncode, done.stdout) == (0, "1\n")


@pytest.mark.parametrize(
    ("took", "count"),
    [
        ("176.367µs", 0),
        ("500ns", 0),
        ("87.715943ms", 0),
        ("9.999999s", 0),
        ("10.000001s", 1),
        ("1m0s", 1),
        ("2m33.5s", 1),
        ("1h0m0s", 1),
    ],
)
def test_reads_go_durations(stubs: Path, took: str, count: int) -> None:
    done = _run(stubs, [], ollama=_embed(1, took))
    assert (done.returncode, done.stdout) == (0, f"{count}\n")


def test_reads_durations_in_the_c_locale(stubs: Path) -> None:
    """A knob command's locale is whatever runuser leaves: µ is two bytes."""
    lines = [_task(i) for i in range(4)] + [_gin("POST", "/api/embed", "176.367µs")]
    done = subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        env={**_run_env(stubs, [], lines), "LC_ALL": "C"},
    )
    assert (done.returncode, done.stdout) == (0, "0\n")


def test_other_requests_neither_count_nor_split_a_request(stubs: Path) -> None:
    """``/api/tags`` (the knob's health line, SocratiCode's check) and
    ``/api/version`` come mid-batch: the batch's inputs still add up."""
    lines = [_task(i) for i in range(5)]
    lines += [_gin("GET", "/api/tags", "822.562µs"), _gin("GET", "/api/version", "50µs")]
    lines += _embed(5)
    done = _run(stubs, [], ollama=lines)
    assert (done.returncode, done.stdout) == (0, "1\n")


def test_the_legacy_endpoint_is_an_embed_too(stubs: Path) -> None:
    lines = [_task(i) for i in range(12)] + [_gin("POST", "/api/embeddings")]
    done = _run(stubs, [], ollama=lines)
    assert (done.returncode, done.stdout) == (0, "1\n")


def test_qdrant_writes_and_ollama_batches_add_up(stubs: Path) -> None:
    done = _run(
        stubs,
        [_line("PUT", "/collections/c/points"), _line("POST", "/collections/c/points/delete")],
        ollama=_embed(30, "40s") + _embed(1),
    )
    assert (done.returncode, done.stdout) == (0, "3\n")


def test_its_marker_request_to_ollama(stubs: Path) -> None:
    """A path nothing serves: a 404 in about 0.2 ms that loads no model, and
    gin logs it with the path (measured 2026-11-08). The name, as clients
    reach it."""
    _run(stubs, [])
    (call,) = [c for c in (stubs / "curl").read_text().splitlines() if ":11434" in c]
    (url,) = [a for a in call.split() if a.startswith("http")]
    assert re.fullmatch(r"http://index\.taild0fb76\.ts\.net:11434/index-inflight/\d+\.\d+", url)


def test_one_marker_for_both_logs(stubs: Path) -> None:
    _run(stubs, [])
    calls = (stubs / "curl").read_text()
    (agent,) = re.findall(r"-A (index-inflight/\S+)", calls)
    assert f":11434/{agent}" in calls


def test_fails_closed_when_ollama_logs_no_requests(stubs: Path) -> None:
    done = _run(stubs, [], ollama=["srv  update_slots: all slots are idle"], ollama_logs=False)
    assert done.returncode != 0
    assert done.stdout == ""
    assert "ollama.service" in done.stderr
    assert "marker" in done.stderr


def test_an_empty_ollama_journal_names_the_group(stubs: Path) -> None:
    done = _run(stubs, [], ollama_logs=False)
    assert done.returncode != 0
    assert done.stdout == ""
    assert "group adm" in done.stderr
    assert "ollama.service" in done.stderr


def test_fails_closed_when_ollama_is_unreachable(stubs: Path) -> None:
    done = _run(stubs, [], ocurl_rc=7)
    assert done.returncode != 0
    assert done.stdout == ""
    assert "ollama" in done.stderr


def test_fails_closed_when_journalctl_fails_on_ollama(stubs: Path) -> None:
    done = _run(stubs, [], orc=1)
    assert done.returncode != 0
    assert done.stdout == ""
    assert "unknown" in done.stderr


def test_fails_closed_when_journald_dropped_ollamas_lines(stubs: Path) -> None:
    """A batch logs four lines an input; the busiest 30 s in 7 days was 554
    lines, and journald dropped none in 30 days. A drop would hide inputs."""
    done = _run(stubs, [], ollama=["Suppressed 2000 messages from ollama.service"])
    assert done.returncode != 0
    assert done.stdout == ""
    assert "suppressed" in done.stderr


def test_fails_closed_when_embeds_show_no_inputs(stubs: Path) -> None:
    """Every request logs at least one input. Requests without any mean
    llama-server's lines changed, and every batch would read as 0 inputs."""
    done = _run(stubs, [], ollama=[_gin("POST", "/api/embed", "2s")])
    assert done.returncode != 0
    assert done.stdout == ""
    assert "format" in done.stderr


def test_fails_closed_on_a_duration_it_cant_read(stubs: Path) -> None:
    done = _run(stubs, [], ollama=[_task(0), _gin("POST", "/api/embed", "soon")])
    assert done.returncode != 0
    assert done.stdout == ""
    assert "format" in done.stderr


def test_a_client_cant_hide_a_batch_with_its_path(stubs: Path) -> None:
    """A path is the client's to choose, but it can't end a request line or
    start a task line: a request line faked inside a path ends no batch."""
    fake = _gin("GET", '/x" | 200 | 1s | 1.1.1.1 | POST     "/api/embed')
    lines = [_task(i) for i in range(6)] + [fake] + [_task(10 + i) for i in range(6)]
    done = _run(stubs, [], ollama=lines)
    assert (done.returncode, done.stdout) == (0, "1\n")


def test_a_stream_of_small_requests_is_a_batch(stubs: Path) -> None:
    """The runbook's re-embed gate (``reembed.py --dry-run --batch 8``) runs
    for hours against the live engine, 8 inputs and a few seconds a request,
    and writes nothing to Qdrant. 50 inputs in the window are a batch: 10
    times the fingerprint's 5. Over 7 days the rule held no window the
    per-request one didn't."""
    lines = [ln for _ in range(7) for ln in _embed(8, "4.2s")]
    done = _run(stubs, [], ollama=lines)
    assert (done.returncode, done.stdout) == (0, "1\n")


def test_forty_nine_inputs_in_small_requests_are_not(stubs: Path) -> None:
    lines = [ln for _ in range(7) for ln in _embed(7, "4.2s")]
    done = _run(stubs, [], ollama=lines)
    assert (done.returncode, done.stdout) == (0, "0\n")


def test_the_stream_rule_adds_nothing_to_a_counted_batch(stubs: Path) -> None:
    """A count says work is in flight; the rule names a stream nothing else
    counted, so a window with a batch already counted stays at its count."""
    lines = _embed(60, "50s")
    done = _run(stubs, [], ollama=lines)
    assert (done.returncode, done.stdout) == (0, "1\n")
