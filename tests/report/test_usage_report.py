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


def test_the_repo_is_read_from_the_resource_when_the_record_lacks_it() -> None:
    """CR 28: the design's § Collector puts vcs.repository.name on the resource."""
    rec = tool("Read")
    rec["vcs.repository.name"] = None
    line = json.loads(export([rec]))
    line["resourceLogs"][0]["resource"]["attributes"].append(
        {"key": "vcs.repository.name", "value": {"stringValue": "notifier"}}
    )
    [r] = ur.load_records([json.dumps(line)]).records
    assert r.repo == "notifier"


def test_a_missing_size_stays_missing_never_zero() -> None:
    """2.1.330 omits it on a record with ``error_type``."""
    loaded = ur.load_records([export([tool("Bash", None, None, size=None)])])
    assert loaded.records[0].num("tool_result_size_bytes") is None


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "x"])
def test_a_number_that_isnt_finite_is_missing(value) -> None:
    """CR 14: one nan poisoned every sum it joined."""
    loaded = ur.load_records([export([tool("Read", size=value)])])
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


def test_two_records_sharing_a_sequence_number_are_both_kept() -> None:
    """CR 5: measured on co-index, two distinct tool_results shared
    ``session.id``, ``event.sequence`` and timestamp: the id tells them apart."""
    a, b = tool("Read", use_id="toolu_a"), tool("Read", use_id="toolu_b")
    b["event.sequence"] = a["event.sequence"]
    q, r = api(), api()
    r["event.sequence"] = q["event.sequence"]
    r["request_id"], q["request_id"] = "req_r", "req_q"
    loaded = ur.load_records([export([a, b, q, r])])
    assert len(loaded.records) == 4 and loaded.duplicates == 0


def test_one_bad_record_is_counted_and_the_rest_are_read() -> None:
    """CR 6: a bad timestamp or sequence raised, and the run died."""
    bad_ts, bad_seq = tool("Read"), tool("Read")
    bad_ts["event.timestamp"] = "garbage"
    bad_seq["event.sequence"] = "x"
    lines = [export([bad_ts, tool("Read"), bad_seq]), '{"resourceLogs": 5}']
    loaded = ur.load_records(lines)
    assert len(loaded.records) == 1
    assert loaded.bad_records == 2 and loaded.bad_lines == 1


def test_a_record_without_a_timestamp_takes_the_export_time_in_utc() -> None:
    rec = tool("Read")
    rec["event.timestamp"] = None
    line = export([rec]).replace('"timeUnixNano": "0"', '"timeUnixNano": "1764000000000000000"')
    [r] = ur.load_records([line]).records
    assert r.ts.isoformat() == "2025-11-24T16:00:00+00:00"


def test_a_timestamp_without_a_zone_is_a_bad_record() -> None:
    """CR 27: it loaded, then the sort raised TypeError."""
    naive = tool("Read")
    naive["event.timestamp"] = "2026-11-28T10:00:00"
    loaded = ur.load_records([export([naive, tool("Read")])])
    assert len(loaded.records) == 1 and loaded.bad_records == 1


@pytest.mark.parametrize(
    "line",
    [
        '{"resourceLogs": [{"scopeLogs": "x"}]}',
        '{"resourceLogs": [{"scopeLogs": [{"logRecords": 5}]}]}',
        '{"resourceLogs": ["x"]}',
        '{"resourceLogs": [{"resource": {"attributes": ["x"]}, "scopeLogs": []}]}',
        "[1, 2]",
    ],
)
def test_a_badly_shaped_line_is_a_bad_line_not_a_crash(line) -> None:
    """CR 39: these raised outside the try: a traceback, exit 1."""
    loaded = ur.load_records([line, export([tool("Read")])])
    assert loaded.bad_lines == 1 and len(loaded.records) == 1


