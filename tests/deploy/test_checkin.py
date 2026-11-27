"""Behaviour tests for deploy/index-checkin.sh's credentials (#8) and body (#10).

The check-in has reported to co-status since notifier#83, under notifier's
names: ``NOTIFIER_API_KEY`` and ``NOTIFIER_MONITOR_ID`` in
``/etc/socraticode/notifier.env``, kept so that cutover was two values. They
are co-status's credentials, so they are named for it: ``STATUS_API_KEY`` and
``STATUS_MONITOR_ID`` in ``/etc/socraticode/status.env``.

- **The old file is not read, not even as a fallback.** A fallback keeps
  notifier's names, and a file holding them, on the host for good. Until
  ``status.env`` exists the check-in fails and co-status alerts after one
  missed deadline, which is why the cutover writes it first.
- **Both values are checked before anything is probed.** The file is written
  by hand at cutover, which is when a misnamed key happens. An empty key is
  otherwise sent as an empty header: a 401 that exits 0.
- **The credentials are sourced, not exported.** No probe reads them, so
  none is handed them in its environment.
- **No key is on curl's argv.** co-index's /proc has no hidepid, so an
  argument is readable by every user there while the call runs. Each key goes
  in a header file from a process substitution.
- **The engine's fingerprint is a finding only on drift (#11).**
  ``engine-fingerprint.py`` exits 1 on drift, which is ``ollama:engine`` with
  its line as the message, and 2 when it couldn't embed: no finding, since a
  timeout behind a client's batch is not a changed engine. It runs only once
  Ollama has answered with the model, so a down Ollama is one finding, not two.
- **The body keys its fault by ``check:subject``, as ``metadata.fault``.**
  co-status reports a run of ``alert`` check-ins once, and an ``alert`` whose
  key differs from the open fault's at once (CannObserv/status#28), so a new
  finding mid-fault is heard. The message stays out of it: it carries ages,
  codes and percentages, and would report every tick.

- **The collector's three findings (D21, #33).** ``otel:collector``: an
  empty export, on the port clients use, wasn't answered 200. Down is a
  finding, never unknown: nothing queues in front of it, as a client's batch
  does in front of Ollama's embed. ``otel:bytes``: its directory is over half
  the cap the retention run holds, so a flood or a wrong estimate is heard
  before the cap deletes anything. ``otel:retention``: that run hasn't
  succeeded in 36 hours, which would otherwise end the 90-day prune silently.

- **The cohort's declared links (#38).** ``link-check.py`` exits 1 with one
  ``subject<TAB>message`` line per finding, each the ``links:<subject>``
  finding, in its order. Exit 2 is unknown, no finding: Qdrant didn't answer
  it, and ``qdrant:collections`` is that finding. Any other exit, or 1 with
  nothing said, is ``links:check``: a broken check is heard. It runs only
  once the authenticated read returned 200, so a down store is one finding.

The script runs from a copy whose host paths point into ``tmp_path``, with
``curl`` and ``openssl`` stubbed on ``PATH``. Nothing leaves the test, and on
co-index it never touches the live timer's files.
"""

import json
import os
import re
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "deploy" / "index-checkin.sh"
HOST_PATHS = re.compile(
    r"/etc/socraticode/|/usr/local/bin/|/usr/local/share/co-index/|/tmp/index-checkin-"
    r"|/var/lib/otelcol|/var/cache/co-index/"
)
RETENTION = REPO_ROOT / "deploy" / "otelcol-retention.sh"
STAMP = ".retention-ok"
MIB = 1024 * 1024

KEY = "status-key"
MONITOR = "01MONITORID"
QDRANT_KEY = "qdrant-key"
STATUS_ENV = f"STATUS_API_KEY={KEY}\nSTATUS_MONITOR_ID={MONITOR}\n"


