"""What the collector writes, and what it never does (D21, #33).

Every cohort VM's Claude Code sends its events here, and they hold Bash
commands, paths in private repos, and whoever is signed in. The design keeps
only what its questions need, and the rest must not reach the disk: no file
contents from Edit or Write, no prompt, no identity.

**The pipeline is an allowlist at each level.** Claude Code puts its identity
attributes (``user.email``, the account and organisation IDs) on every record
and datapoint as well as on the resource, so a list of deletions at one level
misses them at another, and misses whatever a later release adds.

**It fails closed.** A record with no ``event.name`` is dropped. One with no
``tool_name`` loses its ``tool_input``. A value that isn't a scalar is
deleted whatever its key, because redaction and the cut only read strings: a
map or a list under ``tool_name`` would carry anything past both (CR 2). A
key sent twice is one key by the time it is judged.

**Attributes aren't the only way in** (CR 3). A record's severity text and
event-name field, a scope's name and version, the schema URLs, a metric's
description and metadata, and a datapoint's exemplars are all written as
sent unless something blanks them. Trace and span IDs stay: fixed-size
binary, not text.

**Redaction runs over every kept string,** not ``full_command`` alone: the
same command sits in ``tool_input`` too.

These run the pinned image with the tracked config and the run script's own
flags (``collector.py``), send OTLP/HTTP JSON as a client would, and read the
files back. Every value that must not survive carries ``DROPME``, so one scan
of the raw files covers what no single test thought to ask about. Claude
Code's wire shapes are from its documentation (2026-11-20); the pilot (#34)
holds them against a live client.
"""

import json
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from collector import Collector, running

EVENTS = "events.jsonl"
METRICS = "metrics.jsonl"
DROP = "DROPME"
LAST = "last-event-marker"

# Built here, never written whole: a scanner reads this file too.
SECRETS = {
    "ghp": "ghp_" + "a1B2c3D4e5" * 4,
    "ghs": "ghs_" + "Z9y8X7w6V5" * 4,
    "gho": "gho_" + "Q1w2E3r4T5" * 4,
    "ghu": "ghu_" + "M9n8B7v6C5" * 4,
    "ghr": "ghr_" + "L1k2J3h4G5" * 4,
    "github_pat": "github_pat_" + "11ABCDEFG0" + "_" + "q" * 60,
    "sk-ant": "sk-ant-" + "api03-" + "Tt0-_" * 12,
    "tskey": "tskey-" + "auth-" + "kABCDEF1CNTRL-" + "m" * 32,
    "api-key header": "hdr" + "S3cr3tValue" * 3,
    "bearer": "eyJ" + "hbGciOiJIUzI1NiJ9" * 3,
    "hex after key": "0123456789abcdef" * 4,
    # CR 4: the same shapes as people and tools actually write them.
    "hex in json": "fedcba9876543210" * 4,
    "hex after a longer name": "a1b2c3d4e5f60718" * 4,
    "hex after token": "0f1e2d3c4b5a6978" * 4,
    "shorter hex after secret": "9a8b7c6d5e" * 4,
    "api-key in a dict": "dictS3cr3t" + "Zz9" * 4,
    "api_key in a query": "queryS3cr3t" + "Qq7" * 3,
    "short bearer": "dv-tkn-9x7q",
    "bearer with a colon": "abcdefghijklmnopqrst" + ":" + "TAILsecretTAIL",
    "bearer in lower case": "lowerB3arer" + "Ww4" * 3,
    "bearer with no header": "bareB3arer" + "Kk2" * 3,
    "sk-ant after no name": "sk-ant-" + "api03-" + "Uu1-_" * 12,
    "basic": "dXNlcjpwYXNz" + "d29yZDEyMw==",
    "authorization token": "1122334455" * 4,
    "gh with an underscore": "ghp_" + "ab_CD" * 8,
}

IDENTITY = {
    "user.email": f"{DROP}-dev@example.test",
    "user.account_uuid": f"{DROP}-account-uuid",
    "user.account_id": f"{DROP}-account-id",
    "organization.id": f"{DROP}-organisation",
    "user.id": f"{DROP}-anonymous-id",
    "terminal.type": f"{DROP}-terminal",
}
RESOURCE = {
    "service.name": f"{DROP}-claude-code",
    "os.type": f"{DROP}-linux",
    "host.arch": f"{DROP}-amd64",
    "cohort.host": "co-test",
    "vcs.repository.name": "index",
    "vcs.repository.url.full": f"https://example.test/{DROP}/index",
    "app.version": "2.1.329",
    **IDENTITY,
}
COMMON = {
    "session.id": "session-1",
    "prompt.id": "prompt-1",
    "event.timestamp": "2026-11-20T17:00:00.000Z",
    "event.sequence": 7,
    "cohort.host": "co-test",
    "vcs.repository.name": "index",
    "app.version": "2.1.329",
    "workspace.host_paths": f"/home/{DROP}/index",
    **IDENTITY,
}


