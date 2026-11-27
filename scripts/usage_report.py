#!/usr/bin/env python3
"""#36: how the cohort's agents use the index, from the collector's records.

Reads the collector's OTLP/JSON lines (D21): files, or ``-`` for stdin. They
are root-only, so the read happens through sudo at the call and this script
never runs as root:

  sudo sh -c 'zstd -dcq /var/lib/otelcol/archive/events-*.jsonl.zst;
              cat /var/lib/otelcol/events-*-size.jsonl /var/lib/otelcol/events.jsonl' \\
    2>/dev/null | uv run scripts/usage_report.py -

Writes, to git-ignored ``data/`` (``0700``, each file ``0600``; regenerating
overwrites):

- ``report.md``: the summary. Counts, shapes and timings only;
- ``counts.csv``: per host, repo and period (split at docs/usage-markers.json's
  reach): index calls by tool, includeLinked, look-alikes, sibling access and
  follow-ups;
- ``classification.csv``: grep-like calls by class, by rules and final;
- ``efficiency_calls.csv``, ``efficiency_requests.csv``;
- ``ambiguous.csv``: the calls the rules leave ambiguous, with their
  ``prompt.id`` siblings, for the session's model. It writes its labels to
  ``ambiguous-labels.csv`` (``tool_use_id,label``), which the next run folds in;
- ``label-sample.csv`` with ``--sample N``: grep-like calls drawn by seed, for
  the operator (#37), who saves them labelled as ``labels.csv``.

The last two hold command and query text: they never leave ``data/``.
Labels are ``exact`` or ``semantic``. Exits 0, 1 when no record was read, 64
on a usage error. Stdlib only.
"""

import argparse
import csv
import json
import math
import os
import random
import re
import shlex
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SOCRATICODE_SERVERS = {"plugin_socraticode_socraticode", "socraticode"}
# #33's proof record on co-index: not a session's.
EXCLUDED_SESSIONS = {"host-proof-33"}
LABELS = ("exact", "semantic")
CLASSES = ("exact", "semantic", "ambiguous")
SWEEP_READS = 3
SUBAGENT = "subagent:"  # the prefix of a measure counting subagents' calls
THRESHOLD = 0.80


class Usage(Exception):
    pass


# --- records -----------------------------------------------------------------


@dataclass
class Record:
    event: str
    host: str
    repo: str
    session: str
    prompt: str
    seq: int
    ts: datetime
    attrs: dict

    def num(self, key: str) -> float | int | None:
        """2.1.330 sends a tool_result's numbers as strings, an api_request's as numbers."""
        value = self.attrs.get(key)
        if value is None or value == "":
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number):
            return None
        return int(number) if number.is_integer() and "." not in str(value) else number

    def get(self, key: str) -> str | None:
        value = self.attrs.get(key)
        return None if value is None else str(value)


@dataclass
class Loaded:
    records: list[Record] = field(default_factory=list)
    lines: int = 0
    bad_lines: int = 0
    bad_records: int = 0
    excluded: int = 0
    duplicates: int = 0


def _value(v: dict):
    for kind in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if kind in v:
            return v[kind]
    return None


def _attrs(items) -> dict:
    return {a["key"]: _value(a.get("value", {})) for a in items or [] if "key" in a}


def _when(text: str | None, nanos) -> datetime:
    if text:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    return datetime.fromtimestamp(int(nanos or 0) / 1e9, tz=UTC)


def load_records(lines) -> Loaded:
    out = Loaded()
    seen = set()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        out.lines += 1
        try:
            doc = json.loads(line)
            resource_logs = doc["resourceLogs"]
            if not isinstance(resource_logs, list):
                raise TypeError("resourceLogs is no list")
        except (ValueError, KeyError, TypeError):
            out.bad_lines += 1
            continue
        for rl in resource_logs:
            resource = _attrs(rl.get("resource", {}).get("attributes"))
            for sl in rl.get("scopeLogs", []):
                for lr in sl.get("logRecords", []):
                    attrs = _attrs(lr.get("attributes"))
                    event = attrs.get("event.name")
                    if event not in ("tool_result", "api_request"):
                        continue
                    session = str(attrs.get("session.id", ""))
                    if session in EXCLUDED_SESSIONS:
                        out.excluded += 1
                        continue
                    # event.sequence alone isn't unique: two distinct records
                    # shared it on co-index (2026-11-27). The id is.
                    key = (
                        session,
                        event,
                        str(attrs.get("event.sequence")),
                        str(
                            attrs.get("tool_use_id")
                            or attrs.get("request_id")
                            or lr.get("timeUnixNano")
                        ),
                    )
                    if key in seen:
                        out.duplicates += 1
                        continue
                    try:
                        record = Record(
                            event=event,
                            host=str(
                                resource.get("cohort.host") or attrs.get("cohort.host") or "?"
                            ),
                            repo=str(attrs.get("vcs.repository.name") or "?"),
                            session=session,
                            prompt=str(attrs.get("prompt.id") or ""),
                            seq=int(attrs.get("event.sequence") or 0),
                            ts=_when(attrs.get("event.timestamp"), lr.get("timeUnixNano")),
                            attrs=attrs,
                        )
                    except (ValueError, TypeError):
                        out.bad_records += 1
                        continue
                    seen.add(key)
                    out.records.append(record)
    return out


def _json(text: str | None):
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


# --- markers -----------------------------------------------------------------


def _aware(text: str) -> datetime:
    when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if when.tzinfo is None:
        raise ValueError(f"{text!r} has no zone: end it Z")
    return when


def load_markers(path: Path) -> list[dict]:
    try:
        doc = json.loads(Path(path).read_text())
        markers = doc["markers"]
        for m in markers:
            _aware(m["date"])
            for reach in m["reach"]:
                if len({"repo", "host"} & set(reach)) != 1:
                    raise ValueError(f"{m['id']}: a reach names one repo or one host")
                _aware(reach["at"])
            str(m["id"])
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise Usage(f"markers {path}: {exc}") from exc
    return markers