def test_a_timestamp_that_isnt_text_is_a_bad_record() -> None:
    rec = tool("Read")
    rec["event.timestamp"] = True  # boolValue
    loaded = ur.load_records([export([rec, tool("Read")])])
    assert loaded.bad_records == 1 and len(loaded.records) == 1


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
    assert count(r, "codebase_search_linked_share") == 0.5  # CR 13: the issue asks the share


def test_no_codebase_search_has_no_share() -> None:
    assert (
        "codebase_search_linked_share"
        not in analyse([index_call("codebase_impact")]).counts[("co-index", "index", "unsplit")]
    )


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


@pytest.mark.parametrize(
    "cmd",
    [
        "for f in a b; do grep -rn foo $f; done",
        "if grep -rqn foo .; then echo y; fi",
        "! grep -rn foo .",
        "while true; do grep -rn foo .; done",
        "if x; then :; else grep -rn foo .; fi",
    ],
)
def test_a_shell_keyword_is_no_command(cmd) -> None:
    """CR 3: ``do``, ``then``, ``!`` and the rest hid the grep after them."""
    assert _found(cmd) == [("grep", "foo", False)]


@pytest.mark.parametrize(
    "cmd",
    [
        "sudo -u exedev grep -rn foo .",
        "sudo -E -u exedev -- grep -rn foo .",
        "nice -n 10 grep -rn foo .",
        "timeout -s KILL 10 grep -rn foo .",
        "timeout 5s grep -rn foo .",
        "env -u X A=b grep -rn foo .",
        "git ls-files | xargs -n 1 grep foo",
        "git ls-files | xargs -I {} grep foo {}",
        "git ls-files | xargs -0 -P4 grep foo",
    ],
)
def test_a_prefixs_valued_options_are_skipped(cmd) -> None:
    """CR 4: ``sudo -u x`` or ``xargs -n 1`` left their value as the command."""
    assert _found(cmd) == [("grep", "foo", False)]


@pytest.mark.parametrize(
    "cmd", ["git -C /x grep -n foo", "git --no-pager -c core.x=1 grep foo", "git -P grep foo"]
)
def test_gits_own_options_come_before_grep(cmd) -> None:
    """CR 9: ``git -C dir grep`` was missed."""
    assert _found(cmd) == [("git grep", "foo", False)]


@pytest.mark.parametrize(
    "cmd",
    [
        "find . -name '*.py' -exec grep -l auth {} +",
        "find . -name '*.py' -exec grep -l auth {} \\;",
    ],
)
def test_a_grep_under_find_exec_is_a_search(cmd) -> None:
    """CR 10: only the find's glob was seen, so the call read exact."""
    assert _found(cmd) == [("find", "*.py", False), ("grep", "auth", False)]
    [call] = analyse([bash(cmd)]).calls
    assert call.rule == "semantic"


@pytest.mark.parametrize(
    "cmd",
    [
        "# it's a comment\ngrep -rn foo .",
        "  # don't\ngrep -rn foo .",
        "grep -rn foo . # it's here",
    ],
)
def test_a_comment_is_no_part_of_the_command(cmd) -> None:
    """CR 11: an apostrophe in a comment failed the whole lex."""
    assert _found(cmd) == [("grep", "foo", False)]


def test_a_hash_inside_quotes_is_kept() -> None:
    assert _found("grep -rn '# x' .") == [("grep", "# x", False)]


@pytest.mark.parametrize(
    ("cmd", "expected"),
    [
        ("rg -d 2 auth", [("rg", "auth", False)]),
        ("grep -rf pats.txt src/", [("grep", None, False)]),
        ("rg --file=pats.txt src", [("rg", None, False)]),
        ("grep --file pats.txt -e foo src", [("grep", "foo", False)]),
    ],
)
def test_rg_depth_takes_a_value_and_a_pattern_file_is_unknown(cmd, expected) -> None:
    """CR 12: ``rg -d 2`` read 2 as the pattern; ``-f FILE`` took the first path."""
    assert _found(cmd) == expected


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
    # CR 60: shlex drops the quotes, so a lowercase message looks like prose.
    ("permission denied", {}),
    ("no such file or directory", {}),
    ("failed to connect", {}),
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
        ("**/*auth*/**", "semantic"),  # CR 15: the last segment was **
        ("src/**", "exact"),
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