def _value(value: object) -> dict:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, dict):
        return {"kvlistValue": {"values": _attributes(value)}}
    return {"stringValue": value}


def _attributes(attributes: dict) -> list[dict]:
    return [{"key": k, "value": _value(v)} for k, v in attributes.items()]


def _logs(*records: dict, resource: dict = RESOURCE) -> dict:
    return {
        "resourceLogs": [
            {
                "resource": {"attributes": _attributes(resource)},
                "schemaUrl": f"https://example.test/{DROP}-resource-schema",
                "scopeLogs": [
                    {
                        "scope": {
                            "name": f"{DROP}-scope-name",
                            "version": f"{DROP}-scope-version",
                            "attributes": _attributes({"scope.note": f"{DROP}-scope"}),
                        },
                        "schemaUrl": f"https://example.test/{DROP}-scope-schema",
                        "logRecords": list(records),
                    }
                ],
            }
        ]
    }


def _record(attributes: dict, body: str | None = None) -> dict:
    name = attributes.get("event.name", "unnamed")
    return {
        "timeUnixNano": "1795280400000000000",
        "severityText": f"{DROP}-severity",
        "eventName": f"{DROP}-event-name-field",
        "body": {"stringValue": body if body is not None else f"claude_code.{name}"},
        "attributes": _attributes(attributes),
    }


def _raw_record(*pairs: tuple[str, dict]) -> dict:
    """A record from raw key and value pairs: a key twice, or a value no
    honest client sends."""
    record = _record({})
    record["attributes"] = [{"key": k, "value": v} for k, v in pairs]
    return record


def _s(text: str) -> dict:
    return {"stringValue": text}


def _tool_result(tool_name: str | None, tool_use_id: str, **extra: object) -> dict:
    attributes = {
        **COMMON,
        "event.name": "tool_result",
        "tool_use_id": tool_use_id,
        "request_id": "req_011",
        "success": "true",
        "duration_ms": 12,
        "decision_type": "accept",
        "decision_source": "config",
        "tool_result_size_bytes": 345,
        "tool_input_size_bytes": 67,
        "agent_id": "a3f2c1b4d5e6f7a80",
        "parent_agent_id": "b3f2c1b4d5e6f7a80",
        **extra,
    }
    if tool_name is not None:
        attributes["tool_name"] = tool_name
    return _record(attributes)


def _json(**fields: object) -> str:
    """As Claude Code serialises ``tool_input`` and ``tool_parameters``."""
    return json.dumps(fields, separators=(",", ":"))


SOCRATICODE = "mcp__plugin_socraticode_socraticode__codebase_search"
SOCRATICODE_STANDALONE = "mcp__socraticode__codebase_search"
KEPT_INPUT = {
    "Grep": _json(pattern="def keep_grep", path="deploy/"),
    "Glob": _json(pattern="**/keep_glob*.py"),
    "Bash": _json(command="rg -n keep_bash deploy/", description="search"),
    "Agent": _json(subagent_type="Explore", prompt="find keep_agent"),
    "Task": _json(subagent_type="Explore", prompt="find keep_task"),
    SOCRATICODE: _json(query="keep_socraticode", includeLinked=True),
    SOCRATICODE_STANDALONE: _json(query="keep_standalone"),
}
DROPPED_INPUT = {
    "Edit": _json(file_path="/x.py", old_string=f"{DROP}-old", new_string=f"{DROP}-new"),
    "Write": _json(file_path="/x.py", content=f"{DROP}-file-content"),
    "NotebookEdit": _json(notebook_path="/n.ipynb", new_source=f"{DROP}-cell"),
    "WebFetch": _json(url=f"https://example.test/{DROP}", prompt=f"{DROP}-prompt"),
    "mcp__github__search_code": _json(query=f"{DROP}-other-mcp"),
    # Named so a loose match on "socraticode" would keep it.
    "mcp__not_socraticode__thing": _json(query=f"{DROP}-lookalike"),
    # Real tools whose names hold a listed one: the list is anchored.
    "TaskCreate": _json(subject=f"{DROP}-task-create"),
    "BashOutput": _json(bash_id=f"{DROP}-bash-output"),
    "NotebookRead": _json(notebook_path=f"/{DROP}/n.ipynb"),
    "ReadMcpResourceTool": _json(uri=f"{DROP}-resource"),
    "Skill": _json(skill="x", args=f"{DROP}-skill-args"),
}
#: Tools whose ``tool_parameters`` the documentation fills: Bash (the
#: command), Agent (its type), Skill (its name) and MCP tools (server, tool).
PARAMETERS_KEPT = {"Bash", "Agent", "Task", "Skill"}