def _boundaries(markers, host: str, repo: str) -> list[tuple[datetime, str]]:
    out = []
    for m in markers:
        for reach in m["reach"]:
            if reach.get("host") == host or reach.get("repo") == repo:
                out.append((datetime.fromisoformat(reach["at"].replace("Z", "+00:00")), m["id"]))
    return sorted(out)


def _period(bounds, ts: datetime) -> str:
    if not bounds:
        return "unsplit"
    label = f"before {bounds[0][1]}"
    for at, mid in bounds:
        if ts >= at:
            label = f"from {mid}"
    return label


# --- Bash: searches ----------------------------------------------------------


@dataclass
class Search:
    bin: str
    pattern: str | None
    filter: bool = False
    fixed: bool = False
    word: bool = False
    globs: list = field(default_factory=list)  # find's -name/-path patterns


_SEPARATORS = {";", "&&", "||", "&", "\n", "(", ")", "{", "}", ";;"}
_REDIRECTS = {">", ">>", "<", ">&", "<&", "&>", "&>>", ">|", "<<<", "<>"}
_PREFIXES = {"sudo", "command", "exec", "nice", "nohup", "time", "env", "timeout"}
# Each prefix's short options that take a value: ``sudo -u x``, ``xargs -n 1``.
_PREFIX_VALUED = {
    "sudo": set("ugpChDRTr"),
    "nice": {"n"},
    "env": {"u", "C", "S"},
    "timeout": {"s", "k"},
    "exec": {"a"},
    "xargs": set("nILPdsEa"),
}
_KEYWORDS = {"do", "then", "else", "elif", "if", "while", "until", "!"}
# Options that take a value, by tool. Long forms with '=' carry theirs.
_GREP_VALUED = set("efmABCd") | {"D"}
_GREP_LONG_VALUED = {
    "--regexp",
    "--file",
    "--max-count",
    "--after-context",
    "--before-context",
    "--context",
    "--include",
    "--exclude",
    "--exclude-dir",
    "--label",
    "--devices",
    "--directories",
    "--color",
    "--colour",
}
_RG_VALUED = set("efmABCgtTjMErd")
_RG_LONG_VALUED = {
    "--regexp",
    "--file",
    "--max-count",
    "--after-context",
    "--before-context",
    "--context",
    "--glob",
    "--iglob",
    "--type",
    "--type-not",
    "--type-add",
    "--threads",
    "--max-columns",
    "--encoding",
    "--replace",
    "--max-depth",
    "--max-filesize",
    "--sort",
    "--sortr",
    "--colors",
    "--color",
    "--path-separator",
    "--context-separator",
    "--field-match-separator",
    "--engine",
    "--pre",
    "--pre-glob",
    "--ignore-file",
}
_HEREDOC = re.compile(r"(?<!<)<<-?(?!<)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def _strip_heredocs(cmd: str) -> str:
    lines = cmd.split("\n")
    out, i = [], 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        tags = [m.group(2) for m in _HEREDOC.finditer(line)]
        i += 1
        for tag in tags:
            while i < len(lines) and lines[i].strip() != tag:
                i += 1
            i += 1
    return "\n".join(out)


def _strip_comments(cmd: str) -> str:
    """A whole-line comment, or a trailing one on a line with no quote before it."""
    out = []
    for line in cmd.split("\n"):
        if line.lstrip().startswith("#"):
            continue
        m = re.search(r"\s#", line)
        if m and not re.search(r"['\"]", line[: m.start()]):
            line = line[: m.start()]
        out.append(line)
    return "\n".join(out)


def _tokens(cmd: str) -> list[str] | None:
    cmd = _strip_comments(_strip_heredocs(cmd)).replace("\\\n", " ")
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=";&|()<>\n")
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        raw = list(lex)
    except ValueError:
        return None
    # shlex lexes a run of operator characters as one token (";\n", ");").
    out = []
    for tok in raw:
        if tok and all(ch in _OPERATOR_CHARS for ch in tok) and tok not in _OPERATORS:
            out += _split_operators(tok)
        else:
            out.append(tok)
    return out


_OPERATOR_CHARS = set(";&|()<>\n")
_OPERATORS = sorted(
    {";", "&", "|", "(", ")", "<", ">", "\n", "&&", "||", ";;", "|&", ">>", "&>", ">&"}
    | {"<&", "<>", ">|", "&>>", "<<<", "<<"},
    key=len,
    reverse=True,
)


def _split_operators(tok: str) -> list[str]:
    out, i = [], 0
    while i < len(tok):
        op = next(o for o in _OPERATORS if tok.startswith(o, i))
        out.append(op)
        i += len(op)
    return out


def _stages(tokens: list[str]):
    """Yield each pipeline's stages: (argv, index in the pipeline, stdin from a file)."""
    pipeline: list[list[str]] = [[]]
    redirect_in: list[bool] = [False]

    def flush():
        for i, argv in enumerate(pipeline):
            if argv:
                yield argv, i, redirect_in[i]

    skip = False
    for tok in tokens:
        if skip:  # a redirection's target
            skip = False
            continue
        if tok in _SEPARATORS:
            yield from flush()
            pipeline, redirect_in = [[]], [False]
        elif tok == "|" or tok == "|&":
            pipeline.append([])
            redirect_in.append(False)
        elif tok in _REDIRECTS:
            argv = pipeline[-1]
            if argv and argv[-1].isdigit():
                argv.pop()
            if tok in ("<", "<<<", "<>"):
                redirect_in[-1] = True
            skip = True
        else:
            pipeline[-1].append(tok)
    yield from flush()


