"""Qdrant runs with telemetry off, from a pinned image that trusts no
distrusted root (#13).

The image's ``config.yaml`` has ``telemetry_disabled: false``, so Qdrant
reported to Qdrant over TLS, through whatever roots its image trusts. That
image's bundle still held ``emSign ECC Root CA - G3`` after the eMudhra
compromise (CannObserv/processor#66). The payload was anonymised, but it was
Qdrant's only routine outbound TLS, and none of the cohort uses it. The
owner turned it off on 2026-10-23.

The image is a version and a digest, so the bundle the guard checked is the
one that runs. v1.17.0's bundle held G3, as did v1.18.3's and v1.19.2's; the
first without it is v1.20.0 (Debian's ``ca-certificates 20250419+deb13u1``).
An upgrade goes one minor version at a time: deploy/README.md, "Qdrant
upgrade".
"""

import re
import shlex
from pathlib import Path

from image_trust import DISTRUSTED, trusted_cns

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


def _image() -> str:
    return _docker_run()[-1]


def test_the_image_is_a_version_and_a_digest() -> None:
    assert re.fullmatch(r"qdrant/qdrant:v\d+\.\d+\.\d+@sha256:[0-9a-f]{64}", _image())


def test_the_image_bundle_parses() -> None:
    """The control: a bundle that parsed to nothing would hold no distrusted
    root either."""
    assert "ISRG Root X1" in trusted_cns(_image())


def test_the_image_trusts_no_distrusted_root() -> None:
    found = DISTRUSTED & set(trusted_cns(_image()))
    assert not found, f"{sorted(found)}: upgrade (deploy/README.md, Qdrant upgrade)"
