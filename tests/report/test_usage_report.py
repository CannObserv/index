"""Behaviour tests for scripts/usage_report.py, the usage report (#36).

The report reads the collector's OTLP/JSON lines (D21, #33), given as files or
on stdin from ``sudo`` at the call, so it never runs as root. It writes a
Markdown summary and a CSV per table to git-ignored ``data/`` (telemetry
design, § Report):

- **counts** per host, repo and period, split at ``docs/usage-markers.json``'s
  reach (#35): index calls by tool and ``includeLinked``'s share; look-alikes;
  sibling access; a grep after a ``codebase_search`` in one ``prompt.id``;
- **classification** of each grep-like call by rules, then the session's model
  for the ambiguous: a file out (``ambiguous.csv``) and a file back
  (``ambiguous-labels.csv``), no API call;
- **agreement** against the operator's labels (#37), with a Wilson interval;
- **efficiency** per call, by tool and class, and per request, by path.

**An index call** is ``tool_name`` ``mcp_tool`` whose ``tool_parameters`` is
exactly ``{"mcp_server_name": <SocratiCode's>, "mcp_tool_name": <tool>}``
(#54). An ``mcp_tool`` record without it is unidentified, never an index call,
and makes its period's index count a floor.

Every record here is synthetic: CI never sees the real sample.
"""

import csv
import importlib.util
import json
import stat
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "usage_report.py"
SC = "plugin_socraticode_socraticode"