def _skip_options(argv: list[str], valued: set) -> list[str]:
    """Drop leading options, and the value of one that takes it."""
    while argv and argv[0].startswith("-") and argv[0] != "-":
        opt, argv = argv[0], argv[1:]
        if opt == "--":
            break
        if opt.startswith("--"):
            continue  # long forms here carry their value after "="
        for j, ch in enumerate(opt[1:], start=1):
            if ch in valued:
                if j == len(opt) - 1 and argv:
                    argv = argv[1:]
                break
    return argv


def _strip_prefix(argv: list[str]) -> list[str]:
    while argv:
        head = argv[0]
        if head in _KEYWORDS:
            argv = argv[1:]
        elif head in _PREFIXES:
            argv = _skip_options(argv[1:], _PREFIX_VALUED.get(head, set()))
            if head == "timeout":
                argv = argv[1:]  # the duration
        elif re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", head):
            argv = argv[1:]
        else:
            break
    return argv


def _grep_args(argv: list[str], short_valued: set, long_valued: set):
    patterns, positional = [], []
    flags = set()
    from_file = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--":
            positional.extend(argv[i + 1 :])
            break
        if a.startswith("--"):
            name, eq, value = a.partition("=")
            flags.add(name)
            from_file |= name == "--file"
            if name in ("--regexp",):
                if eq:
                    patterns.append(value)
                elif i + 1 < len(argv):
                    patterns.append(argv[i + 1])
                    i += 1
            elif name in long_valued and not eq:
                i += 1
        elif a.startswith("-") and len(a) > 1:
            j = 1
            while j < len(a):
                ch = a[j]
                flags.add("-" + ch)
                if ch in short_valued:
                    value = a[j + 1 :]
                    if not value and i + 1 < len(argv):
                        value = argv[i + 1]
                        i += 1
                    if ch == "e":
                        patterns.append(value)
                    from_file |= ch == "f"
                    break
                j += 1
        else:
            positional.append(a)
        i += 1
    if not patterns and from_file:
        patterns.append(None)  # read from the file: unknown here
    elif not patterns and positional:
        patterns.append(positional.pop(0))
    elif not patterns:
        patterns.append(None)
    return patterns, positional, flags


def _find_globs(argv: list[str]) -> list[str]:
    out = []
    for i, a in enumerate(argv):
        if a in ("-name", "-iname", "-path", "-ipath", "-regex", "-iregex", "-wholename"):
            if i + 1 < len(argv):
                out.append(argv[i + 1])
    return out


def bash_searches(cmd: str) -> list[Search] | None:
    """grep, rg, git grep and find in a Bash command; None if it doesn't lex.

    A grep reading its stdin from a pipe filters another command's output: it
    doesn't search the code, so it's a ``filter``, not a look-alike.
    """
    tokens = _tokens(cmd)
    if tokens is None:
        return None
    found: list[Search] = []
    for argv, index, stdin_file in _stages(tokens):
        found += _stage_searches(argv, index, stdin_file)
    return found


_FIND_EXEC = {"-exec", "-execdir", "-ok", "-okdir"}


def _stage_searches(argv: list[str], index: int, stdin_file: bool) -> list[Search]:
    found: list[Search] = []
    argv = _strip_prefix(argv)
    via_xargs = False
    if argv and argv[0] == "xargs":
        via_xargs = True
        argv = _skip_options(argv[1:], _PREFIX_VALUED["xargs"])
    if not argv:
        return found
    head = os.path.basename(argv[0])
    sub = _skip_options(argv[1:], {"C", "c"}) if head == "git" else []
    if head == "git" and sub[:1] == ["grep"]:
        pats, _, flags = _grep_args(sub[1:], _GREP_VALUED, _GREP_LONG_VALUED)
        found += [_search("git grep", p, False, flags) for p in pats]
    elif head in ("grep", "egrep", "fgrep"):
        pats, paths, flags = _grep_args(argv[1:], _GREP_VALUED, _GREP_LONG_VALUED)
        recursive = bool(flags & {"-r", "-R", "--recursive", "--dereference-recursive"})
        reads_pipe = index > 0 and not paths and not recursive and not via_xargs
        if head == "fgrep":
            flags.add("-F")
        found += [_search("grep", p, reads_pipe and not stdin_file, flags) for p in pats]
    elif head == "rg":
        pats, paths, flags = _grep_args(argv[1:], _RG_VALUED, _RG_LONG_VALUED)
        reads_pipe = index > 0 and not paths and not via_xargs
        found += [_search("rg", p, reads_pipe and not stdin_file, flags) for p in pats]
    elif head == "find":
        own = argv[1:]
        execs = []
        for i, a in enumerate(own):
            if a in _FIND_EXEC:
                own, rest = own[:i], own[i + 1 :]
                end = next((j for j, t in enumerate(rest) if t in (";", "+")), len(rest))
                execs = rest[:end]
                break
        globs = _find_globs(own)
        found.append(Search("find", globs[0] if globs else None, globs=globs))
        if execs:
            found += _stage_searches(execs, 0, True)  # find hands it the files
    return found


def _search(binary, pattern, is_filter, flags) -> Search:
    return Search(
        binary,
        pattern,
        filter=is_filter,
        fixed=bool(flags & {"-F", "--fixed-strings"}),
        word=bool(flags & {"-w", "--word-regexp"}),
    )


# --- the rules ---------------------------------------------------------------

