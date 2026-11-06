"""SocratiCode's client config for co-index's shared store holds (#18, D20).

This repo is the store's own client too: co-index indexes ``projectId: index``
and nothing else (D11, D20). Adapted from CannObserv/processor's
``tests/test_socraticode_config.py`` (itself broker's): no linked siblings, this
repo's ignore list, and the patching knob kept searchable as a context artifact.
Nothing here reaches the store; the host-local checks skip loudly where their
file is absent rather than pass vacuously.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import pytest

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / ".socraticode.json"
MANIFEST = ROOT / ".socraticodecontextartifacts.json"
INDEX_IGNORE = ROOT / ".socraticodeignore"
SETTINGS = ROOT / ".claude" / "settings.json"
SETTINGS_LOCAL = ROOT / ".claude" / "settings.local.json"

#: Where a file on this host puts a variable in the MCP server's environment: the env
#: file a shell here sources (D18's tokens), and the three settings scopes Claude Code
#: merges into a session (the plugin's server inherits the session's environment).
ENV_SOURCES = {
    "repo-env": ROOT / ".env",
    "project-settings": SETTINGS,
    "local-settings": SETTINGS_LOCAL,
    "user-settings": Path.home() / ".claude" / "settings.json",
}

#: Each splits or redirects the namespace with every health check green:
#: the prefix is prepended to the store-wide ``socraticode_metadata`` too; branch
#: awareness indexes a fresh set per branch; the project-id override outranks
#: ``.socraticode.json`` and is for one removal, never persisted; and a URL built from
#: ``QDRANT_HOST`` defaults to port 16333, which reads as a network fault.
GUARDED = [
    "QDRANT_COLLECTION_PREFIX",
    "SOCRATICODE_BRANCH_AWARE",
    "SOCRATICODE_PROJECT_ID",
    "QDRANT_HOST",
]

#: The store on co-index, as every other client spells it. A collection holds vectors
#: from one model at one dimension: a client that differs poisons what siblings read.
CLIENT_ENV = {
    "QDRANT_MODE": "external",
    "QDRANT_URL": "https://index.taild0fb76.ts.net:6333",
    "OLLAMA_MODE": "external",
    "OLLAMA_URL": "http://index:11434",
    "EMBEDDING_MODEL": "nomic-embed-text",
    "EMBEDDING_DIMENSIONS": "768",
}

#: The newest version any cohort client writes with (``builtByVersion`` in the store's
#: metadata, 2026-11-06: 1.14.0 to 1.16.1; 1.16.2 was a day old and wrote nothing).
#: Pin forward, never back: a client older than a collection's ``indexFormatVersion``
#: reads zero rows from it without an error (notifier#72). Moving it is a decision;
#: re-pin the driver, and VS Code's ``claudeCode.environmentVariables``, with it.
SOCRATICODE_SPEC = "socraticode@1.16.1"
_EXACT_SPEC = re.compile(r"^socraticode@(\d+\.\d+\.\d+)$")

#: The driver's pre-install (the health hook, index, status, verify), off-repo.
DRIVER_PIN = Path.home() / ".socraticode" / "pin" / "node_modules" / "socraticode"

#: Vendored skills and their installed tooling; nothing in ``skills/`` is first-party,
#: so ``skills/`` goes whole. ``.skills/`` holds the doctor (a vendored copy) and the
#: patching knob, which is first-party: it stays searchable as a context artifact.
VENDORED = {"skills-vendor/", "skills/", ".claude/skills/", ".skills/"}
KNOB = "./.skills/patching-hosts"

#: using-git-worktrees' root and the harness's, both inside this tree.
NESTED_CHECKOUTS = {".worktrees/", ".claude/worktrees/"}


#: Each SessionStart hook and the dedupe marker its entry carries; distinct per hook
#: so one hook's strip cannot evict the other's entry.
HOOKS = {
    "socraticode-reminder.sh": "socraticode-prefetch",
    "socraticode-health.sh": "socraticode-health",
}

#: Upstream's ``assertValidProjectId``: it throws on anything else rather than sanitize.
PROJECT_ID_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")

_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def _read_if_present(path: Path) -> str | None:
    """Only ``FileNotFoundError`` means absent: a ``PermissionError`` propagates."""
    try:
        return path.read_text()
    except FileNotFoundError:
        return None


def _variables(path: Path, text: str) -> dict[str, str]:
    """What the file puts in the server's environment: a settings file's ``env``
    block, or an env file's assignments (so a comment naming a variable is no finding).
    """
    if path.suffix == ".json":
        return {k: str(v) for k, v in json.loads(text).get("env", {}).items()}
    return {
        m[1]: m[2].strip().strip("'\"") for ln in text.splitlines() if (m := _ASSIGNMENT.match(ln))
    }


def _index_ignore_entries() -> set[str]:
    lines = (ln.strip() for ln in INDEX_IGNORE.read_text().splitlines())
    return {ln for ln in lines if ln and not ln.startswith("#")}


@pytest.fixture(scope="module")
def config() -> dict:
    return json.loads(CONFIG.read_text())


@pytest.fixture(scope="module")
def client_env() -> dict[str, str]:
    return _variables(SETTINGS, SETTINGS.read_text())


# ── Guards ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", list(ENV_SOURCES.values()), ids=list(ENV_SOURCES))
@pytest.mark.parametrize("variable", GUARDED)
def test_guarded_variables_are_set_nowhere(variable: str, path: Path) -> None:
    text = _read_if_present(path)
    if text is None:
        pytest.skip(f"{path} not present on this machine")
    assert variable not in _variables(path, text), f"{path} sets {variable}"


def test_the_shared_store_is_never_addressed_without_this_project_id() -> None:
    """The store's address and processor's name ship together, or not at all.

    Without ``.socraticode.json`` the id is ``sha256(abs_path)[:12]``, and exe.dev VMs
    check repos out at the same path: two hosts, one collection, under a host-local
    lock. A literal, not ``ROOT.name``: in a worktree that is the worktree's directory.
    """
    addressing = []
    for label, path in ENV_SOURCES.items():
        text = _read_if_present(path)
        if text is None:
            continue
        env = _variables(path, text)
        if env.get("QDRANT_MODE") == "external" or "QDRANT_URL" in env:
            addressing.append(label)
    if not addressing:
        return
    assert CONFIG.exists(), f"{addressing} address the shared store, but {CONFIG.name} is missing"
    assert json.loads(CONFIG.read_text()).get("projectId") == "index"


def test_local_settings_are_ignored_by_the_tracked_gitignore() -> None:
    """The file holding the cohort's one Qdrant key: a leak is a rotation everywhere.

    By the source ``check-ignore -v`` names, since a global excludes file protects one
    machine only; a tracked file is never reported ignored, so a committed key fails too.
    """
    if not shutil.which("git"):
        pytest.skip("git not installed")
    target = str(SETTINGS_LOCAL.relative_to(ROOT))
    result = subprocess.run(
        ["git", "check-ignore", "-v", target], cwd=ROOT, capture_output=True, text=True
    )
    assert result.returncode == 0, f"{target} is not ignored, or is tracked: {result.stderr}"
    source = result.stdout.split(":", 1)[0]
    assert source == ".gitignore", f"{target} is ignored by {source}, not the tracked .gitignore"


def test_the_committed_settings_hold_no_qdrant_key(client_env: dict[str, str]) -> None:
    assert "QDRANT_API_KEY" not in client_env


# ── The config ──────────────────────────────────────────────────────────────────


def test_project_id_is_index(config: dict) -> None:
    """``codebase_index`` in the shared store. A malformed file is ignored by
    upstream, not reported, so parsing it here is half the point."""
    assert config["projectId"] == "index"
    assert set(config["projectId"]) <= PROJECT_ID_CHARS


def test_no_linked_projects(config: dict) -> None:
    """This repo reads no sibling's code. D20 admits a sibling here only as a link
    stub, and an index run pointed at one would write that sibling's collections."""
    assert config == {"projectId": "index"}