def _keeps_parameters(tool: str) -> bool:
    return tool in PARAMETERS_KEPT or tool.startswith("mcp__")


BASH_SECRET_COMMAND = " ; ".join(
    [
        f"git clone https://x-access-token:{SECRETS['ghp']}@github.com/CannObserv/keep-clone-url",
        f"GH_TOKEN={SECRETS['ghs']} gh api repos/CannObserv/keep-gh-api/contents/x",
        f"echo {SECRETS['gho']} {SECRETS['ghu']} {SECRETS['ghr']}",
        f"echo {SECRETS['github_pat']}",
        f"ANTHROPIC_API_KEY={SECRETS['sk-ant']} claude -p hi",
        f"tailscale up --auth-key={SECRETS['tskey']}",
        f'curl -H "api-key: {SECRETS["api-key header"]}" https://index.example.test:6333/keep-url',
        f"curl -H 'X-API-Key: {SECRETS['api-key header']}' http://status:9000/keep-status",
        f'curl -H "Authorization: Bearer {SECRETS["bearer"]}" https://api.example.test/keep-bearer',
        f"QDRANT_API_KEY={SECRETS['hex after key']} run-keep-hex",
        'curl -d "{\\"api_key\\": \\"'
        + SECRETS["hex in json"]
        + '\\"}" https://x.example.test/keep-json',
        f"QDRANT_KEY_RO={SECRETS['hex after a longer name']} run-keep-ro",
        f"STATUS_TOKEN={SECRETS['hex after token']} run-keep-token",
        f"CLIENT_SECRET={SECRETS['shorter hex after secret']} run-keep-secret",
        'python3 -c \'requests.get(u, headers={"api-key": "'
        + SECRETS["api-key in a dict"]
        + "\"})'",
        f'curl "https://a.example.test/keep-query?api_key={SECRETS["api_key in a query"]}&page=2"',
        f'curl -H "Authorization: Bearer {SECRETS["short bearer"]}" https://a.example.test/keep-short',
        f'curl -H "Authorization: Bearer {SECRETS["bearer with a colon"]}" https://a.example.test/keep-colon',
        f"curl -H 'authorization:bearer {SECRETS['bearer in lower case']}' u",
        f"curl --oauth2-bearer {SECRETS['bearer with no header']} https://a.example.test/keep-bare",
        f"echo {SECRETS['sk-ant after no name']}",
        f'curl -H "Authorization: Basic {SECRETS["basic"]}" https://a.example.test/keep-basic',
        f'curl -H "Authorization: token {SECRETS["authorization token"]}" https://a.example.test/keep-token',
        f"echo {SECRETS['gh with an underscore']}",
    ]
)


