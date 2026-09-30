"""Drift tests for the long-running units, qdrant.service and ollama.service.

**A clean stop is a success, and Qdrant still restarts (#4).** The qdrant
container exits 128+15 on SIGTERM and ``docker run`` passes that through, so
``systemctl stop qdrant`` left the unit ``failed`` with ``ExecMainStatus=143``
although Qdrant shut down cleanly. Ollama's container exits 0 and never showed
it. ``SuccessExitStatus=143`` fixes the record, but under ``Restart=on-failure``
it would also stop the restart after an **out-of-band** SIGTERM (``docker stop
qdrant``), which 143 is what triggers today. ``Restart=always`` keeps that
restart; an explicit stop never restarts under either.

**The start limit is read where systemd reads it.** ``StartLimitIntervalSec=``
is a ``[Unit]`` key. In ``[Service]`` systemd ignores it with a warning, and
the interval falls back to the default 10 s: with ``RestartSec=5s`` at most
two starts fit in that window, so a burst of 5 can never trip and a restart
loop has no bound. ``Restart=always`` relies on that bound.

Tracked in ``deploy/``, installed as:

- ``qdrant.service``, ``ollama.service`` -> ``/etc/systemd/system/``

Pure assertions on the tracked copies run everywhere; installed-parity and live
assertions skip where the node is not this one, CI included.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "deploy"
UNITS = ["qdrant.service", "ollama.service"]
INSTALLED = Path("/etc/systemd/system")


def _section(unit: str, name: str) -> dict[str, list[str]]:
    """``name``'s keys in ``unit``, each with every value it is given.

    Unit files repeat keys (``Documentation=``, ``After=``), which
    ``configparser`` rejects or collapses, so this reads them directly.
    """
    keys: dict[str, list[str]] = {}
    current = None
    for raw in (DEPLOY / unit).read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            continue
        if current == name:
            key, _, value = line.partition("=")
            keys.setdefault(key.strip(), []).append(value.strip())
    return keys


def _show(unit: str, *props: str) -> dict[str, str]:
    """The loaded unit's ``props``; skips where the unit is not loaded here."""
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        pytest.skip("systemctl not available on this host")
    shown = subprocess.run(
        [systemctl, "show", unit, "-p", ",".join(("LoadState", *props))],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    values = dict(ln.partition("=")[::2] for ln in shown.splitlines())
    if values.get("LoadState") != "loaded":
        pytest.skip(f"{unit} not loaded on this host")
    return values


def test_qdrant_clean_stop_exit_is_success() -> None:
    statuses = " ".join(_section("qdrant.service", "Service").get("SuccessExitStatus", [])).split()
    assert "143" in statuses


def test_qdrant_restarts_on_out_of_band_sigterm() -> None:
    """With 143 a success, ``on-failure`` would no longer restart after an
    out-of-band SIGTERM. ``always`` does; an explicit stop still does not."""
    assert _section("qdrant.service", "Service").get("Restart") == ["always"]


@pytest.mark.parametrize("unit", UNITS)
def test_start_limit_is_in_unit_section(unit: str) -> None:
    unit_keys = _section(unit, "Unit")
    assert unit_keys.get("StartLimitBurst") == ["5"]
    assert unit_keys.get("StartLimitIntervalSec") == ["600"]
    assert not {"StartLimitBurst", "StartLimitIntervalSec"} & _section(unit, "Service").keys()


@pytest.mark.parametrize("unit", UNITS)
def test_systemd_ignores_no_key(unit: str) -> None:
    """``systemd-analyze verify`` also fails on an ExecStart= that is not
    installed, as in CI, so only its unknown-key warnings are asserted."""
    analyze = shutil.which("systemd-analyze")
    if analyze is None:
        pytest.skip("systemd-analyze not available on this host")
    verify = subprocess.run(
        [analyze, "verify", str(DEPLOY / unit)],
        capture_output=True,
        text=True,
    )
    ignored = [ln for ln in verify.stderr.splitlines() if "ignoring" in ln]
    assert ignored == []


@pytest.mark.parametrize("unit", UNITS)
def test_installed_copy_matches_tracked(unit: str) -> None:
    try:
        installed = (INSTALLED / unit).read_text()
    except FileNotFoundError:
        pytest.skip(f"{INSTALLED / unit} not installed on this host")
    assert installed == (DEPLOY / unit).read_text()


@pytest.mark.parametrize("unit", UNITS)
def test_loaded_unit_is_the_installed_one(unit: str) -> None:
    """A copied file is not a loaded one until ``systemctl daemon-reload``."""
    assert _show(unit, "NeedDaemonReload")["NeedDaemonReload"] == "no"


def test_loaded_qdrant_has_the_tracked_exit_handling() -> None:
    shown = _show("qdrant.service", "Restart", "SuccessExitStatus")
    assert shown["Restart"] == "always"
    assert "143" in shown["SuccessExitStatus"].split()


@pytest.mark.parametrize("unit", UNITS)
def test_loaded_start_limit_is_the_tracked_one(unit: str) -> None:
    shown = _show(unit, "StartLimitBurst", "StartLimitIntervalUSec")
    assert shown["StartLimitBurst"] == "5"
    assert shown["StartLimitIntervalUSec"] == "10min"
