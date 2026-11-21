"""Behaviour tests for deploy/qdrant-cert-renew.sh (D14, #49).

``tailscale cert`` without ``--min-validity`` hands back the cached
certificate while it is still valid. Once it is due (in ARI's suggested
window, else past two thirds of its lifetime) it also starts a renewal *in
the background* and returns at once, so the script compared too early, logged
``unchanged``, and the new certificate waited a week for the next run. On
2026-11-16 that left 25 days on the installed one, and the check-in's
``tls:cert`` opened on 2026-11-20 (#49).

With ``--min-validity`` tailscaled renews *synchronously* whenever the cached
certificate has less than that left, and answers from its cache otherwise
(Tailscale v1.104.7, ``feature/acme/cert.go``). So:

- **The value is tied to the check-in's line.** The timer is weekly with an
  hour's jitter, so a renewal lands when the certificate has between the
  value less 7 days 1 hour and the value left. That low end must be at or
  above the check-in's line plus a day (it floors to whole days), or the
  alert opens before the run that would renew.
- **A renewal restarts Qdrant, and nothing else does.** Qdrant reads the
  certificate once, at start.

The script runs from a copy whose host paths point into ``tmp_path``, with
``tailscale``, ``systemctl`` and ``openssl`` stubbed on ``PATH``. It never
touches the live certificate or Qdrant.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "deploy" / "qdrant-cert-renew.sh"
TIMER = REPO_ROOT / "deploy" / "qdrant-cert-renew.timer"
CHECKIN = REPO_ROOT / "deploy" / "index-checkin.sh"
HOST_PATHS = re.compile(r"/etc/socraticode/tls/")

OLD, NEW = "OLD-CERT\n", "NEW-CERT\n"
NAME = "index.taild0fb76.ts.net"


def _stub(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(0o755)


@pytest.fixture
def host(tmp_path: Path) -> Path:
    """The installed certificate is OLD. The stubbed tailscaled holds one too:
    STUB_CACHED (default OLD) with STUB_DAYS_LEFT days on it (default 85).

    ``tailscale cert`` with ``--min-validity <N>h`` returns NEW when the cached
    one has less than N hours left, as tailscaled's synchronous renewal does,
    and the cached one otherwise. Without the flag it always returns the
    cached one, as tailscaled does while its own renewal runs in the
    background. STUB_CERT_EXIT fails it."""
    tls = tmp_path / "tls"
    tls.mkdir()
    (tls / "qdrant.crt").write_text(OLD)
    (tls / "qdrant.key").write_text("OLD-KEY\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub(
        bin_dir / "tailscale",
        'printf "%s\\n" "$*" >> "$STUB_LOG"\n'
        'if [[ $1 == status ]]; then echo \'{"Self":{"DNSName":"' + NAME + ".\"}}'; exit 0; fi\n"
        '[[ -z ${STUB_CERT_EXIT:-} ]] || exit "$STUB_CERT_EXIT"\n'
        "crt= key= mv=\n"
        "shift\n"
        "while [[ $# -gt 1 ]]; do\n"
        "  case $1 in\n"
        "    --cert-file) crt=$2; shift 2 ;;\n"
        "    --key-file) key=$2; shift 2 ;;\n"
        "    --min-validity) mv=${2%h}; shift 2 ;;\n"
        "    *) shift ;;\n"
        "  esac\n"
        "done\n"
        "out=${STUB_CACHED:-OLD-CERT}\n"
        "if [[ -n $mv ]] && (( ${STUB_DAYS_LEFT:-85} * 24 < mv )); then out=NEW-CERT; fi\n"
        'printf "%s\\n" "$out" > "$crt"\n'
        'printf "%s\\n" "${out%-CERT}-KEY" > "$key"\n',
    )
    _stub(bin_dir / "systemctl", 'printf "%s\\n" "$*" >> "$STUB_LOG.systemctl"\n')
    # The expiry line names which certificate the file holds.
    _stub(
        bin_dir / "openssl",
        'in=; prev=; for a in "$@"; do [[ $prev != -in ]] || in=$a; prev=$a; done\n'
        'echo "notAfter=$(head -1 "$in")"\n',
    )
    return tmp_path


def _run(host: Path, **env: str) -> subprocess.CompletedProcess[str]:
    copy = host / "qdrant-cert-renew.sh"
    copy.write_text(HOST_PATHS.sub(f"{host}/tls/", SCRIPT.read_text()))
    return subprocess.run(
        ["bash", str(copy)],
        env={
            **os.environ,
            "PATH": f"{host / 'bin'}:{os.environ['PATH']}",
            "STUB_LOG": str(host / "tailscale.log"),
            **env,
        },
        capture_output=True,
        text=True,
    )


def _restarts(host: Path) -> list[str]:
    log = host / "tailscale.log.systemctl"
    return log.read_text().splitlines() if log.exists() else []


def _cert_calls(host: Path) -> list[str]:
    return [c for c in (host / "tailscale.log").read_text().splitlines() if c.startswith("cert ")]


def test_the_copy_reaches_no_host_path() -> None:
    body = SCRIPT.read_text().split("\n", 1)[1]
    paths = re.findall(r"(?<![\w.:/])/(?:etc|var|tmp|usr|run|srv|opt|root)/[^\s\"')]*", body)
    assert "/etc/socraticode/tls/qdrant.crt" in paths, "the scan finds no paths at all"
    assert [p for p in paths if not HOST_PATHS.match(p)] == []


def test_a_renewal_tailscale_has_due_is_collected_in_the_same_run(host: Path) -> None:
    """2026-11-16: 25 days left, renewal due. Without ``--min-validity`` the
    call returned the cached certificate and renewed behind the script's back."""
    result = _run(host, STUB_DAYS_LEFT="25")
    assert result.returncode == 0, result.stderr
    assert (host / "tls" / "qdrant.crt").read_text() == NEW
    assert "renewed, restarting qdrant" in result.stdout
    assert "now expires NEW-CERT" in result.stdout
    assert _restarts(host) == ["restart qdrant"]


