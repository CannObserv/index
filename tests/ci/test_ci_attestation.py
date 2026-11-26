"""The local-gate attestation's CI side (#50, gregoryfoster/skills#688).

With ``.skills/attest-local-gate`` committed as ``on``, shipping-work's
``push.sh`` runs the gate, pushes, and posts the commit status ``gate/local``
= ``success`` on the pushed SHA. CI's ``attest`` job reads it, and ``test``
skips for that commit. Everything here pins the one property that matters:
**``test`` skips only for an attested commit on a branch push or a human,
same-repo pull request, and runs for every other event, a bot's PR, a fork's
PR, and any attest job that failed or didn't run.**

The skip is job-level (CannObserv/notifier's shape), so a skipped ``test``
reads ``skipped`` by job, never ``success``. ``!cancelled()`` keeps it from
inheriting GitHub's implicit ``success()``, which would skip ``test``
whenever ``attest`` failed or was skipped: the opposite of fail-safe.

The ``if:`` expressions are evaluated, not string-matched, by the small
GitHub-expression evaluator below. It covers the subset ``ci.yml`` uses
(``&&``, ``||``, ``!``, ``==``, ``!=``, parentheses, ``startsWith()``,
``cancelled()``), compares strings case-insensitively as Actions does, and
refuses anything else, so an edit that outgrows it fails here by name.
"""

import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"
KNOB = REPO_ROOT / ".skills" / "attest-local-gate"
GITMODULES = REPO_ROOT / ".gitmodules"
ATTEST_JOB = "attest"
REPO = "CannObserv/index"


def _workflow() -> dict:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))


def _on() -> dict:
    wf = _workflow()
    # PyYAML reads the bare key `on` as the boolean True.
    return wf.get("on", wf.get(True)) or {}


def _jobs() -> dict[str, dict]:
    return _workflow()["jobs"]


# --- a GitHub-expression evaluator, for the subset ci.yml uses -------------

_TOKEN = re.compile(
    r"\s*(?:(?P<str>'(?:[^']|'')*')|(?P<op>&&|\|\||==|!=|!|\(|\)|,)"
    r"|(?P<id>[A-Za-z_][A-Za-z0-9_\-]*(?:\.[A-Za-z_][A-Za-z0-9_\-]*)*))"
)


def _tokens(expr: str) -> list[tuple[str, str]]:
    out, pos = [], 0
    expr = expr.strip()
    while pos < len(expr):
        m = _TOKEN.match(expr, pos)
        if not m or m.end() == pos:
            raise ValueError(f"unsupported expression at {expr[pos:]!r}")
        kind = m.lastgroup
        out.append((kind, m.group(kind)))
        pos = m.end()
        while pos < len(expr) and expr[pos].isspace():
            pos += 1
    return out


def _norm(v):
    return v.lower() if isinstance(v, str) else v


def _eq(a, b) -> bool:
    if a is None or b is None:
        return (a in (None, "")) and (b in (None, ""))
    return _norm(a) == _norm(b)