def _corpus() -> list[tuple[str, dict]]:
    """Every export the module sends, in order: ``(signal, body)``."""
    kept_tools = [
        _tool_result(tool, f"keep-{n}", tool_input=value, tool_parameters=_json(mcp_tool_name="x"))
        for n, (tool, value) in enumerate(KEPT_INPUT.items())
    ]
    dropped_tools = [
        _tool_result(
            tool,
            f"drop-{n}",
            tool_input=value,
            tool_parameters=_json(note="kept" if _keeps_parameters(tool) else f"{DROP}-parameters"),
        )
        for n, (tool, value) in enumerate(DROPPED_INPUT.items())
    ]
    exports = [
        ("logs", _logs(*kept_tools)),
        ("logs", _logs(*dropped_tools)),
        (
            "logs",
            _logs(
                _tool_result(
                    "Read",
                    "read-1",
                    tool_input=_json(
                        file_path='/home/x/keep "quoted" read.py', offset=10, limit=20
                    ),
                ),
                _tool_result("Read", "read-2", tool_input=f"{DROP}-not-the-documented-shape"),
                _tool_result(None, "nameless-1", tool_input=_json(content=f"{DROP}-nameless")),
                _tool_result("Bash", "structured-1", tool_input={"command": f"{DROP}-structured"}),
                _tool_result("Bash", "structured-2", tool_parameters={"x": f"{DROP}-structured"}),
                _tool_result("Bash", "structured-3", tool_input=12345678, tool_parameters=True),
                # What a client without OTEL_LOG_TOOL_DETAILS sends: no input at all.
                _tool_result("Read", "read-3"),
                _tool_result(
                    "Bash",
                    "failed-1",
                    success="false",
                    error_type="ShellError",
                    error=f"{DROP}-full error text with a path",
                    mcp_server_scope="project",
                    **{"vcs.ref.head.name": f"{DROP}-branch", "new.attribute": f"{DROP}-unlisted"},
                ),
                _tool_result(
                    "Bash",
                    "secret-1",
                    tool_parameters=_json(bash_command="git", full_command=BASH_SECRET_COMMAND),
                    tool_input=_json(command=BASH_SECRET_COMMAND),
                ),
                _raw_record(
                    ("event.name", _s("tool_result")),
                    ("tool_name", _s("Bash")),
                    ("tool_use_id", _s("typed-1")),
                    ("error_type", _value({"nested": f"{DROP}-map {SECRETS['ghp']}"})),
                    ("request_id", {"arrayValue": {"values": [_s(f"{DROP}-list")]}}),
                    ("session.id", {"arrayValue": {"values": []}}),
                ),
                _raw_record(
                    ("event.name", _s("tool_result")),
                    ("tool_name", _s("Bash")),
                    ("tool_use_id", _s("twice-1")),
                    ("tool_input", _s(_json(command="first"))),
                    ("tool_input", _value({"command": f"{DROP}-second-is-a-map"})),
                ),
                _raw_record(
                    ("event.name", _s("tool_result")),
                    ("tool_name", _s("Write")),
                    ("tool_use_id", _s("twice-2")),
                    ("tool_input", _s(f"{DROP}-first")),
                    ("tool_input", _s(f"{DROP}-second")),
                ),
                _tool_result(
                    "Bash",
                    "long-1",
                    tool_parameters=_json(full_command="cat > f <<EOF " + "k" * 9000 + DROP),
                ),
            ),
        ),
        (
            "logs",
            _logs(
                _record(
                    {
                        **COMMON,
                        "event.name": "api_request",
                        "model": "claude-fable-5-2",
                        "cost_usd": 0.0123,
                        "duration_ms": 2345,
                        "input_tokens": 11,
                        "output_tokens": 22,
                        "cache_read_tokens": 33,
                        "cache_creation_tokens": 44,
                        "cache_creation_5m_tokens": 40,
                        "cache_creation_1h_tokens": 4,
                        "request_id": "req_011",
                        "agent_id": "a3f2c1b4d5e6f7a80",
                        "client_request_id": f"{DROP}-client-request",
                        "query_source": f"{DROP}-query-source",
                        "tool_parameters": f"{DROP}-not-an-api-request-field",
                        "tool_input": f"{DROP}-nor-this",
                    }
                ),
                _record(
                    {
                        **COMMON,
                        "event.name": "tool_result",
                        "tool_name": "Grep",
                        "tool_use_id": "b",
                    },
                    body=f"{DROP}-body",
                ),
            ),
        ),
        (
            "logs",
            _logs(
                _record({**COMMON, "event.name": "user_prompt", "prompt": f"{DROP}-prompt-text"}),
                _record(
                    {**COMMON, "event.name": "assistant_response", "response": f"{DROP}-reply"}
                ),
                _record({**COMMON, "event.name": "api_error", "error": f"{DROP}-api-error"}),
                _record({**COMMON, "event.name": "tool_decision", "tool_name": f"{DROP}-decision"}),
                _record({**COMMON, "event.name": "api_request_body", "body": f"{DROP}-raw-body"}),
                _record(
                    {**COMMON, "event.name": "claude_code.tool_result", "x": f"{DROP}-prefixed"}
                ),
                _record({**COMMON, "note": f"{DROP}-no-event-name"}),
            ),
        ),
        ("metrics", _metrics()),
        ("metrics", _odd_metrics()),
        (
            "logs",
            _logs(
                _tool_result("Grep", "odd-resource"),
                resource={
                    "cohort.host": {"nested": f"{DROP}-resource-map"},
                    "app.version": "v" * 5000,
                },
            ),
        ),
        ("logs", _logs(_tool_result("Grep", LAST))),
    ]
    return exports


def _sum(name: str, value: float, exemplar: bool = True, **attributes: object) -> dict:
    metric = _sum_with_exemplar(name, value, **attributes)
    if not exemplar:
        del metric["sum"]["dataPoints"][0]["exemplars"]
    return metric


