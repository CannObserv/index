#!/usr/bin/python3 -I
"""#38: the store-side link check, for the check-in.

Each cohort repo's committed .socraticode.json may declare linkedProjects:
relative paths (../archiver). A VM's search across them reads each
sibling's codebase_<projectId>, the ID from the sibling's own file. This
confirms, from the store, that every declared link names a collection that
exists and holds points.

  link-check.py check <qdrant-url> <key-file> <cohort.json> <cache.json>

Exit 0 clean; 1 with one "subject<TAB>message" line per finding, in the
cohort list's order then each repo's links' (the check-in's `links`
findings, spliced into hand-built JSON, so neither part holds a quote, a
backslash or a control character); 2 unknown, when Qdrant didn't answer
(the check-in's qdrant:collections is that finding); 3 when the check
itself failed, a refused key or a failed TLS check among it: the check-in
read the same URL with the same key a moment before.

Public repos' files come from raw.githubusercontent.com, anonymously, every
REFRESH_H hours (the check-in runs every 10 minutes), or at once when the
cache doesn't hold the list's public repos or base. A refresh
that fails is unknown: the old cache stands, all or nothing, and the next
try waits RETRY_H. No good fetch in STALE_H is the `cache` finding.
Private repos are hand-listed in cohort.json (D18: no token read) and never
fetched. Stdlib only.
"""

import contextlib
import http.client
import json
import os
import posixpath
import re
import signal
import ssl
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta

REFRESH_H = 6
RETRY_H = 1
STALE_H = 24
RAW_TIMEOUT = 10
QDRANT_TIMEOUT = 5
MAX_BYTES = 64 * 1024
# urllib's timeouts are per socket call, and a name lookup has none: these
# bound each phase whole, and expire as unknown. The check-in's `timeout`
# outlasts both, and is a broken check (test_link_check.py).
REFRESH_BUDGET = 40
QDRANT_BUDGET = 30
STAMP = "%Y-%m-%dT%H:%M:%SZ"
# A projectId goes into Qdrant's URL: no `/`, and no leading dot for `..`.
COLLECTION_ID = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]*")


NOT_FETCHED = object()


class Unknown(Exception):
    """Qdrant didn't answer: no finding."""


class Broken(Exception):
    """The check's own fault, not Qdrant's: exit 3, the check-in's links:check."""


class RefreshFailed(Exception):
    """GitHub didn't answer as a file or a 404: keep the old cache."""


@contextlib.contextmanager
def _budget(seconds: float, expired: type[Exception]):
    """Raise ``expired`` in this, the main thread, after ``seconds``."""

    def expire(signum: int, frame: object) -> None:
        raise expired(f"over its {seconds} s budget")

    previous = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _subject(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._/-]", "?", text)


def _message(text: str) -> str:
    return "".join("?" if c in '"\\' or not c.isprintable() else c for c in text)


def _parse(stamp: object, now: datetime) -> datetime | None:
    """A stamp, or None when unreadable or ahead of the clock: one that ran
    ahead and was set back would freeze the cache and hide its staleness."""
    try:
        when = datetime.strptime(str(stamp), STAMP).replace(tzinfo=UTC)
    except ValueError:
        return None
    return None if when > now + timedelta(minutes=5) else when


def _read_cache(path: str) -> dict | None:
    try:
        with open(path) as f:
            cache = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(cache, dict) or not isinstance(cache.get("repos"), dict):
        return None
    return cache


