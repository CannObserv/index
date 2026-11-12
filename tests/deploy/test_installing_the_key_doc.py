"""The client procedure orders a fresh server before the first index (#42).

**A SocratiCode server reads its ``env`` block, and the key, only at start.**
It reads ``.socraticode.json`` live. So a session from before the block reached
the checkout stays in managed mode, and its first ``codebase_index`` writes the
new collection names into the **local** store. That's what happened on
CannObserv/usa-wa#556: nothing failed, and nothing reached co-index. The doc
puts a reload and a server-side ``Qdrant mode: external`` check ahead of its
first ``codebase_index``.

**The ``check-ignore`` step is written once.** A merge left it, and its
paragraph, in the doc twice until #42.

These read the document. They pin the order of the steps, not their wording.
"""

from pathlib import Path

DOC = Path(__file__).resolve().parents[2] / "docs" / "installing-the-key.md"
CHECK_IGNORE = "git -C <repo> check-ignore -v .claude/settings.local.json"
MODE = "Qdrant mode: external"


def _text() -> str:
    return DOC.read_text()


def _first_index() -> int:
    text = _text()
    assert "codebase_index" in text, "the doc never names codebase_index"
    return text.index("codebase_index")


def test_a_reload_precedes_the_first_index() -> None:
    text = _text().lower()
    assert "reload the editor window" in text
    assert text.index("reload the editor window") < _first_index()


def test_the_mode_check_precedes_the_first_index() -> None:
    text = _text()
    assert MODE in text
    assert text.index(MODE) < _first_index()


def test_the_check_ignore_step_is_written_once() -> None:
    assert _text().count(CHECK_IGNORE) == 1