def test_each_look_alike_count_is_split_by_class() -> None:
    """CR 62: a raw grep-vs-index ratio is no finding (the design's anti-goal)."""
    r = analyse(
        [
            bash("rg -n Foo.bar"),
            bash("rg -n auth"),
            bash("rg -n qdrant"),
            tool("Grep", None, {"pattern": "Foo.bar"}),
            tool("Glob", None, {"pattern": "**/*auth*"}, agent="a1"),
        ]
    )
    for measure, n in {
        "lookalike:bash_search:exact": 1,
        "lookalike:bash_search:semantic": 1,
        "lookalike:bash_search:ambiguous": 1,
        "lookalike:grep_tool:exact": 1,
        "subagent:lookalike:glob:semantic": 1,
    }.items():
        assert count(r, measure) == n, measure
    assert count(r, "lookalike:bash_search") == 3


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
    r4 = analyse([_read(p, prompt="p4") for p in "abcd"])  # CR 53: past the threshold
    assert count(r4, "lookalike:read_sweep") == 1 and count(r4, "lookalike:read_sweep_reads") == 4


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
    """CR 45: both measures, side by side, as the design reads them."""
    r = analyse(
        [
            index_call(args={"query": "q", "includeLinked": True}),
            bash("gh repo clone CannObserv/notifier"),
        ]
    )
    assert count(r, "codebase_search_linked") == 1
    assert count(r, "sibling_access") == 1


def test_a_follow_up_is_split_by_class() -> None:
    """CR 63: an exact grep for a symbol the search returned is correct use;
    only the semantic and ambiguous ones say the index answered thin."""
    r = analyse([index_call(), bash("rg -n Foo.bar"), bash("rg -n auth"), bash("rg -n qdrant")])
    assert count(r, "followup:grep_after_search") == 3
    assert count(r, "followup:grep_after_search:exact") == 1
    assert count(r, "followup:grep_after_search:semantic") == 1
    assert count(r, "followup:grep_after_search:ambiguous") == 1


def test_a_subagents_calls_count_apart_from_the_main_threads() -> None:
    """CR 24: subagents share the parent's prompt.id (measured, 8 of 8), so one
    Explore counted as an Explore, its Greps, and the parent's follow-ups."""
    r = analyse(
        [
            index_call(ts="2026-11-28T10:00:00.000Z"),
            tool("Grep", None, {"pattern": "auth"}, agent="a1", ts="2026-11-28T10:00:01.000Z"),
            index_call(agent="a1", ts="2026-11-28T10:00:02.000Z"),
            tool("Agent", {"subagent_type": "Explore"}, None, ts="2026-11-28T10:00:03.000Z"),
        ]
    )
    assert count(r, "lookalike:explore") == 1
    assert count(r, "lookalike:grep_tool") == 0
    assert count(r, "subagent:lookalike:grep_tool") == 1
    assert count(r, "index_calls") == 1 and count(r, "subagent:index_calls") == 1
    assert count(r, "subagent:tool_calls") == 2 and count(r, "tool_calls") == 2
    assert count(r, "followup:grep_after_search") == 0


def test_a_subagents_api_requests_count_apart() -> None:
    """CR 40: every other measure split them; api_requests didn't."""
    sub = api()
    sub["agent_id"] = "a1"
    r = analyse([api(), sub])
    assert count(r, "api_requests") == 1 and count(r, "subagent:api_requests") == 1