# Generic nouns and verbs that stand in for a concept when grepped bare. A plain
# word not listed is ambiguous: it may be a project's own term, a known symbol.
COMMON = set(
    """
    api auth authentication authorization authorize backup backoff build cache
    caching check client config configuration connect connection credential
    credentials database db debug deploy deployment download email embed
    embedding endpoint error errors event events exception exceptions fail
    failure format handler handlers health install job jobs key keys limit lock
    log logging login logout logs message messages metric metrics middleware
    migration model models notification notify parse parser password payment
    permission permissions queue rate report request requests response restore
    retry route router schema search secret secrets server service session
    sessions setting settings snapshot status task tasks test tests timeout token
    tokens update upgrade upload user users validate validation vector webhook
    worker
    """.split()
)
_DEF = re.compile(
    r"^\^?\s*(def|class|function|func|fn|const|let|var|import|from|export|type|interface|struct)\s"
)
_EXACT_SHAPES = [
    re.compile(p)
    for p in (
        r"^\^|(?<!\\)\$$|\\[bB<>]",  # anchored
        r"/",  # a path
        r"\w\\?\.\w",  # dotted: Foo.bar, a file name
        r"::|->",  # qualified
        r"\w\\?\(",  # a call
        r"[=:,'\"`\[\]{}<>@#%]",  # a log line, a message, an assignment
        r"[A-Za-z0-9]-[A-Za-z]",  # kebab-case: a unit, a file, a skill
        r"[A-Za-z]_|_[A-Za-z]",  # snake_case, SCREAMING_CASE
        r"[a-z][A-Z]",  # camelCase
        r"^[A-Z0-9]{3,}$",  # TODO, an acronym
        r"[A-Za-z][0-9]|[0-9][A-Za-z]",
        r"^-",  # a flag
    )
]


def _alternatives(pattern: str) -> list[str]:
    p = pattern.strip()
    if p.startswith("(") and p.endswith(")") and p.count("(") == 1:
        p = p[1:-1]
    p = p.replace("\\|", "|")
    out, depth, cur = [], 0, ""
    for ch in p:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "|" and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return out


def _alt_class(alt: str) -> str:
    raw = alt.strip()
    if not raw:
        return "ambiguous"
    if _DEF.search(raw):
        return "exact"
    words = re.sub(r"\.\*|\.\+|\\s[+*]?|\s+", " ", raw).split()
    if len(words) > 1:
        if all(re.fullmatch(r"[a-z]+", w) for w in words):
            return "english"
        if all(re.fullmatch(r"[A-Za-z0-9]+", w) for w in words):
            return "exact"  # capitals or a number: it quotes a message
        if any(rx.search(raw) for rx in _EXACT_SHAPES):
            return "exact"
        return "ambiguous"
    if any(rx.search(raw) for rx in _EXACT_SHAPES):
        return "exact"
    if re.fullmatch(r"[a-z]+", raw):
        return "common" if raw in COMMON else "word"
    return "ambiguous"


def classify_pattern(pattern: str | None, *, fixed: bool = False, word: bool = False) -> str:
    """exact, semantic or ambiguous, by the design's rules (§ Report)."""
    if not pattern:
        return "ambiguous"
    if fixed:
        return "exact"
    if word and re.fullmatch(r"\w+", pattern):
        return "exact"
    kinds = [_alt_class(a) for a in _alternatives(pattern)]
    if len(kinds) == 1:
        return {"exact": "exact", "common": "semantic", "english": "semantic"}.get(
            kinds[0], "ambiguous"
        )
    if "exact" in kinds and all(k in ("exact", "word") for k in kinds):
        return "exact"  # a project's own terms beside its symbols
    if all(k in ("word", "common", "english") for k in kinds):
        return "semantic"
    return "ambiguous"


def classify_glob(pattern: str | None) -> str:
    """A name or extension shape is a path (exact); ``*word*`` stands in for a concept."""
    if not pattern:
        return "exact"
    segments = [s for s in pattern.split("/") if s and s != "**"]
    base = segments[-1] if segments else ""
    m = re.fullmatch(r"\*+([A-Za-z]+)\*+(\.[\w{},]+)?", base)
    if not m:
        return "exact"
    return "semantic" if m.group(1).lower() in COMMON else "ambiguous"


def _combine(classes: list[str]) -> str:
    if not classes:
        return "ambiguous"
    if "semantic" in classes:
        return "semantic"
    if all(c == "exact" for c in classes):
        return "exact"
    return "ambiguous"


# --- calls -------------------------------------------------------------------


@dataclass
class Call:
    rec: Record
    kind: str  # index, mcp_other, mcp_unidentified, grep_like, bash_filter, explore, read, other
    tool: str  # index:<tool>, or the tool_name
    rule: str | None = None  # grep-like only
    final: str | None = None
    text: str = ""  # the pattern or command: data/ only
    linked: bool = False
    siblings: list = field(default_factory=list)

    @property
    def use_id(self) -> str:
        return self.rec.get("tool_use_id") or ""

    @property
    def cls(self) -> str:
        if self.kind == "index":
            return "index"
        if self.kind == "grep_like":
            return self.final or self.rule or "ambiguous"
        if self.kind == "bash_filter":
            return "filter"
        return "other"


def _mcp(rec: Record) -> tuple[str, str | None]:
    # The key order is deliberate: #54's keep rule writes tool_parameters only
    # in the client's shape, these two keys in this order, and deletes any
    # other. So a reordered record can't reach the disk; matching it here
    # would only disagree with the collector.
    params = _json(rec.get("tool_parameters"))
    if (
        isinstance(params, dict)
        and list(params) == ["mcp_server_name", "mcp_tool_name"]
        and all(isinstance(v, str) for v in params.values())
    ):
        if params["mcp_server_name"] in SOCRATICODE_SERVERS:
            return "index", params["mcp_tool_name"]
        return "mcp_other", params["mcp_server_name"]
    return "mcp_unidentified", None


def _bash_text(rec: Record) -> tuple[str, list[Search] | None]:
    """full_command (cut at 4096 B), else the client's input (cut at 512)."""
    params = _json(rec.get("tool_parameters"))
    tin = _json(rec.get("tool_input"))
    candidates = []
    if isinstance(params, dict) and isinstance(params.get("full_command"), str):
        candidates.append(params["full_command"])
    if isinstance(tin, dict) and isinstance(tin.get("command"), str):
        candidates.append(tin["command"])
    for cmd in candidates:
        found = bash_searches(cmd)
        if found is not None:
            return cmd, found
    raw = (
        candidates[0] if candidates else (rec.get("tool_parameters") or rec.get("tool_input") or "")
    )
    return raw, None


