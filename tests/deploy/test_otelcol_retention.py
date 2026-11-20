"""Behaviour tests for deploy/otelcol-retention.sh: compress, prune, cap (D21, #33).

The collector's exporter rotates by size and does nothing else to its files.
This run, once a day, is the rest of the storage line:

- **Each rotated file becomes a standard zstd stream** (``zstd -dc`` gives the
  JSONL back), written under another name and renamed, so a reader never
  sees half of one. The exporter's own ``compression: zstd`` isn't that.
- **It lands in ``archive/``, never beside the rotated file.** The exporter
  counts a ``…-size.jsonl.zst`` in its own directory as one of its backups
  and deletes all but the newest few (CR 1, measured on 0.165.0).
- **A file is pruned 90 days after its last write.** The compressed file
  keeps the rotated file's mtime, so compressing late doesn't restart the
  clock.
- **The directory is held under the cap** by deleting the oldest compressed
  files. The active files are never deleted: the exporter holds them open.
  Over the cap with nothing left to delete is a failure, not a pass.
- **Every file is left 600.** The exporter creates them 644.
- **Success leaves a stamp,** which the check-in reads (``otel:retention``):
  a run that fails, or a timer that stops, would otherwise end the prune
  silently.

The script runs from a copy whose directory is ``tmp_path``'s. The cap is
tested with sparse files: sizes are apparent bytes.
"""

import os
import re
import stat
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "deploy"
SCRIPT = DEPLOY / "otelcol-retention.sh"
HOST_DIR = "/var/lib/otelcol"
STAMP = ".retention-ok"
DAY = 86400
MIB = 1024 * 1024
ROTATED = "events-2026-11-19T03-04-05.678-size.jsonl"
LINES = '{"resourceLogs":[{"n":1}]}\n{"resourceLogs":[{"n":2}]}\n'


@pytest.fixture
def data(tmp_path: Path) -> Path:
    directory = tmp_path / "otelcol"
    directory.mkdir()
    return directory


def _run(data: Path, path: str | None = None) -> subprocess.CompletedProcess[str]:
    copy = data.parent / "otelcol-retention.sh"
    copy.write_text(SCRIPT.read_text().replace(HOST_DIR, str(data)))
    env = {**os.environ, "LC_ALL": "C"}
    if path is not None:
        env["PATH"] = f"{path}:{env['PATH']}"
    return subprocess.run(["bash", str(copy)], env=env, capture_output=True, text=True)


def _put(data: Path, name: str, content: str = LINES, age_days: float = 0, mode: int = 0o644):
    path = data / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(content)
    path.chmod(mode)
    when = time.time() - age_days * DAY
    os.utime(path, (when, when))
    return path


def _sparse(data: Path, name: str, mib: int, age_days: float) -> Path:
    path = data / name
    path.parent.mkdir(exist_ok=True)
    with path.open("wb") as handle:
        handle.truncate(mib * MIB)
    when = time.time() - age_days * DAY
    os.utime(path, (when, when))
    return path


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _names(data: Path) -> set[str]:
    """Every file under the directory, relative to it, bar the stamp."""
    found = {p.relative_to(data).as_posix() for p in data.rglob("*") if not p.is_dir()}
    return found - {STAMP}


ARCHIVED = f"archive/{ROTATED}.zst"


def test_the_copy_reaches_no_other_host_path() -> None:
    body = SCRIPT.read_text().split("\n", 1)[1]
    paths = re.findall(r"(?<![\w.:/])/(?:etc|var|tmp|usr|run|srv|opt|home|root)/[^\s\"')]*", body)
    assert HOST_DIR in paths, "the scan finds no paths at all"
    assert [p for p in paths if not p.startswith(HOST_DIR)] == []


def test_a_rotated_file_becomes_a_standard_zstd_stream(data: Path) -> None:
    rotated = _put(data, ROTATED, age_days=2)
    written = rotated.stat().st_mtime
    done = _run(data)
    assert done.returncode == 0, done.stderr
    assert _names(data) == {ARCHIVED}
    compressed = data / ARCHIVED
    back = subprocess.run(["zstd", "-dc", str(compressed)], capture_output=True, text=True)
    assert back.stdout == LINES
    assert _mode(compressed) == 0o600
    assert compressed.stat().st_mtime == pytest.approx(written, abs=1)


def test_the_active_files_keep_their_content_and_become_600(data: Path) -> None:
    for name in ("events.jsonl", "metrics.jsonl"):
        _put(data, name, mode=0o644)
    assert _run(data).returncode == 0
    for name in ("events.jsonl", "metrics.jsonl"):
        assert (data / name).read_text() == LINES
        assert _mode(data / name) == 0o600