def _load():
    spec = importlib.util.spec_from_file_location("usage_report", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["usage_report"] = module
    spec.loader.exec_module(module)
    return module


ur = _load()

# --- synthetic OTLP/JSON -----------------------------------------------------

_seq = iter(range(1, 1_000_000))


def _attrs(values: dict) -> list[dict]:
    out = []
    for key, value in values.items():
        if value is None:
            continue
        if isinstance(value, bool):
            v = {"boolValue": value}
        elif isinstance(value, int):
            v = {"intValue": str(value)}
        elif isinstance(value, float):
            v = {"doubleValue": value}
        else:
            v = {"stringValue": value}
        out.append({"key": key, "value": v})
    return out


def tool(
    name: str,
    params=None,
    tin=None,
    *,
    prompt: str = "p1",
    ts: str = "2026-11-28T10:00:00.000Z",
    session: str = "s1",
    repo: str = "index",
    size: str | None = "100",
    dur: str = "5",
    use_id: str | None = None,
    agent: str | None = None,
) -> dict:
    """A ``tool_result`` as 2.1.330 sends it: numbers as strings."""
    seq = next(_seq)
    return {
        "event.name": "tool_result",
        "event.timestamp": ts,
        "event.sequence": seq,
        "session.id": session,
        "prompt.id": prompt,
        "vcs.repository.name": repo,
        "app.version": "2.1.330",
        "agent_id": agent,
        "tool_name": name,
        "tool_use_id": use_id or f"toolu_{seq}",
        "success": "true",
        "duration_ms": dur,
        "tool_result_size_bytes": size,
        "tool_input_size_bytes": "10",
        "tool_parameters": params
        if params is None or isinstance(params, str)
        else json.dumps(params),
        "tool_input": tin if tin is None or isinstance(tin, str) else json.dumps(tin),
    }


def bash(cmd: str, **kw) -> dict:
    first = cmd.split()[0] if cmd.split() else ""
    params = {"bash_command": first, "full_command": cmd, "description": "d"}
    return tool("Bash", params, {"command": cmd[:512], "description": "d"}, **kw)


def index_call(name: str = "codebase_search", args: dict | None = None, server: str = SC, **kw):
    params = json.dumps({"mcp_server_name": server, "mcp_tool_name": name}, separators=(",", ":"))
    tin = json.dumps(args or {"query": "q", "limit": 5}, separators=(",", ":"))
    return tool("mcp_tool", params, tin, **kw)


def api(
    *,
    prompt: str = "p1",
    ts: str = "2026-11-28T10:00:01.000Z",
    session: str = "s1",
    repo: str = "index",
    inp: int = 10,
    out: int = 20,
    cache_read: int = 1000,
    cache_create: int = 50,
    cost: float = 0.01,
    dur: int = 2000,
) -> dict:
    return {
        "event.name": "api_request",
        "event.timestamp": ts,
        "event.sequence": next(_seq),
        "session.id": session,
        "prompt.id": prompt,
        "vcs.repository.name": repo,
        "app.version": "2.1.330",
        "model": "m",
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_tokens": cache_read,
        "cache_creation_tokens": cache_create,
        "cost_usd": cost,
        "duration_ms": dur,
    }


def export(records: list[dict], host: str = "co-index") -> str:
    logs = [
        {
            "timeUnixNano": "0",
            "body": {"stringValue": f"claude_code.{r['event.name']}"},
            "attributes": _attrs(r),
        }
        for r in records
    ]
    return json.dumps(
        {
            "resourceLogs": [
                {
                    "resource": {"attributes": _attrs({"cohort.host": host})},
                    "scopeLogs": [{"scope": {}, "logRecords": logs}],
                }
            ]
        }
    )


def analyse(records, *, host="co-index", markers=None, **kw):
    loaded = ur.load_records([export(records, host)])
    return ur.analyse(loaded.records, markers=markers or [], cohort=COHORT, **kw)


COHORT = {"index", "notifier", "watcher", "status", "observo"}


def count(result, measure, host="co-index", repo="index", period=None):
    keys = [k for k in result.counts if k[0] == host and k[1] == repo]
    if period is not None:
        keys = [k for k in keys if k[2] == period]
    return sum(result.counts[k].get(measure, 0) for k in keys)


# --- loading -----------------------------------------------------------------


def test_records_take_the_host_from_the_resource_and_coerce_string_numbers() -> None:
    loaded = ur.load_records([export([tool("Read", None, {"file_path": "x"}, size="42")])])
    [rec] = loaded.records
    assert rec.host == "co-index" and rec.repo == "index" and rec.event == "tool_result"
    assert rec.num("tool_result_size_bytes") == 42 and rec.num("duration_ms") == 5
    assert isinstance(rec.ts, datetime)


def test_a_missing_size_stays_missing_never_zero() -> None:
    """2.1.330 omits it on a record with ``error_type``."""
    loaded = ur.load_records([export([tool("Bash", None, None, size=None)])])
    assert loaded.records[0].num("tool_result_size_bytes") is None


def test_the_host_proof_session_is_excluded() -> None:
    lines = [export([tool("Read", session="host-proof-33"), tool("Read")])]
    loaded = ur.load_records(lines)
    assert len(loaded.records) == 1 and loaded.excluded == 1


def test_a_record_read_twice_is_counted_once() -> None:
    """A rotated file and its archive read together, or a file named twice."""
    line = export([tool("Read")])
    loaded = ur.load_records([line, line])
    assert len(loaded.records) == 1 and loaded.duplicates == 1


def test_an_unparseable_line_is_counted_and_skipped() -> None:
    loaded = ur.load_records(["{not json", "", export([tool("Read")])])
    assert len(loaded.records) == 1 and loaded.bad_lines == 1


# --- MCP: what an index call is ----------------------------------------------


@pytest.mark.parametrize("server", [SC, "socraticode"])
def test_an_index_call_is_mcp_tool_naming_socraticodes_server(server) -> None:
    r = analyse([index_call("codebase_search", server=server), index_call("codebase_impact")])
    assert count(r, "index:codebase_search") == 1
    assert count(r, "index:codebase_impact") == 1
    assert count(r, "index_calls") == 2


def test_another_servers_call_is_not_an_index_call() -> None:
    other = json.dumps({"mcp_server_name": "claude-vscode", "mcp_tool_name": "getDiagnostics"})
    r = analyse([tool("mcp_tool", other)])
    assert count(r, "index_calls") == 0 and count(r, "mcp_other") == 1


@pytest.mark.parametrize(
    "params",
    [
        None,  # before #54's install
        json.dumps({"mcp_server_name": SC, "mcp_tool_name": "codebase_search", "x": 1}),
        json.dumps({"mcp_server_name": SC}),
        '{"mcp_server_name":"' + SC + '","mcp_tool_na',  # cut at 4096
    ],
)
def test_an_mcp_call_in_any_other_shape_is_unidentified_and_the_count_a_floor(params) -> None:
    r = analyse([tool("mcp_tool", params), index_call()])
    assert count(r, "index_calls") == 1
    assert count(r, "mcp_unidentified") == 1
    assert count(r, "index_calls_floor") == 1


def test_a_period_with_every_mcp_call_identified_is_no_floor() -> None:
    assert count(analyse([index_call()]), "index_calls_floor") == 0


def test_a_bash_command_naming_codebase_search_is_not_an_index_call() -> None:
    """#34's mistake: grepping the files for ``codebase_`` reads Bash text."""
    r = analyse([bash("grep -c codebase_search events.jsonl")])
    assert count(r, "index_calls") == 0


def test_the_share_of_codebase_search_passing_include_linked() -> None:
    cut = '{"query":"' + "q" * 50 + '","includeLinked":true,"limit":5,"projectPath":"/x'
    r = analyse(
        [
            index_call(args={"query": "a", "includeLinked": True}),
            index_call(args={"query": "b", "includeLinked": False}),
            index_call(args={"query": "c"}),
            tool(
                "mcp_tool",
                json.dumps({"mcp_server_name": SC, "mcp_tool_name": "codebase_search"}),
                cut,
            ),
        ]
    )
    assert count(r, "codebase_search_linked") == 2
    assert count(r, "index:codebase_search") == 4


# --- Bash: searches and filters ----------------------------------------------


@pytest.mark.parametrize(
    ("cmd", "expected"),
    [
        ("grep -rn 'def foo' scripts/", [("grep", "def foo", False)]),
        ("rg -n auth", [("rg", "auth", False)]),
        ("git grep -n retry", [("git grep", "retry", False)]),
        ("ps aux | grep qdrant", [("grep", "qdrant", True)]),
        ("journalctl -u x | rg -c error", [("rg", "error", True)]),
        ("cat a.log | grep -c x file.txt", [("grep", "x", False)]),
        ("grep -e foo -e bar src", [("grep", "foo", False), ("grep", "bar", False)]),
        ("sudo grep -A 3 -m 2 ExecStart deploy/qdrant.service", [("grep", "ExecStart", False)]),
        ("cd x && grep -rl token . 2>/dev/null; echo done", [("grep", "token", False)]),
        ("git ls-files | xargs grep -n Foo.bar", [("grep", "Foo.bar", False)]),
        ("grep pat < input.txt", [("grep", "pat", False)]),
        ("find . -name '*.py' -newer x", [("find", "*.py", False)]),
        ("find deploy -type f", [("find", None, False)]),
        ("rg -g '*.md' --type py 'x y'", [("rg", "x y", False)]),
        ("echo grep foo", []),
        ("cat <<'EOF' > f\ngrep inside heredoc\nEOF\nls", []),
        ("ls -la\ngrep -n bar baz.py", [("grep", "bar", False)]),
    ],
)
def test_bash_searches_and_filters_are_told_apart(cmd, expected) -> None:
    found = ur.bash_searches(cmd)
    assert [(s.bin, s.pattern, s.filter) for s in found] == expected


def _found(cmd: str) -> list[tuple]:
    return [(s.bin, s.pattern, s.filter) for s in ur.bash_searches(cmd)]


@pytest.mark.parametrize(
    "cmd",
    [
        "ls;\ngrep -rn foo .",
        "(cd x && ls);grep -rn foo .",
        "cmd &&\ngrep -rn foo .",
        "a ||\ngrep -rn foo .",
    ],
)
def test_a_run_of_operators_still_separates_commands(cmd) -> None:
    """CR 1: shlex lexes ``;\\n`` or ``);`` as one token."""
    assert _found(cmd) == [("grep", "foo", False)]


def test_a_here_string_is_no_heredoc() -> None:
    """CR 2: ``<<<`` took the lines after it for a heredoc's body."""
    assert _found("grep x <<< foo\ngrep -rn bar .") == [
        ("grep", "x", False),
        ("grep", "bar", False),
    ]


def test_grep_flags_are_read() -> None:
    [s] = ur.bash_searches("grep -rnFw 'x: y' .")
    assert s.fixed and s.word
    [s] = ur.bash_searches("rg --fixed-strings -i z")
    assert s.fixed and not s.word


def test_an_unbalanced_command_is_none() -> None:
    assert ur.bash_searches("grep 'unterminated") is None


def test_a_bash_call_that_only_filters_is_no_look_alike() -> None:
    r = analyse([bash("ps aux | grep qdrant"), bash("rg -n Foo.bar")])
    assert count(r, "lookalike:bash_search") == 1
    assert count(r, "bash_filter") == 1


def test_a_full_command_cut_at_4096_falls_back_to_the_clients_input() -> None:
    rec = tool(
        "Bash", '{"bash_command":"rg","full_command":"rg -n Foo.ba', {"command": "rg -n Foo.bar"}
    )
    [call] = [c for c in analyse([rec]).calls]
    assert call.kind == "grep_like" and call.rule == "exact"


def test_a_command_nothing_parses_is_ambiguous_when_it_names_a_search() -> None:
    rec = tool("Bash", '{"full_command":"grep \'x', '{"command":"grep \'x')
    [call] = analyse([rec]).calls
    assert call.kind == "grep_like" and call.rule == "ambiguous"


# --- the rules, on synthetic committed examples ------------------------------

EXACT = [
    ("def load_records", {}),
    ("class Foo", {}),
    ("Foo.bar", {}),
    ("usage_report", {}),
    ("loadRecords", {}),
    ("ExecStart=", {}),
    ("TODO", {}),
    ("MAX_RETRIES", {}),
    ("^import ", {}),
    ("\\bfoo\\b", {}),
    ("deploy/otelcol.yaml", {}),
    ("otelcol.yaml", {}),
    ("Connection refused: port 6333", {}),
    ("'index-checkin: 202'", {}),
    ("--dry-run", {}),
    ("from reembed import|import reembed", {}),
    ("foo(", {}),
    ("Foo::bar", {}),
    ("anything goes here", {"fixed": True}),
    ("retry", {"word": True}),
    ("index-checkin", {}),  # a kebab-case name: a unit, a file, a skill
    ("Attestation posted on the SHA", {}),  # a capitalised phrase quotes a message
    ("port 6333 refused", {}),
    (",field,", {}),
    ("Foo.bar|qdrant|load_records", {}),  # symbols beside a project's own term
]
SEMANTIC = [
    ("auth|login", {}),
    ("qdrant|ollama", {}),  # plain words, even a project's own
    ("auth\\|login\\|session", {}),
    ("(retry|backoff)", {}),
    ("how errors are handled", {}),
    ("retry.*backoff", {}),
    ("authentication", {}),
    ("config", {}),
]
AMBIGUOUS = [
    ("qdrant", {}),
    ("Qdrant", {}),
    ("$PATTERN", {}),
    ("how errors|Foo.bar", {}),
    ("auth|Foo.bar", {}),
    ("", {}),
    (None, {}),
]


@pytest.mark.parametrize(("pattern", "flags"), EXACT)
def test_exact_string_patterns(pattern, flags) -> None:
    assert ur.classify_pattern(pattern, **flags) == "exact"


@pytest.mark.parametrize(("pattern", "flags"), SEMANTIC)
def test_semantic_stand_in_patterns(pattern, flags) -> None:
    assert ur.classify_pattern(pattern, **flags) == "semantic"


@pytest.mark.parametrize(("pattern", "flags"), AMBIGUOUS)
def test_ambiguous_patterns(pattern, flags) -> None:
    assert ur.classify_pattern(pattern, **flags) == "ambiguous"


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("**/*.py", "exact"),
        ("*.{ts,tsx}", "exact"),
        ("tests/**/test_*.py", "exact"),
        ("deploy/otelcol.yaml", "exact"),
        ("**/*auth*", "semantic"),
        ("*config*.py", "semantic"),
        ("**/*qdrant*", "ambiguous"),
        (None, "exact"),
    ],
)
def test_glob_and_find_name_patterns(pattern, expected) -> None:
    assert ur.classify_glob(pattern) == expected