def _sum_with_exemplar(name: str, value: float, **attributes: object) -> dict:
    return {
        "name": name,
        "unit": "s",
        "description": f"{DROP}-description",
        "metadata": _attributes({"note": f"{DROP}-metadata"}),
        "sum": {
            "aggregationTemporality": 1,
            "isMonotonic": True,
            "dataPoints": [
                {
                    "asDouble": value,
                    "timeUnixNano": "1795280400000000000",
                    "attributes": _attributes({**COMMON, **attributes}),
                    "exemplars": [
                        {
                            "asDouble": value,
                            "timeUnixNano": "1795280400000000000",
                            "filteredAttributes": _attributes({"user.email": f"{DROP}-exemplar"}),
                        }
                    ],
                }
            ],
        },
    }


def _metrics() -> dict:
    return {
        "resourceMetrics": [
            {
                "resource": {"attributes": _attributes(RESOURCE)},
                "scopeMetrics": [
                    {
                        "scope": {
                            "name": f"{DROP}-meter-name",
                            "attributes": _attributes({"scope.note": f"{DROP}-meter"}),
                        },
                        "schemaUrl": f"https://example.test/{DROP}-meter-schema",
                        "metrics": [
                            _sum(
                                "claude_code.active_time.total", 42.5, exemplar=False, type="user"
                            ),
                            # The same metric, its point carrying an exemplar.
                            _sum("claude_code.active_time.total", 777.5, type="cli"),
                            {
                                # The kept name, as another type: its points
                                # carry fields a sum's don't.
                                "name": "claude_code.active_time.total",
                                "histogram": {
                                    "aggregationTemporality": 1,
                                    "dataPoints": [
                                        {"count": "1", "sum": 987654.5, "timeUnixNano": "1"}
                                    ],
                                },
                            },
                            _sum("claude_code.cost.usage", 1.5, model=f"{DROP}-cost"),
                            _sum("claude_code.token.usage", 99, type=f"{DROP}-tokens"),
                            _sum("claude_code.session.count", 1, start_type=f"{DROP}-session"),
                            _sum("claude_code.lines_of_code.count", 3, type=f"{DROP}-lines"),
                        ],
                    }
                ],
            }
        ]
    }


def _odd_metrics() -> dict:
    """The kept metric, with a map where a scalar belongs: on the resource,
    and on the point."""
    point = _sum("claude_code.active_time.total", 5.25, exemplar=False)
    point["sum"]["dataPoints"][0]["attributes"] = _attributes(
        {"type": {"nested": f"{DROP}-point-map"}, "session.id": "session-odd"}
    )
    return {
        "resourceMetrics": [
            {
                "resource": {
                    "attributes": _attributes({"cohort.host": {"nested": f"{DROP}-resource-map"}})
                },
                "scopeMetrics": [{"scope": {}, "metrics": [point]}],
            }
        ]
    }


@pytest.fixture(scope="module")
def collector(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Collector]:
    with running(tmp_path_factory.mktemp("otelcol")) as started:
        for signal, body in _corpus():
            status, answer = started.post(signal, body)
            assert status == 200, f"{signal}: {status} {answer} {started.logs()[-2000:]}"
        started.wait_for(EVENTS, LAST)
        started.wait_for(METRICS, "claude_code.active_time.total")
        yield started


def _plain(value: dict) -> object:
    [(kind, inner)] = value.items()
    if kind == "intValue":
        return int(inner)
    if kind == "kvlistValue":
        return {kv["key"]: _plain(kv["value"]) for kv in inner.get("values", [])}
    return inner


def _map(attributes: list[dict]) -> dict:
    return {kv["key"]: _plain(kv["value"]) for kv in attributes}


def _records(collector: Collector) -> dict[str, dict]:
    """Every written log record's attributes, by ``tool_use_id`` (or, for an
    ``api_request``, by its event name)."""
    found = {}
    for line in collector.lines(EVENTS):
        for resource in line["resourceLogs"]:
            for scope in resource["scopeLogs"]:
                for record in scope["logRecords"]:
                    attributes = _map(record.get("attributes", []))
                    key = attributes.get("tool_use_id", attributes.get("event.name"))
                    found[key] = {"attributes": attributes, "body": _plain(record["body"])}
    return found


def test_a_kept_event_is_written(collector: Collector) -> None:
    """The control: every "absent" below is about a pipeline that writes."""
    assert LAST in _records(collector)


