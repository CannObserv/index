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

The script runs from a copy whose host paths point into ``tmp_path``, with
``curl`` and ``openssl`` stubbed on ``PATH``. Nothing leaves the test, and on
co-index it never touches the live timer's files.
"""

import json
import os
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "deploy" / "index-checkin.sh"
HOST_PATHS = re.compile(r"/etc/socraticode/|/usr/local/bin/|/tmp/index-checkin-")

KEY = "status-key"
MONITOR = "01MONITORID"
QDRANT_KEY = "qdrant-key"
STATUS_ENV = f"STATUS_API_KEY={KEY}\nSTATUS_MONITOR_ID={MONITOR}\n"


def _stub(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(0o755)


@pytest.fixture
def host(tmp_path: Path) -> Path:
    """``tmp_path`` laid out as the script's host paths, with no credential file."""
    (tmp_path / "etc" / "socraticode").mkdir(parents=True)
    (tmp_path / "etc" / "socraticode" / "qdrant.key").write_text(f"{QDRANT_KEY}\n")
    (tmp_path / "tmp").mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub(bin_dir / "tailnet-bind.sh", "echo 100.64.0.1\n")
    _stub(bin_dir / "openssl", "echo 'notAfter=Jan  1 00:00:00 2099 GMT'\n")
    # The runner's own disk would otherwise decide whether `disk:root` is a finding.
    _stub(bin_dir / "df", "echo Use%; echo ' 42%'\n")
    # Logs its argv, prints STUB_FP_OUT, exits STUB_FP_EXIT (0: no drift).
    _stub(
        bin_dir / "engine-fingerprint.py",
        'printf "%s\\n" "$*" >> "$CURL_LOG.fp"\n'
        '[[ -z ${STUB_FP_OUT:-} ]] || printf "%s\\n" "$STUB_FP_OUT"\n'
        'exit "${STUB_FP_EXIT:-0}"\n',
    )
    # One line per call in each log: its argv as `ps` shows it, and what it
    # was sent, with each `-H @file` read. Plus any credential name it was
    # handed in its environment. The check-in's body arrives on stdin, and is
    # kept, which also drains the pipe into it so it never breaks.
    #
    # Qdrant and Ollama answer nothing, unless STUB_HEALTHY is set: then
    # Qdrant answers 200 with its key and 401 without, and Ollama lists the
    # model.
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
        "[[ -n ${STUB_HEALTHY:-} ]] || exit 0\n"
        'if [[ " $* " == *"/api/tags"* ]]; then\n'
        '  out=; prev=; for a in "$@"; do [[ $prev != -o ]] || out=$a; prev=$a; done\n'
        '  echo \'{"models":[{"name":"nomic-embed-text:latest"}]}\' > "$out"\n'
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
        "/tmp/index-checkin-": f"{host}/tmp/index-checkin-",
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


def _fingerprint_calls(host: Path) -> list[str]:
    log = host / "curl.log.fp"
    return log.read_text().splitlines() if log.exists() else []


def test_the_fingerprint_is_checked_against_the_engine_clients_use(host: Path) -> None:
    _write(host, "status.env", STATUS_ENV)
    assert _run(host, healthy=True).returncode == 0
    assert _fingerprint_calls(host) == [
        f"check http://100.64.0.1:11434 {host}/etc/socraticode/engine-fingerprint.json"
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