def test_a_subagents_unidentified_call_and_linked_share_are_read() -> None:
    """CR 52: a subagent-only period still reads as a floor and has its share."""
    r = analyse(
        [
            tool("mcp_tool", agent="a1"),
            index_call(args={"query": "q", "includeLinked": True}, agent="a1"),
            index_call(agent="a1"),
        ]
    )
    assert count(r, "index_calls_floor") == 1
    assert count(r, "subagent:codebase_search_linked_share") == 0.5


def test_a_subagents_reads_are_no_read_sweep_of_the_parent() -> None:
    r = analyse([_read(p, agent="a1") for p in "abc"])
    assert count(r, "lookalike:read_sweep") == 0


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


def test_a_prompt_spanning_a_marker_credits_each_call_to_its_own_period() -> None:
    """CR 25: its follow-up and sweep went to the period of its first call."""
    t = "2026-11-28T{}.000Z".format
    r = analyse(
        [
            index_call(ts=t("11:59:00")),
            bash("rg -n Foo.bar", ts=t("12:01:00")),
            _read("a", prompt="p2", ts=t("11:58:00")),
            _read("b", prompt="p2", ts=t("11:59:00")),
            _read("c", prompt="p2", ts=t("12:02:00")),
        ],
        markers=MARKERS,
    )
    after, before = "from policy-v2", "from collection-on-co-index"
    assert count(r, "followup:grep_after_search", period=after) == 1
    assert count(r, "followup:prompts", period=after) == 1
    assert count(r, "followup:grep_after_search", period=before) == 0
    assert count(r, "lookalike:read_sweep", period=after) == 1
    assert count(r, "lookalike:read_sweep", period=before) == 0


def test_classification_has_a_row_per_period_as_well_as_all() -> None:
    """CR 54: every test read period "all" only."""
    r = analyse(
        [
            bash("rg -n auth", ts="2026-11-28T11:00:00.000Z"),
            bash("rg -n Foo.bar", ts="2026-11-28T13:00:00.000Z"),
        ],
        markers=MARKERS,
    )
    rows = {(x["period"], x["pass"], x["class"]): x["calls"] for x in r.classification}
    assert rows[("from collection-on-co-index", "rules", "semantic")] == 1
    assert rows[("from policy-v2", "rules", "exact")] == 1
    assert rows[("from policy-v2", "rules", "semantic")] == 0
    assert rows[("all", "rules", "exact")] == 1


def test_a_prompt_following_up_on_both_sides_of_a_marker_counts_once_in_each() -> None:
    """CR 57: follow-up prompts per period, and a call exactly at a reach."""
    t = "2026-11-28T{}.000Z".format
    r = analyse(
        [
            index_call(ts=t("11:58:00")),
            bash("rg -n Foo.bar", ts=t("11:59:00")),
            bash("rg -n Foo.baz", ts=t("12:01:00")),
            index_call("codebase_impact", prompt="p9", ts="2026-11-28T12:00:00.000Z"),
        ],
        markers=MARKERS,
    )
    for per in ("from collection-on-co-index", "from policy-v2"):
        assert count(r, "followup:prompts", period=per) == 1, per
    assert count(r, "index:codebase_impact", period="from policy-v2") == 1  # at the reach


def test_a_lower_bound_equal_to_the_threshold_is_established() -> None:
    recs = [bash("rg -n Foo.bar", use_id=f"u{i}") for i in range(20)]
    labels = {f"u{i}": "exact" for i in range(20)}
    lower = ur.wilson(20, 20)[0]
    assert analyse(recs, labels=labels, threshold=lower).agreement.established


def test_a_record_before_every_marker_is_before_the_first() -> None:
    r = analyse([index_call(ts="2026-11-20T00:00:00.000Z")], markers=MARKERS)
    assert count(r, "index_calls", period="before collection-on-co-index") == 1


def test_a_host_and_repo_no_marker_reached_is_one_unsplit_period() -> None:
    r = analyse([index_call(repo="notifier")], host="co-notifier", markers=MARKERS)
    keys = [k for k in r.counts if k[0] == "co-notifier"]
    assert [k[2] for k in keys] == ["unsplit"]