@pytest.mark.parametrize(("age_days", "kept"), [(89.9, True), (90.1, False)])
def test_a_compressed_file_is_pruned_ninety_days_after_its_last_write(
    data: Path, age_days: float, kept: bool
) -> None:
    name = ARCHIVED
    _put(data, name, "zst", age_days=age_days)
    assert _run(data).returncode == 0
    assert (name in _names(data)) is kept


def test_compressing_late_does_not_restart_the_clock(data: Path) -> None:
    """A rotated file found after 90 days, the timer having been dead, goes
    in the run that finds it."""
    _put(data, ROTATED, age_days=91)
    assert _run(data).returncode == 0
    assert _names(data) == set()


def test_over_the_cap_the_oldest_compressed_files_go(data: Path) -> None:
    """Eight files of 200 MiB against 1 GiB: exactly the three oldest go.
    Their ages follow neither their names nor the order they were created
    in, so no directory order passes for "oldest first" without the sort."""
    ages = {0: 40, 1: 10, 2: 70, 3: 20, 4: 80, 5: 30, 6: 60, 7: 50}
    for n, age in ages.items():
        _sparse(data, f"archive/events-2026-10-0{n}T00-00-00.000-size.jsonl.zst", 200, age)
    done = _run(data)
    assert done.returncode == 0, done.stderr
    assert _names(data) == {
        f"archive/events-2026-10-0{n}T00-00-00.000-size.jsonl.zst" for n in (0, 1, 3, 5, 7)
    }
    assert done.stderr.count("over the cap") == 3


def test_under_the_cap_nothing_is_deleted(data: Path) -> None:
    for n, age in enumerate((30, 20)):
        _sparse(data, f"archive/events-2026-10-0{n}T00-00-00.000-size.jsonl.zst", 400, age)
    assert _run(data).returncode == 0
    assert len(_names(data)) == 2


def test_the_active_file_is_never_deleted_and_the_run_fails(data: Path) -> None:
    """The exporter holds it open: deleting it frees nothing until a restart.
    Its own rotation is what bounds it, so this is a fault to be heard."""
    _sparse(data, "events.jsonl", 1100, 0)
    _sparse(data, ARCHIVED, 10, 5)
    done = _run(data)
    assert done.returncode != 0
    assert _names(data) == {"events.jsonl"}
    assert "nothing left to delete" in done.stderr
    assert not (data / STAMP).exists()


def test_success_leaves_a_fresh_stamp(data: Path) -> None:
    stamp = _put(data, STAMP, "", age_days=3)
    assert _run(data).returncode == 0
    assert time.time() - stamp.stat().st_mtime < 60


def _failing_zstd(tmp_path: Path) -> str:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "zstd").write_text("#!/usr/bin/env bash\necho 'zstd: no space' >&2\nexit 1\n")
    (bin_dir / "zstd").chmod(0o755)
    return str(bin_dir)


def test_a_failed_compression_keeps_the_file_and_leaves_no_stamp(data: Path) -> None:
    _put(data, ROTATED)
    stamp = _put(data, STAMP, "", age_days=3)
    done = _run(data, path=_failing_zstd(data.parent))
    assert done.returncode != 0
    assert _names(data) == {ROTATED}
    assert (data / ROTATED).read_text() == LINES
    assert time.time() - stamp.stat().st_mtime > 2 * DAY


def test_a_rotated_file_the_exporter_deleted_meanwhile_is_skipped(data: Path) -> None:
    """Past its limit the exporter deletes its oldest rotated file, at any
    moment. That happens under a flood, which is when the cap matters most,
    and the run used to stop at the first such file: no prune, no cap, no
    stamp (CR 7). Here ``zstd`` finds the first file already gone."""
    first = "events-2026-11-18T00-00-00.000-size.jsonl"
    _put(data, first)
    _put(data, ROTATED)
    bin_dir = data.parent / "bin"
    bin_dir.mkdir()
    real = subprocess.run(["which", "zstd"], capture_output=True, text=True).stdout.strip()
    (bin_dir / "zstd").write_text(
        "#!/usr/bin/env bash\n"
        f'for a in "$@"; do [[ $a == */{first} ]] && rm -f "$a"; done\n'
        f'exec {real} "$@"\n'
    )
    (bin_dir / "zstd").chmod(0o755)
    done = _run(data, path=str(bin_dir))
    assert done.returncode == 0, done.stderr
    assert _names(data) == {ARCHIVED}
    assert (data / STAMP).exists()
    assert "compressed 1," in done.stdout


