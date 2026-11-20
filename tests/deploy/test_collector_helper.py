"""The test collector can't reach a host path (CR 10).

``collector.py`` starts the pinned image with the run script's own arguments,
its two host paths swapped for scratch ones. On co-index the real data
directory is ``/var/lib/otelcol``. Were the run script's mounts to change
without the helper following, the test container would mount it read-write,
as root, and the tests would write their fixtures into the cohort's data.
"""

from pathlib import Path

import pytest
from collector import CONFIG, RUN_SCRIPT, adapt, docker_run_words, options

DATA = Path("/tmp/pytest-of-x/otelcol-data")


def _words() -> list[str]:
    return docker_run_words(RUN_SCRIPT)[3:]


def test_the_run_scripts_mounts_become_scratch_ones() -> None:
    args = adapt(_words(), "otelcol-test-x", DATA)
    assert options(args, "--volume") == [
        f"{CONFIG}:/etc/otelcol-contrib/config.yaml:ro",
        f"{DATA}:/var/lib/otelcol",
    ]
    assert "--rm" not in args, "a collector that exits at its start keeps its logs"
    assert options(args, "--cap-drop") == ["ALL"], "the flags under test are the script's"


@pytest.mark.parametrize(
    "mount",
    [
        ["--volume", "/var/lib/otelcol-new:/var/lib/otelcol"],
        ["--volume", "/var/lib/otelcol/archive:/archive"],
        ["--volume=/var/lib/otelcol:/var/lib/otelcol"],
        ["-v", "/var/lib/otelcol:/var/lib/otelcol"],
        ["--mount", "type=bind,src=/var/lib/otelcol,dst=/var/lib/otelcol"],
    ],
    ids=["a new host path", "a path under the real directory", "attached", "short", "mount"],
)
def test_a_mount_the_helper_does_not_rewrite_is_refused(mount: list[str]) -> None:
    words = _words()
    words[-1:-1] = mount  # before the image
    with pytest.raises(AssertionError, match="mount"):
        adapt(words, "otelcol-test-x", DATA)