def value_of(expr: str, ctx: dict, status: str = "success"):
    """Evaluate a GitHub expression over ``ctx`` (a nested dict; a missing key is null).

    `&&` and `||` return an operand, as in Actions, so a string-valued
    expression (a concurrency group) evaluates to its string."""
    expr = expr.strip()
    if expr.startswith("${{") and expr.endswith("}}"):
        expr = expr[3:-2]
    toks = _tokens(expr)
    pos = 0

    def peek():
        return toks[pos] if pos < len(toks) else (None, None)

    def take(value=None):
        nonlocal pos
        tok = peek()
        if value is not None and tok[1] != value:
            raise ValueError(f"expected {value!r}, got {tok[1]!r} in {expr!r}")
        pos += 1
        return tok

    def lookup(path: str):
        cur = ctx
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return None
            cur = cur[part]
        return cur

    def primary():
        kind, val = take()
        if kind == "str":
            return val[1:-1].replace("''", "'")
        if val == "(":
            v = or_()
            take(")")
            return v
        if kind == "id" and peek()[1] == "(":
            take("(")
            args = []
            if peek()[1] != ")":
                args.append(or_())
                while peek()[1] == ",":
                    take(",")
                    args.append(or_())
            take(")")
            if val == "startsWith" and len(args) == 2:
                return isinstance(args[0], str) and _norm(args[0]).startswith(_norm(args[1]))
            if val == "cancelled" and not args:
                return status == "cancelled"
            raise ValueError(f"unsupported function {val}() in {expr!r}")
        if kind == "id":
            if val in ("true", "false"):
                return val == "true"
            if val == "null":
                return None
            return lookup(val)
        raise ValueError(f"unexpected {val!r} in {expr!r}")

    def unary():
        if peek()[1] == "!":
            take("!")
            return not unary()
        return primary()

    def comparison():
        left = unary()
        while peek()[1] in ("==", "!="):
            op = take()[1]
            right = unary()
            left = _eq(left, right) if op == "==" else not _eq(left, right)
        return left

    def and_():
        left = comparison()
        while peek()[1] == "&&":
            take("&&")
            right = comparison()
            left = left and right
        return left

    def or_():
        left = and_()
        while peek()[1] == "||":
            take("||")
            right = and_()
            left = left or right
        return left

    value = or_()
    if pos != len(toks):
        raise ValueError(f"trailing tokens in {expr!r}: {toks[pos:]}")
    return value


def evaluate(expr: str, ctx: dict, status: str = "success") -> bool:
    """An `if:`: the value's truthiness, as Actions reads it."""
    return bool(value_of(expr, ctx, status))


def test_the_evaluator_reads_what_actions_reads():
    """Guard the guard: precedence, null, case and the two functions."""
    ctx = {"a": "Yes", "github": {"ref": "refs/tags/v1"}}
    assert evaluate("a == 'yes'", ctx)  # Actions compares strings case-insensitively
    assert evaluate("missing != 'yes'", ctx)
    assert evaluate("missing == ''", ctx)
    assert evaluate("startsWith(github.ref, 'refs/tags/')", ctx)
    assert not evaluate("a == 'no' && a == 'yes' || a == 'no'", ctx)  # && binds tighter
    assert evaluate("!(a == 'no')", ctx)
    assert evaluate("${{ !cancelled() && a == 'yes' }}", ctx)
    assert not evaluate("${{ !cancelled() && a == 'yes' }}", ctx, status="cancelled")
    with pytest.raises(ValueError):
        evaluate("contains(a, 'y')", ctx)


# --- the event matrix -------------------------------------------------------


def _ctx(event: str, attested: str | None, *, bot=False, fork=False, ref="refs/heads/main"):
    github = {"event_name": event, "ref": ref, "repository": REPO, "sha": "c0ffee"}
    if event == "pull_request":
        github["event"] = {
            "pull_request": {
                "user": {"type": "Bot" if bot else "User"},
                "head": {
                    "sha": "feed",
                    "repo": {"full_name": "someone/index" if fork else REPO},
                },
            }
        }
    needs = {ATTEST_JOB: {"outputs": {"attested": attested}}} if attested is not None else {}
    return {"github": github, "needs": needs}


# (label, context, does `test` run?)
MATRIX = [
    ("push-attested", _ctx("push", "yes"), False),
    ("push-not-attested", _ctx("push", "no"), True),
    ("push-attest-failed", _ctx("push", None), True),
    ("push-tag-attested", _ctx("push", "yes", ref="refs/tags/v1"), True),
    ("pr-human-attested", _ctx("pull_request", "yes"), False),
    ("pr-human-not-attested", _ctx("pull_request", "no"), True),
    ("pr-bot-attested", _ctx("pull_request", "yes", bot=True), True),
    ("pr-fork-attested", _ctx("pull_request", "yes", fork=True), True),
    ("dispatch-attested", _ctx("workflow_dispatch", "yes"), True),
    ("dispatch", _ctx("workflow_dispatch", None), True),
    ("schedule-attested", _ctx("schedule", "yes"), True),
    ("schedule", _ctx("schedule", None), True),
]


