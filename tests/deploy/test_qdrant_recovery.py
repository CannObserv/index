"""Behaviour tests for deploy/qdrant-recovery.sh, Qdrant's recovery point (#9, #30).

It's the recovery point and check for the steps ``patching-hosts`` doesn't
run: a Docker restart, a Qdrant upgrade, an engine change (deploy/README.md).
The owner's rule is a snapshot, off the node, before any step that stops
Qdrant: a re-index is expensive. A patching run's snapshot is
``recovery-point.sh``'s, from the knob's ``datastore qdrant`` line. Its counts
file is ``BEFORE``'s format, so ``check`` takes it after a step, for the real
query the skill's own check doesn't run.

- **``snapshot``** writes the collections' point counts, takes a full-storage
  snapshot, and copies it **out of the container**: the snapshots directory
  isn't on a volume, so ``--rm`` would discard it at the next restart. The
  copy is mode 600, must read back as a tar, and its sha256 is printed for the
  owner's off-node copy. The in-container copy is deleted.
- **``check BEFORE``** compares the counts with ``BEFORE``. A client's index
  run moves counts both ways, so only a collection gone, or emptied, fails.
  Then a real query: an Ollama embedding against the largest code collection
  must return hits. Ollama serialises embeds, so behind a client's batch the
  embed may time out: each try waits ``EMBED_T`` seconds (60), and only a
  timeout is retried, up to 3 tries, then it fails with a message, not a hang.
- **The API key never reaches an argv.** It goes to curl as a header file.
- **Each Qdrant call's limit is the script's own** (#39): 1800 s for the
  snapshot, 300 s for its delete, 30 s for the rest. Nothing reads one from
  the environment, where a stray ``T=0`` would be no limit at all.

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
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "deploy" / "qdrant-recovery.sh"
HOST_PATHS = re.compile(r"/etc/socraticode/|/var/backups")

KEY = "qdrant-key"
RO_KEY = "qdrant-ro-key"
SNAPSHOT = "full-snapshot-2026-10-08-14-08-16.snapshot"
INNER = b"inner snapshot bytes"

# Routes on method and URL, answering from $STATE. Logs each call's argv,
# and each -H @file header as sent, one call per line.
CURL = r"""#!/usr/bin/env python3
import json, os, sys, time
args = sys.argv[1:]
with open(os.environ["CURL_LOG"], "a") as f:
    f.write(" ".join(args) + "\n")
sent, method, data, url, limit = [], None, None, None, None
for i, a in enumerate(args):
    prev = args[i - 1] if i else ""
    if prev == "-H":
        sent.append(open(a[1:]).read().strip() if a.startswith("@") else a)
    elif prev == "-X":
        method = a
    elif prev == "-d":
        data = a
    elif prev == "--max-time":
        limit = float(a)
    elif a.startswith("http"):
        url = a
with open(os.environ["CURL_LOG"] + ".sent", "a") as f:
    f.write(" | ".join(sent) + "\n")
method = method or ("POST" if data else "GET")
st = json.load(open(os.environ["STATE"]))
Q, O = "https://index.taild0fb76.ts.net:6333", "http://index.taild0fb76.ts.net:11434"
path = url.split("?")[0]
# A slow Qdrant: a call to a path in qdrant_delays answers after that many
# seconds, or past --max-time gives up as curl does.
qd = st.get("qdrant_delays", {}).get(path.split(":6333", 1)[-1], 0)
if qd and limit and qd > limit:
    time.sleep(limit)
    print(f"curl: (28) Operation timed out after {limit * 1000:.0f} ms", file=sys.stderr)
    sys.exit(28)
time.sleep(qd)
if (method, path) == ("GET", Q + "/collections"):
    out = {"result": {"collections": [{"name": n} for n in st["counts"]]}}
elif method == "POST" and path.endswith("/points/count"):
    out = {"result": {"count": st["counts"][path.split("/")[-3]]}}
elif (method, path) == ("POST", Q + "/snapshots"):
    out = {"result": {"name": st["snapshot"]}}
elif method == "DELETE" and path.startswith(Q + "/snapshots/"):
    out = {"result": True}
elif method == "GET" and path.startswith(Q + "/collections/"):
    name = path.split("/")[-1]
    out = {"result": {
        "points_count": st.get("points", st["counts"]).get(name),
        "config": {"params": {"vectors": st.get("vectors", {"size": 2})}},
    }}