def test_each_marker_names_the_pairs_it_leaves_out_of_its_split() -> None:
    """CR 43: a pair a marker hasn't reached has no period for it; the report
    says so instead of leaving the reader to notice."""
    loaded = ur.load_records(
        [
            export([index_call(), index_call(repo="notifier")]),
            export([index_call(repo="notifier")], host="co-notifier"),
        ]
    )
    r = ur.analyse(loaded.records, markers=MARKERS, cohort=COHORT)
    by_id = {m["id"]: m for m in r.markers}
    assert by_id["policy-v2"]["reached"] == ["co-index · index"]
    assert by_id["policy-v2"]["left_out"] == ["co-index · notifier", "co-notifier · notifier"]
    assert by_id["collection-on-co-index"]["left_out"] == ["co-notifier · notifier"]


def test_the_report_lists_each_markers_left_out_pairs(tmp_path) -> None:
    markers = tmp_path / "m.json"
    markers.write_text(json.dumps({"markers": MARKERS}))
    out = tmp_path / "data"
    lines = _sample_lines() + export([index_call()], host="co-notifier") + "\n"
    assert _run(lines, "-", "--markers", str(markers), out=out).returncode == 0
    report = (out / "report.md").read_text()
    assert "## Markers" in report
    assert "| policy-v2 |" in report and "co-notifier · index" in report


def test_the_committed_markers_load() -> None:
    markers = ur.load_markers(REPO_ROOT / "docs" / "usage-markers.json")
    assert markers[0]["id"] == "collection-on-co-index"


@pytest.mark.parametrize(
    ("date", "at"),
    [
        ("2026-11-23T20:00:00", "2026-11-23T20:00:00Z"),
        ("2026-11-23T20:00:00Z", "2026-11-23T20:00:00"),
    ],
)
def test_a_marker_time_without_a_zone_is_refused(tmp_path, date, at) -> None:
    """CR 7: it loaded, then the first comparison raised TypeError."""
    path = tmp_path / "m.json"
    marker = {"id": "x", "date": date, "reach": [{"host": "h", "at": at}]}
    path.write_text(json.dumps({"markers": [marker]}))
    with pytest.raises(ur.Usage):
        ur.load_markers(path)


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


def test_token_sums_are_whole_numbers_in_every_table() -> None:
    """CR 8: they were floats, which report.md printed as 1.235e+05."""
    r = analyse([api(prompt="p", inp=123456, cache_read=9876543, dur=1500)])
    [row] = [x for x in r.efficiency_requests if x["requests"]]
    assert row["input_tokens_sum"] == 123456 and isinstance(row["input_tokens_sum"], int)
    assert isinstance(row["cache_read_tokens_sum"], int)
    assert isinstance(row["duration_ms_sum"], int)
    assert "123456" in ur._md_table([row], ["input_tokens_sum"])


def test_a_request_with_an_unidentified_mcp_call_has_no_known_path() -> None:
    """CR 26: before #54's install it read grep-only or neither."""
    r = analyse([tool("mcp_tool"), bash("rg -n Foo.bar"), api()])
    rows = {row["path"]: row["requests"] for row in r.efficiency_requests}
    assert rows["unidentified"] == 1 and rows["grep-only"] == 0


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
            bash("ls", prompt="pn3"),
            api(prompt="pn3"),
        ]
    )
    rows = {row["path"]: row for row in r.efficiency_requests}
    assert set(rows) == {"index-only", "grep-only", "both", "neither", "unidentified"}
    pi = rows["index-only"]
    assert pi["requests"] == 1 and pi["input_tokens_sum"] == 15
    assert pi["cache_read_tokens_sum"] == 1500 and pi["cost_usd_sum"] == pytest.approx(0.75)
    assert rows["grep-only"]["input_tokens_sum"] == 7
    assert rows["both"]["requests"] == 1 and rows["neither"]["requests"] == 2  # CR 51


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