_SEARCH_WORD = re.compile(r"""(^|[\s|;&("'])(sudo\s+)?(grep|egrep|fgrep|rg|find|git\s+grep)\s""")


def _classify_call(rec: Record) -> Call:
    name = rec.get("tool_name") or "?"
    if name == "mcp_tool":
        kind, which = _mcp(rec)
        if kind == "index":
            tin = rec.get("tool_input") or ""
            parsed = _json(tin)
            linked = (
                parsed.get("includeLinked") is True
                if isinstance(parsed, dict)
                else bool(re.search(r'"includeLinked"\s*:\s*true', tin))
            )
            return Call(rec, "index", f"index:{which}", text=tin, linked=linked)
        return Call(rec, kind, "mcp_tool")
    if name == "Bash":
        cmd, found = _bash_text(rec)
        if found is None:
            if _SEARCH_WORD.search(cmd):
                return Call(rec, "grep_like", name, rule="ambiguous", text=cmd)
            return Call(rec, "other", name, text=cmd)
        searches = [s for s in found if not s.filter]
        if searches:
            classes = []
            for s in searches:
                if s.bin == "find":
                    classes += [classify_glob(g) for g in s.globs] or ["exact"]
                else:
                    classes.append(classify_pattern(s.pattern, fixed=s.fixed, word=s.word))
            return Call(rec, "grep_like", name, rule=_combine(classes), text=cmd)
        if found:
            return Call(rec, "bash_filter", name, text=cmd)
        return Call(rec, "other", name, text=cmd)
    tin = _json(rec.get("tool_input"))
    tin = tin if isinstance(tin, dict) else {}
    if name == "Grep":
        pattern = tin.get("pattern") if isinstance(tin.get("pattern"), str) else None
        return Call(rec, "grep_like", name, rule=classify_pattern(pattern), text=pattern or "")
    if name == "Glob":
        pattern = tin.get("pattern") if isinstance(tin.get("pattern"), str) else None
        return Call(rec, "grep_like", name, rule=classify_glob(pattern), text=pattern or "")
    if name in ("Agent", "Task"):
        params = _json(rec.get("tool_parameters"))
        kind = (params if isinstance(params, dict) else tin).get("subagent_type")
        return Call(rec, "explore" if kind == "Explore" else "other", name)
    if name == "Read":
        path = tin.get("file_path") if isinstance(tin.get("file_path"), str) else ""
        return Call(rec, "read", name, text=path)
    return Call(rec, "other", name)


def _siblings_of(call: Call, cohort: set[str]) -> list[str]:
    own = call.rec.repo
    others = cohort - {own}
    found = []
    if call.rec.get("tool_name") == "Bash":
        cmd = call.text
        for m in re.finditer(
            r"\bgit\s+clone\b[^|;&\n]*?[/:]CannObserv/([A-Za-z0-9._-]+)", cmd, re.I
        ):
            found.append(m.group(1))
        for m in re.finditer(r"\bgh\s+repo\s+clone\s+(?:CannObserv/)?([A-Za-z0-9._-]+)", cmd, re.I):
            found.append(m.group(1))
        for m in re.finditer(
            r"\bgh\s+api\s+[^|;&\n]*?repos/CannObserv/([A-Za-z0-9._-]+)/contents\b", cmd, re.I
        ):
            found.append(m.group(1))
    elif call.kind == "read":
        m = re.match(r"^/home/[^/]+/([^/]+)/", call.text)
        if m:
            found.append(m.group(1))
        found += re.findall(r"(?:^|/)\.\./([^/]+)/", call.text)
    names = {re.sub(r"\.git$", "", n) for n in found}
    return sorted(n for n in names if n in others)


# --- the analysis ------------------------------------------------------------


@dataclass
class Agreement:
    n: int = 0
    agree: int = 0
    unresolved: int = 0
    unmatched: int = 0
    lower: float | None = None
    upper: float | None = None
    threshold: float = THRESHOLD

    @property
    def established(self) -> bool:
        return self.lower is not None and self.lower >= self.threshold

    def line(self) -> str:
        if not self.n:
            return "Agreement: no operator labels yet (#37). No trend is established."
        rate = self.agree / self.n
        verdict = "established" if self.established else "not established"
        return (
            f"Agreement: {self.agree} of {self.n} ({rate:.0%}), Wilson 95% "
            f"[{self.lower:.2f}, {self.upper:.2f}], threshold {self.threshold:.2f}: {verdict}. "
            f"Unresolved, counted as disagreement: {self.unresolved}. "
            f"Labels matching no call: {self.unmatched}."
        )


@dataclass
class Analysis:
    calls: list[Call]
    counts: dict
    classification: list[dict]
    efficiency_calls: list[dict]
    efficiency_requests: list[dict]
    agreement: Agreement
    requests: int = 0


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float | None, float | None]:
    if n == 0:
        return None, None
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def _stats(values: list) -> dict:
    vals = [v for v in values if v is not None]
    return {
        "n": len(vals),
        "sum": sum(vals) if vals else None,
        "median": statistics.median(vals) if vals else None,
        "mean": round(statistics.fmean(vals), 3) if vals else None,
    }