def _write_cache(path: str, cache: dict) -> None:
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, mode=0o755, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".links.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(cache, f, indent=1, sort_keys=True)
            f.write("\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def _fetch(raw: str, repo: str) -> dict | None:
    """A repo's committed config: what the check reads of it, None for no
    file, or {"invalid": why}."""
    req = urllib.request.Request(
        f"{raw}/{repo}/HEAD/.socraticode.json", headers={"User-Agent": "co-index-link-check"}
    )
    try:
        with urllib.request.urlopen(req, timeout=RAW_TIMEOUT) as r:
            body = r.read(MAX_BYTES + 1)
            # A read stops short at a closed connection without raising: what
            # it left unread is a broken fetch, never a file that isn't JSON.
            if len(body) <= MAX_BYTES and r.length:
                raise RefreshFailed(f"{repo}: body cut short, {r.length} bytes unread")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise RefreshFailed(f"{repo}: HTTP {e.code}") from e
    # HTTPException (IncompleteRead, BadStatusLine) is no OSError.
    except (OSError, ValueError, http.client.HTTPException) as e:
        raise RefreshFailed(f"{repo}: {type(e).__name__}: {e}") from e
    if len(body) > MAX_BYTES:
        return {"invalid": "is over 64 KiB"}
    try:
        config = json.loads(body)
    except ValueError:
        return {"invalid": "is not JSON"}
    if not isinstance(config, dict):
        return {"invalid": "is not a JSON object"}
    return {k: config[k] for k in ("projectId", "linkedProjects") if k in config}


def _refresh(cohort: dict, cache: dict | None, now: datetime, path: str) -> dict:
    """The cache to check against, fetched anew when it's due."""
    public = {e["repo"] for e in cohort["repos"] if not e.get("private")}
    if cache is not None:
        fetched = _parse(cache.get("fetched_at"), now)
        attempted = _parse(cache.get("attempted_at"), now)
        # A cache that isn't the list's -- a repo added or made public, or
        # another base -- is due however fresh, or it reads that repo as one
        # with no file for hours.
        matches = set(cache["repos"]) == public and cache.get("raw") == cohort["raw"]
        if matches and fetched is not None and now - fetched < timedelta(hours=REFRESH_H):
            return cache
        # The retry gate waits out a failed attempt only: after a good one,
        # a mismatch is refetched at once (CR 11).
        failed = attempted is not None and (fetched is None or attempted > fetched)
        if failed and now - attempted < timedelta(hours=RETRY_H):
            return cache
    stamp = now.strftime(STAMP)
    try:
        with _budget(REFRESH_BUDGET, RefreshFailed):
            repos = {
                e["repo"]: _fetch(cohort["raw"], e["repo"])
                for e in cohort["repos"]
                if not e.get("private")
            }
        cache = {"fetched_at": stamp, "attempted_at": stamp, "raw": cohort["raw"], "repos": repos}
    except RefreshFailed as e:
        print(f"link-check: refresh failed, cache kept: {e}", file=sys.stderr)
        old = cache or {}
        cache = {
            "fetched_at": old.get("fetched_at"),
            "attempted_at": stamp,
            "raw": old.get("raw"),
            "repos": old.get("repos", {}),
        }
    _write_cache(path, cache)
    return cache


class Store:
    """Qdrant's exact point counts, each collection asked once."""

    def __init__(self, url: str, key: str) -> None:
        self.url, self.key, self.counts = url.rstrip("/"), key, {}

    def count(self, collection: str) -> int | None:
        """Its points, or None when there is no such collection."""
        if collection not in self.counts:
            req = urllib.request.Request(
                f"{self.url}/collections/{collection}/points/count",
                data=b'{"exact": true}',
                headers={"api-key": self.key, "Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=QDRANT_TIMEOUT) as r:
                    self.counts[collection] = int(json.load(r)["result"]["count"])
            # The check-in's own read took this URL and key a moment before.
            # So a refused key or a failed TLS check is no blip, and unknown
            # would stay silent for good: both are the check broken.
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    raise Broken(f"{collection}: HTTP {e.code}, the key refused") from e
                if e.code != 404:
                    raise Unknown(f"{collection}: HTTP {e.code}") from e
                self.counts[collection] = None
            except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException) as e:
                reason = getattr(e, "reason", e)
                if isinstance(reason, ssl.SSLError):
                    raise Broken(f"{collection}: {reason!r}") from e
                raise Unknown(f"{collection}: {e}") from e
        return self.counts[collection]


def _findings(cohort: dict, cache: dict, now: datetime, store: Store) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    fetched = _parse(cache.get("fetched_at"), now)
    if fetched is None:
        return [("cache", "no good fetch from GitHub yet")]
    hours = int((now - fetched).total_seconds() // 3600)
    if hours >= STALE_H:
        found.append(("cache", f"no good fetch from GitHub in {hours} h"))

    # A public repo the cache doesn't hold yet is unknown: its refresh is due
    # and failed. Its links, and every link into it, wait for one.
    configs = {
        e["repo"]: e["hand_listed"]["config"]
        if e.get("private")
        else cache["repos"].get(e["repo"], NOT_FETCHED)
        for e in cohort["repos"]
    }

    def readable(config: object) -> bool:
        return isinstance(config, dict) and "invalid" not in config

    for repo, config in configs.items():
        if config is None or config is NOT_FETCHED:
            continue
        if not readable(config):
            why = config.get("invalid") if isinstance(config, dict) else None
            found.append((repo, f".socraticode.json {why or 'is not a JSON object'}"))
            continue
        links = config.get("linkedProjects", [])
        if not isinstance(links, list) or not all(isinstance(p, str) for p in links):
            found.append((repo, ".socraticode.json linkedProjects is not a list of paths"))
            continue
        for path in links:
            name = posixpath.basename(path.rstrip("/"))
            subject = f"{repo}/{name}"
            if name not in configs:
                found.append((subject, f"{path}: no listed repo answers to it"))
                continue
            target = configs[name]
            if target is NOT_FETCHED:
                continue
            pid = target.get("projectId") if readable(target) else None
            if not isinstance(pid, str) or not pid:
                found.append((subject, f"{path}: {name} commits no projectId"))
            elif not COLLECTION_ID.fullmatch(pid):
                found.append((subject, f"{path}: {name}'s projectId is not a collection name"))
            else:
                collection = f"codebase_{pid}"
                points = store.count(collection)
                if points is None:
                    found.append((subject, f"{path}: no collection {collection}"))
                elif points == 0:
                    found.append((subject, f"{path}: {collection} holds no points"))
    return [(_subject(s), _message(m)) for s, m in found]


def main(argv: list[str]) -> int:
    if len(argv) != 6 or argv[1] != "check":
        print(
            "usage: link-check.py check <qdrant-url> <key-file> <cohort.json> <cache.json>",
            file=sys.stderr,
        )
        return 3
    _, _, url, key_file, cohort_file, cache_file = argv
    try:
        with open(key_file) as f:
            key = f.read().strip()
        with open(cohort_file) as f:
            cohort = json.load(f)
        now = datetime.now(UTC)
        cache = _refresh(cohort, _read_cache(cache_file), now, cache_file)
        with _budget(QDRANT_BUDGET, Unknown):
            found = _findings(cohort, cache, now, Store(url, key))
    except Unknown as e:
        print(f"link-check: unknown, Qdrant didn't answer: {e}", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001 -- any other failure is the check's own
        print(f"link-check: {type(e).__name__}: {e}", file=sys.stderr)
        return 3
    for subject, message in found:
        print(f"{subject}\t{message}")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
