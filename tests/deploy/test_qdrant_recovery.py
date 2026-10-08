"""Behaviour tests for deploy/qdrant-recovery.sh, Qdrant's recovery point (#9).

``patching-hosts`` can't name Qdrant as a data store, so its
``recovery-point.sh`` takes nothing on co-index. The owner's rule is a
snapshot, off the node, before any step that stops Qdrant: a re-index is
expensive. This script takes it, and checks Qdrant after the step.

- **``snapshot``** writes the collections' point counts, takes a full-storage
  snapshot, and copies it **out of the container**: the snapshots directory
  isn't on a volume, so ``--rm`` would discard it at the next restart. The
  copy is mode 600, must read back as a tar, and its sha256 is printed for the
  owner's off-node copy. The in-container copy is deleted.
- **``check BEFORE``** compares the counts with ``BEFORE``. A client's index
  run moves counts both ways, so only a collection gone, or emptied, fails.
  Then a real query: an Ollama embedding against the largest code collection
  must return hits.
- **The API key never reaches an argv.** It goes to curl as a header file.

The script runs from a copy whose host paths point into ``tmp_path``, with
``curl`` and ``docker`` stubbed on ``PATH``. Nothing reaches Qdrant or Docker.
"""

import hashlib
import io
import json
import os
import re
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "deploy" / "qdrant-recovery.sh"
HOST_PATHS = re.compile(r"/etc/socraticode/|/var/backups")

KEY = "qdrant-key"
SNAPSHOT = "full-snapshot-2026-10-08-14-08-16.snapshot"
INNER = b"inner snapshot bytes"

# Routes on method and URL, answering from $STATE. Logs each call's argv,
# and each -H @file header as sent, one call per line.
CURL = r"""#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["CURL_LOG"], "a") as f:
    f.write(" ".join(args) + "\n")
sent, method, data, url = [], None, None, None
for i, a in enumerate(args):
    prev = args[i - 1] if i else ""
    if prev == "-H":
        sent.append(open(a[1:]).read().strip() if a.startswith("@") else a)
    elif prev == "-X":
        method = a
    elif prev == "-d":
        data = a
    elif a.startswith("http"):
        url = a
with open(os.environ["CURL_LOG"] + ".sent", "a") as f:
    f.write(" | ".join(sent) + "\n")
method = method or ("POST" if data else "GET")
st = json.load(open(os.environ["STATE"]))
Q, O = "https://index.taild0fb76.ts.net:6333", "http://index.taild0fb76.ts.net:11434"
path = url.split("?")[0]
if (method, path) == ("GET", Q + "/collections"):
    out = {"result": {"collections": [{"name": n} for n in st["counts"]]}}
elif method == "POST" and path.endswith("/points/count"):
    out = {"result": {"count": st["counts"][path.split("/")[-3]]}}
elif (method, path) == ("POST", Q + "/snapshots"):
    out = {"result": {"name": st["snapshot"]}}
elif method == "DELETE" and path.startswith(Q + "/snapshots/"):
    out = {"result": True}
elif method == "GET" and path.startswith(Q + "/collections/"):
    out = {"result": {"config": {"params": {"vectors": {"size": 2}}}}}
elif (method, path) == ("POST", O + "/api/embed"):
    out = {"embeddings": [[0.1, 0.2]]}
elif method == "POST" and path.endswith("/points/query"):
    out = {"result": {"points": [{"id": i, "score": 0.68} for i in range(st["hits"])]}}
else:
    sys.exit(f"stub curl: no route for {method} {url}")
print(json.dumps(out))
"""

DOCKER = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$DOCKER_LOG"
[[ $1 == cp ]] && cat "$SNAP_TAR"
exit "${DOCKER_RC:-0}"
"""


def _tar(name: str, content: bytes) -> bytes:
    """What ``docker cp <container>:<file> -`` writes: a tar holding the file."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name)
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


@pytest.fixture
def host(tmp_path: Path) -> Path:
    """``tmp_path`` laid out as the script's host paths, stubs on ``PATH``."""
    (tmp_path / "etc" / "socraticode").mkdir(parents=True)
    (tmp_path / "etc" / "socraticode" / "qdrant.key").write_text(f"{KEY}\n")
    (tmp_path / "var" / "backups").mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("curl", CURL), ("docker", DOCKER)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    # A snapshot is itself a tar; the script reads it back as one.
    (tmp_path / "snap.tar").write_bytes(_tar(SNAPSHOT, _tar("collections/c", INNER)))
    copy = tmp_path / "qdrant-recovery.sh"
    copy.write_text(HOST_PATHS.sub(lambda m: f"{tmp_path}{m.group(0)}", SCRIPT.read_text()))
    return tmp_path


def _run(host: Path, *args: str, **state: object) -> subprocess.CompletedProcess[str]:
    st = {"counts": {"codebase_a": 10, "context_a": 3}, "hits": 3, "snapshot": SNAPSHOT}
    st.update(state)
    (host / "state.json").write_text(json.dumps(st))
    env = {
        **os.environ,
        "PATH": f"{host / 'bin'}:{os.environ['PATH']}",
        "STATE": str(host / "state.json"),
        "CURL_LOG": str(host / "curl.log"),
        "DOCKER_LOG": str(host / "docker.log"),
        "SNAP_TAR": str(host / "snap.tar"),
        "DOCKER_RC": str(state.get("docker_rc", 0)),
        "LC_ALL": "C",
    }
    return subprocess.run(
        ["bash", str(host / "qdrant-recovery.sh"), *args], capture_output=True, text=True, env=env
    )