def test_the_client_env_is_the_cohort_store(client_env: dict[str, str]) -> None:
    assert {k: client_env.get(k) for k in CLIENT_ENV} == CLIENT_ENV


def test_qdrant_is_addressed_by_a_full_https_url(client_env: dict[str, str]) -> None:
    """The MagicDNS FQDN the certificate names; https, or upstream refuses to send the
    key; the port spelled out; no path, which upstream's client drops."""
    url = urlparse(client_env["QDRANT_URL"])
    assert url.scheme == "https"
    assert url.port == 6333
    assert url.hostname is not None and url.hostname.endswith(".ts.net")
    assert url.hostname.count(".") >= 2, f"{url.hostname} is not the full MagicDNS name"
    assert url.path in ("", "/")


def test_the_session_launch_is_pinned(client_env: dict[str, str]) -> None:
    """Unset, the plugin launches ``npx -y --prefer-online socraticode@latest``: an
    install at session start, the 1.2 G peak behind broker's 2026-09-16 outage."""
    assert client_env.get("SOCRATICODE_SPEC") == SOCRATICODE_SPEC


def test_the_session_and_driver_pins_agree(client_env: dict[str, str]) -> None:
    """Two builds writing one store is what a half re-pin makes. VM-local."""
    text = _read_if_present(DRIVER_PIN / "package.json")
    if text is None:
        pytest.skip(f"no driver pin at {DRIVER_PIN}")
    match = _EXACT_SPEC.match(client_env.get("SOCRATICODE_SPEC", ""))
    assert match, "SOCRATICODE_SPEC is not an exact pin"
    assert json.loads(text)["version"] == match[1]