def test_a_model_label_never_overrides_a_rules_class() -> None:
    """CR 55: the model judges only what the rules leave ambiguous."""
    r = analyse([bash("rg -n Foo.bar", use_id="t2")], model_labels={"t2": "semantic"})
    assert r.calls[0].final == "exact"


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


def test_labels_that_name_no_grep_like_call_are_reported_as_such() -> None:
    """CR 29: the line said "no operator labels yet"."""
    ag = analyse([index_call(use_id="i1")], labels={"i1": "exact", "x": "semantic"}).agreement
    assert ag.n == 0 and ag.unmatched == 2
    assert "2 labels match no grep-like call" in ag.line()
    assert "no operator labels" not in ag.line()


def test_agreement_is_established_when_its_lower_bound_reaches_the_threshold() -> None:
    recs = [bash("rg -n Foo.bar", use_id=f"u{i}") for i in range(60)]
    labels = {f"u{i}": "exact" for i in range(60)}
    assert analyse(recs, labels=labels, threshold=0.9).agreement.established
    assert not analyse(recs, labels=labels, threshold=0.99).agreement.established


def test_a_label_file_saved_with_a_bom_is_read(tmp_path) -> None:
    """CR 31: a spreadsheet's UTF-8 BOM read as a missing tool_use_id column."""
    path = tmp_path / "l.csv"
    path.write_bytes("\ufefftool_use_id,label\nt1,exact\n".encode())
    assert ur.read_labels(path) == {"t1": "exact"}


def _no_id(rec: dict) -> dict:
    rec["tool_use_id"] = ""
    return rec


def test_a_call_without_a_tool_use_id_is_never_labelled_or_sampled(tmp_path) -> None:
    """CR 32: every such call shared the key "", so a label graded the wrong one."""
    path = tmp_path / "l.csv"
    path.write_text("tool_use_id,label\n,exact\n  ,semantic\nt1,exact\n")
    assert ur.read_labels(path) == {"t1": "exact"}
    r = analyse([_no_id(bash("rg -n Foo.bar")), _no_id(bash("rg -n auth"))], labels={"": "exact"})
    assert r.agreement.n == 0 and r.agreement.unmatched == 1
    out = tmp_path / "data"
    lines = export([_no_id(bash("rg -n Foo.bar")), bash("rg -n x.y", use_id="k")]) + "\n"
    assert _run(lines, "-", "--sample", "5", out=out).returncode == 0
    ids = [row["tool_use_id"] for row in csv.DictReader((out / "label-sample.csv").open())]
    assert ids == ["k"]


def test_an_id_labelled_two_ways_is_refused(tmp_path) -> None:
    """CR 33: the last label won, silently."""
    path = tmp_path / "l.csv"
    path.write_text("tool_use_id,label\nt1,exact\nt1,exact\n")
    assert ur.read_labels(path) == {"t1": "exact"}
    path.write_text("tool_use_id,label\nt1,exact\nt1,semantic\n")
    with pytest.raises(ur.Usage):
        ur.read_labels(path)


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
    for term in ("cost_usd", "estimate", "different tasks", "Agreement", "floor", "`subagent:`"):
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


PUBLIC_TABLES = (
    "report.md",
    "counts.csv",
    "classification.csv",
    "efficiency_calls.csv",
    "efficiency_requests.csv",
)


def test_no_pattern_path_or_query_of_any_tool_reaches_the_tables(tmp_path) -> None:
    """CR 36: the term is planted in every field a tool's text can come from."""
    term = SECRET_TERM
    recs = [
        tool("Grep", None, {"pattern": term, "path": "."}, prompt="q1"),
        tool("Glob", None, {"pattern": f"**/*{term}*"}, prompt="q1"),
        _read(f"/home/exedev/index/{term}.py", prompt="q1"),
        _read(f"/home/exedev/notifier/{term}.py", prompt="q1"),
        tool("Agent", {"subagent_type": "Explore"}, {"prompt": term}, prompt="q1"),
        index_call(args={"query": term}, prompt="q1", agent="a1"),
        bash(f"git clone https://github.com/CannObserv/notifier {term}", prompt="q1"),
        api(prompt="q1"),
    ]
    out = tmp_path / "data"
    assert _run(export(recs) + "\n", "-", "--sample", "9", out=out).returncode == 0
    for name in PUBLIC_TABLES:
        assert term not in (out / name).read_text(), name


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
    assert list(csv.DictReader((out / "ambiguous.csv").open())) == []  # CR 47: resolved, gone


