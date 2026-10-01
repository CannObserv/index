"""Behaviour tests for deploy/index-checkin.sh's credentials (#8).

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

The script runs from a copy whose host paths point into ``tmp_path``, with
``curl`` and ``openssl`` stubbed on ``PATH``. Nothing leaves the test, and on
co-index it never touches the live timer's files.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "deploy" / "index-checkin.sh"
HOST_PATHS = re.compile(r"/etc/socraticode/|/usr/local/bin/|/tmp/index-checkin-")

KEY = "status-key"
MONITOR = "01MONITORID"
STATUS_ENV = f"STATUS_API_KEY={KEY}\nSTATUS_MONITOR_ID={MONITOR}\n"


def _stub(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(0o755)


@pytest.fixture
def host(tmp_path: Path) -> Path:
    """``tmp_path`` laid out as the script's host paths, with no credential file."""
    (tmp_path / "etc" / "socraticode").mkdir(parents=True)
    (tmp_path / "etc" / "socraticode" / "qdrant.key").write_text("qdrant-key\n")
    (tmp_path / "tmp").mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub(bin_dir / "tailnet-bind.sh", "echo 100.64.0.1\n")
    _stub(bin_dir / "openssl", "echo 'notAfter=Jan  1 00:00:00 2099 GMT'\n")
    # One line per call. The check-in's body arrives on stdin, and is drained
    # so the pipe into it never breaks.
    _stub(
        bin_dir / "curl",
        'printf "%s\\n" "$*" >> "$CURL_LOG"\n[[ " $* " != *" @- "* ]] || cat > /dev/null\n',
    )
    return tmp_path


def _write(host: Path, name: str, content: str) -> None:
    (host / "etc" / "socraticode" / name).write_text(content)


def _run(host: Path) -> subprocess.CompletedProcess[str]:
    local = {
        "/etc/socraticode/": f"{host}/etc/socraticode/",
        "/usr/local/bin/": f"{host}/bin/",
        "/tmp/index-checkin-": f"{host}/tmp/index-checkin-",
    }
    copy = host / "index-checkin.sh"
    copy.write_text(HOST_PATHS.sub(lambda m: local[m.group()], SCRIPT.read_text()))
    return subprocess.run(
        ["bash", str(copy)],
        env={
            **os.environ,
            "PATH": f"{host / 'bin'}:{os.environ['PATH']}",
            "CURL_LOG": str(host / "curl.log"),
        },
        capture_output=True,
        text=True,
    )


def _checkins(host: Path) -> list[str]:
    log = host / "curl.log"
    calls = log.read_text().splitlines() if log.exists() else []
    return [call for call in calls if "/checkin" in call]


def test_checks_in_with_the_status_credentials(host: Path) -> None:
    _write(host, "status.env", STATUS_ENV)
    result = _run(host)
    assert result.returncode == 0, result.stderr
    [checkin] = _checkins(host)
    assert f"http://status:9000/api/v1/monitors/{MONITOR}/checkin" in checkin
    assert f"X-API-Key: {KEY}" in checkin


def test_notifier_env_alone_does_not_check_in(host: Path) -> None:
    _write(host, "notifier.env", f"NOTIFIER_API_KEY={KEY}\nNOTIFIER_MONITOR_ID={MONITOR}\n")
    assert _run(host).returncode != 0
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