def test_a_compressed_file_is_600_before_it_has_its_name(data: Path) -> None:
    """``zstd`` gives its output the source's mode, 644 from the exporter,
    whatever the umask (CR 17). A reader that finds the file by its final
    name must never find it open."""
    _put(data, ROTATED, mode=0o644)
    bin_dir = data.parent / "bin"
    bin_dir.mkdir()
    real = subprocess.run(["which", "mv"], capture_output=True, text=True).stdout.strip()
    (bin_dir / "mv").write_text(
        "#!/usr/bin/env bash\n"
        f'stat -c %a "${{@: -2:1}}" >> "{data.parent}/modes"\n'
        f'exec {real} "$@"\n'
    )
    (bin_dir / "mv").chmod(0o755)
    assert _run(data, path=str(bin_dir)).returncode == 0
    assert (data.parent / "modes").read_text().split() == ["600"]


def test_without_zstd_it_fails_before_touching_anything(data: Path) -> None:
    """A rebuilt host without the package: say so at once, not on the first
    day a file rotates."""
    copy = data.parent / "otelcol-retention.sh"
    copy.write_text(SCRIPT.read_text().replace(HOST_DIR, str(data)))
    empty = data.parent / "empty-bin"
    empty.mkdir()
    for tool in (
        "bash",
        "find",
        "install",
        "sort",
        "awk",
        "wc",
        "touch",
        "mv",
        "rm",
        "chmod",
        "env",
    ):
        found = subprocess.run(["which", tool], capture_output=True, text=True).stdout.strip()
        (empty / tool).symlink_to(found)
    done = subprocess.run(
        [str(empty / "bash"), str(copy)], env={"PATH": str(empty)}, capture_output=True, text=True
    )
    assert done.returncode != 0
    assert "zstd" in done.stderr
    assert not (data / STAMP).exists()


def test_a_half_written_file_from_a_killed_run_is_removed(data: Path) -> None:
    _put(data, f"{ARCHIVED}.tmp", "half")
    _put(data, ROTATED)
    assert _run(data).returncode == 0
    assert _names(data) == {ARCHIVED}


def test_only_the_exporters_own_files_are_touched(data: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside-size.jsonl"
    outside.write_text("not ours")
    (data / "link-size.jsonl").symlink_to(outside)
    (data / "sub").mkdir()
    _put(data / "sub", ROTATED)
    _put(data, "notes.txt", "kept", age_days=400)
    assert _run(data).returncode == 0
    assert outside.read_text() == "not ours"
    assert (data / "link-size.jsonl").is_symlink()
    assert (data / "sub" / ROTATED).exists()
    assert (data / "notes.txt").read_text() == "kept"


def test_a_compressed_file_left_beside_the_rotated_ones_is_moved(data: Path) -> None:
    """Whatever put it there, the exporter would delete it as a backup."""
    _put(data, f"{ROTATED}.zst", "zst", age_days=3)
    assert _run(data).returncode == 0
    assert _names(data) == {ARCHIVED}


def test_the_archive_is_roots_alone(data: Path) -> None:
    _put(data, ROTATED)
    assert _run(data).returncode == 0
    assert _mode(data / "archive") == 0o700


def test_a_missing_directory_fails(data: Path) -> None:
    data.rmdir()
    done = _run(data)
    assert done.returncode != 0
    assert "missing" in done.stderr


def test_it_says_what_it_did(data: Path) -> None:
    _put(data, ROTATED)
    _put(data, "archive/events-2026-08-01T00-00-00.000-size.jsonl.zst", "old", age_days=100)
    done = _run(data)
    assert done.returncode == 0
    assert re.search(
        r"otelcol-retention: compressed 1, pruned 1, over the cap 0, \d+ bytes", done.stdout
    )


def _unit(name: str) -> str:
    return (DEPLOY / name).read_text()


def test_the_timer_runs_daily_and_catches_up() -> None:
    """``Persistent=true``: a run missed while the host was down is due at
    boot, not a day later."""
    timer = _unit("otelcol-retention.timer")
    assert re.search(r"^OnCalendar=\*-\*-\* \d\d:\d\d:00 UTC$", timer, flags=re.M)
    assert "Persistent=true" in timer
    assert "WantedBy=timers.target" in timer


def test_the_service_runs_the_script_at_idle_priority() -> None:
    """It shares two vCPUs with Ollama's embedding (D1)."""
    service = _unit("otelcol-retention.service")
    assert "Type=oneshot" in service
    assert "ExecStart=/usr/local/bin/otelcol-retention.sh" in service
    assert "Nice=19" in service
    assert "[Install]" not in service, "the timer starts it"