#: Where the VS Code extension's launch actually reads the pin: the settings ``env``
#: block reached the server's environment but not its launch on three cohort hosts
#: (gregoryfoster/skills#332). Machine-scoped, never a committed workspace setting.
VSCODE_MACHINE = Path.home() / ".vscode-server" / "data" / "Machine" / "settings.json"


def test_vscode_launches_the_same_pin() -> None:
    """Host-local: skips where this host runs no VS Code server."""
    text = _read_if_present(VSCODE_MACHINE)
    if text is None:
        pytest.skip(f"no {VSCODE_MACHINE}")
    entries = json.loads(text).get("claudeCode.environmentVariables", [])
    specs = [e["value"] for e in entries if e.get("name") == "SOCRATICODE_SPEC"]
    assert specs == [SOCRATICODE_SPEC]


# ── Index scope ─────────────────────────────────────────────────────────────────


def test_the_index_excludes_the_vendored_skills_and_nested_checkouts() -> None:
    """Unexcluded, vendored prose outnumbers this repo's own files, and every
    sibling's ``includeLinked`` search would serve it as this repo's."""
    assert VENDORED | NESTED_CHECKOUTS <= _index_ignore_entries()


def test_every_excluded_skill_is_vendored_or_an_upstream_override() -> None:
    """``skills/`` goes whole only because nothing in it is this repo's own.

    A first-party skill would drop out of the index without a word: narrow the
    ``skills/`` entry before adding one.
    """
    for entry in sorted((ROOT / "skills").iterdir()):
        if entry.is_symlink():
            assert os.readlink(entry).startswith("../skills-vendor/"), entry
        else:
            assert "overrides:" in (entry / "SKILL.md").read_text(), f"{entry} is first-party"
    for entry in sorted((ROOT / ".claude" / "skills").iterdir()):
        assert entry.is_symlink() and os.readlink(entry).startswith("../../skills/"), entry


def test_the_index_excludes_exactly_the_issues_list() -> None:
    """#18's list, no more: the design record stays in the code index."""
    assert _index_ignore_entries() == VENDORED | NESTED_CHECKOUTS


def test_the_knob_left_the_code_index_for_the_context_store() -> None:
    """``.skills/`` goes whole, and the knob is first-party: excluded from one store
    only because the other still answers from it."""
    paths = {a["path"] for a in json.loads(MANIFEST.read_text())["artifacts"]}
    assert KNOB in paths


def test_the_manifest_is_an_object_whose_paths_resolve() -> None:
    """A rejected manifest is silent: the repo indexes 'successfully' with no context
    search. A bare array is refused outright; an unresolved path is skipped alone,
    so ``artifacts N/N`` never reaches parity."""
    manifest = json.loads(MANIFEST.read_text())
    assert isinstance(manifest, dict), "a top-level array is rejected outright"
    artifacts = manifest["artifacts"]
    assert artifacts
    names = [a["name"] for a in artifacts]
    assert len(names) == len({n.lower() for n in names}), names
    for artifact in artifacts:
        assert set(artifact) == {"name", "path", "description"}, artifact
        path = artifact["path"]
        assert not any(c in path for c in "*?["), f"{path} is a glob: the server stat()s it"
        assert (ROOT / path).exists(), f"{path} does not resolve"


# ── Hooks ───────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("hook", list(HOOKS))
def test_each_hook_is_a_symlink_into_the_vendored_tree(hook: str) -> None:
    """A copy freezes at install day, and the health hook is silent when clean, so a
    stale copy looks healthy. Shape, not resolution: it dangles without submodules."""
    path = ROOT / ".claude" / "hooks" / hook
    assert path.is_symlink(), f"{hook} is not a symlink"
    target = os.readlink(path)
    assert not Path(target).is_absolute(), f"{hook} -> {target}"
    assert "skills-vendor/" in target, f"{hook} -> {target} leaves the vendored tree"


@pytest.mark.parametrize(("hook", "marker"), list(HOOKS.items()), ids=list(HOOKS))
def test_each_hook_is_registered_exactly_once(hook: str, marker: str) -> None:
    """Registered, or the hook is a file that never runs; once, or it runs twice."""
    entries = [
        h
        for group in json.loads(SETTINGS.read_text())["hooks"]["SessionStart"]
        for h in group["hooks"]
        if h["command"].endswith(f"# {marker}")
    ]
    assert len(entries) == 1, f"{marker}: {len(entries)} SessionStart entries"
    assert hook in entries[0]["command"]