def analyse(
    records: list[Record],
    *,
    markers: list[dict],
    cohort: set[str],
    sweep_reads: int = SWEEP_READS,
    model_labels: dict | None = None,
    labels: dict | None = None,
    threshold: float = THRESHOLD,
) -> Analysis:
    model_labels = model_labels or {}
    order = sorted(records, key=lambda r: (r.ts, r.session, r.seq))
    tools = [r for r in order if r.event == "tool_result"]
    apis = [r for r in order if r.event == "api_request"]
    calls = [_classify_call(r) for r in tools]
    bounds_cache: dict = {}

    def period(rec: Record) -> str:
        key = (rec.host, rec.repo)
        if key not in bounds_cache:
            bounds_cache[key] = _boundaries(markers, rec.host, rec.repo)
        return _period(bounds_cache[key], rec.ts)

    counts: dict = defaultdict(Counter)
    by_prompt: dict = defaultdict(list)
    for call in calls:
        if call.kind == "grep_like":
            call.final = call.rule if call.rule != "ambiguous" else model_labels.get(call.use_id)
        call.siblings = _siblings_of(call, cohort)
        key = (call.rec.host, call.rec.repo, period(call.rec))
        c = counts[key]
        # A subagent shares its parent's prompt.id: its calls count apart, so
        # one Explore isn't also counted as every Grep it ran.
        pre = SUBAGENT if call.rec.get("agent_id") else ""
        c[pre + "tool_calls"] += 1
        if call.kind == "index":
            c[pre + "index_calls"] += 1
            c[pre + call.tool] += 1
            if call.tool == "index:codebase_search" and call.linked:
                c[pre + "codebase_search_linked"] += 1
        elif call.kind in ("mcp_other", "mcp_unidentified"):
            c[pre + call.kind] += 1
        elif call.kind == "grep_like":
            c[
                pre
                + {"Grep": "lookalike:grep_tool", "Glob": "lookalike:glob"}.get(
                    call.tool, "lookalike:bash_search"
                )
            ] += 1
        elif call.kind == "bash_filter":
            c[pre + "bash_filter"] += 1
        elif call.kind == "explore":
            c[pre + "lookalike:explore"] += 1
        if call.siblings:
            c[pre + "sibling_access"] += 1
        by_prompt[(call.rec.session, call.rec.prompt)].append(call)

    for every in by_prompt.values():
        seq = [c for c in every if not c.rec.get("agent_id")]  # the main thread's
        if not seq:
            continue
        key = (seq[0].rec.host, seq[0].rec.repo, period(seq[0].rec))
        reads = after_search = 0
        searched = searched_index = False
        for call in seq:
            if call.kind == "read" and not searched:
                reads += 1
            if call.kind == "grep_like" and searched_index:
                after_search += 1
            if call.kind in ("index", "grep_like", "explore"):
                searched = True
            if call.tool == "index:codebase_search":
                searched_index = True
        if reads >= sweep_reads:
            counts[key]["lookalike:read_sweep"] += 1
            counts[key]["lookalike:read_sweep_reads"] += reads
        if after_search:
            counts[key]["followup:grep_after_search"] += after_search
            counts[key]["followup:prompts"] += 1
    for c in counts.values():
        if c.get("mcp_unidentified") or c.get(SUBAGENT + "mcp_unidentified"):
            c["index_calls_floor"] = 1
        for pre in ("", SUBAGENT):
            if c.get(pre + "index:codebase_search"):
                share = c.get(pre + "codebase_search_linked", 0) / c[pre + "index:codebase_search"]
                c[pre + "codebase_search_linked_share"] = round(share, 3)
    for r in apis:
        counts[(r.host, r.repo, period(r))]["api_requests"] += 1

    classification = []
    for scope_key, scope in _scopes(calls, period):
        for pass_name, attr in (("rules", "rule"), ("final", "final")):
            tally = Counter(getattr(c, attr) or "ambiguous" for c in scope)
            for cls in CLASSES:
                classification.append(
                    {**scope_key, "pass": pass_name, "class": cls, "calls": tally.get(cls, 0)}
                )

    rows = defaultdict(list)
    for call in calls:
        rows[(call.tool, call.cls)].append(call)
    efficiency_calls = []
    for (tool_key, cls), group in sorted(rows.items()):
        size = _stats([c.rec.num("tool_result_size_bytes") for c in group])
        dur = _stats([c.rec.num("duration_ms") for c in group])
        efficiency_calls.append(
            {
                "tool": tool_key,
                "class": cls,
                "calls": len(group),
                "size_n": size["n"],
                "size_sum": size["sum"],
                "size_median": size["median"],
                "size_mean": size["mean"],
                "duration_n": dur["n"],
                "duration_median": dur["median"],
                "duration_mean": dur["mean"],
            }
        )

    paths = {}
    for (session, prompt), seq in by_prompt.items():
        has_index = any(c.kind == "index" for c in seq)
        has_grep = any(c.kind == "grep_like" for c in seq)
        paths[(session, prompt)] = {
            (True, False): "index-only",
            (False, True): "grep-only",
            (True, True): "both",
            (False, False): "neither",
        }[(has_index, has_grep)]
    per_prompt: dict = defaultdict(lambda: defaultdict(int))
    for r in apis:
        totals = per_prompt[(r.session, r.prompt)]
        for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens"):
            totals[k] += int(r.num(k) or 0)
        totals["cost_usd"] += float(r.num("cost_usd") or 0.0)
        totals["duration_ms"] += int(r.num("duration_ms") or 0)
    grouped = defaultdict(list)
    for key, totals in per_prompt.items():
        grouped[paths.get(key, "neither")].append(totals)
    efficiency_requests = []
    metrics = (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
        "cost_usd",
        "duration_ms",
    )
    for path_name in ("index-only", "grep-only", "both", "neither"):
        group = grouped.get(path_name, [])
        row = {"path": path_name, "requests": len(group)}
        for m in metrics:
            values = [g[m] for g in group]
            s = _stats(values)
            row[f"{m}_sum"] = round(s["sum"], 6) if m == "cost_usd" and s["sum"] else s["sum"]
            row[f"{m}_median"] = s["median"]
        efficiency_requests.append(row)

    agreement = Agreement(threshold=threshold)
    if labels:
        by_id = {c.use_id: c for c in calls if c.kind == "grep_like"}
        for use_id, label in labels.items():
            call = by_id.get(use_id)
            if call is None:
                agreement.unmatched += 1
                continue
            agreement.n += 1
            if call.final is None:
                agreement.unresolved += 1
            elif call.final == label:
                agreement.agree += 1
        agreement.lower, agreement.upper = wilson(agreement.agree, agreement.n)

    return Analysis(
        calls=calls,
        counts=dict(counts),
        classification=classification,
        efficiency_calls=efficiency_calls,
        efficiency_requests=efficiency_requests,
        agreement=agreement,
        requests=len(per_prompt),
    )


