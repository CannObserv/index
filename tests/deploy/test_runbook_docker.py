"""The runbook's ``docker`` commands go through ``sudo`` (D20, #18).

D20 takes ``exedev`` out of group ``docker``: an operator's ``docker`` call
runs as root or fails at the socket. Every ``docker`` command in a code block
of ``deploy/README.md``, a comment's included, is written ``sudo docker``.
Prose that names a command (``docker stop qdrant`` as an out-of-band
SIGTERM, say) describes a hazard and isn't checked.
"""

import re
from pathlib import Path

RUNBOOK = Path(__file__).resolve().parents[2] / "deploy" / "README.md"
BARE = re.compile(r"(?<!sudo )\bdocker [a-z]")


def _code_lines() -> list[tuple[int, str]]:
    lines, fenced = [], False
    for n, line in enumerate(RUNBOOK.read_text().splitlines(), 1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif fenced:
            lines.append((n, line))
    return lines


def test_the_runbook_has_docker_commands() -> None:
    assert any("docker " in line for _, line in _code_lines())


def test_every_docker_command_goes_through_sudo() -> None:
    bare = [f"README.md:{n}: {line.strip()}" for n, line in _code_lines() if BARE.search(line)]
    assert bare == []