def test_nothing_marked_for_dropping_reaches_the_disk(collector: Collector) -> None:
    """One scan of the raw files: identities, contents, prompts, bodies, the
    scope's and the resource's attributes, and every dropped event."""
    for name in collector.files():
        raw = collector.raw(name)
        at = raw.find(DROP)
        assert at == -1, f"{name}: …{raw[max(0, at - 160) : at + 60]}…"


def test_only_the_two_events_are_kept(collector: Collector) -> None:
    names = {r["attributes"]["event.name"] for r in _records(collector).values()}
    assert names == {"tool_result", "api_request"}


def test_a_tool_result_keeps_its_ungated_fields(collector: Collector) -> None:
    attributes = _records(collector)["failed-1"]["attributes"]
    assert attributes == {
        "event.name": "tool_result",
        "event.timestamp": "2026-11-20T17:00:00.000Z",
        "event.sequence": 7,
        "session.id": "session-1",
        "prompt.id": "prompt-1",
        "cohort.host": "co-test",
        "vcs.repository.name": "index",
        "app.version": "2.1.329",
        "tool_name": "Bash",
        "tool_use_id": "failed-1",
        "request_id": "req_011",
        "success": "false",
        "duration_ms": 12,
        "error_type": "ShellError",
        "decision_type": "accept",
        "decision_source": "config",
        "tool_result_size_bytes": 345,
        "tool_input_size_bytes": 67,
        "mcp_server_scope": "project",
        "agent_id": "a3f2c1b4d5e6f7a80",
        "parent_agent_id": "b3f2c1b4d5e6f7a80",
    }


def test_an_api_request_keeps_tokens_cost_and_its_keys(collector: Collector) -> None:
    assert _records(collector)["api_request"]["attributes"] == {
        "event.name": "api_request",
        "event.timestamp": "2026-11-20T17:00:00.000Z",
        "event.sequence": 7,
        "session.id": "session-1",
        "prompt.id": "prompt-1",
        "cohort.host": "co-test",
        "vcs.repository.name": "index",
        "app.version": "2.1.329",
        "model": "claude-fable-5-2",
        "cost_usd": 0.0123,
        "duration_ms": 2345,
        "input_tokens": 11,
        "output_tokens": 22,
        "cache_read_tokens": 33,
        "cache_creation_tokens": 44,
        "cache_creation_5m_tokens": 40,
        "cache_creation_1h_tokens": 4,
        "request_id": "req_011",
        "agent_id": "a3f2c1b4d5e6f7a80",
    }


@pytest.mark.parametrize("n", range(len(KEPT_INPUT)), ids=list(KEPT_INPUT))
def test_tool_input_is_kept_for_a_listed_tool(collector: Collector, n: int) -> None:
    tool, sent = list(KEPT_INPUT.items())[n]
    attributes = _records(collector)[f"keep-{n}"]["attributes"]
    assert attributes["tool_name"] == tool
    assert attributes["tool_input"] == sent
    assert ("tool_parameters" in attributes) is _keeps_parameters(tool)


@pytest.mark.parametrize("n", range(len(DROPPED_INPUT)), ids=list(DROPPED_INPUT))
def test_tool_input_is_dropped_for_any_other_tool(collector: Collector, n: int) -> None:
    """The record stays, for its sizes and duration. Its input goes."""
    attributes = _records(collector)[f"drop-{n}"]["attributes"]
    assert "tool_input" not in attributes
    assert attributes["tool_result_size_bytes"] == 345


@pytest.mark.parametrize("n", range(len(DROPPED_INPUT)), ids=list(DROPPED_INPUT))
def test_tool_parameters_stay_only_where_the_documentation_fills_them(
    collector: Collector, n: int
) -> None:
    """``tool_input`` is gated by tool, and this was not: a release that
    gave Edit or Write a parameter would have put it on disk (CR 5)."""
    tool = list(DROPPED_INPUT)[n]
    attributes = _records(collector)[f"drop-{n}"]["attributes"]
    if _keeps_parameters(tool):
        assert attributes["tool_parameters"] == _json(note="kept")
    else:
        assert "tool_parameters" not in attributes


def test_a_read_keeps_its_path_alone(collector: Collector) -> None:
    attributes = _records(collector)["read-1"]["attributes"]
    assert json.loads(attributes["tool_input"]) == {"file_path": '/home/x/keep "quoted" read.py'}


def test_a_read_in_another_shape_loses_its_input(collector: Collector) -> None:
    assert "tool_input" not in _records(collector)["read-2"]["attributes"]