def test_a_call_is_semantic_if_any_search_is_exact_if_all_are() -> None:
    calls = analyse(
        [
            bash("grep -rn auth . && grep -rn Foo.bar ."),
            bash("grep -rn Foo.bar . && grep -rn 'def x' ."),
            bash("grep -rn qdrant . && grep -rn Foo.bar ."),
        ]
    ).calls
    assert [c.rule for c in calls] == ["semantic", "exact", "ambiguous"]


# --- look-alikes, siblings, follow-ups ---------------------------------------


def test_grep_glob_and_explore_are_look_alikes() -> None:
    r = analyse(
        [
            tool("Grep", None, {"pattern": "auth|login", "path": "."}),
            tool("Glob", None, {"pattern": "**/*.py"}),
            tool(
                "Agent", {"subagent_type": "Explore"}, {"subagent_type": "Explore", "prompt": "x"}
            ),
            tool("Agent", {"subagent_type": "general-purpose"}, None),
        ]
    )
    assert count(r, "lookalike:grep_tool") == 1
    assert count(r, "lookalike:glob") == 1
    assert count(r, "lookalike:explore") == 1
    assert [c.rule for c in r.calls if c.kind == "grep_like"] == ["semantic", "exact"]


def _read(path, **kw):
    return tool("Read", None, {"file_path": path}, **kw)


