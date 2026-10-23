"""The embedding engine is pinned, and the image that runs is the one pinned (#11).

A new Ollama can change ``nomic-embed-text``'s vectors with the model digest,
prefixes and dimensions unchanged: 0.40.5 case-folds input that 0.21.2 did
not, and moved every chunk holding an uppercase letter. SocratiCode's index
profile can't see that, and it re-embeds only files whose hash changed, so a
rebuild onto such a release leaves every collection silently mixed.

``FROM ollama/ollama:latest`` let any rebuild cross that line unchosen. The
base is a version and a digest, and the local tag names the same version, so
``ollama-run.sh`` starts only the image built from that pin, and changing the
engine is a commit (the runbook's "Engine change").

**The image trusts no distrusted root (#13).** Its CA bundle is whatever
``ca-certificates`` was on the day it was built, and Go 1.26's ``crypto/x509``
trusts that bundle as given: the name-based distrust came in 1.27.3. Built
2026-10-17, it still held ``emSign ECC Root CA - G3`` after the eMudhra
compromise (CannObserv/processor#66), and model pulls from
``registry.ollama.ai`` verify against it. A rebuild is ``--pull --no-cache``,
so the base and the apt layer are today's, and the check below runs on
co-index after it: the bundle is parsed as certificates, never grepped for a
label.
"""

import re
import socket
import ssl
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "deploy"
HOST = "co-index"
BUNDLE = "/etc/ssl/certs/ca-certificates.crt"
# Subject CNs. processor#66; https://exe.dev/security/emudhra-compromise.
DISTRUSTED = {"emSign ECC Root CA - G3"}

BASE = re.compile(r"^FROM ollama/ollama:(\S+?)(?:@(sha256:[0-9a-f]+))?(?:\s+AS\s+\S+)?$", re.M)
VERSION = re.compile(r"^\d+\.\d+\.\d+$")


def _base() -> tuple[str, str | None]:
    [match] = BASE.findall((DEPLOY / "ollama-slim" / "Dockerfile").read_text())
    return match[0], match[1] or None


def _run_images() -> list[str]:
    return re.findall(r"socraticode/ollama-slim:[\w.-]+", (DEPLOY / "ollama-run.sh").read_text())


def test_the_base_is_a_version_and_a_digest() -> None:
    version, digest = _base()
    assert VERSION.match(version), f"ollama/ollama:{version} is not a release version"
    assert digest is not None and len(digest) == len("sha256:") + 64, "no digest"


def test_the_unit_runs_the_pinned_version() -> None:
    """The run script's tag is the base's version, so a rebuild under another
    tag never starts, and a tag never names a build of some other release."""
    version, _ = _base()
    assert set(_run_images()) == {f"socraticode/ollama-slim:{version}"}


def test_the_runbook_builds_the_tag_the_unit_runs() -> None:
    version, _ = _base()
    builds = re.findall(
        r"docker build .*?-t (\S+) deploy/ollama-slim", (DEPLOY / "README.md").read_text()
    )
    assert builds == [f"socraticode/ollama-slim:{version}"]


def test_the_runbook_builds_from_todays_base_and_archive() -> None:
    """``--pull`` alone reuses a cached apt layer when the base is unchanged,
    and the bundle with it."""
    flags = re.findall(
        r"docker build (.*?)-t \S+ deploy/ollama-slim", (DEPLOY / "README.md").read_text()
    )
    assert [set(f.split()) for f in flags] == [{"--pull", "--no-cache"}]


def _trusted_cns() -> list[str]:
    """The subject CNs of the roots in the bundle of the image the unit runs."""
    if socket.gethostname() != HOST:
        pytest.skip(f"not {HOST}")
    [image] = set(_run_images())
    pem = subprocess.run(
        ["docker", "run", "--rm", "--network", "none", "--entrypoint", "/bin/cat", image, BUNDLE],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(cadata=pem)
    return [
        v for ca in ctx.get_ca_certs() for rdn in ca["subject"] for k, v in rdn if k == "commonName"
    ]


def test_the_image_bundle_parses() -> None:
    """The control: a bundle that parsed to nothing would hold no distrusted
    root either."""
    assert "ISRG Root X1" in _trusted_cns()


def test_the_image_trusts_no_distrusted_root() -> None:
    found = DISTRUSTED & set(_trusted_cns())
    assert not found, f"{sorted(found)}: rebuild (the runbook's build line), restart ollama.service"
