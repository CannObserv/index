"""Each container is published on the tailnet address alone (D3, D21).

A publish with no address binds ``0.0.0.0``, which on co-index also means
exe.dev's internal ``10.42.0.0/16``. The Tailscale ACL decides who reaches
these ports only while the tailnet address is the one they are on. For the
collector that matters twice: it has no ingest auth (D21), and its files hold
every cohort VM's Bash commands.

**The address is resolved in the process that publishes.** systemd reads
every ``EnvironmentFile=`` before ``ExecStartPre=`` runs, so no pre-step can
hand it over (D3).

**No address means no container.** ``tailnet-bind.sh`` exits non-zero when
tailscaled assigns nothing, and the script must stop there: an empty
``ADDR`` would publish ``:4318:4318``, on every interface.

The static tests read the tracked scripts. The behaviour tests run each from
a copy, with ``docker``, ``install`` and ``tailnet-bind.sh`` stubbed, so
nothing starts and no host path is touched.
"""

import os
import subprocess
from pathlib import Path

import pytest
from collector import DEPLOY, docker_run_words, options

PORTS = {"qdrant-run.sh": 6333, "ollama-run.sh": 11434, "otelcol-run.sh": 4318}
OTHER_WAYS_TO_OPEN_A_PORT = ("-p", "-P", "--publish-all", "--network", "--net", "--expose")


@pytest.mark.parametrize("script", sorted(PORTS))
def test_one_port_is_published_on_the_resolved_address(script: str) -> None:
    words = docker_run_words(DEPLOY / script)
    port = PORTS[script]
    assert options(words, "--publish") == [f"${{ADDR}}:{port}:{port}"]
    assert [w for w in words if w.split("=")[0] in OTHER_WAYS_TO_OPEN_A_PORT] == []
    assert not [w for w in words if w.startswith("--publish=")]


@pytest.mark.parametrize("script", sorted(PORTS))
def test_the_address_is_resolved_before_the_run(script: str) -> None:
    text = (DEPLOY / script).read_text()
    resolve = 'ADDR="$(/usr/local/bin/tailnet-bind.sh)"'
    assert "set -euo pipefail" in text
    assert text.index("set -euo pipefail") < text.index(resolve) < text.index("exec docker run")


def _stub(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(0o755)


def _run(tmp_path: Path, script: str, bind: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    """``script`` from a copy whose ``/usr/local/bin/`` is ``tmp_path``'s, with
    ``bind`` as ``tailnet-bind.sh``'s body. Returns the run and the call log."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    for tool in ("docker", "install"):
        _stub(bin_dir / tool, f'printf "%s\\n" "{tool} $*" >> "{log}"\n')
    _stub(bin_dir / "tailnet-bind.sh", bind)
    copy = tmp_path / script
    copy.write_text((DEPLOY / script).read_text().replace("/usr/local/bin/", f"{bin_dir}/"))
    done = subprocess.run(
        ["bash", str(copy)],
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
    )
    return done, log


def _runs(log: Path) -> list[str]:
    calls = log.read_text().splitlines() if log.exists() else []
    return [call for call in calls if call.startswith("docker run ")]


@pytest.mark.parametrize("script", sorted(PORTS))
def test_the_container_is_published_where_tailnet_bind_says(tmp_path: Path, script: str) -> None:
    done, log = _run(tmp_path, script, "echo 100.64.0.9\n")
    assert done.returncode == 0, done.stderr
    [run] = _runs(log)
    port = PORTS[script]
    assert f"--publish 100.64.0.9:{port}:{port} " in run
    assert "0.0.0.0" not in run


@pytest.mark.parametrize("script", sorted(PORTS))
def test_no_address_starts_no_container(tmp_path: Path, script: str) -> None:
    done, log = _run(tmp_path, script, "echo 'tailnet-bind: no Tailscale address' >&2\nexit 1\n")
    assert done.returncode != 0
    assert _runs(log) == []