def _scopes(calls: list[Call], period):
    grep = [c for c in calls if c.kind == "grep_like"]
    yield {"host": "all", "repo": "all", "period": "all"}, grep
    groups = defaultdict(list)
    for c in grep:
        groups[(c.rec.host, c.rec.repo, period(c.rec))].append(c)
    for (host, repo, per), group in sorted(groups.items()):
        yield {"host": host, "repo": repo, "period": per}, group


# --- labels and files --------------------------------------------------------


def read_labels(path: Path) -> dict:
    try:
        with Path(path).open(newline="") as fh:
            reader = csv.DictReader(fh)
            if not reader.fieldnames or not {"tool_use_id", "label"} <= set(reader.fieldnames):
                raise Usage(f"{path}: needs the columns tool_use_id and label")
            out = {}
            for row in reader:
                label = (row.get("label") or "").strip().lower()
                if not label:
                    continue
                if label not in LABELS:
                    raise Usage(f"{path}: label {label!r} is not exact or semantic")
                out[row["tool_use_id"].strip()] = label
            return out
    except OSError as exc:
        raise Usage(f"{path}: {exc}") from exc


def _context(call: Call, calls_in_prompt: list[Call]) -> str:
    parts = []
    for other in calls_in_prompt:
        if other is call:
            parts.append("[this call]")
            continue
        name = other.rec.get("tool_name") or "?"
        if other.kind == "index":
            parts.append(f"mcp_tool {other.tool.split(':', 1)[1]} {other.text[:200]}")
        elif other.text:
            parts.append(f"{name} {other.text[:200]}")
        else:
            parts.append(name)
    return " ⏎ ".join(parts)


def _private_writer(path: Path):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    return os.fdopen(fd, "w", newline="")


def _write_csv(path: Path, rows: list[dict], fields: list[str] | None = None) -> None:
    fields = fields or (list(rows[0]) if rows else [])
    with _private_writer(path) as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _md_table(rows: list[dict], fields: list[str]) -> str:
    def cell(v):
        if v is None:
            return "–"
        if isinstance(v, float):
            return f"{v:.4g}"
        return str(v)

    head = "| " + " | ".join(fields) + " |\n|" + "---|" * len(fields) + "\n"
    return head + "".join("| " + " | ".join(cell(r.get(f)) for f in fields) + " |\n" for r in rows)


COUNT_ORDER = [
    "index_calls",
    "index_calls_floor",
    "mcp_unidentified",
    "mcp_other",
    "codebase_search_linked",
    "codebase_search_linked_share",
    "lookalike:grep_tool",
    "lookalike:glob",
    "lookalike:bash_search",
    "bash_filter",
    "lookalike:explore",
    "lookalike:read_sweep",
    "lookalike:read_sweep_reads",
    "sibling_access",
    "followup:grep_after_search",
    "followup:prompts",
    "tool_calls",
    "api_requests",
]