def _stub(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(0o755)


@pytest.fixture
def host(tmp_path: Path) -> Path:
    """``tmp_path`` laid out as the script's host paths, with no credential
    file. The collector's directory is there, its retention run just done."""
    (tmp_path / "otelcol").mkdir()
    (tmp_path / "otelcol" / STAMP).touch()
    (tmp_path / "etc" / "socraticode").mkdir(parents=True)
    (tmp_path / "etc" / "socraticode" / "qdrant.key").write_text(f"{QDRANT_KEY}\n")
    (tmp_path / "tmp").mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub(bin_dir / "tailnet-bind.sh", "echo 100.64.0.1\n")
    _stub(bin_dir / "openssl", "echo 'notAfter=Jan  1 00:00:00 2099 GMT'\n")
    # Logs its argv, prints STUB_FP_OUT, exits STUB_FP_EXIT (0: no drift).
    _stub(
        bin_dir / "engine-fingerprint.py",
        'printf "%s\\n" "$*" >> "$CURL_LOG.fp"\n'
        '[[ -z ${STUB_FP_OUT:-} ]] || printf "%s\\n" "$STUB_FP_OUT"\n'
        'exit "${STUB_FP_EXIT:-0}"\n',
    )
    # Logs its argv, prints STUB_LINKS_OUT (\t and \n as escapes), exits
    # STUB_LINKS_EXIT (0: clean).
    _stub(
        bin_dir / "link-check.py",
        'printf "%s\\n" "$*" >> "$CURL_LOG.links"\n'
        '[[ -z ${STUB_LINKS_OUT:-} ]] || printf "%b\\n" "$STUB_LINKS_OUT"\n'
        'exit "${STUB_LINKS_EXIT:-0}"\n',
    )
    # One line per call in each log: its argv as `ps` shows it, and what it
    # was sent, with each `-H @file` read. Plus any credential name it was
    # handed in its environment. The check-in's body arrives on stdin, and is
    # kept, which also drains the pipe into it so it never breaks.
    #
    # Qdrant, Ollama and the collector answer nothing, unless STUB_HEALTHY
    # is set: then Qdrant answers 200 with its key and 401 without, Ollama
    # lists the model, and the collector answers 200.
    _stub(
        bin_dir / "curl",
        'printf "%s\\n" "$*" >> "$CURL_LOG"\n'
        "sent=() prev=\n"
        'for a in "$@"; do\n'
        '  if [[ $prev == -H && $a == @* ]]; then a="$(cat "${a#@}")"; fi\n'
        '  sent+=("$a")\n'
        "  prev=$a\n"
        "done\n"
        'printf "%s\\n" "${sent[*]}" >> "$CURL_LOG.sent"\n'
        'env | grep -o "^STATUS_[A-Z_]*" >> "$CURL_LOG.env" || true\n'
        '[[ " $* " != *" @- "* ]] || cat > "$CURL_LOG.body"\n'
        # STUB_OTEL=<code>:<exit> is the collector's answer alone, as real
        # curl gives it: 000 and exit 7 for a refused connection.
        'if [[ " $* " == *":4318/"* && -n ${STUB_OTEL:-} ]]; then\n'
        '  printf "%s" "${STUB_OTEL%%:*}"; exit "${STUB_OTEL##*:}"\n'
        "fi\n"
        "[[ -n ${STUB_HEALTHY:-} ]] || exit 0\n"
        'if [[ " $* " == *"/api/tags"* ]]; then\n'
        '  out=; prev=; for a in "$@"; do [[ $prev != -o ]] || out=$a; prev=$a; done\n'
        '  echo \'{"models":[{"name":"nomic-embed-text:latest"}]}\' > "$out"\n'
        'elif [[ " $* " == *":4318/"* ]]; then\n'
        "  printf 200\n"
        'elif [[ " $* " == *"%{http_code}"* && " $* " != *"/checkin"* ]]; then\n'
        '  [[ " ${sent[*]} " == *"api-key: "* ]] && printf 200 || printf 401\n'
        "fi\n",
    )
    return tmp_path


def _write(host: Path, name: str, content: str) -> None:
    (host / "etc" / "socraticode" / name).write_text(content)


def _run(host: Path, healthy: bool = False, **env: str) -> subprocess.CompletedProcess[str]:
    local = {
        "/etc/socraticode/": f"{host}/etc/socraticode/",
        "/usr/local/bin/": f"{host}/bin/",
        "/usr/local/share/co-index/": f"{host}/share/",
        "/tmp/index-checkin-": f"{host}/tmp/index-checkin-",
        "/var/lib/otelcol": f"{host}/otelcol",
        "/var/cache/co-index/": f"{host}/cache/",
    }
    copy = host / "index-checkin.sh"
    copy.write_text(HOST_PATHS.sub(lambda m: local[m.group()], SCRIPT.read_text()))
    # An exported STATUS_* in the runner's shell would stand in for status.env.
    inherited = {k: v for k, v in os.environ.items() if not k.startswith("STATUS_")}
    return subprocess.run(
        ["bash", str(copy)],
        env={
            **inherited,
            "PATH": f"{host / 'bin'}:{os.environ['PATH']}",
            "CURL_LOG": str(host / "curl.log"),
            "LC_ALL": "C",  # bash's own errors, asserted below, are translated
            **({"STUB_HEALTHY": "1"} if healthy else {}),
            **env,
        },
        capture_output=True,
        text=True,
    )


def _host_paths(script: str) -> list[str]:
    """The absolute host paths in ``script``, bar the shebang: the copy runs
    under ``bash``, never through it."""
    body = script.split("\n", 1)[1]
    return re.findall(
        r"(?<![\w.:/])/(?:etc|var|tmp|usr|run|srv|opt|home|root|proc|sys)/[^\s\"')]*", body
    )


def test_the_copy_reaches_no_host_path() -> None:
    """Nothing leaves the test only while ``HOST_PATHS`` rewrites every host
    path. A new one in the script would be read, or written, on the real host."""
    paths = _host_paths(SCRIPT.read_text())
    assert "/usr/local/bin/tailnet-bind.sh" in paths, "the scan finds no paths at all"
    assert [p for p in paths if not HOST_PATHS.match(p)] == []


def _checkins(host: Path) -> list[str]:
    """The check-in calls, as sent."""
    log = host / "curl.log.sent"
    calls = log.read_text().splitlines() if log.exists() else []
    return [call for call in calls if "/checkin" in call]


def test_checks_in_with_the_status_credentials(host: Path) -> None:
    _write(host, "status.env", STATUS_ENV)
    result = _run(host)
    assert result.returncode == 0, result.stderr
    [checkin] = _checkins(host)
    assert f"http://status:9000/api/v1/monitors/{MONITOR}/checkin" in checkin
    assert f"X-API-Key: {KEY}" in checkin


def test_no_credential_is_on_curls_argv(host: Path) -> None:
    """argv is readable by every user on co-index, whose /proc has no hidepid,
    for as long as the call runs. The keys go in header files instead."""
    _write(host, "status.env", STATUS_ENV)
    assert _run(host).returncode == 0
    argv = (host / "curl.log").read_text()
    sent = (host / "curl.log.sent").read_text()
    for secret in (KEY, QDRANT_KEY):
        assert secret in sent, "the control: curl was handed it"
        assert secret not in argv


def test_no_child_process_is_handed_the_credentials(host: Path) -> None:
    """They go to co-status as arguments. Exported, they would also sit in the
    environment of every probe, none of which reads them."""
    _write(host, "status.env", STATUS_ENV)
    assert _run(host).returncode == 0
    assert (host / "curl.log.env").read_text() == ""


def test_notifier_env_alone_does_not_check_in(host: Path) -> None:
    _write(host, "notifier.env", f"NOTIFIER_API_KEY={KEY}\nNOTIFIER_MONITOR_ID={MONITOR}\n")
    result = _run(host)
    assert result.returncode == 1
    assert "status.env: No such file or directory" in result.stderr
    assert _checkins(host) == []


@pytest.mark.parametrize("missing", ["STATUS_API_KEY", "STATUS_MONITOR_ID"])
@pytest.mark.parametrize("value", ["", None], ids=["empty", "absent"])
def test_a_missing_value_fails_naming_it(host: Path, missing: str, value: str | None) -> None:
    lines = [ln for ln in STATUS_ENV.splitlines() if not ln.startswith(f"{missing}=")]
    if value is not None:
        lines.append(f"{missing}={value}")
    _write(host, "status.env", "\n".join(lines) + "\n")
    result = _run(host)
    assert result.returncode == 1
    assert f"{missing} unset in" in result.stderr
    assert "status.env" in result.stderr
    assert not (host / "curl.log").exists(), "probed before the credentials were checked"


def _body(host: Path) -> dict:
    """The check-in's body, parsed: it is hand-built JSON, so parsing is a check."""
    return json.loads((host / "curl.log.body").read_text())


def _expires_in(host: Path, days: int) -> None:
    # The script floors seconds to whole days: half a day's margin makes that
    # `days`, however long the run takes.
    when = datetime.now(UTC) + timedelta(days=days, hours=12)
    _stub(host / "bin" / "openssl", f"echo 'notAfter={when:%b %d %H:%M:%S %Y} GMT'\n")


@pytest.mark.parametrize(("days", "alerts"), [(21, True), (22, False)])
def test_the_cert_finding_opens_under_the_line_plus_a_day(
    host: Path, days: int, alerts: bool
) -> None:
    """Whole days, floored: 21 days 12 hours is 21, inside the line. The renew
    script's tie to this line counts on it (test_cert_renew.py, CR 1)."""
    [line] = re.findall(r"^CERT_DAYS_MIN=(\d+)$", SCRIPT.read_text(), re.M)
    assert int(line) == 21
    _write(host, "status.env", STATUS_ENV)
    _expires_in(host, days)
    assert _run(host, healthy=True).returncode == 0
    assert ("tls:cert" in _body(host)["metadata"]["fault"]) is alerts


def test_an_alert_keys_its_fault_by_check_and_subject(host: Path) -> None:
    """co-status reports an ``alert`` whose ``metadata.fault`` differs from the
    open fault's at once (#10, CannObserv/status#28). Without it, a new finding
    mid-fault waits for the fault to clear."""
    _write(host, "status.env", STATUS_ENV)
    assert _run(host).returncode == 0
    body = _body(host)
    assert body["status"] == "alert", "the control: the stubbed Qdrant answers nothing"
    findings = body["variables"]["findings"]
    assert body["metadata"]["fault"] == [f"{f['check']}:{f['subject']}" for f in findings]
    assert {"qdrant:collections", "qdrant:auth"} <= set(body["metadata"]["fault"])


def test_the_fault_key_ignores_the_message(host: Path) -> None:
    """Messages carry ages, codes and percentages. A key holding one would
    open a new fault, and send a report, on every check-in."""
    _write(host, "status.env", STATUS_ENV)
    runs = []
    for days in (5, 10):
        _expires_in(host, days)
        assert _run(host).returncode == 0
        runs.append(_body(host))
    messages = [[f["message"] for f in b["variables"]["findings"]] for b in runs]
    assert messages[0] != messages[1], "the control: the cert's message changed"
    assert "tls:cert" in runs[0]["metadata"]["fault"]
    assert runs[0]["metadata"]["fault"] == runs[1]["metadata"]["fault"]


def test_a_healthy_host_sends_ok_with_an_empty_fault(host: Path) -> None:
    """The body sent every tick. An unparseable one is a 4xx that curl exits 0
    on, heard only as a missed deadline."""
    _write(host, "status.env", STATUS_ENV)
    assert _run(host, healthy=True).returncode == 0
    body = _body(host)
    assert body["status"] == "ok", body["variables"]["findings"]
    assert body["variables"]["finding_count"] == 0
    assert body["metadata"] == {"fault": []}


def test_two_findings_are_counted_as_two(host: Path) -> None:
    """#48: ``grep -c`` counted lines, and the findings are one line, so the
    count and the journal's ``findings=N`` were never more than 1. Measured
    on 2026-11-20 with exactly these two. The journal line is curl's ``-w``
    format, read from its argv: the stub doesn't print it."""
    _write(host, "status.env", STATUS_ENV)
    _expires_in(host, 5)
    assert _run(host, healthy=True, STUB_OTEL="000:7").returncode == 0
    body = _body(host)
    assert body["metadata"]["fault"] == ["tls:cert", "otel:collector"], "the control"
    assert body["variables"]["finding_count"] == 2
    [argv] = [c for c in (host / "curl.log").read_text().splitlines() if "/checkin" in c]
    assert "sent=alert findings=2" in argv


def test_the_count_is_the_number_of_findings(host: Path) -> None:
    """Everything down: the stubbed Qdrant, Ollama and collector answer nothing."""
    _write(host, "status.env", STATUS_ENV)
    assert _run(host).returncode == 0
    body = _body(host)
    assert len(body["variables"]["findings"]) > 2, "the control"
    assert body["variables"]["finding_count"] == len(body["variables"]["findings"])


def test_a_full_disk_is_the_disk_gcs_finding_not_this_ones(host: Path) -> None:
    """``/``'s headroom pages on ``co-index-disk-headroom``, the cohort disk GC's
    sensor (#16, CannObserv/status#72). Here too, one disk would page twice."""
    _write(host, "status.env", STATUS_ENV)
    _stub(host / "bin" / "df", "echo Use%; echo ' 99%'\n")
    assert _run(host, healthy=True).returncode == 0
    body = _body(host)
    assert body["status"] == "ok", body["variables"]["findings"]
    assert body["metadata"] == {"fault": []}


def _fingerprint_calls(host: Path) -> list[str]:
    log = host / "curl.log.fp"
    return log.read_text().splitlines() if log.exists() else []


def test_the_fingerprint_is_checked_against_the_engine_clients_use(host: Path) -> None:
    _write(host, "status.env", STATUS_ENV)
    assert _run(host, healthy=True).returncode == 0
    assert _fingerprint_calls(host) == [
        f"check http://100.64.0.1:11434 {host}/share/engine-fingerprint.json"
    ]


def test_drift_is_the_ollama_engine_finding(host: Path) -> None:
    _write(host, "status.env", STATUS_ENV)
    line = "engine 0.40.5 drifted from the 0.34.0 fingerprint (cosine < 0.99999): code 0.9100"
    assert _run(host, healthy=True, STUB_FP_EXIT="1", STUB_FP_OUT=line).returncode == 0
    body = _body(host)
    assert body["status"] == "alert"
    assert body["metadata"]["fault"] == ["ollama:engine"]
    assert body["variables"]["findings"] == [
        {"check": "ollama", "subject": "engine", "message": line}
    ]


def test_an_unknown_fingerprint_is_no_finding(host: Path) -> None:
    """Exit 2: the probes queued behind a client's batch and timed out."""
    _write(host, "status.env", STATUS_ENV)
    result = _run(host, healthy=True, STUB_FP_EXIT="2")
    assert result.returncode == 0, result.stderr
    assert _body(host)["status"] == "ok"


def test_a_down_ollama_is_one_finding_not_two(host: Path) -> None:
    _write(host, "status.env", STATUS_ENV)
    assert _run(host).returncode == 0
    assert _fingerprint_calls(host) == []
    assert "ollama:engine" not in _body(host)["metadata"]["fault"]


def _fault(host: Path) -> list[str]:
    return _body(host)["metadata"]["fault"]


def _finding(host: Path, key: str) -> dict:
    [found] = [
        f for f in _body(host)["variables"]["findings"] if f"{f['check']}:{f['subject']}" == key
    ]
    return found


def test_the_collector_is_probed_with_an_empty_export_where_clients_send(host: Path) -> None:
    """The ingest path itself, on the tailnet address, so no second port
    opens. An empty export is answered and writes nothing
    (test_otelcol_pipeline.py)."""
    _write(host, "status.env", STATUS_ENV)
    assert _run(host, healthy=True).returncode == 0
    [probe] = [c for c in (host / "curl.log.sent").read_text().splitlines() if ":4318" in c]
    assert "http://100.64.0.1:4318/v1/logs" in probe
    assert "-d {}" in probe
    assert "Content-Type: application/json" in probe


def test_a_collector_that_does_not_answer_is_a_finding(host: Path) -> None:
    _write(host, "status.env", STATUS_ENV)
    assert _run(host).returncode == 0
    assert "otel:collector" in _fault(host)
    assert "empty export" in _finding(host, "otel:collector")["message"]


@pytest.mark.parametrize(
    ("answer", "said"),
    [("000:7", "000"), ("000:28", "000"), ("503:0", "503"), ("404:0", "404")],
    ids=["refused", "timed out", "refusing data", "no pipeline"],
)
def test_a_collector_that_is_down_or_refusing_is_a_finding_and_the_checkin_goes(
    host: Path, answer: str, said: str
) -> None:
    """curl exits 7 on a refused connection and 28 on a timeout: under
    ``set -e`` either would end the script, and the host would go silent
    over a dead collector. And only 200 is healthy: the memory limiter
    answers 503 while it refuses data (CR 8)."""
    _write(host, "status.env", STATUS_ENV)
    result = _run(host, healthy=True, STUB_OTEL=answer)
    assert result.returncode == 0, result.stderr
    assert _fault(host) == ["otel:collector"]
    assert said in _finding(host, "otel:collector")["message"]
    assert len(_checkins(host)) == 1


def _threshold() -> int:
    [mib] = re.findall(
        r"^OTEL_BYTES_MAX=\$\(\( (\d+) \* 1024 \* 1024 \)\)$", SCRIPT.read_text(), re.M
    )
    return int(mib) * MIB


def _cap() -> int:
    [expr] = re.findall(r"^CAP_BYTES=\$\(\(\s*([\d\s*]+)\)\)", RETENTION.read_text(), re.M)
    total = 1
    for factor in expr.split("*"):
        total *= int(factor)
    return total


def test_the_bytes_finding_comes_at_half_the_cap() -> None:
    """Before the retention run deletes anything to hold it."""
    assert _threshold() * 2 == _cap()


def _fill(host: Path, size: int) -> None:
    with (host / "otelcol" / "events.jsonl").open("wb") as sparse:
        sparse.truncate(size)


def test_a_directory_over_the_threshold_is_a_finding(host: Path) -> None:
    _write(host, "status.env", STATUS_ENV)
    _fill(host, _threshold() + MIB)
    assert _run(host, healthy=True).returncode == 0
    assert _fault(host) == ["otel:bytes"]
    assert f"{_threshold() // MIB + 1} MiB" in _finding(host, "otel:bytes")["message"]


def test_a_directory_under_the_threshold_is_not(host: Path) -> None:
    _write(host, "status.env", STATUS_ENV)
    _fill(host, _threshold() - MIB)
    assert _run(host, healthy=True).returncode == 0
    assert _fault(host) == []


def _stamp_age(host: Path, hours: float) -> None:
    when = time.time() - hours * 3600
    os.utime(host / "otelcol" / STAMP, (when, when))


@pytest.mark.parametrize(("hours", "fault"), [(35, []), (37, ["otel:retention"])])
def test_a_retention_run_overdue_by_half_a_day_is_a_finding(
    host: Path, hours: float, fault: list[str]
) -> None:
    """The timer is daily, so 36 hours is one run missed, with room for a
    slow one."""
    _write(host, "status.env", STATUS_ENV)
    _stamp_age(host, hours)
    assert _run(host, healthy=True).returncode == 0
    assert _fault(host) == fault


def test_a_retention_run_that_never_succeeded_is_a_finding(host: Path) -> None:
    _write(host, "status.env", STATUS_ENV)
    (host / "otelcol" / STAMP).unlink()
    assert _run(host, healthy=True).returncode == 0
    assert _fault(host) == ["otel:retention"]
    assert "never" in _finding(host, "otel:retention")["message"]


def test_a_missing_directory_is_reported_and_the_checkin_still_goes(host: Path) -> None:
    """Under ``set -e`` and ``pipefail`` a failed ``du`` or ``stat`` would
    end the script, and the host would go silent over a missing directory."""
    _write(host, "status.env", STATUS_ENV)
    (host / "otelcol" / STAMP).unlink()
    (host / "otelcol").rmdir()
    result = _run(host, healthy=True)
    assert result.returncode == 0, result.stderr
    assert _fault(host) == ["otel:retention"]


def test_the_checkin_is_ordered_after_the_collector() -> None:
    unit = (REPO_ROOT / "deploy" / "index-checkin.service").read_text()
    [after] = re.findall(r"^After=(.*)$", unit, re.M)
    assert {"qdrant.service", "ollama.service", "otelcol.service"} <= set(after.split())


def _link_calls(host: Path) -> list[str]:
    log = host / "curl.log.links"
    return log.read_text().splitlines() if log.exists() else []


def test_the_links_are_checked_against_the_store_clients_use(host: Path) -> None:
    """The same URL, the key as a file path and never its value, the
    installed list, and a cache under /var/cache."""
    _write(host, "status.env", STATUS_ENV)
    assert _run(host, healthy=True).returncode == 0
    [call] = _link_calls(host)
    assert call.split() == [
        "check",
        "https://index.taild0fb76.ts.net:6333",
        f"{host}/etc/socraticode/qdrant.key",
        f"{host}/share/cohort.json",
        f"{host}/cache/links.json",
    ]
    assert QDRANT_KEY not in call
    assert _body(host)["status"] == "ok"


def test_each_link_finding_rides_the_checkin_in_its_order(host: Path) -> None:
    _write(host, "status.env", STATUS_ENV)
    out = "cache\\tno good fetch from GitHub in 25 h\\nnotifier/watcher\\t../watcher: no collection"
    assert _run(host, healthy=True, STUB_LINKS_OUT=out, STUB_LINKS_EXIT="1").returncode == 0
    assert _fault(host) == ["links:cache", "links:notifier/watcher"]
    assert _finding(host, "links:notifier/watcher")["message"] == "../watcher: no collection"
    assert _body(host)["variables"]["finding_count"] == 2


def test_link_findings_come_after_the_stores_and_before_ollamas(host: Path) -> None:
    """The fault key is in the checks' fixed order (#10)."""
    _write(host, "status.env", STATUS_ENV)
    result = _run(
        host,
        healthy=True,
        STUB_LINKS_OUT="status/ghost\\tx",
        STUB_LINKS_EXIT="1",
        STUB_FP_OUT="drift",
        STUB_FP_EXIT="1",
    )
    assert result.returncode == 0
    assert _fault(host) == ["links:status/ghost", "ollama:engine"]


def test_an_unknown_link_check_is_no_finding(host: Path) -> None:
    _write(host, "status.env", STATUS_ENV)
    assert _run(host, healthy=True, STUB_LINKS_EXIT="2").returncode == 0
    assert _body(host)["status"] == "ok"


@pytest.mark.parametrize(("code", "out"), [("3", ""), ("127", ""), ("1", ""), ("124", "")])
def test_a_broken_link_check_is_heard(host: Path, code: str, out: str) -> None:
    """3 is its own failure, 127 a missing install, 1 with nothing said a
    crash, 124 its ``timeout``: none of them is a clean cohort."""
    _write(host, "status.env", STATUS_ENV)
    result = _run(host, healthy=True, STUB_LINKS_EXIT=code, STUB_LINKS_OUT=out)
    assert result.returncode == 0, result.stderr
    assert _fault(host) == ["links:check"]
    assert code in _finding(host, "links:check")["message"]


def test_links_are_not_checked_when_the_store_did_not_answer(host: Path) -> None:
    """``qdrant:collections`` is that finding; a second would page one store twice."""
    _write(host, "status.env", STATUS_ENV)
    assert _run(host, STUB_LINKS_EXIT="3").returncode == 0
    assert _link_calls(host) == []
    assert not [k for k in _fault(host) if k.startswith("links:")]