def test_a_read_sweep_is_three_reads_in_one_prompt_before_any_search() -> None:
    r = analyse(
        [
            _read("a", prompt="p1"),
            _read("b", prompt="p1"),
            _read("c", prompt="p1"),
            _read("a", prompt="p2"),
            _read("b", prompt="p2"),
            index_call(prompt="p3", ts="2026-11-28T10:00:00.000Z"),
            _read("a", prompt="p3", ts="2026-11-28T10:00:01.000Z"),
            _read("b", prompt="p3", ts="2026-11-28T10:00:02.000Z"),
            _read("c", prompt="p3", ts="2026-11-28T10:00:03.000Z"),
        ]
    )
    assert count(r, "lookalike:read_sweep") == 1
    assert count(r, "lookalike:read_sweep_reads") == 3
    r2 = analyse([_read("a", prompt="p2"), _read("b", prompt="p2")], sweep_reads=2)
    assert count(r2, "lookalike:read_sweep") == 1


@pytest.mark.parametrize(
    "rec",
    [
        bash("git clone https://github.com/CannObserv/notifier.git /tmp/n"),
        bash("git clone git@github.com:CannObserv/watcher"),
        bash("gh repo clone CannObserv/status"),
        bash("gh repo clone observo -- --depth 1"),
        bash("gh api repos/CannObserv/notifier/contents/AGENTS.md --jq .content"),
        _read("/home/exedev/notifier/src/app.py"),
        _read("/home/exedev/index/../watcher/README.md"),
    ],
)
def test_sibling_access_names_a_cohort_repo(rec) -> None:
    assert count(analyse([rec]), "sibling_access") == 1