@pytest.mark.parametrize(("ctx", "runs"), [m[1:] for m in MATRIX], ids=[m[0] for m in MATRIX])
def test_test_skips_only_for_an_attested_branch_push_or_human_pr(ctx, runs):
    assert evaluate(_jobs()["test"]["if"], ctx) is runs


@pytest.mark.parametrize("ctx", [m[1] for m in MATRIX], ids=[m[0] for m in MATRIX])
def test_test_never_runs_in_a_cancelled_run(ctx):
    """Every context: `!cancelled() && (a) || (b)` would still run through (b)."""
    assert not evaluate(_jobs()["test"]["if"], ctx, status="cancelled")


def test_test_waits_for_attest_and_survives_its_failure():
    """`needs` without a status function adds an implicit success(): a failed or
    skipped attest would then skip `test`. `!cancelled()` must lead the `if:`."""
    test = _jobs()["test"]
    needs = test.get("needs")
    assert ATTEST_JOB in ([needs] if isinstance(needs, str) else needs or [])
    cond = test["if"].strip()
    assert re.match(r"^\$\{\{\s*!cancelled\(\)\s*&&\s*\(", cond), cond
    assert cond.endswith(")}}") or cond.endswith(") }}"), cond


@pytest.mark.parametrize(
    ("event", "runs"),
    [("push", True), ("pull_request", True), ("workflow_dispatch", False), ("schedule", False)],
)
def test_attest_reads_only_where_its_answer_can_skip(event, runs):
    """Elsewhere a transient API error would only fail the run. Unset reads as no."""
    assert evaluate(_jobs()[ATTEST_JOB]["if"], _ctx(event, None)) is runs


def test_lint_always_runs():
    lint = _jobs()["lint"]
    assert "if" not in lint and "needs" not in lint


def test_the_attest_job_holds_one_step_and_statuses_read_only():
    job = _jobs()[ATTEST_JOB]
    assert job.get("permissions") == {"statuses": "read"}
    assert job.get("outputs") == {"attested": "${{ steps.attest.outputs.attested }}"}
    steps = job["steps"]
    assert len(steps) == 1 and steps[0].get("id") == "attest"
    assert "uses" not in steps[0], "it runs no action and checks out no code"


def test_no_other_job_or_the_workflow_grants_statuses():
    assert _workflow()["permissions"] == {"contents": "read"}
    others = {n: j.get("permissions") for n, j in _jobs().items() if n != ATTEST_JOB}
    assert all(p is None for p in others.values()), others


def _group(ctx: dict) -> str:
    """The run's concurrency group, with `github.workflow` taken as `CI`."""
    raw = _workflow()["concurrency"]["group"]
    return re.sub(r"\$\{\{(.*?)\}\}", lambda m: str(value_of(m.group(1), ctx)), raw).replace(
        "None", ""
    )


def _gctx(event: str, ref: str = "refs/heads/main", head_ref: str | None = None) -> dict:
    github = {"workflow": "CI", "event_name": event, "ref": ref}
    if head_ref:
        github["head_ref"] = head_ref
    return {"github": github}


def test_a_push_cannot_cancel_a_pending_schedule_or_dispatch():
    """GitHub keeps one pending run per group and cancels the older one, whatever
    cancel-in-progress says: a push queued behind a pending schedule would
    cancel that day's clean-runner `test`."""
    push = _group(_gctx("push"))
    assert _group(_gctx("push")) == push, "main's push runs still queue behind each other"
    assert _group(_gctx("schedule")) != push
    assert _group(_gctx("workflow_dispatch")) != push
    assert _group(_gctx("schedule")) != _group(_gctx("workflow_dispatch"))
    pr = _gctx("pull_request", ref="refs/pull/7/merge", head_ref="topic")
    assert _group(pr) not in (push, _group(_gctx("schedule")))