def test_a_certificate_tailscale_already_renewed_is_installed(host: Path) -> None:
    """2026-11-23: tailscaled holds the new certificate, the file the old."""
    result = _run(host, STUB_CACHED="NEW-CERT")
    assert result.returncode == 0, result.stderr
    assert (host / "tls" / "qdrant.crt").read_text() == NEW
    assert _restarts(host) == ["restart qdrant"]


def test_a_fresh_certificate_is_unchanged_and_qdrant_is_left_alone(host: Path) -> None:
    result = _run(host)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "qdrant-cert-renew: unchanged, expires OLD-CERT"
    assert _restarts(host) == []


def test_a_failed_renewal_fails_the_unit_and_leaves_qdrant_alone(host: Path) -> None:
    """A synchronous renewal can fail (ACME, DNS-01, a rate limit). The unit
    fails, and the check-in's line is what is heard."""
    result = _run(host, STUB_DAYS_LEFT="25", STUB_CERT_EXIT="1")
    assert result.returncode != 0
    assert (host / "tls" / "qdrant.crt").read_text() == OLD
    assert _restarts(host) == []


def test_the_files_keep_their_modes(host: Path) -> None:
    """``tailscale cert`` writes by rename: a new file, the key 0600. The
    script's chmods are what keep the modes the setup gave them."""
    assert _run(host, STUB_DAYS_LEFT="25").returncode == 0
    assert (host / "tls" / "qdrant.crt").stat().st_mode & 0o777 == 0o644
    assert (host / "tls" / "qdrant.key").stat().st_mode & 0o777 == 0o640


def _min_validity_hours() -> int:
    [days] = re.findall(r"^MIN_VALIDITY_DAYS=(\d+)$", SCRIPT.read_text(), re.M)
    return int(days) * 24


def _checkin_line_hours() -> int:
    [days] = re.findall(r"^CERT_DAYS_MIN=(\d+)$", CHECKIN.read_text(), re.M)
    return int(days) * 24


def test_the_call_asks_for_the_min_validity(host: Path) -> None:
    assert _run(host).returncode == 0
    [call] = _cert_calls(host)
    assert f"--min-validity {_min_validity_hours()}h" in call
    assert call.endswith(NAME)


def _renews_before_the_checkin_alerts(min_validity_hours: int) -> bool:
    """A weekly timer with an hour's jitter renews when the certificate has
    between the min-validity less 7 days 1 hour, and the min-validity, left.
    The timer is read, so a change to its cadence fails here."""
    timer = TIMER.read_text()
    assert re.search(r"^OnCalendar=Mon \d\d:\d\d$", timer, re.M), "not weekly any more"
    [jitter] = re.findall(r"^RandomizedDelaySec=(\d+)h$", timer, re.M)
    worst_case = min_validity_hours - 7 * 24 - int(jitter)
    # The check-in floors to whole days and alerts unless that is over its
    # line, so ``tls:cert`` opens with less than CERT_DAYS_MIN + 1 days left.
    return worst_case >= _checkin_line_hours() + 24


def test_the_min_validity_renews_before_the_checkin_alerts() -> None:
    assert _renews_before_the_checkin_alerts(_min_validity_hours())


def test_a_min_validity_a_day_short_is_refused() -> None:
    """The control: 29 days renews with as little as 21 days 23 hours left,
    and the check-in has alerted from 22 (CR 1)."""
    assert not _renews_before_the_checkin_alerts(29 * 24)


SERVICE = REPO_ROOT / "deploy" / "qdrant-cert-renew.service"


def test_a_hung_renewal_is_bounded_well_inside_a_week() -> None:
    """With ``--min-validity`` the call waits for the issuance, and nothing in
    ``tailscale cert`` bounds it. A oneshot's default start timeout is
    infinite, and a timer does not start a unit that is still active: a hung
    ACME call would end every later weekly try (CR 3). 2026-11-16's issuance
    took 34 s."""
    [service] = re.findall(r"^\[Service\]$(.*?)(?=^\[|\Z)", SERVICE.read_text(), re.M | re.S)
    [minutes] = re.findall(r"^TimeoutStartSec=(\d+)min$", service, re.M)
    assert 5 <= int(minutes) <= 30