@pytest.mark.parametrize(
    "rec",
    [
        bash("git clone https://github.com/CannObserv/index"),  # its own repo
        bash("git clone https://github.com/someone/notifier-fork"),
        bash("gh api repos/CannObserv/notifier/issues"),
        _read("/home/exedev/index/docs/status/x.md"),
        _read("/home/exedev/unknown/x.md"),
    ],
)
def test_no_sibling_access(rec) -> None:
    assert count(analyse([rec]), "sibling_access") == 0


def test_linked_searches_are_counted_beside_sibling_access() -> None:
    r = analyse([index_call(args={"query": "q", "includeLinked": True})])
    assert count(r, "codebase_search_linked") == 1


def test_a_grep_after_a_codebase_search_in_the_same_prompt_is_a_follow_up() -> None:
    r = analyse(
        [
            bash("rg -n Foo.bar", prompt="p1", ts="2026-11-28T10:00:00.000Z"),
            index_call(prompt="p1", ts="2026-11-28T10:00:01.000Z"),
            bash("rg -n Foo.baz", prompt="p1", ts="2026-11-28T10:00:02.000Z"),
            bash("rg -n Foo.qux", prompt="p1", ts="2026-11-28T10:00:03.000Z"),
            bash("rg -n Foo.bar", prompt="p2", ts="2026-11-28T10:00:04.000Z"),
            index_call("codebase_impact", prompt="p3", ts="2026-11-28T10:00:00.000Z"),
            bash("rg -n Foo.bar", prompt="p3", ts="2026-11-28T10:00:05.000Z"),
        ]
    )
    assert count(r, "followup:grep_after_search") == 2
    assert count(r, "followup:prompts") == 1