elif (method, path) == ("POST", O + "/api/embed"):
    # A slow Ollama: the Nth embed answers after embed_delays[N] seconds. Past
    # --max-time, curl gives up, as curl does: exit 28. With no limit, it waits.
    if st.get("embed_rc"):
        print(f"curl: ({st['embed_rc']}) as curl says it", file=sys.stderr)
        sys.exit(st["embed_rc"])
    n = sum(O + "/api/embed" in c for c in open(os.environ["CURL_LOG"])) - 1
    delays = st.get("embed_delays", [])
    delay = delays[n] if n < len(delays) else 0
    if limit and delay > limit:
        time.sleep(limit)
        print(f"curl: (28) Operation timed out after {limit * 1000:.0f} ms", file=sys.stderr)
        sys.exit(28)
    time.sleep(delay)
    if st.get("embed_error"):
        print(json.dumps({"error": st["embed_error"]}))
        sys.exit(22)
    out = st.get("embed_answer", {"embeddings": [[0.1, 0.2]]})
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
    (tmp_path / "etc" / "socraticode" / "qdrant-ro.key").write_text(f"{RO_KEY}\n")
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


def _run(
    host: Path, *args: str, env: dict[str, str] | None = None, **state: object
) -> subprocess.CompletedProcess[str]:
    st = {"counts": {"codebase_a": 10, "context_a": 3}, "hits": 3, "snapshot": SNAPSHOT}
    st.update(state)
    (host / "state.json").write_text(json.dumps(st))
    full = {
        **os.environ,
        "PATH": f"{host / 'bin'}:{os.environ['PATH']}",
        "STATE": str(host / "state.json"),
        "CURL_LOG": str(host / "curl.log"),
        "DOCKER_LOG": str(host / "docker.log"),
        "SNAP_TAR": str(host / "snap.tar"),
        "DOCKER_RC": str(state.get("docker_rc", 0)),
        "LC_ALL": "C",
        **(env or {}),
    }
    # A bound, so a check that hangs fails here instead of hanging the suite.
    return subprocess.run(
        ["bash", str(host / "qdrant-recovery.sh"), *args],
        capture_output=True,
        text=True,
        env=full,
        timeout=60,
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


def _check(
    host: Path, before: str = "codebase_a 10\ncontext_a 3\n", **kw: object
) -> subprocess.CompletedProcess[str]:
    (host / "before.txt").write_text(before)
    return _run(host, "check", str(host / "before.txt"), **kw)  # type: ignore[arg-type]


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


def _embeds(host: Path) -> list[str]:
    """Each embed call's argv, in order."""
    calls = (host / "curl.log").read_text().splitlines()
    return [c for c in calls if ":11434/api/embed" in c]


def test_the_embed_has_60_s_by_default(host: Path) -> None:
    done = _check(host)
    assert done.returncode == 0, done.stderr
    (embed,) = _embeds(host)
    assert "--max-time 60 " in embed


def test_an_embed_that_timed_out_is_retried(host: Path) -> None:
    """Ollama serialises embeds: behind a client's batch, the first may time out (#20)."""
    done = _check(host, env={"EMBED_T": "1"}, embed_delays=[3])
    assert done.returncode == 0, done.stderr
    assert [("--max-time 1 " in e) for e in _embeds(host)] == [True, True]
    assert "no answer in 1 s (try 1 of 3)" in done.stderr
    assert "query codebase_a: 3 hits" in done.stdout


def test_three_timed_out_embeds_fail_loudly(host: Path) -> None:
    """Bounded, not a hang: the check runs inside a patching step's verification."""
    done = _check(host, env={"EMBED_T": "1"}, embed_delays=[3, 3, 3, 3])
    assert done.returncode != 0
    assert len(_embeds(host)) == 3
    assert "Traceback" not in done.stderr
    assert "no answer in 1 s, 3 tries" in done.stderr
    # `EMBED_T=… sudo …` loses it to sudo's env_reset: name the form that works.
    assert "sudo EMBED_T=" in done.stderr
    assert "/points/query" not in (host / "curl.log").read_text()


def test_an_embed_that_failed_is_not_retried(host: Path) -> None:
    """Only a timeout is a queue; an error from Ollama is a fault."""
    done = _check(host, embed_error='model "nomic-embed-text" not found')
    assert done.returncode != 0
    assert len(_embeds(host)) == 1
    assert "Traceback" not in done.stderr
    assert 'model \\"nomic-embed-text\\" not found' in done.stderr


def test_an_embed_answer_with_no_embedding_fails_with_a_message(host: Path) -> None:
    done = _check(host, embed_answer={"embeddings": []})
    assert done.returncode != 0
    assert "Traceback" not in done.stderr
    assert 'embed answered no embedding: {"embeddings": []}' in done.stderr


@pytest.mark.parametrize("limit", ["0", "-5", "1.5", "soon"])
def test_an_embed_limit_that_isnt_a_positive_integer_is_refused(host: Path, limit: str) -> None:
    """curl reads --max-time 0 as no limit at all, so 0 would hang the check."""
    done = _check(host, env={"EMBED_T": limit})
    assert done.returncode != 0
    assert "EMBED_T" in done.stderr
    assert not (host / "curl.log").exists()


def _qdrant_limits(host: Path) -> list[tuple[str, str]]:
    """Each Qdrant call's method and path, with the ``--max-time`` it was sent."""
    out = []
    for call in (host / "curl.log").read_text().splitlines():
        if ":6333/" not in call:
            continue
        args = call.split()
        method = args[args.index("-X") + 1] if "-X" in args else "GET"
        url = next(a for a in args if a.startswith("http")).split("?")[0]
        out.append((f"{method} {url.split(':6333', 1)[1]}", args[args.index("--max-time") + 1]))
    return out


@pytest.mark.parametrize("stray", ["0", "soon", "5"])
def test_snapshot_keeps_its_own_limits_whatever_t_says(host: Path, stray: str) -> None:
    """A stray ``T`` (``sudo -E``) never reaches curl (#39): ``-m 0`` is no limit at all.

    The snapshot waits up to 1800 s, its delete 300 s, every other call 30 s.
    """
    done = _run(host, "snapshot", env={"T": stray})
    assert done.returncode == 0, done.stderr
    assert _qdrant_limits(host) == [
        ("GET /collections", "30"),
        ("POST /collections/codebase_a/points/count", "30"),
        ("POST /collections/context_a/points/count", "30"),
        ("POST /snapshots", "1800"),
        (f"DELETE /snapshots/{SNAPSHOT}", "300"),
    ]


@pytest.mark.parametrize("stray", ["0", "soon", "5"])
def test_check_keeps_its_own_limits_whatever_t_says(host: Path, stray: str) -> None:
    done = _check(host, env={"T": stray})
    assert done.returncode == 0, done.stderr
    limits = _qdrant_limits(host)
    assert [c for c, _ in limits] == [
        "GET /collections",
        "POST /collections/codebase_a/points/count",
        "POST /collections/context_a/points/count",
        "GET /collections/codebase_a",
        "POST /collections/codebase_a/points/query",
    ]
    assert {t for _, t in limits} == {"30"}


# --- query (#40): the knob's recovery line --------------------------------
#
# It runs as the knob user, never root, under `timeout -k 10 60` (knob.md:69,
# _probe-lib.sh:174-184), with its output to /dev/null: apply.sh after a step
# that restarted Qdrant, and probe.sh --post-boot after the reboot. The exit
# code is all a run records. So it reads the read-only key, takes no counts file, sizes the
# code collections from their info rather than exact counts (55.6 s for all
# of them, #39), and holds every limit to its own budget.

CODE = {"codebase_a": 10, "codebase_b": 40, "context_a": 99}


def _query(host: Path, **kw: object) -> subprocess.CompletedProcess[str]:
    return _run(host, "query", **kw)  # type: ignore[arg-type]


def _calls(host: Path) -> list[str]:
    return [c for c, _ in _qdrant_limits(host)]


def test_query_passes_on_hits_against_the_largest_code_collection(host: Path) -> None:
    done = _query(host, counts=CODE)
    assert done.returncode == 0, done.stderr
    assert "query codebase_b: 3 hits" in done.stdout


def test_query_sizes_by_points_count_never_an_exact_count(host: Path) -> None:
    done = _query(host, counts=CODE, points={"codebase_a": 50, "codebase_b": 40})
    assert done.returncode == 0, done.stderr
    assert "query codebase_a: 3 hits" in done.stdout
    assert "/points/count" not in (host / "curl.log").read_text()


def test_query_reads_only_the_code_collections(host: Path) -> None:
    done = _query(host, counts=CODE)
    assert done.returncode == 0, done.stderr
    assert _calls(host) == [
        "GET /collections",
        "GET /collections/codebase_a",
        "GET /collections/codebase_b",
        "POST /collections/codebase_b/points/query",
    ]


def test_query_searches_through_the_named_vector(host: Path) -> None:
    done = _query(host, counts=CODE, vectors={"dense": {"size": 2, "distance": "Cosine"}})
    assert done.returncode == 0, done.stderr
    (search,) = [c for c in (host / "curl.log").read_text().splitlines() if "/points/query" in c]
    assert '"using": "dense"' in search


def test_query_fails_when_it_finds_nothing(host: Path) -> None:
    done = _query(host, counts=CODE, hits=0)
    assert done.returncode != 0
    assert "query codebase_b: 0 hits" in done.stdout


def test_query_fails_with_no_code_collection(host: Path) -> None:
    done = _query(host, counts={"context_a": 3})
    assert done.returncode != 0
    assert "no codebase_ collection" in done.stderr


def test_query_takes_no_argument(host: Path) -> None:
    done = _run(host, "query", "BEFORE")
    assert done.returncode != 0
    assert "usage" in done.stderr


def test_query_sends_the_read_only_key_and_never_the_admin_key(host: Path) -> None:
    done = _query(host, counts=CODE)
    assert done.returncode == 0, done.stderr
    sent = (host / "curl.log.sent").read_text()
    assert f"api-key: {RO_KEY}" in sent
    assert KEY not in sent.replace(RO_KEY, "")
    assert RO_KEY not in (host / "curl.log").read_text()


def _unreadable(path: Path) -> None:
    path.chmod(0)
    if os.access(path, os.R_OK):
        pytest.skip("running as root: every file is readable")


def test_query_runs_without_the_admin_key(host: Path) -> None:
    """As the knob user, who can't read the root-only admin key."""
    _unreadable(host / "etc" / "socraticode" / "qdrant.key")
    done = _query(host, counts=CODE)
    assert done.returncode == 0, done.stderr


def test_query_without_the_read_only_key_names_its_group(host: Path) -> None:
    _unreadable(host / "etc" / "socraticode" / "qdrant-ro.key")
    done = _query(host, counts=CODE)
    assert done.returncode != 0
    assert "qdrant-ro.key" in done.stderr
    assert "group qdrant-ro" in done.stderr
    assert not (host / "curl.log").exists()


@pytest.mark.parametrize("stray", ["0", "300", "soon"])
def test_query_keeps_its_own_limits_whatever_the_environment_says(host: Path, stray: str) -> None:
    """Each Qdrant call at most 5 s, the embed 30 s: EMBED_T is check's, not the mode's.

    A recovery line has 60 s; a limit from the environment could spend it.
    """
    done = _query(host, counts=CODE, env={"EMBED_T": stray, "T": stray})
    assert done.returncode == 0, done.stderr
    assert {t for _, t in _qdrant_limits(host)} == {"5"}
    (embed,) = _embeds(host)
    assert "--max-time 30 " in embed


def test_query_tries_the_embed_once(host: Path) -> None:
    """A second try wouldn't fit in 60 s: the operator runs check, with its retries."""
    done = _query(host, counts=CODE, embed_rc=28)
    assert done.returncode != 0
    assert len(_embeds(host)) == 1
    assert "no answer in 30 s, 1 try:" in done.stderr
    assert "qdrant-recovery.sh check" in done.stderr
    assert "/points/query" not in (host / "curl.log").read_text()


def test_query_lists_and_sizes_within_10_s_all_told(host: Path) -> None:
    """The code collections grow with the cohort: their reads share one deadline.

    Each info call answers in 4 s. The first two get 5 s each. The third gets
    what's left of 10 s and times out, or, on a slow runner, never starts.
    Either way the mode fails by the deadline, with no embed.
    """
    counts = {"codebase_a": 1, "codebase_b": 2, "codebase_c": 3}
    delays = {f"/collections/{c}": 4 for c in counts}
    start = time.monotonic()
    done = _query(host, counts=counts, qdrant_delays=delays)
    assert time.monotonic() - start < 12
    assert done.returncode != 0
    limits = [int(t) for c, t in _qdrant_limits(host) if c.startswith("GET /collections/")]
    assert limits[:2] == [5, 5]
    assert all(t < 5 for t in limits[2:])
    assert _embeds(host) == []
