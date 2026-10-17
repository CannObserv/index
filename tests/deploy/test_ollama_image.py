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
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "deploy"

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
        r"docker build -t (\S+) deploy/ollama-slim", (DEPLOY / "README.md").read_text()
    )
    assert builds == [f"socraticode/ollama-slim:{version}"]