# --- periods -----------------------------------------------------------------

MARKERS = [
    {
        "id": "collection-on-co-index",
        "date": "2026-11-23T20:00:00Z",
        "reach": [{"host": "co-index", "at": "2026-11-23T20:00:00Z"}],
    },
    {
        "id": "policy-v2",
        "date": "2026-11-28T12:00:00Z",
        "reach": [{"repo": "index", "at": "2026-11-28T12:00:00Z"}],
    },
]


def test_periods_split_at_each_markers_reach() -> None:
    r = analyse(
        [
            index_call(ts="2026-11-28T11:00:00.000Z"),
            index_call(ts="2026-11-28T13:00:00.000Z"),
            index_call(ts="2026-11-28T13:30:00.000Z"),
            index_call(ts="2026-11-28T13:00:00.000Z", repo="notifier"),
        ],
        markers=MARKERS,
    )
    assert count(r, "index_calls", period="from collection-on-co-index") == 1
    assert count(r, "index_calls", period="from policy-v2") == 2
    # notifier on co-index: the host's marker splits it, policy-v2 hasn't reached it.
    assert count(r, "index_calls", repo="notifier", period="from collection-on-co-index") == 1


def test_a_record_before_every_marker_is_before_the_first() -> None:
    r = analyse([index_call(ts="2026-11-20T00:00:00.000Z")], markers=MARKERS)
    assert count(r, "index_calls", period="before collection-on-co-index") == 1


def test_a_host_and_repo_no_marker_reached_is_one_unsplit_period() -> None:
    r = analyse([index_call(repo="notifier")], host="co-notifier", markers=MARKERS)
    keys = [k for k in r.counts if k[0] == "co-notifier"]
    assert [k[2] for k in keys] == ["unsplit"]


def test_the_committed_markers_load() -> None:
    markers = ur.load_markers(REPO_ROOT / "docs" / "usage-markers.json")
    assert markers[0]["id"] == "collection-on-co-index"


def test_a_malformed_markers_file_is_refused(tmp_path) -> None:
    bad = tmp_path / "m.json"
    bad.write_text(json.dumps({"markers": [{"id": "x", "reach": [{"repo": "y"}]}]}))
    with pytest.raises(ur.Usage):
        ur.load_markers(bad)


# --- efficiency --------------------------------------------------------------


def test_per_call_efficiency_by_tool_and_class() -> None:
    r = analyse(
        [
            index_call(size="300", dur="900"),
            index_call(size="100", dur="100"),
            bash("rg -n Foo.bar", size="50", dur="10"),
            bash("rg -n auth", size="70", dur="30"),
            bash("ls", size=None, dur="1"),
        ]
    )
    rows = {(row["tool"], row["class"]): row for row in r.efficiency_calls}
    idx = rows[("index:codebase_search", "index")]
    assert idx["calls"] == 2 and idx["size_sum"] == 400 and idx["size_median"] == 200
    assert idx["duration_median"] == 500
    assert rows[("Bash", "exact")]["size_sum"] == 50
    assert rows[("Bash", "semantic")]["size_sum"] == 70
    other = rows[("Bash", "other")]
    assert other["calls"] == 1 and other["size_n"] == 0 and other["size_sum"] is None