def _backups(host: Path, pattern: str) -> list[Path]:
    return sorted((host / "var" / "backups").glob(pattern))


def test_every_host_path_is_one_the_copy_rewrites() -> None:
    """A host path the copy missed would send the test to the live host."""
    text = SCRIPT.read_text()
    assert "/etc/socraticode/qdrant.key" in text
    assert "/var/backups" in text
    assert not re.search(r"/etc/(?!socraticode/)|/var/(?!backups)", text)


def test_usage_without_a_subcommand(host: Path) -> None:
    done = _run(host)
    assert done.returncode != 0
    assert "usage" in done.stderr


def test_snapshot_records_the_counts_before(host: Path) -> None:
    done = _run(host, "snapshot")
    assert done.returncode == 0, done.stderr
    (counts,) = _backups(host, "qdrant-counts-before-*.txt")
    assert counts.read_text() == "codebase_a 10\ncontext_a 3\n"
    assert stat.S_IMODE(counts.stat().st_mode) == 0o600


def test_snapshot_is_copied_out_of_the_container_at_mode_600(host: Path) -> None:
    done = _run(host, "snapshot")
    assert done.returncode == 0, done.stderr
    assert f"cp qdrant:/qdrant/snapshots/{SNAPSHOT} -" in (host / "docker.log").read_text()
    (snap,) = _backups(host, "qdrant-*.snapshot")
    assert snap.read_bytes() == _tar("collections/c", INNER)
    assert stat.S_IMODE(snap.stat().st_mode) == 0o600


def test_snapshot_prints_the_sha256_for_the_off_node_copy(host: Path) -> None:
    done = _run(host, "snapshot")
    (snap,) = _backups(host, "qdrant-*.snapshot")
    digest = hashlib.sha256(snap.read_bytes()).hexdigest()
    assert f"{digest}  {snap}" in done.stdout


def test_snapshot_deletes_the_copy_inside_the_container(host: Path) -> None:
    _run(host, "snapshot")
    calls = (host / "curl.log").read_text().splitlines()
    assert any(re.search(rf"-X DELETE \S*/snapshots/{re.escape(SNAPSHOT)}", c) for c in calls)


def test_a_failed_copy_fails_and_leaves_no_snapshot(host: Path) -> None:
    done = _run(host, "snapshot", docker_rc=1)
    assert done.returncode != 0
    assert _backups(host, "qdrant-*.snapshot") == []
    assert SNAPSHOT in done.stderr


def test_a_copy_that_isnt_a_tar_fails(host: Path) -> None:
    (host / "snap.tar").write_bytes(_tar(SNAPSHOT, b"cut off"))
    done = _run(host, "snapshot")
    assert done.returncode != 0
    assert _backups(host, "qdrant-*.snapshot") == []


def test_without_the_key_it_says_to_use_sudo(host: Path) -> None:
    (host / "etc" / "socraticode" / "qdrant.key").chmod(0)
    if os.access(host / "etc" / "socraticode" / "qdrant.key", os.R_OK):
        pytest.skip("running as root: every file is readable")
    done = _run(host, "snapshot")
    assert done.returncode != 0
    assert "sudo" in done.stderr


@pytest.mark.parametrize("args", [("snapshot",), ("check", "BEFORE")])
def test_the_key_is_sent_but_never_an_argument(host: Path, args: tuple[str, ...]) -> None:
    (host / "before.txt").write_text("codebase_a 10\ncontext_a 3\n")
    args = tuple(str(host / "before.txt") if a == "BEFORE" else a for a in args)
    done = _run(host, *args)
    assert done.returncode == 0, done.stderr
    assert KEY not in (host / "curl.log").read_text()
    assert f"api-key: {KEY}" in (host / "curl.log.sent").read_text()


def _check(host: Path, before: str, **state: object) -> subprocess.CompletedProcess[str]:
    (host / "before.txt").write_text(before)
    return _run(host, "check", str(host / "before.txt"), **state)


def test_check_passes_when_counts_move_and_the_query_hits(host: Path) -> None:
    """A client's index run adds and deletes points while the run goes on."""
    done = _check(host, "codebase_a 8\ncontext_a 5\n")
    assert done.returncode == 0, done.stderr
    assert "changed: codebase_a 8 -> 10" in done.stdout
    assert "query codebase_a: 3 hits" in done.stdout


def test_check_fails_on_a_missing_collection(host: Path) -> None:
    done = _check(host, "codebase_a 10\ncontext_a 3\ngone 7\n")
    assert done.returncode != 0
    assert "missing: gone (had 7)" in done.stdout


def test_check_fails_on_an_emptied_collection(host: Path) -> None:
    done = _check(host, "codebase_a 10\ncontext_a 3\n", counts={"codebase_a": 10, "context_a": 0})
    assert done.returncode != 0
    assert "emptied: context_a (had 3)" in done.stdout


def test_check_fails_when_the_query_finds_nothing(host: Path) -> None:
    done = _check(host, "codebase_a 10\ncontext_a 3\n", hits=0)
    assert done.returncode != 0
    assert "query codebase_a: 0 hits" in done.stdout


def test_check_queries_the_largest_code_collection(host: Path) -> None:
    counts = {"codebase_a": 10, "codebase_b": 40, "context_a": 99}
    done = _check(host, "codebase_a 10\ncodebase_b 40\ncontext_a 99\n", counts=counts)
    assert done.returncode == 0, done.stderr
    assert "query codebase_b:" in done.stdout
