"""Behaviour tests for deploy/link-check.py, the store-side link check (#38).

Every cohort repo's ``.socraticode.json`` may declare ``linkedProjects``:
relative paths (``../archiver``), not project IDs. A VM's ``codebase_search``
with ``includeLinked`` searches each sibling's ``codebase_<projectId>``, the
ID read from the sibling's own committed file. This checks, from the store,
that each declared link names a collection that exists and holds points.

  link-check.py check <qdrant-url> <key-file> <cohort.json> <cache.json>

- **Exit 0** clean. **Exit 1**, findings, one ``subject<TAB>message`` line
  each, in the list's order then the links': the check-in's ``links``
  findings, spliced into JSON it builds by hand, so neither part holds a
  quote, a backslash or a control character.
- **Exit 2**, unknown: Qdrant didn't answer. The check-in's own
  ``qdrant:collections`` is that finding. **Exit 3**, the check itself failed.
- **A finding:** a link whose target's ``projectId`` has no
  ``codebase_<id>``, or one with 0 points (counted exactly); a path whose
  last component names no listed repo; a link to a listed repo that commits
  no ``projectId`` (nothing can be confirmed for it); a ``.socraticode.json``
  that isn't JSON, or whose ``linkedProjects`` isn't a list of paths.
- **Not a finding:** a repo with no ``.socraticode.json``, or none in
  ``linkedProjects``.
- **The cache.** The check-in runs every 10 minutes and must not fetch from
  GitHub each time. Public repos' files come from raw.githubusercontent.com,
  anonymously, once the cache is 6 hours old. A refresh that fails is
  ``unknown``: the old cache stands, the next try waits an hour, and the
  refresh is all or nothing. A cache with no good fetch in 24 hours is the
  ``cache`` finding, as a stale ``otel:retention`` stamp is: a dead fetch is
  heard. Hand-listed private repos are never fetched.

Qdrant and GitHub are one stub HTTP server in a thread; nothing leaves the test.
"""

import json
import os
import stat
import subprocess
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "deploy" / "link-check.py"
KEY = "qdrant-test-key"


class World:
    """GitHub's files and Qdrant's collections, set per test."""

    def __init__(self) -> None:
        # repo -> (status, body)
        self.files: dict[str, tuple[int, str]] = {}
        # collection -> points
        self.collections: dict[str, int] = {}
        self.qdrant_status: int | None = None
        self.raw_status: int | None = None
        self.requests: list[tuple[str, str, str]] = []

    def config(self, repo: str, project_id: str | None = None, links: list[str] | None = None):
        body: dict = {}
        if project_id is not None:
            body["projectId"] = project_id
        if links is not None:
            body["linkedProjects"] = links
        self.files[repo] = (200, json.dumps(body))

    def fetched(self) -> list[str]:
        return [path for method, path, _ in self.requests if path.startswith("/raw/")]


@pytest.fixture
def world() -> Iterator[tuple[World, str]]:
    state = World()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def _send(self, status: int, body: str) -> None:
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _qdrant(self) -> bool:
            """False when the request is refused, having answered it."""
            if self.headers.get("api-key") != KEY:
                self._send(401, '{"status":{"error":"Must provide an API key"}}')
                return False
            if state.qdrant_status is not None:
                self._send(state.qdrant_status, "{}")
                return False
            return True

        def do_GET(self) -> None:
            state.requests.append(("GET", self.path, ""))
            if self.path.startswith("/raw/"):
                repo, ref, name = self.path.removeprefix("/raw/").split("/", 2)
                assert (ref, name) == ("HEAD", ".socraticode.json"), self.path
                assert "Authorization" not in self.headers, "fetched with a credential"
                if state.raw_status is not None:
                    self._send(state.raw_status, "")
                    return
                status, body = state.files.get(repo, (404, "404: Not Found"))
                self._send(status, body)
            elif self.path.startswith("/collections/"):
                if not self._qdrant():
                    return
                name = self.path.removeprefix("/collections/")
                if name in state.collections:
                    self._send(200, '{"result":{"status":"green"},"status":"ok"}')
                else:
                    self._send(404, '{"status":{"error":"Not found"}}')
            else:
                self._send(404, "{}")

        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
            state.requests.append(("POST", self.path, body))
            if not self._qdrant():
                return
            name = self.path.removeprefix("/collections/").removesuffix("/points/count")
            assert json.loads(body) == {"exact": True}, "an approximate count"
            if name not in state.collections:
                self._send(404, '{"status":{"error":"Not found"}}')
                return
            self._send(200, json.dumps({"result": {"count": state.collections[name]}}))

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    try:
        yield state, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