def test_per_request_efficiency_by_path_keeps_cache_reads_apart() -> None:
    r = analyse(
        [
            index_call(prompt="pi"),
            api(prompt="pi", inp=10, cache_read=1000, cost=0.5),
            api(prompt="pi", inp=5, cache_read=500, cost=0.25),
            bash("rg -n x.y", prompt="pg"),
            api(prompt="pg", inp=7),
            index_call(prompt="pb"),
            bash("rg -n x.y", prompt="pb"),
            api(prompt="pb"),
            bash("ls", prompt="pn"),
            api(prompt="pn"),
        ]
    )
    rows = {row["path"]: row for row in r.efficiency_requests}
    assert set(rows) == {"index-only", "grep-only", "both", "neither"}
    pi = rows["index-only"]
    assert pi["requests"] == 1 and pi["input_tokens_sum"] == 15
    assert pi["cache_read_tokens_sum"] == 1500 and pi["cost_usd_sum"] == pytest.approx(0.75)
    assert rows["grep-only"]["input_tokens_sum"] == 7


# --- the ambiguous loop and agreement ----------------------------------------


def test_wilson_interval() -> None:
    lo, hi = ur.wilson(90, 100)
    assert lo == pytest.approx(0.8256, abs=1e-4) and hi == pytest.approx(0.9448, abs=1e-4)
    assert ur.wilson(0, 0) == (None, None)


def test_model_labels_resolve_the_ambiguous() -> None:
    recs = [bash("rg -n qdrant", use_id="t1"), bash("rg -n Foo.bar", use_id="t2")]
    r = analyse(recs, model_labels={"t1": "semantic"})
    assert {c.use_id: c.final for c in r.calls} == {"t1": "semantic", "t2": "exact"}
    r = analyse(recs)
    assert {c.use_id: c.final for c in r.calls} == {"t1": None, "t2": "exact"}


def test_agreement_counts_an_unresolved_call_as_a_disagreement() -> None:
    recs = [
        bash("rg -n Foo.bar", use_id="a"),
        bash("rg -n auth", use_id="b"),
        bash("rg -n qdrant", use_id="c"),
        bash("rg -n Foo.baz", use_id="d"),
    ]
    labels = {"a": "exact", "b": "exact", "c": "exact", "d": "exact", "gone": "exact"}
    ag = analyse(recs, labels=labels).agreement
    assert (ag.n, ag.agree, ag.unresolved, ag.unmatched) == (4, 2, 1, 1)
    assert ag.lower == pytest.approx(ur.wilson(2, 4)[0])
    assert not ag.established


def test_agreement_is_established_when_its_lower_bound_reaches_the_threshold() -> None:
    recs = [bash("rg -n Foo.bar", use_id=f"u{i}") for i in range(60)]
    labels = {f"u{i}": "exact" for i in range(60)}
    assert analyse(recs, labels=labels, threshold=0.9).agreement.established
    assert not analyse(recs, labels=labels, threshold=0.99).agreement.established


def test_label_files_take_only_exact_or_semantic(tmp_path) -> None:
    path = tmp_path / "l.csv"
    path.write_text("tool_use_id,label,extra\nt1,exact,x\nt2, Semantic ,y\nt3,,z\n")
    assert ur.read_labels(path) == {"t1": "exact", "t2": "semantic"}
    path.write_text("tool_use_id,label\nt1,maybe\n")
    with pytest.raises(ur.Usage):
        ur.read_labels(path)
    path.write_text("id,label\nt1,exact\n")
    with pytest.raises(ur.Usage):
        ur.read_labels(path)


# --- the command line --------------------------------------------------------