def test_a_record_with_no_tool_name_loses_its_input(collector: Collector) -> None:
    """A missing name must not read as "listed": the match would error, and
    an ignored error would keep the input."""
    assert "tool_input" not in _records(collector)["nameless-1"]["attributes"]


@pytest.mark.parametrize("record", ["structured-1", "structured-2", "structured-3"])
def test_an_input_that_is_not_a_string_is_dropped(collector: Collector, record: str) -> None:
    """Both are JSON text or nothing: a map, a number, a boolean."""
    attributes = _records(collector)[record]["attributes"]
    assert "tool_input" not in attributes and "tool_parameters" not in attributes


def test_a_read_with_no_input_is_kept(collector: Collector) -> None:
    """Under ``propagate`` a statement that erred on the missing value would
    lose every batch holding a Read."""
    attributes = _records(collector)["read-3"]["attributes"]
    assert attributes["tool_name"] == "Read" and "tool_input" not in attributes


def test_the_body_is_the_event_name(collector: Collector) -> None:
    assert _records(collector)["b"]["body"] == "claude_code.tool_result"


def test_the_resource_keeps_host_repository_and_version(collector: Collector) -> None:
    resources = [
        _map(resource["resource"].get("attributes", []))
        for name, key in ((EVENTS, "resourceLogs"), (METRICS, "resourceMetrics"))
        for line in collector.lines(name)
        for resource in line[key]
    ]
    wanted = {"cohort.host": "co-test", "vcs.repository.name": "index", "app.version": "2.1.329"}
    odd = {"app.version": "v" * 4096}  # its map gone, its long value cut
    assert wanted in resources and odd in resources and {} in resources
    assert all(r in (wanted, odd, {}) for r in resources)


def test_a_value_that_is_not_a_scalar_goes_whatever_its_key(collector: Collector) -> None:
    """Redaction and the cut read strings only (CR 2)."""
    assert _records(collector)["typed-1"]["attributes"] == {
        "event.name": "tool_result",
        "tool_name": "Bash",
        "tool_use_id": "typed-1",
    }


def test_a_key_sent_twice_is_judged_once(collector: Collector) -> None:
    """The checks read a key's first value and delete its first entry. Sent
    twice, the second entry was written unchecked (CR 2)."""
    records = _records(collector)
    assert records["twice-1"]["attributes"]["tool_input"] == _json(command="first")
    assert "tool_input" not in records["twice-2"]["attributes"]
    raw = collector.raw(EVENTS)
    assert raw.count("twice-1") == 1 and raw.count("twice-2") == 1


def test_nothing_rides_outside_the_attributes(collector: Collector) -> None:
    """Severity, the event-name field, the scope and the schema URLs (CR 3)."""
    for line in collector.lines(EVENTS):
        for resource in line["resourceLogs"]:
            assert not resource.get("schemaUrl")
            for scope in resource["scopeLogs"]:
                assert not scope.get("schemaUrl")
                assert scope.get("scope", {}) == {}
                for record in scope["logRecords"]:
                    assert set(record) <= {
                        "timeUnixNano",
                        "observedTimeUnixNano",
                        "body",
                        "attributes",
                    }


def test_only_active_time_is_kept_of_the_metrics(collector: Collector) -> None:
    metrics = [
        metric
        for line in collector.lines(METRICS)
        for resource in line["resourceMetrics"]
        for scope in resource["scopeMetrics"]
        for metric in scope["metrics"]
    ]
    assert [m["name"] for m in metrics] == ["claude_code.active_time.total"] * 2
    [odd] = metrics[1]["sum"]["dataPoints"]
    assert (odd["asDouble"], _map(odd["attributes"])) == (5.25, {"session.id": "session-odd"})
    assert set(metrics[0]) == {"name", "unit", "sum"}, "its description and metadata are gone"
    [point] = metrics[0]["sum"]["dataPoints"]
    assert set(point) == {"attributes", "timeUnixNano", "asDouble"}
    assert point["asDouble"] == 42.5
    assert _map(point["attributes"]) == {
        "type": "user",
        "session.id": "session-1",
        "cohort.host": "co-test",
        "vcs.repository.name": "index",
        "app.version": "2.1.329",
    }


@pytest.mark.parametrize("shape", sorted(SECRETS))
def test_a_planted_token_is_masked(collector: Collector, shape: str) -> None:
    """In ``tool_parameters`` and in ``tool_input``: the whole file is read."""
    raw = collector.raw(EVENTS)
    assert "keep-clone-url" in raw, "the control: the command itself was written"
    assert SECRETS[shape] not in raw
    # No tail of it either: a mask that stops early leaves most of a token.
    assert SECRETS[shape][-12:] not in raw


