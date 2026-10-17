"""Behaviour tests for scripts/reembed.py: re-embed the store on an engine change (#11).

Every SocratiCode point carries the text it was embedded from. The script
rebuilds that text exactly, as SocratiCode 1.14.0 built it, and embeds it on
a given Ollama:

- **code:** ``<documentPrefix><relativePath>\\n<content>``;
- **artifact:** ``<documentPrefix>context:<name>:<path.normalize(path)>\\n<content>``,
  with Node's ``path.normalize``, which keeps a directory's trailing ``/``;
- without ``documentIncludesPath``, the prefix and content alone;
- each cut to ``contextLength`` UTF-16 code units, as JS ``substring`` cuts.

**``--dry-run``** writes nothing. Against the engine that built the store,
every point must come back at cosine 0.999999 or more: that is the gate,
since a near-miss of the text reads 0.999, not an error. ``--sample N``
checks each collection's first N points: a drift check against a candidate
engine. ``--gate-out`` records a full run's verdicts, and refuses a sample.

**``--apply --gate FILE``** overwrites the ``dense`` vector only, in a
collection the gate passed under the profile it still has. Payload and BM25
are untouched. A collection still ``in-progress`` is refused either way.

The Qdrant key arrives on stdin. Qdrant and Ollama are stub servers in
threads; nothing leaves the test.
"""

import copy
import hashlib
import importlib.util
import json
import math
import subprocess
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "reembed.py"
KEY = "qdrant-test-key"

PROFILE = {
    "schemaVersion": 1,
    "indexFormatVersion": 2,
    "kind": "code",
    "documentPrefix": "search_document: ",
    "documentIncludesPath": True,
    "embedding": {"provider": "ollama", "model": "nomic-embed-text", "contextLength": 2048},
}


def _load():
    spec = importlib.util.spec_from_file_location("reembed", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _vector(text: str, engine: str) -> list[float]:
    """A unit vector that is a pure function of the engine and the text."""
    raw = [b - 127.5 for b in hashlib.sha512(f"{engine}|{text}".encode()).digest()] * 12
    norm = math.sqrt(sum(x * x for x in raw))
    return [x / norm for x in raw]


def _code(i: int, path: str = "src/App.py", content: str = "def Main(): pass") -> dict:
    return {
        "id": f"00000000-0000-0000-0000-{i:012d}",
        "payload": {"relativePath": path, "content": f"{content} #{i}", "type": "code"},
    }


def _artifact(i: int, name: str = "plans", path: str = "./docs/plans/") -> dict:
    return {
        "id": f"00000000-0000-0000-0001-{i:012d}",
        "payload": {
            "artifactName": name,
            "relativePath": path,
            "content": f"## Plan {i}",
            "type": "artifact",
        },
    }


class Store:
    def __init__(self) -> None:
        self.collections: dict[str, list[dict]] = {}
        self.metadata: dict[str, dict] = {}
        self.writes: list[tuple[str, dict]] = []
        self.unauthorised = 0

    def add(self, name: str, points: list[dict], text, engine="old", profile=None, status=None):
        """``text`` is how the indexer built each point's text: right or wrong."""
        for p in points:
            p["vector"] = {"dense": _vector(text(p["payload"]), engine)}
        self.collections[name] = points
        meta = {"collectionName": name, "effectiveIndexProfile": json.dumps(profile or PROFILE)}
        if status:
            meta["indexingStatus"] = status
        self.metadata[name] = meta


def _right(payload: dict, profile=PROFILE) -> str:
    """The text as SocratiCode 1.14.0 built it, for this suite's payloads."""
    head = payload["relativePath"]
    if payload["type"] == "artifact":
        node = {"./docs/plans/": "docs/plans/", "docs/a.md": "docs/a.md"}[head]
        head = f"context:{payload['artifactName']}:{node}"
    return f"{profile['documentPrefix']}{head}\n{payload['content']}"


def _stub(handler_factory) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_factory)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass

    def _send(self, status: int, body: object) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")