PUBLIC = ["notifier", "watcher", "status", "power-map"]


def _cohort(tmp_path: Path, base: str, hand: dict | None = None) -> Path:
    """notifier, watcher, status and power-map public; ``hand`` private."""
    repos: list[dict] = [{"repo": r, "tag": f"tag:{r}"} for r in PUBLIC]
    for repo, config in (hand or {}).items():
        repos.append(
            {
                "repo": repo,
                "tag": f"tag:{repo}",
                "private": True,
                "hand_listed": {"as_of": "2026-11-26", "source": "test", "config": config},
            }
        )
    path = tmp_path / "cohort.json"
    path.write_text(json.dumps({"raw": f"{base}/raw", "repos": repos}))
    return path


def _healthy(state: World) -> None:
    """notifier and watcher link each other, status links notifier,
    power-map declares none, and every collection holds points."""
    state.config("notifier", "notifier", ["../watcher"])
    state.config("watcher", "watcher", ["../notifier"])
    state.config("status", "status", ["../notifier"])
    state.config("power-map", "power-map")
    state.collections = {f"codebase_{r}": 10 for r in PUBLIC}


def _iso(hours_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run(
    tmp_path: Path, base: str, hand: dict | None = None, cohort: Path | None = None
) -> subprocess.CompletedProcess[str]:
    key = tmp_path / "qdrant.key"
    key.write_text(f"{KEY}\n")
    cohort = cohort or _cohort(tmp_path, base, hand)
    return subprocess.run(
        [str(SCRIPT), "check", base, str(key), str(cohort), str(tmp_path / "cache" / "links.json")],
        capture_output=True,
        text=True,
        env={k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")},
        timeout=60,
    )


def _cache(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "cache" / "links.json").read_text())


def _write_cache(tmp_path: Path, fetched: float | None, attempted: float, repos: dict) -> None:
    (tmp_path / "cache").mkdir(exist_ok=True)
    (tmp_path / "cache" / "links.json").write_text(
        json.dumps(
            {
                "fetched_at": None if fetched is None else _iso(fetched),
                "attempted_at": _iso(attempted),
                "repos": repos,
            }
        )
    )


def _lines(result: subprocess.CompletedProcess[str]) -> list[tuple[str, str]]:
    return [tuple(ln.split("\t")) for ln in result.stdout.splitlines()]  # type: ignore[misc]


# --- clean ------------------------------------------------------------------


def test_every_link_populated_is_clean_and_caches_what_it_fetched(
    world: tuple[World, str], tmp_path: Path
) -> None:
    state, base = world
    _healthy(state)
    result = _run(tmp_path, base)
    assert (result.returncode, result.stdout) == (0, ""), result.stderr
    cache = _cache(tmp_path)
    assert cache["fetched_at"] == cache["attempted_at"]
    fetched = datetime.strptime(cache["fetched_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    assert datetime.now(UTC) - fetched < timedelta(minutes=1)
    assert sorted(cache["repos"]) == sorted(PUBLIC)
    assert cache["repos"]["notifier"] == {"projectId": "notifier", "linkedProjects": ["../watcher"]}
    assert sorted(state.fetched()) == sorted(f"/raw/{r}/HEAD/.socraticode.json" for r in PUBLIC)


def test_the_cache_is_root_readable_only_by_its_mode(
    world: tuple[World, str], tmp_path: Path
) -> None:
    """It holds public repos' files, nothing secret: 0644, written whole."""
    state, base = world
    _healthy(state)
    assert _run(tmp_path, base).returncode == 0
    mode = stat.S_IMODE((tmp_path / "cache" / "links.json").stat().st_mode)
    assert mode == 0o644
    assert [p.name for p in (tmp_path / "cache").iterdir()] == ["links.json"]


def test_no_config_or_no_links_is_not_a_finding(world: tuple[World, str], tmp_path: Path) -> None:
    """provisioner today: no ``.socraticode.json``. power-map, address-validator,
    usa-wa and index: no ``linkedProjects`` key at all."""
    state, base = world
    _healthy(state)
    del state.files["power-map"]
    result = _run(tmp_path, base, hand={"provisioner": None})
    assert (result.returncode, result.stdout) == (0, ""), result.stderr
    assert _cache(tmp_path)["repos"]["power-map"] is None


def test_a_trailing_slash_answers_to_the_repo(world: tuple[World, str], tmp_path: Path) -> None:
    state, base = world
    _healthy(state)
    state.config("status", "status", ["../notifier/"])
    assert _run(tmp_path, base).returncode == 0


# --- findings ---------------------------------------------------------------


def test_a_link_to_a_missing_collection_is_a_finding(
    world: tuple[World, str], tmp_path: Path
) -> None:
    state, base = world
    _healthy(state)
    del state.collections["codebase_watcher"]
    result = _run(tmp_path, base)
    assert result.returncode == 1, result.stderr
    assert _lines(result) == [("notifier/watcher", "../watcher: no collection codebase_watcher")]


def test_a_link_to_an_empty_collection_is_a_finding(
    world: tuple[World, str], tmp_path: Path
) -> None:
    state, base = world
    _healthy(state)
    state.collections["codebase_notifier"] = 0
    result = _run(tmp_path, base)
    assert result.returncode == 1
    assert _lines(result) == [
        ("watcher/notifier", "../notifier: codebase_notifier holds no points"),
        ("status/notifier", "../notifier: codebase_notifier holds no points"),
    ], "each declaring repo, in the list's order"


def test_the_projectid_comes_from_the_siblings_file_not_its_name(
    world: tuple[World, str], tmp_path: Path
) -> None:
    """Each equals its repo name today. A sibling that renames its project
    moves its collection, and every link to it follows the new name."""
    state, base = world
    _healthy(state)
    state.config("watcher", "watch2", ["../notifier"])
    result = _run(tmp_path, base)
    assert _lines(result) == [("notifier/watcher", "../watcher: no collection codebase_watch2")]
    state.collections["codebase_watch2"] = 3
    tmp_cache = tmp_path / "cache" / "links.json"
    tmp_cache.unlink()
    assert _run(tmp_path, base).returncode == 0


def test_a_path_no_listed_repo_answers_to_is_a_finding(
    world: tuple[World, str], tmp_path: Path
) -> None:
    state, base = world
    _healthy(state)
    state.config("status", "status", ["../notifier", "../ghost"])
    result = _run(tmp_path, base)
    assert result.returncode == 1
    assert _lines(result) == [("status/ghost", "../ghost: no listed repo answers to it")]


@pytest.mark.parametrize("target_file", ["no projectId", "no file"])
def test_a_link_to_a_repo_with_no_projectid_is_a_finding(
    world: tuple[World, str], tmp_path: Path, target_file: str
) -> None:
    """Nothing can be confirmed for it: its collection has no known name."""
    state, base = world
    _healthy(state)
    if target_file == "no file":
        del state.files["watcher"]
    else:
        state.config("watcher", None, ["../notifier"])
    result = _run(tmp_path, base)
    assert result.returncode == 1
    assert _lines(result) == [("notifier/watcher", "../watcher: watcher commits no projectId")]


def test_a_projectid_that_is_no_collection_name_is_a_finding(
    world: tuple[World, str], tmp_path: Path
) -> None:
    """It goes into Qdrant's URL: a ``/`` or ``..`` would ask for another path."""
    state, base = world
    _healthy(state)
    state.config("watcher", "../../x", ["../notifier"])
    result = _run(tmp_path, base)
    assert _lines(result) == [
        ("notifier/watcher", "../watcher: watcher's projectId is not a collection name")
    ]
    assert not [p for _, p, _ in state.requests if ".." in p.removeprefix("/raw/")]


@pytest.mark.parametrize(
    "body",
    ["{not json", '{"projectId":"status","linkedProjects":"../notifier"}', '["status"]'],
    ids=["not JSON", "links not a list", "not an object"],
)
def test_an_unreadable_config_is_a_finding_on_its_repo(
    world: tuple[World, str], tmp_path: Path, body: str
) -> None:
    state, base = world
    _healthy(state)
    state.files["status"] = (200, body)
    result = _run(tmp_path, base)
    assert result.returncode == 1
    [(subject, message)] = _lines(result)
    assert subject == "status"
    assert message.startswith(".socraticode.json ")


def test_findings_hold_no_character_that_breaks_hand_built_json(
    world: tuple[World, str], tmp_path: Path
) -> None:
    state, base = world
    _healthy(state)
    state.config("status", "status", ['../no"pe\\\x07'])
    result = _run(tmp_path, base)
    assert result.returncode == 1
    [(subject, message)] = _lines(result)
    for part in (subject, message):
        assert not any(c in part for c in '"\\') and part.isprintable(), part
        json.loads(f'"{part}"')


# --- hand-listed ------------------------------------------------------------


def test_a_hand_listed_repo_is_checked_and_never_fetched(
    world: tuple[World, str], tmp_path: Path
) -> None:
    """observo: private, its two links listed by hand."""
    state, base = world
    _healthy(state)
    del state.collections["codebase_power-map"]
    hand = {"observo": {"projectId": "observo", "linkedProjects": ["../notifier", "../power-map"]}}
    result = _run(tmp_path, base, hand=hand)
    assert _lines(result) == [
        ("observo/power-map", "../power-map: no collection codebase_power-map")
    ]
    assert "/raw/observo/HEAD/.socraticode.json" not in state.fetched()
    assert "observo" not in _cache(tmp_path)["repos"]


def test_a_link_to_a_hand_listed_repo_reads_its_listed_projectid(
    world: tuple[World, str], tmp_path: Path
) -> None:
    state, base = world
    _healthy(state)
    state.config("status", "status", ["../observo"])
    state.collections["codebase_obs"] = 5
    hand = {"observo": {"projectId": "obs"}}
    assert _run(tmp_path, base, hand=hand).returncode == 0
    assert ("POST", "/collections/codebase_obs/points/count", '{"exact": true}') in state.requests


# --- the cache --------------------------------------------------------------


def test_a_fresh_cache_is_read_and_github_is_not(world: tuple[World, str], tmp_path: Path) -> None:
    """The host step's plant: a cache entry naming a collection that doesn't
    exist arrives as a finding, without touching the store or GitHub."""
    state, base = world
    _healthy(state)
    repos = {
        "notifier": {"projectId": "notifier", "linkedProjects": ["../watcher"]},
        "watcher": {"projectId": "nonexistent"},
        "status": None,
        "power-map": None,
    }
    _write_cache(tmp_path, fetched=5.9, attempted=5.9, repos=repos)
    result = _run(tmp_path, base)
    assert state.fetched() == []
    assert _lines(result) == [
        ("notifier/watcher", "../watcher: no collection codebase_nonexistent")
    ]
    assert _cache(tmp_path)["repos"] == repos, "a read rewrote it"


def test_a_cache_six_hours_old_is_refreshed(world: tuple[World, str], tmp_path: Path) -> None:
    state, base = world
    _healthy(state)
    _write_cache(tmp_path, fetched=6.1, attempted=6.1, repos={"watcher": {"projectId": "gone"}})
    assert _run(tmp_path, base).returncode == 0
    assert len(state.fetched()) == len(PUBLIC)
    assert _cache(tmp_path)["repos"]["watcher"]["projectId"] == "watcher"


@pytest.mark.parametrize("status", [500, 429, 403])
def test_a_failed_refresh_keeps_the_old_cache_and_is_no_finding(
    world: tuple[World, str], tmp_path: Path, status: int
) -> None:
    state, base = world
    _healthy(state)
    old = {r: json.loads(state.files[r][1]) for r in PUBLIC}
    _write_cache(tmp_path, fetched=7, attempted=7, repos=old)
    before = _cache(tmp_path)
    state.config("watcher", "renamed", ["../notifier"])  # would be a finding if read
    state.raw_status = status
    result = _run(tmp_path, base)
    assert (result.returncode, result.stdout) == (0, ""), result.stderr
    after = _cache(tmp_path)
    assert (after["fetched_at"], after["repos"]) == (before["fetched_at"], before["repos"])
    assert after["attempted_at"] > before["attempted_at"]


def test_a_refresh_is_all_or_nothing(world: tuple[World, str], tmp_path: Path) -> None:
    """One repo answering 500 keeps every repo's old entry: a half-new cache
    would stamp a fetch time on files it never read."""
    state, base = world
    _healthy(state)
    _write_cache(tmp_path, fetched=7, attempted=7, repos={"watcher": {"projectId": "watcher"}})
    state.files["status"] = (500, "")
    assert _run(tmp_path, base).returncode == 0
    assert _cache(tmp_path)["repos"] == {"watcher": {"projectId": "watcher"}}


def test_a_failed_refresh_waits_an_hour_to_try_again(
    world: tuple[World, str], tmp_path: Path
) -> None:
    state, base = world
    _healthy(state)
    _write_cache(tmp_path, fetched=8, attempted=0.9, repos={})
    _run(tmp_path, base)
    assert state.fetched() == []
    _write_cache(tmp_path, fetched=8, attempted=1.1, repos={})
    _run(tmp_path, base)
    assert len(state.fetched()) == len(PUBLIC)


def test_a_cache_a_day_old_is_a_finding_and_still_checked(
    world: tuple[World, str], tmp_path: Path
) -> None:
    state, base = world
    _healthy(state)
    state.raw_status = 503
    _write_cache(
        tmp_path, fetched=25, attempted=2, repos={"notifier": {"linkedProjects": ["../ghost"]}}
    )
    result = _run(tmp_path, base)
    assert result.returncode == 1
    assert _lines(result) == [
        ("cache", "no good fetch from GitHub in 25 h"),
        ("notifier/ghost", "../ghost: no listed repo answers to it"),
    ]


@pytest.mark.parametrize("cache", [None, "{torn"], ids=["absent", "unreadable"])
def test_no_cache_and_a_failed_fetch_is_the_cache_finding_alone(
    world: tuple[World, str], tmp_path: Path, cache: str | None
) -> None:
    state, base = world
    _healthy(state)
    state.raw_status = 500
    if cache is not None:
        (tmp_path / "cache").mkdir()
        (tmp_path / "cache" / "links.json").write_text(cache)
    result = _run(tmp_path, base)
    assert result.returncode == 1
    assert _lines(result) == [("cache", "no good fetch from GitHub yet")]
    assert _cache(tmp_path)["fetched_at"] is None


def test_an_unreachable_github_is_a_failed_refresh(
    world: tuple[World, str], tmp_path: Path
) -> None:
    """Nothing listens on the raw base: urllib raises, and that's unknown, not a crash."""
    _, base = world
    with ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler) as dead:
        port = dead.server_port
    _write_cache(tmp_path, fetched=7, attempted=7, repos={"status": None})
    before = _cache(tmp_path)["fetched_at"]
    cohort = _cohort(tmp_path, f"http://127.0.0.1:{port}")
    result = _run(tmp_path, base, cohort=cohort)
    assert (result.returncode, result.stdout) == (0, ""), result.stderr
    assert _cache(tmp_path)["fetched_at"] == before


# --- Qdrant -----------------------------------------------------------------


@pytest.mark.parametrize("status", [500, 503, 401])
def test_qdrant_not_answering_is_unknown(
    world: tuple[World, str], tmp_path: Path, status: int
) -> None:
    """The check-in runs this only after its authenticated read returned 200,
    so a store down now is a blip, and its own finding next tick."""
    state, base = world
    _healthy(state)
    state.qdrant_status = status
    result = _run(tmp_path, base)
    assert (result.returncode, result.stdout) == (2, "")


def test_the_key_is_read_from_its_file_and_sent_as_a_header(
    world: tuple[World, str], tmp_path: Path
) -> None:
    state, base = world
    _healthy(state)
    result = _run(tmp_path, base)
    assert result.returncode == 0, "the stub refuses a request without the key"
    assert any(m == "POST" for m, _, _ in state.requests)


def test_each_collection_is_counted_once(world: tuple[World, str], tmp_path: Path) -> None:
    state, base = world
    _healthy(state)
    _run(tmp_path, base)
    counts = [p for m, p, _ in state.requests if m == "POST"]
    assert sorted(counts) == sorted(set(counts))


# --- the check itself -------------------------------------------------------


def test_a_missing_cohort_list_exits_3_naming_it(world: tuple[World, str], tmp_path: Path) -> None:
    _, base = world
    result = _run(tmp_path, base, cohort=tmp_path / "absent.json")
    assert result.returncode == 3
    assert "absent.json" in result.stderr
    assert result.stdout == ""


def test_usage_is_exit_3(tmp_path: Path) -> None:
    result = subprocess.run([str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 3
    assert "usage" in result.stderr