def test_a_masked_command_keeps_what_the_report_reads(collector: Collector) -> None:
    """``full_command`` is kept for the URL in a clone or a ``gh api`` call."""
    parameters = json.loads(_records(collector)["secret-1"]["attributes"]["tool_parameters"])
    command = parameters["full_command"]
    for kept in (
        "github.com/CannObserv/keep-clone-url",
        "gh api repos/CannObserv/keep-gh-api/contents/x",
        "https://index.example.test:6333/keep-url",
        "http://status:9000/keep-status",
        "https://api.example.test/keep-bearer",
        "run-keep-hex",
        "https://x.example.test/keep-json",
        "run-keep-secret",
        "keep-query?api_key=[REDACTED]&page=2",
        "https://a.example.test/keep-short",
        "https://a.example.test/keep-colon",
        "https://a.example.test/keep-basic",
        "https://a.example.test/keep-token",
        "[REDACTED]",
    ):
        assert kept in command
    assert json.loads(_records(collector)["secret-1"]["attributes"]["tool_input"])["command"]


def test_a_long_value_is_cut(collector: Collector) -> None:
    """``full_command`` arrives untruncated, and a heredoc is file contents
    by another route. 4096 bytes keep the command and bound the rest."""
    parameters = _records(collector)["long-1"]["attributes"]["tool_parameters"]
    assert len(parameters) == 4096
    assert parameters.startswith('{"full_command":"cat > f <<EOF kkk')


def test_an_empty_export_writes_nothing(collector: Collector) -> None:
    """It is the knob's ``health`` line and the check-in's probe, so it must
    be safe at any time (knob.md): answered, and no line written."""
    before = {name: collector.raw(name) for name in collector.files()}
    assert collector.post("logs", {})[0] == 200
    assert collector.post("metrics", {})[0] == 200
    assert {name: collector.raw(name) for name in collector.files()} == before


def test_traces_have_no_pipeline(collector: Collector) -> None:
    """Spans carry tool input and output; nothing here asks for them."""
    assert collector.post("traces", {})[0] == 404


def test_a_full_client_batch_is_written(tmp_path: Path) -> None:
    """Under rotation the exporter refuses one export larger than a file.
    Claude Code sends up to 512 records at once, and a Bash record at the
    4096 cut is about 9.5 kB: 4.9 MB, which a 4 MB file refused whole
    (CR 6)."""
    full = _json(full_command="y" * 5000)
    batch = [
        _tool_result("Bash", f"full-{n}", tool_parameters=full, tool_input=full) for n in range(512)
    ]
    with running(tmp_path) as started:
        status, answer = started.post("logs", _logs(*batch))
        assert status == 200, answer
        started.wait_for(EVENTS, "full-511")
        assert len(started.raw(EVENTS)) > 4_500_000


def test_the_exporter_leaves_the_archive_alone(tmp_path: Path) -> None:
    """``otelcol-retention.sh`` finds rotated files by ``*-size.jsonl``, the
    exporter's naming on 0.165.0, and compresses them into ``archive/``.

    Not beside them: the exporter's rotation library counts any
    ``<signal>-<UTC>-size.jsonl.zst`` in its own directory as one of its
    backups, and deletes all but the newest ``max_backups``. Measured on
    0.165.0, with 20 such files planted and two rotations: 14 were left. The
    archive would have been a fortnight, not 90 days (CR 1).
    """
    archived = [
        f"archive/events-2026-10-{day:02d}T00-00-00.000-size.jsonl.zst" for day in range(1, 21)
    ]
    with running(tmp_path, planted=dict.fromkeys(archived, "compressed earlier")) as started:
        filler = _json(full_command="x" * 5000)  # cut to 4096 on the way in
        batch = [_tool_result("Bash", f"fill-{n}", tool_parameters=filler) for n in range(200)]
        for _ in range(30):  # about 1 MB each: two rotations at least
            assert started.post("logs", _logs(*batch))[0] == 200
        started.post("logs", _logs(_tool_result("Grep", LAST)))
        started.wait_for(EVENTS, LAST)
        time.sleep(3)  # the library prunes in the background, after a rotation
        names = started.files()
        rotated = [n for n in names if n.startswith("events-") and n.endswith("-size.jsonl")]
        assert len(rotated) >= 2, names
        assert sorted(started.files("archive")) == sorted(a.split("/")[1] for a in archived)