def test_the_sample_for_the_operator_is_seeded_and_unlabelled(tmp_path) -> None:
    out = tmp_path / "data"
    assert _run(_sample_lines(), "-", "--sample", "2", "--seed", "7", out=out).returncode == 0
    first = (out / "label-sample.csv").read_text()
    rows = list(csv.DictReader(first.splitlines()))
    assert len(rows) == 2 and all(r["label"] == "" for r in rows)
    assert stat.S_IMODE((out / "label-sample.csv").stat().st_mode) == 0o600  # CR 37
    assert {r["tool_use_id"] for r in rows} <= {"amb1", "sem1", "ex1"}
    assert _run(_sample_lines(), "-", "--sample", "2", "--seed", "7", out=out).returncode == 0
    assert (out / "label-sample.csv").read_text() == first


def test_the_seed_decides_the_draw(tmp_path) -> None:
    """CR 50: a fixed generator would pass the repeat check above."""
    lines = export([bash(f"rg -n Foo.b{i}", use_id=f"u{i:02}") for i in range(30)]) + "\n"
    draws = []
    for seed in ("1", "2"):
        out = tmp_path / seed
        assert _run(lines, "-", "--sample", "5", "--seed", seed, out=out).returncode == 0
        draws.append([r["tool_use_id"] for r in csv.DictReader((out / "label-sample.csv").open())])
    assert draws[0] != draws[1]


def test_the_report_says_how_many_ambiguous_calls_await_the_model(tmp_path) -> None:
    """CR 58: untested lines: the count awaiting the model, and a call that
    both filters and searches."""
    out = tmp_path / "data"
    assert _run(_sample_lines(), "-", out=out).returncode == 0
    assert "Ambiguous awaiting the model: 1." in (out / "report.md").read_text()


def test_a_bash_call_that_filters_and_searches_is_a_search() -> None:
    r = analyse([bash("ps aux | grep qdrant && rg -n Foo.bar")])
    [call] = r.calls
    assert call.kind == "grep_like" and call.rule == "exact"
    assert count(r, "bash_filter") == 0 and count(r, "lookalike:bash_search") == 1


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


def test_an_out_that_is_a_file_is_a_usage_error(tmp_path) -> None:
    """CR 16: it raised FileExistsError, a traceback and exit 1."""
    out = tmp_path / "data"
    out.write_text("x")
    res = _run(_sample_lines(), "-", out=out)
    assert res.returncode == 64 and "Traceback" not in res.stderr


@pytest.mark.parametrize("n", ["0", "-1"])
def test_a_sweep_threshold_below_one_is_a_usage_error(tmp_path, n) -> None:
    res = _run(_sample_lines(), "-", "--sweep-reads", n, out=tmp_path / "d")
    assert res.returncode == 64


def test_a_planted_link_in_data_is_not_written_through(tmp_path) -> None:
    """CR 17: the private writer followed a symlink at its path."""
    out = tmp_path / "data"
    out.mkdir(mode=0o700)
    target = tmp_path / "elsewhere"
    target.write_text("keep")
    (out / "ambiguous.csv").symlink_to(target)
    res = _run(_sample_lines(), "-", out=out)
    assert res.returncode == 64
    assert target.read_text() == "keep"