@pytest.fixture
def env() -> Iterator[tuple[Store, dict, str, str]]:
    """The store, the engine's state, and the Qdrant and Ollama URLs."""
    store = Store()
    engine = {"name": "old", "version": "0.34.0", "requests": []}

    class Qdrant(_Handler):
        def _auth(self) -> bool:
            if self.headers.get("api-key") != KEY:
                store.unauthorised += 1
                self._send(401, {"status": "unauthorised"})
                return False
            return True

        def do_POST(self) -> None:
            if not self._auth():
                return
            name = self.path.split("/")[2]
            body = self._body()
            if name == "socraticode_metadata":
                points = [{"id": i, "payload": m} for i, m in enumerate(store.metadata.values())]
            else:
                points = sorted(store.collections[name], key=lambda p: p["id"])
            start = 0
            if body.get("offset") is not None:
                start = next(i for i, p in enumerate(points) if p["id"] == body["offset"])
            page = points[start : start + body["limit"]]
            rest = points[start + body["limit"] :]
            out = []
            for p in page:
                q = {"id": p["id"], "payload": copy.deepcopy(p["payload"])}
                if body.get("with_vector"):
                    assert body["with_vector"] == ["dense"]
                    q["vector"] = copy.deepcopy(p["vector"])
                out.append(q)
            next_offset = rest[0]["id"] if rest else None
            self._send(200, {"result": {"points": out, "next_page_offset": next_offset}})

        def do_PUT(self) -> None:
            if not self._auth():
                return
            name = self.path.split("/")[2]
            assert self.path.split("?")[0] == f"/collections/{name}/points/vectors"
            body = self._body()
            store.writes.append((name, body))
            by_id = {p["id"]: p for p in store.collections[name]}
            for p in body["points"]:
                by_id[p["id"]]["vector"].update(p["vector"])
            self._send(200, {"result": {"status": "completed"}})

    class Ollama(_Handler):
        def do_GET(self) -> None:
            self._send(200, {"version": engine["version"]})

        def do_POST(self) -> None:
            body = self._body()
            engine["requests"].append(body)
            vectors = [_vector(t, engine["name"]) for t in body["input"]]
            self._send(200, {"embeddings": vectors})

    q, qurl = _stub(Qdrant)
    o, ourl = _stub(Ollama)
    yield store, engine, qurl, ourl
    q.shutdown()
    o.shutdown()


def _run(qurl: str, ourl: str, *args: str, key: str = KEY) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", "-I", str(SCRIPT), "--qdrant", qurl, "--ollama", ourl, *args],
        input=f"{key}\n",
        capture_output=True,
        text=True,
        timeout=60,
    )


# The text rebuild, unit by unit.


@pytest.mark.parametrize(
    "given, node",
    [
        ("./docs/plans/", "docs/plans/"),
        ("docs/plans", "docs/plans"),
        ("docs//a/./b/../c", "docs/a/c"),
        ("", "."),
        ("./", "./"),
        ("a/..", "."),
        ("a/../", "./"),
        ("../x/", "../x/"),
        ("/abs/x/", "/abs/x/"),
        ("/..", "/"),
    ],
)
def test_normalize_is_nodes(given: str, node: str) -> None:
    assert _load().node_normalize(given) == node


def test_truncation_counts_utf16_code_units_as_js_does() -> None:
    m = _load()
    assert m.js_substring("abcdef", 4) == "abcd"
    assert m.js_substring("abc", 4) == "abc"
    # U+1F600 is two code units: three of them are six, and four units end
    # on the second's high surrogate, as JS's substring leaves it.
    assert m.js_substring("\U0001f600" * 3, 4) == "\U0001f600\U0001f600"
    assert m.js_substring("\U0001f600" * 3, 3) == "\U0001f600\ud83d"


def test_the_rebuilt_texts() -> None:
    m = _load()
    code = _code(1)["payload"]
    art = _artifact(1)["payload"]
    assert m.document_text(code, PROFILE) == "search_document: src/App.py\ndef Main(): pass #1"
    assert m.document_text(art, PROFILE) == "search_document: context:plans:docs/plans/\n## Plan 1"
    bare = {**PROFILE, "documentIncludesPath": False}
    assert m.document_text(code, bare) == "search_document: def Main(): pass #1"
    short = {**PROFILE, "embedding": {**PROFILE["embedding"], "contextLength": 20}}
    assert m.document_text(code, short) == "search_document: src"


# Dry run.


