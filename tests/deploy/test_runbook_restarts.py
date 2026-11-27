"""The runbook never proves "didn't restart" with ``NRestarts`` (#53).

``NRestarts`` counts only the restarts a unit's ``Restart=`` policy made. A
``systemctl restart``, a stop and a start, or Docker's restart, which takes
Qdrant, Ollama and the collector with it (``Requires=docker.service``), leaves
it where it was. On 2026-11-27 ``qdrant.service`` read ``NRestarts=0`` with
its ``ActiveEnterTimestamp`` at the renewal's ``systemctl restart`` four days
before (#52). A before-and-after read of it passes either way.

What moves on any start is the unit's ``InvocationID`` and the container's
``StartedAt``. ``NRestarts`` stays only where it means what it counts: after
a reboot, ``0`` is no crash loop.
"""

import re
from pathlib import Path

RUNBOOK = Path(__file__).resolve().parents[2] / "deploy" / "README.md"


def _sentences(text: str) -> list[str]:
    """Each paragraph and list item, split into sentences, each on one line."""
    blocks = re.split(r"\n\s*\n|\n(?=\s*- )", text)
    return [
        sentence
        for block in blocks
        for sentence in re.split(r"(?<=[.;])\s+(?=[A-Z`*])", " ".join(block.split()))
    ]


def _proofs_by_nrestarts(text: str) -> list[str]:
    """A sentence naming it must be the reboot check, or say what it counts."""
    uses = [s for s in _sentences(text) if "NRestarts" in s]
    return [s for s in uses if "reboot" not in s and "counts only" not in s]


def test_nrestarts_is_read_only_after_a_reboot() -> None:
    text = RUNBOOK.read_text()
    assert any("NRestarts" in s and "reboot" in s for s in _sentences(text)), (
        "the control: the reboot check reads it"
    )
    assert _proofs_by_nrestarts(text) == []


REMOVED = "Qdrant and Ollama don't restart: read their `NRestarts` before and after."


def test_the_sentence_53_removed_is_refused() -> None:
    """The control: put back, the rule finds it, and only it."""
    text = RUNBOOK.read_text().replace("was (#53).", f"was (#53). {REMOVED}")
    assert REMOVED in text, "the control's control: it went in"
    assert _proofs_by_nrestarts(text) == [REMOVED]


def _first_install() -> str:
    """The collector's "First install" paragraph, up to its code block."""
    text = RUNBOOK.read_text()
    [para] = re.findall(r"^\*\*First install\.\*\*(.*?)^```", text, re.M | re.S)
    return para


def test_the_collectors_first_install_reads_what_any_start_moves() -> None:
    para = _first_install()
    assert "InvocationID" in para
    assert "StartedAt" in para