def _run(stdin: str, *args: str, out: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--out", str(out), *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


SECRET_TERM = "zebrapayload"  # a plain word no rule decides: ambiguous


def _sample_lines() -> str:
    recs = [
        index_call(args={"query": SECRET_TERM, "includeLinked": True}, prompt="p1"),
        bash(f"rg -n {SECRET_TERM}", prompt="p1", use_id="amb1"),
        bash("grep -rn auth .", prompt="p2", use_id="sem1"),
        bash("rg -n Foo.bar", prompt="p2", use_id="ex1"),
        api(prompt="p1"),
        api(prompt="p2"),
    ]
    return export(recs) + "\n"


def test_the_report_reads_stdin_and_writes_markdown_and_csvs_private(tmp_path) -> None:
    out = tmp_path / "data"
    res = _run(_sample_lines(), "-", out=out)
    assert res.returncode == 0, res.stderr
    names = {p.name for p in out.iterdir()}
    assert {
        "report.md",
        "counts.csv",
        "classification.csv",
        "efficiency_calls.csv",
        "efficiency_requests.csv",
        "ambiguous.csv",
    } <= names
    assert stat.S_IMODE(out.stat().st_mode) == 0o700
    for p in out.iterdir():
        assert stat.S_IMODE(p.stat().st_mode) == 0o600, p.name
    report = (out / "report.md").read_text()
    for term in ("cost_usd", "estimate", "different tasks", "Agreement", "floor"):
        assert term in report, term


def test_the_summary_and_tables_carry_no_command_or_query_text(tmp_path) -> None:
    """Counts and shapes only: the ambiguous set and the sample hold the text."""
    out = tmp_path / "data"
    assert _run(_sample_lines(), "-", "--sample", "2", out=out).returncode == 0
    for name in (
        "report.md",
        "counts.csv",
        "classification.csv",
        "efficiency_calls.csv",
        "efficiency_requests.csv",
    ):
        assert SECRET_TERM not in (out / name).read_text(), name
    assert SECRET_TERM in (out / "ambiguous.csv").read_text()


def test_the_ambiguous_set_goes_out_and_its_labels_come_back(tmp_path) -> None:
    out = tmp_path / "data"
    assert _run(_sample_lines(), "-", out=out).returncode == 0
    rows = list(csv.DictReader((out / "ambiguous.csv").open()))
    assert [r["tool_use_id"] for r in rows] == ["amb1"]
    assert rows[0]["label"] == ""
    assert "mcp_tool codebase_search" in rows[0]["siblings"]
    rows[0]["label"] = "semantic"
    with (out / "ambiguous-labels.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    assert _run(_sample_lines(), "-", out=out).returncode == 0
    by_class = {
        (r["class"], r["pass"]): int(r["calls"])
        for r in csv.DictReader((out / "classification.csv").open())
        if r["period"] == "all"
    }
    assert by_class[("semantic", "final")] == 2 and by_class[("ambiguous", "final")] == 0
    assert by_class[("ambiguous", "rules")] == 1


def test_the_sample_for_the_operator_is_seeded_and_unlabelled(tmp_path) -> None:
    out = tmp_path / "data"
    assert _run(_sample_lines(), "-", "--sample", "2", "--seed", "7", out=out).returncode == 0
    first = (out / "label-sample.csv").read_text()
    rows = list(csv.DictReader(first.splitlines()))
    assert len(rows) == 2 and all(r["label"] == "" for r in rows)
    assert {r["tool_use_id"] for r in rows} <= {"amb1", "sem1", "ex1"}
    assert _run(_sample_lines(), "-", "--sample", "2", "--seed", "7", out=out).returncode == 0
    assert (out / "label-sample.csv").read_text() == first


def test_operator_labels_are_read_from_the_data_directory(tmp_path) -> None:
    out = tmp_path / "data"
    out.mkdir(mode=0o700)
    (out / "labels.csv").write_text("tool_use_id,label\nsem1,semantic\nex1,exact\n")
    assert _run(_sample_lines(), "-", out=out).returncode == 0
    report = (out / "report.md").read_text()
    assert "2 of 2" in report


def test_files_are_read_as_well_as_stdin(tmp_path) -> None:
    src = tmp_path / "events.jsonl"
    src.write_text(_sample_lines())
    out = tmp_path / "data"
    res = _run("", str(src), out=out)
    assert res.returncode == 0, res.stderr


def test_no_records_writes_nothing_and_exits_1(tmp_path) -> None:
    out = tmp_path / "data"
    res = _run("", "-", out=out)
    assert res.returncode == 1 and "no records" in res.stderr
    assert not out.exists()


def test_a_bad_label_file_is_a_usage_error(tmp_path) -> None:
    out = tmp_path / "data"
    bad = tmp_path / "l.csv"
    bad.write_text("tool_use_id,label\nx,perhaps\n")
    res = _run(_sample_lines(), "-", "--labels", str(bad), out=out)
    assert res.returncode == 64, res.stderr


def test_data_is_git_ignored() -> None:
    res = subprocess.run(
        ["git", "check-ignore", "-q", "data/report.md"], cwd=REPO_ROOT, check=False
    )
    assert res.returncode == 0, "data/ must be in .gitignore"
