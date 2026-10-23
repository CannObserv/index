"""Qdrant runs with telemetry off (#13).

The image's ``config.yaml`` has ``telemetry_disabled: false``, so Qdrant
reported to Qdrant over TLS, through whatever roots its image trusts. That
image's bundle still held ``emSign ECC Root CA - G3`` after the eMudhra
compromise (CannObserv/processor#66). The payload was anonymised, but it was
Qdrant's only routine outbound TLS, and none of the cohort uses it. The
owner turned it off on 2026-10-23.
"""

import shlex
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "deploy" / "qdrant-run.sh"


def _docker_run() -> list[str]:
    """The ``exec docker run`` statement's words, continuation lines joined
    and comments ignored."""
    text = SCRIPT.read_text()
    statement = text[text.index("exec docker run") :].replace("\\\n", " ")
    return shlex.split(statement.splitlines()[0])


def _envs() -> dict[str, str]:
    words = _docker_run()
    pairs = [words[i + 1] for i, w in enumerate(words) if w == "--env"]
    return dict(p.partition("=")[::2] for p in pairs)


def test_reads_the_run_statement() -> None:
    """The control: a parse that misses the statement passes the test below
    vacuously."""
    assert _envs()["QDRANT__SERVICE__ENABLE_TLS"] == "1"


def test_telemetry_is_disabled() -> None:
    assert _envs().get("QDRANT__TELEMETRY_DISABLED") == "true"