def test_every_table_has_its_header_when_it_has_no_rows(tmp_path) -> None:
    """CR 30: with no tool call, efficiency_calls.csv was a bare line end."""
    out = tmp_path / "data"
    assert _run(export([api()]) + "\n", "-", out=out).returncode == 0
    for name in ("efficiency_calls", "ambiguous", "counts", "classification"):
        header = (out / f"{name}.csv").read_text().splitlines()[0]
        assert header.count(",") >= 3, (name, header)


@pytest.mark.parametrize("value", ["-0.1", "1.5", "nan"])
def test_an_agreement_threshold_outside_0_to_1_is_a_usage_error(tmp_path, value) -> None:
    """CR 34: -1 made any trend established, 2 none."""
    res = _run(_sample_lines(), "-", "--agreement-threshold", value, out=tmp_path / "d")
    assert res.returncode == 64


def test_it_refuses_to_run_as_root(tmp_path, monkeypatch, capsys) -> None:
    """CR 42: documented everywhere, enforced nowhere. Root would leave
    root-owned real calls in data/; only the read goes through sudo."""
    monkeypatch.setattr(ur.os, "geteuid", lambda: 0)
    out = tmp_path / "data"
    assert ur.main(["-", "--out", str(out)]) == 64
    assert "root" in capsys.readouterr().err
    assert not out.exists()


def test_a_rerun_overwrites_a_longer_old_file(tmp_path) -> None:
    """CR 48: without truncation, old bytes would outlive a shorter rewrite."""
    out = tmp_path / "data"
    out.mkdir(mode=0o700)
    (out / "report.md").write_text("x" * 100_000)
    assert _run(_sample_lines(), "-", out=out).returncode == 0
    assert "xxxx" not in (out / "report.md").read_text()


def test_a_rerun_resets_the_modes_a_fresh_directory_never_has(tmp_path) -> None:
    """CR 49: an existing 0755 directory and 0644 file become 0700 and 0600."""
    out = tmp_path / "data"
    out.mkdir()
    out.chmod(0o755)
    (out / "report.md").write_text("old")
    (out / "report.md").chmod(0o644)
    assert _run(_sample_lines(), "-", out=out).returncode == 0
    assert stat.S_IMODE(out.stat().st_mode) == 0o700
    assert stat.S_IMODE((out / "report.md").stat().st_mode) == 0o600


@pytest.mark.parametrize("case", ["labels", "cohort", "sample", "input"])
def test_each_usage_error_exits_64_without_a_traceback(tmp_path, case) -> None:
    """CR 56: four usage errors no test ran."""
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    args = {
        "labels": ["-", "--labels", str(tmp_path / "missing.csv")],
        "cohort": ["-", "--cohort", str(bad)],
        "sample": ["-", "--sample", "0"],
        "input": [str(tmp_path / "missing.jsonl")],
    }[case]
    res = _run(_sample_lines(), *args, out=tmp_path / "data")
    assert res.returncode == 64 and "Traceback" not in res.stderr, res.stderr


def test_since_keeps_only_the_window_and_drops_the_rest_at_load() -> None:
    """CR 61: a full 90-day, ten-VM read projects to 2-3 GB of memory, on the
    store's host. A record before --since is never kept."""
    old = tool("Read", ts="2026-11-20T23:59:59.999Z")
    new = tool("Read", ts="2026-11-21T00:00:00.000Z")
    since = datetime.fromisoformat("2026-11-21T00:00:00+00:00")
    loaded = ur.load_records([export([old, new])], since=since)
    assert len(loaded.records) == 1 and loaded.before_since == 1


@pytest.mark.parametrize(("value", "code"), [("2026-11-21", 0), ("21/11/2026", 64), ("x", 64)])
def test_since_takes_a_utc_date(tmp_path, value, code) -> None:
    res = _run(_sample_lines(), "-", "--since", value, out=tmp_path / "d")
    assert res.returncode == code, res.stderr
    if code == 0:
        assert "since 2026-11-21" in (tmp_path / "d" / "report.md").read_text()


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