def test_the_engine_that_built_the_store_passes_and_nothing_is_written(env) -> None:
    store, engine, qurl, ourl = env
    store.add("codebase_a", [_code(i) for i in range(70)], _right)
    store.add("context_a", [_artifact(i) for i in range(5)], _right)
    result = _run(qurl, ourl, "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "codebase_a" in result.stdout and "context_a" in result.stdout
    assert "FAIL" not in result.stdout
    assert store.writes == []
    assert sum(len(r["input"]) for r in engine["requests"]) == 75, "every point, once"
    assert {r["model"] for r in engine["requests"]} == {"nomic-embed-text"}
    assert all(r["truncate"] is True for r in engine["requests"])


def test_a_text_rebuilt_wrong_fails_its_collection(env) -> None:
    """Python's normpath drops the trailing slash: the 2026-10-16 near-miss."""
    store, _, qurl, ourl = env
    store.add("codebase_a", [_code(i) for i in range(3)], _right)

    def dropped(payload: dict) -> str:
        return _right(payload).replace("docs/plans/", "docs/plans")

    store.add("context_a", [_artifact(i) for i in range(3)], dropped)
    result = _run(qurl, ourl, "--dry-run")
    assert result.returncode == 1
    [ok] = [ln for ln in result.stdout.splitlines() if ln.startswith("codebase_a")]
    [bad] = [ln for ln in result.stdout.splitlines() if ln.startswith("context_a")]
    assert "PASS" in ok and "FAIL" in bad


def test_a_sample_checks_the_first_n_of_each_collection(env) -> None:
    store, engine, qurl, ourl = env
    store.add("codebase_a", [_code(i) for i in range(50)], _right)
    store.add("codebase_b", [_code(i) for i in range(50)], _right)
    assert _run(qurl, ourl, "--dry-run", "--sample", "7").returncode == 0
    assert sum(len(r["input"]) for r in engine["requests"]) == 14


def test_a_candidate_engine_reads_as_drift(env) -> None:
    store, engine, qurl, ourl = env
    store.add("codebase_a", [_code(i) for i in range(5)], _right)
    engine["name"], engine["version"] = "new", "0.40.5"
    result = _run(qurl, ourl, "--dry-run", "--sample", "5")
    assert result.returncode == 1
    assert "FAIL" in result.stdout and "0.40.5" in result.stdout


def test_only_profiled_collections_are_read(env) -> None:
    """``*_symgraph_*`` hold one-dimension placeholders, and no profile."""
    store, _, qurl, ourl = env
    store.add("codebase_a", [_code(1)], _right)
    store.collections["a_symgraph_index"] = [{"id": "x", "payload": {}, "vector": [0.0]}]
    result = _run(qurl, ourl, "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "symgraph" not in result.stdout


def test_a_collection_still_indexing_is_refused(env) -> None:
    store, engine, qurl, ourl = env
    store.add("codebase_a", [_code(1)], _right, status="in-progress")
    store.add("codebase_b", [_code(1)], _right, status="completed")
    result = _run(qurl, ourl, "--dry-run")
    assert result.returncode == 1
    [line] = [ln for ln in result.stdout.splitlines() if ln.startswith("codebase_a")]
    assert "in-progress" in line
    assert sum(len(r["input"]) for r in engine["requests"]) == 1, "codebase_b only"


def test_the_key_comes_from_stdin(env) -> None:
    store, _, qurl, ourl = env
    store.add("codebase_a", [_code(1)], _right)
    assert _run(qurl, ourl, "--dry-run", key="wrong").returncode != 0
    assert store.unauthorised > 0
    empty = _run(qurl, ourl, "--dry-run", key="")
    assert empty.returncode == 64
    assert "stdin" in empty.stderr


# Gate and apply.


def _gate(qurl: str, ourl: str, path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return _run(qurl, ourl, "--dry-run", "--gate-out", str(path), *args)


def test_a_gate_is_never_a_sample(env, tmp_path: Path) -> None:
    store, _, qurl, ourl = env
    store.add("codebase_a", [_code(1)], _right)
    result = _gate(qurl, ourl, tmp_path / "gate.json", "--sample", "1")
    assert result.returncode == 64
    assert not (tmp_path / "gate.json").exists()


def test_apply_rewrites_dense_only_where_the_gate_passed(env, tmp_path: Path) -> None:
    store, engine, qurl, ourl = env
    store.add("codebase_a", [_code(i) for i in range(40)], _right)
    store.add("context_a", [_artifact(i) for i in range(3)], lambda p: "built some other way")
    gate = tmp_path / "gate.json"
    assert _gate(qurl, ourl, gate).returncode == 1, "context_a fails the gate"
    payloads = copy.deepcopy({n: [p["payload"] for p in c] for n, c in store.collections.items()})
    context_before = copy.deepcopy(store.collections["context_a"])

    engine["name"], engine["version"] = "new", "0.40.5"
    result = _run(qurl, ourl, "--apply", "--gate", str(gate))
    assert result.returncode == 1, "context_a is refused, so not all written"
    assert {name for name, _ in store.writes} == {"codebase_a"}
    for _, body in store.writes:
        assert all(set(p["vector"]) == {"dense"} for p in body["points"])
    for p in store.collections["codebase_a"]:
        assert p["vector"]["dense"] == _vector(_right(p["payload"]), "new")
    assert store.collections["context_a"] == context_before
    assert {n: [p["payload"] for p in c] for n, c in store.collections.items()} == payloads
    [refused] = [ln for ln in result.stdout.splitlines() if ln.startswith("context_a")]
    assert "gate" in refused


def test_apply_refuses_a_profile_changed_since_the_gate(env, tmp_path: Path) -> None:
    """A client on a newer SocratiCode may build its text another way."""
    store, _, qurl, ourl = env
    store.add("codebase_a", [_code(1)], _right)
    gate = tmp_path / "gate.json"
    assert _gate(qurl, ourl, gate).returncode == 0
    changed = {**PROFILE, "indexFormatVersion": 3}
    store.metadata["codebase_a"]["effectiveIndexProfile"] = json.dumps(changed)
    result = _run(qurl, ourl, "--apply", "--gate", str(gate))
    assert result.returncode == 1
    assert store.writes == []
    assert "profile" in result.stdout


def test_apply_needs_a_gate(env) -> None:
    _, _, qurl, ourl = env
    assert _run(qurl, ourl, "--apply").returncode == 64


def test_a_smaller_batch_keeps_searches_waiting_less(env) -> None:
    """Ollama serves one request at a time: a client's query waits behind
    whatever batch is running, so a long run takes small ones."""
    store, engine, qurl, ourl = env
    store.add("codebase_a", [_code(i) for i in range(12)], _right)
    assert _run(qurl, ourl, "--dry-run", "--batch", "5").returncode == 0
    assert [len(r["input"]) for r in engine["requests"]] == [5, 5, 2]