def write_report(out: Path, loaded: Loaded, result: Analysis, args, sample_rows) -> None:
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(out, 0o700)

    count_rows = []
    for (host, repo, per), c in sorted(result.counts.items()):
        measures = [m for m in COUNT_ORDER if m in c] + sorted(m for m in c if m not in COUNT_ORDER)
        for m in measures:
            count_rows.append(
                {"host": host, "repo": repo, "period": per, "measure": m, "value": c[m]}
            )
    _write_csv(out / "counts.csv", count_rows, ["host", "repo", "period", "measure", "value"])
    _write_csv(
        out / "classification.csv",
        result.classification,
        ["host", "repo", "period", "pass", "class", "calls"],
    )
    _write_csv(out / "efficiency_calls.csv", result.efficiency_calls)
    _write_csv(out / "efficiency_requests.csv", result.efficiency_requests)

    by_prompt = defaultdict(list)
    for c in result.calls:
        by_prompt[(c.rec.session, c.rec.prompt)].append(c)
    fields = ["tool_use_id", "host", "repo", "prompt_id", "tool", "call", "siblings", "label"]

    def row(c: Call) -> dict:
        return {
            "tool_use_id": c.use_id,
            "host": c.rec.host,
            "repo": c.rec.repo,
            "prompt_id": c.rec.prompt,
            "tool": c.tool,
            "call": c.text,
            "siblings": _context(c, by_prompt[(c.rec.session, c.rec.prompt)]),
            "label": "",
        }

    ambiguous = [c for c in result.calls if c.kind == "grep_like" and c.rule == "ambiguous"]
    _write_csv(out / "ambiguous.csv", [row(c) for c in ambiguous if c.final is None], fields)
    if sample_rows is not None:
        _write_csv(out / "label-sample.csv", [row(c) for c in sample_rows], fields)

    first = min((r.ts for r in loaded.records), default=None)
    last = max((r.ts for r in loaded.records), default=None)
    md = ["# Usage report", ""]
    md.append(
        f"Records: {len(loaded.records)} ({sum(r.event == 'tool_result' for r in loaded.records)} "
        f"tool_result, {sum(r.event == 'api_request' for r in loaded.records)} api_request), "
        f"{first:%Y-%m-%dT%H:%MZ} to {last:%Y-%m-%dT%H:%MZ}. Lines {loaded.lines}, "
        f"unparseable {loaded.bad_lines}, bad records {loaded.bad_records}, "
        f"duplicates dropped {loaded.duplicates}, "
        f"excluded (proof sessions) {loaded.excluded}."
    )
    md += ["", "## Counts", ""]
    md.append(
        "Per host, repo and period, split at docs/usage-markers.json's reach. An index call "
        "is `mcp_tool` naming SocratiCode's server in `tool_parameters`. **A period with "
        "unidentified MCP calls (`mcp_unidentified`, before #54's install) reads its index "
        "calls as a floor** (`index_calls_floor` 1). A grep reading a pipe is a `bash_filter`, "
        f"not a look-alike. A read sweep is {args.sweep_reads}+ Reads in one prompt before "
        "any search."
    )
    for (host, repo, per), c in sorted(result.counts.items()):
        md += ["", f"### {host} · {repo} · {per}", ""]
        measures = [m for m in COUNT_ORDER if m in c] + sorted(m for m in c if m not in COUNT_ORDER)
        md.append(
            _md_table([{"measure": m, "value": c[m]} for m in measures], ["measure", "value"])
        )
    md += ["## Classification of grep-like calls", "", result.agreement.line(), ""]
    md.append(
        "Rules first (exact-string / semantic stand-in / ambiguous); the session's model "
        "labels the ambiguous through `ambiguous.csv` → `ambiguous-labels.csv`. "
        f"Ambiguous awaiting the model: {sum(1 for c in ambiguous if c.final is None)}."
    )
    md.append("")
    md.append(
        _md_table(
            [r for r in result.classification if r["period"] == "all"],
            ["pass", "class", "calls"],
        )
    )
    md.append(
        "Per period in classification.csv. Every trend there reads beside the agreement line "
        "above, and reads as established only when it says so."
    )
    md += ["", "## Efficiency per call", ""]
    md.append(
        _md_table(
            result.efficiency_calls,
            [
                "tool",
                "class",
                "calls",
                "size_n",
                "size_median",
                "size_mean",
                "duration_median",
                "duration_mean",
            ],
        )
    )
    md += ["## Efficiency per request (prompt.id)", ""]
    md.append(
        "**Caveats:** requests on different paths are usually different tasks, so this is "
        "no controlled comparison. `cost_usd` is an estimate on a subscription plan; tokens "
        "are the firmer number. Cache reads are counted apart."
    )
    md.append("")
    md.append(
        _md_table(
            result.efficiency_requests,
            [
                "path",
                "requests",
                "input_tokens_sum",
                "output_tokens_sum",
                "cache_read_tokens_sum",
                "cache_creation_tokens_sum",
                "cost_usd_sum",
                "duration_ms_median",
            ],
        )
    )
    with _private_writer(out / "report.md") as fh:
        fh.write("\n".join(md) + "\n")


def _read_inputs(paths: list[str]):
    for p in paths:
        if p == "-":
            yield from sys.stdin
        else:
            with open(p, encoding="utf-8", errors="replace") as fh:
                yield from fh


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Usage(message)


def main(argv: list[str]) -> int:
    parser = _Parser(description=__doc__.split("\n\n")[0])
    parser.add_argument("inputs", nargs="+", help="OTLP/JSON lines files, or - for stdin")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "data")
    parser.add_argument("--markers", type=Path, default=REPO_ROOT / "docs" / "usage-markers.json")
    parser.add_argument("--cohort", type=Path, default=REPO_ROOT / "deploy" / "cohort.json")
    parser.add_argument(
        "--labels", type=Path, help="the operator's labels (default data/labels.csv)"
    )
    parser.add_argument(
        "--model-labels", type=Path, help="the model's (default data/ambiguous-labels.csv)"
    )
    parser.add_argument("--sample", type=int, help="write N grep-like calls to label-sample.csv")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--sweep-reads", type=int, default=SWEEP_READS)
    parser.add_argument("--agreement-threshold", type=float, default=THRESHOLD)
    try:
        args = parser.parse_args(argv)
        markers = load_markers(args.markers)
        try:
            cohort = {r["repo"] for r in json.loads(args.cohort.read_text())["repos"]}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise Usage(f"cohort {args.cohort}: {exc}") from exc
        labels_path = args.labels or args.out / "labels.csv"
        model_path = args.model_labels or args.out / "ambiguous-labels.csv"
        labels = read_labels(labels_path) if args.labels or labels_path.exists() else {}
        model = read_labels(model_path) if args.model_labels or model_path.exists() else {}
        if args.sample is not None and args.sample < 1:
            raise Usage("--sample takes a positive count")
        if args.sweep_reads < 1:
            raise Usage("--sweep-reads takes a positive count")
        if args.out.exists() and not args.out.is_dir():
            raise Usage(f"--out {args.out} is not a directory")
        try:
            loaded = load_records(_read_inputs(args.inputs))
        except OSError as exc:
            raise Usage(str(exc)) from exc
    except Usage as exc:
        print(f"usage_report: {exc}", file=sys.stderr)
        return 64
    if not loaded.records:
        print(
            f"usage_report: no records read ({loaded.lines} lines, {loaded.bad_lines} "
            "unparseable): was the sudo read refused?",
            file=sys.stderr,
        )
        return 1
    result = analyse(
        loaded.records,
        markers=markers,
        cohort=cohort,
        sweep_reads=args.sweep_reads,
        model_labels=model,
        labels=labels,
        threshold=args.agreement_threshold,
    )
    sample_rows = None
    if args.sample is not None:
        pool = sorted((c for c in result.calls if c.kind == "grep_like"), key=lambda c: c.use_id)
        sample_rows = random.Random(args.seed).sample(pool, min(args.sample, len(pool)))
    try:
        write_report(args.out, loaded, result, args, sample_rows)
    except OSError as exc:
        print(f"usage_report: can't write {args.out}: {exc}", file=sys.stderr)
        return 64
    print(f"usage_report: {len(loaded.records)} records → {args.out}/report.md")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