def test_a_schedule_runs_test_on_a_clean_runner():
    """attestation.md: the only clean-runner run a directly shipped commit gets."""
    schedule = _on().get("schedule")
    assert schedule and all("cron" in s for s in schedule), _on()


# --- the attest step's script, against a stub gh ----------------------------


def _attest_step() -> dict:
    return _jobs()[ATTEST_JOB]["steps"][0]


def test_the_step_takes_its_inputs_from_env_not_inline_expressions():
    step = _attest_step()
    assert "${{" not in step["run"], "expressions go in env:, never spliced into the script"
    assert step["env"]["HEAD"] == "${{ github.event.pull_request.head.sha || github.sha }}"
    assert step["env"]["GH_TOKEN"] == "${{ github.token }}"


def test_the_steps_script_has_no_shell_comments_and_splits():
    """The shlex trap (#688: watcher, status): comments go in YAML, above the step."""
    for line in _attest_step()["run"].splitlines():
        assert not line.lstrip().startswith("#"), line
        shlex.split(line)


@pytest.fixture
def run_step(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" >> "$GH_LOG"\n'
        '[ "${GH_FAIL:-}" = 1 ] && { echo "HTTP 403: Resource not accessible" >&2; exit 1; }\n'
        'printf "%s\\n" "$GH_N"\n'
    )
    gh.chmod(0o755)

    def run(n: str = "0", fail: bool = False):
        out, summary, log = (tmp_path / f for f in ("out", "summary", "gh.log"))
        for f in (out, summary, log):
            f.write_text("")
        env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "GH_LOG": str(log),
            "GH_N": n,
            "GH_FAIL": "1" if fail else "",
            "GH_TOKEN": "t",
            "REPO": REPO,
            "HEAD": "feed",
            "GITHUB_OUTPUT": str(out),
            "GITHUB_STEP_SUMMARY": str(summary),
        }
        # Actions' default shell for run: on Linux.
        proc = subprocess.run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", _attest_step()["run"]],
            env=env,
            capture_output=True,
            text=True,
        )
        return proc, out.read_text(), summary.read_text(), log.read_text()

    return run


@pytest.mark.parametrize(("n", "verdict"), [("0", "no"), ("1", "yes"), ("2", "yes")])
def test_the_step_says_yes_only_for_a_success(run_step, n, verdict):
    proc, out, summary, log = run_step(n)
    assert proc.returncode == 0, proc.stderr
    assert out == f"attested={verdict}\n"
    assert f"attested={verdict}" in proc.stdout, "the verdict is in the log"
    assert f"attested={verdict}" in summary, "and in the job summary"
    assert f"repos/{REPO}/commits/feed/status?per_page=100" in log
    assert 'select(.context == "gate/local" and .state == "success")' in log


def test_a_refused_read_fails_the_step_and_sets_nothing(run_step):
    proc, out, _, _ = run_step(fail=True)
    assert proc.returncode != 0
    assert out == ""


# --- the knob, and what would let it lie ------------------------------------


def test_the_knob_is_committed_on():
    """One word, by _attest-gate.sh's grammar: blank lines and `#` lines aside."""
    assert KNOB.is_file() and not KNOB.is_symlink()
    words = [
        w
        for line in KNOB.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("#")
        for w in line.split()
    ]
    assert words == ["on"], words


def test_no_submodule_hides_its_edits():
    """gregoryfoster/skills#694: a stamp can pass a tree whose edits git hides.

    `ignore` in .gitmodules is one way; .git/config and skip-worktree or
    assume-unchanged paths are machine-local, and the brief checks them by hand.
    """
    keys = subprocess.run(
        ["git", "config", "--file", str(GITMODULES), "--name-only", "--list"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert keys, "read nothing from .gitmodules"
    assert not [k for k in keys if k.endswith(".ignore")], keys
