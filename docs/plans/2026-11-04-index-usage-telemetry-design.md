# Observing how the cohort uses the index

**Status:** design, awaiting approval (2026-11-04)
**Amends:** [2026-09-11-shared-qdrant-vm-design.md](2026-09-11-shared-qdrant-vm-design.md), as D21 once approved
**Output:** a suite of issues: this repo, gregoryfoster/skills, and each cohort repo

## Why

The cohort keeps the index; that is not in question. What is unknown is
whether agents use it. Nobody can see how often a session calls
`codebase_search` against grep, `rg`, Glob, the Explore subagent or a run of
file reads, nor how often a cross-repo question goes through
`includeLinked: true` against a sibling cloned or fetched for a local look.

The operator wants that seen so they can decide whether agents need a
stronger push toward the index, then **change the instructions or skills and
watch what the change does**. That is a time series with dated markers, not
a one-off snapshot.

Separately, each VM should link its siblings the intended way: a one-file
`.socraticode.json` stub, not a full clone. Getting there is one sweep. After
it, the only thing that needs watching is a link a repo **declares** in
`linkedProjects` that its VM hasn't caught up with.

## Goals

1. **Usage, over time, per host and repo:** index calls against their
   look-alikes, cross-repo searches against sibling clones and fetches.
2. **Greps told apart with stated confidence:** an exact-string grep (correct
   under the cohort's policy) separated from a grep standing in for a
   semantic question. "Confident" means agreement with a hand-labelled sample
   is reported alongside every trend.
3. **Efficiency:** per call (bytes returned, duration) and per user request
   (tokens, cache reads, estimated cost, duration), by path taken. The
   assumption that the index is cheaper is tested, not presumed. Grep may win
   in some cases, and the report should show where.
4. **Markers:** every change to policy wording, skills or hooks is dated,
   and each report reads against them.
5. **Setup:** a one-time audit of every VM's links, then a standing check
   for declared links with no stub, a stub naming an empty collection, and a
   full clone where a stub belongs.

## Non-goals

- A baseline from before collection starts. No backfill from session
  transcripts.
- A controlled comparison (fixed questions answered both ways). Worth
  returning to once the everyday numbers exist. Not MVP.
- Dashboards, a query backend, or posting reports to GitHub.
- Detecting dependencies nobody has declared (code coupling, behaviour).
  "Declared" means present in `linkedProjects`.
- An unattended report on a timer. First reports are triggered from a
  session.
- Persisting Qdrant's access log. OTel covers the client side; the log stays
  what `index-inflight.sh` reads.

## Anti-goals

- **Telemetry that becomes a production dependency.** D9 holds: a dead
  collector loses data and stops nothing. No client blocks on it.
- **Keeping data no question needs.** File contents from Edit/Write,
  prompt text and identities are dropped before anything touches disk.
- **A report that counts correct grep use as failure.** A raw grep-vs-search
  ratio is not a finding.
- **Filling `/`.** It is at 61% of 30 GB (2026-11-04). The headroom alert
  fires at 80%.

## Constraints that shape it

- **A repo can't turn on Claude Code's telemetry.** Claude Code ignores
  `OTEL_*` exporter variables in a repo's `.claude/settings.json` and
  `settings.local.json`. They go in each VM's `~/.claude/settings.json` (or
  managed settings). Enabling is a **host step**, done per VM like the stub
  audit, and no commit to a client repo can carry it.
- **Without `OTEL_LOG_TOOL_DETAILS=1`, every SocratiCode call reports as the
  literal `mcp_tool`**, and no Grep pattern or Bash command is sent. The flag
  is required.
- **Traffic flows one way.** VMs can reach `co-index` over the tailnet;
  `co-index` reaches no VM. Clients push.
- **Session-triggered reports only.** The model judging ambiguous greps is
  this repo's session model, so no Anthropic API key lands on this host.
- **Claude Code ≥ 2.1.310** on clients, for `agent_id`/`parent_agent_id` on
  subagent events. `co-index` runs 2.1.309 today.
- **Repo configs readable anonymously** for notifier, watcher, archiver,
  replicator, broker, power-map, processor, address-validator.
  `status` and `observo` return 404 on raw, so they are private or carry no
  `.socraticode.json`. `index` has none yet (D20 is unfinished).
- The store holds nine `codebase_*` collections today:
  address-validator, archiver, broker, notifier, observo, power-map,
  processor, replicator, watcher.

## Approaches

| | What | Trade-off |
|---|---|---|
| **A — chosen** | OTel Collector on `co-index`, rotated JSONL on disk, a tested Python report run from a session | One small service, flat files, no charts. It matches what the repo already is: shell, Python, tests, units bound to the tailnet |
| B | Collector plus a log/metrics backend with dashboards (Grafana+Loki, SigNoz) | Live charts and ad-hoc queries, for hundreds of MB to GBs of RAM on a box Qdrant and Ollama share (~6 GB free), and more to patch. Overkill for a before-and-after read every week or two |
| C | No collector: each VM summarises its own transcripts and pushes counts | Depends on an undocumented file format and on a job on every VM staying alive: the silent decay the store was built to end. Dropped |

The operator picked **A**, with the report run from a session at first and
written to a git-ignored directory, not GitHub, because a report is
regenerable from what the collector holds.

## Design

### D21 (proposed): `co-index` receives the cohort's Claude Code telemetry

`co-index` gains a third role: an OTLP receiver for Claude Code's events. D9
holds. Clients export asynchronously, and a dead receiver costs data and
nothing else. It adds no client checkout and no credential. D11 is
untouched.

### Collector

- `otelcol-contrib`, pinned by version and digest like Qdrant, run as a
  Docker unit beside the store's, published on the tailnet address only (D3,
  `tailnet-bind.sh`), OTLP/HTTP on `:4318`. **The ACL adds `:4318`** to
  D17's client rule. The design record's ACL blocks follow.
- **No ingest auth** *(my call)*. Same acceptance as Ollama's (D4): the
  ACL bounds who can send, the port only accepts writes, and a forged event
  can skew a count but read nothing. A bearer token would be a second shared
  secret on every VM, for that.
- **Before anything is written, the pipeline keeps:**
  - `tool_result` events: everything always present (`tool_name`,
    `tool_use_id`, `success`, `duration_ms`, `tool_result_size_bytes`,
    `tool_input_size_bytes`, `prompt.id`, `session.id`, `agent_id`,
    `parent_agent_id`, `mcp_server_scope`), plus `tool_parameters`, and
    `tool_input` **only** for Grep, Glob, Bash, Read (path only), Agent and
    the SocratiCode MCP tools;
  - `api_request` events: tokens by type, `cost_usd`, `duration_ms`,
    `model`, `prompt.id`, `session.id`, `agent_id`;
  - `claude_code.active_time.total`;
  - resource: host (see Clients), `vcs.repository.name`, `app.version`.
- **It drops:** every other event (`user_prompt`, `assistant_response`,
  `api_error`, `tool_decision`, …), every other metric (session cost and
  token counters duplicate `api_request`), `user.email`,
  `user.account_uuid`, `user.account_id`, `organization.id`, and
  `tool_input` for Edit, Write, NotebookEdit and any tool not listed above.
- **Secrets in Bash commands** *(my call)*. `full_command` is kept because
  clone and `gh api` detection needs the URL. A redaction step masks known
  token shapes (`ghp_`, `github_pat_`, `sk-ant-`, `api-key:` headers, a
  64-hex run after `key`) before writing. Files are root-owned and 0600.
- **Storage:** file exporter, JSONL, rotated and zstd-compressed, pruned at
  **90 days** (the operator's choice) with a **total-size cap** as backstop,
  so a misbehaving client can't fill `/`. If 90 days outgrows the cap, the
  fallbacks are a larger disk or a copy off-VM to GCS. Neither is built now.
- **The check-in reports it**: collector not answering, and on-disk bytes
  above a threshold, as `index-checkin.sh` findings, so co-status hears.
- Added to the patching knob's services, and to the runbook.

### Clients

Per VM, in `~/.claude/settings.json` `env`:

```
CLAUDE_CODE_ENABLE_TELEMETRY=1
OTEL_LOGS_EXPORTER=otlp
OTEL_METRICS_EXPORTER=otlp
OTEL_METRIC_EXPORT_INTERVAL=300000        # active time only; 5 min is enough
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
OTEL_EXPORTER_OTLP_ENDPOINT=http://index:4318
OTEL_LOG_TOOL_DETAILS=1                   # required: else SocratiCode is "mcp_tool"
OTEL_METRICS_INCLUDE_REPOSITORY=true
OTEL_METRICS_INCLUDE_VERSION=true
OTEL_RESOURCE_ATTRIBUTES=cohort.host=<vm>
```

`OTEL_LOG_USER_PROMPTS` and `OTEL_LOG_ASSISTANT_RESPONSES` stay off.
`cohort.host` names the VM explicitly. The collector may also stamp the
peer's tailnet address, as a cross-check. The exact variable set is the
pilot's to confirm.

### Pilot, before any other VM

`co-index`'s own sessions export first, for at least three days of normal
work. It measures bytes per event by type, events per day, and compressed
bytes per day, and confirms the drops work: no Edit content and no email on
disk. It also confirms the VS Code extension's sessions honour
`~/.claude/settings.json` the way the CLI's do. The rollout issues wait for
its numbers, which go in the runbook with a projected 90-day size for ten
VMs. The working estimate to beat is 5–10 MB a day uncompressed across
the cohort, and about a tenth of that compressed.

### Markers

A committed, machine-readable log in this repo, one entry per change that
could move usage: date (UTC), what changed, where (upstream skill, a repo's
`AGENTS.md` policy block, a hook), and **which repos it reached, and when**.
A skill change merged upstream reaches each repo only at that repo's next
daily vendor bump, and the policy block only when `init-socraticode` re-runs,
so the merge date alone misdates the change. File name and format are the
builder's.

*What counts, and who records it, my call:* only **deliberate
interventions** aimed at usage (policy wording, the reminder or health
hook, a skill change made for this purpose), never routine daily bumps.
Whoever ships the intervention adds the entry. Reach per repo is recorded
by hand when the change is seen to land: a public repo's vendored-skills
commit or `AGENTS.md` change is readable anonymously, and a private repo's
is confirmed by the operator. Until a repo's reach is recorded, the report
leaves that repo out of the before-and-after split rather than guessing.

### Report

`scripts/usage_report.py`, tested like everything else here, run from a
session. It writes to a git-ignored `data/` directory: a Markdown summary
plus CSV per table. Regenerating it overwrites; nothing in `data/` is
precious.

**Counts, per host, repo and period** (periods split at markers):

- index calls by tool (`codebase_search`, `_graph_query`, `_impact`, …),
  and the share of `codebase_search` passing `includeLinked: true`;
- look-alikes: Grep tool, `grep`/`rg`/`find` in Bash, Glob, Explore
  subagents, read sweeps (several Reads in one `prompt.id` with no search
  before them; threshold is the builder's);
- sibling access: `git clone`/`gh repo clone`/`gh api …/contents` naming a
  cohort repo, and Reads under a sibling path, against linked searches;
- follow-ups: a grep in the same `prompt.id` after a `codebase_search`,
  the "index answered thin" signal.

**Classification of each grep-like call**, in two passes:

1. **Rules** on the pattern and arguments. Exact-string: a quoted message,
   a log line, an anchored or qualified symbol (`def foo`, `Foo.bar`), a
   path. Semantic stand-in: alternations of plain words (`auth|login`),
   bare English, unanchored common nouns. Plus a third bucket, ambiguous.
2. **The session's model judges the ambiguous**, from the call and the
   other calls in its `prompt.id` (no prompt text is collected). The script
   writes the ambiguous set out, the session labels it, and the script
   folds the labels in. The skill or procedure that drives that loop is the
   builder's to shape.

**Confidence** (decided by the operator, 2026-11-04):

- **The operator hand-labels** a first sample of about 100–150 real
  grep-like calls, drawn from the pilot's and early rollout's data. A
  session model never labels the sample, or the check would grade the
  model against itself.
- **The real sample stays on the host**, in git-ignored `data/`, never
  committed: this repo is public, and the calls carry private repos'
  paths and terms that redaction doesn't catch.
- **CI tests the rules** against synthetic, committed examples of each
  class. It cannot measure agreement on real traffic.
- **Every report measures agreement** of the whole classifier (rules
  plus the model pass) against the operator's labels, with an interval,
  and prints it beside the trends. A trend reads as established only
  above a threshold the first labelled sample sets.
- Labelling the sample gates the first report's classification tables.
  The counts and efficiency tables don't wait for it.

**Efficiency:** per call, `tool_result_size_bytes` and `duration_ms` by
tool and class. Per user request, sum `api_request` tokens (cache reads
apart), `cost_usd` and duration by `prompt.id`, grouped by path: index
only, grep-like only, both, neither. The report states plainly that
requests on different paths are usually different tasks, and that
`cost_usd` is an estimate on a subscription plan. Tokens are the firmer
number.

### Setup checks

**Store-side, here.** A check reads each cohort repo's committed
`.socraticode.json` (anonymously where public), and for every declared link
confirms the sibling's `projectId` names a `codebase_*` collection that
exists and holds points. Findings ride `index-checkin.sh` to co-status, at
a cadence that doesn't fetch from GitHub every 10 minutes. Private repos
(`status`, `observo`) are read with a token or listed by hand, the
builder's call within D18's bounds. The cohort repo list lives in one
place; D17's client list is today's.

**VM-side, upstream.** Only the VM can see whether a link resolves to a
stub or a full clone. The daily health hook already reports unresolved
links, on stdout at the day's first SessionStart, which the session sees.
It should also report a **full clone where a stub would do** (with its
size), and a link whose collection is missing or empty.

**One-time audit.** One issue per cohort repo: replace full sibling clones
with stubs, confirm every declared link resolves, confirm the store `env`
block applies (the workspace-trust caveat), upgrade Claude Code if below
2.1.310, and enable telemetry (above). Filed after the pilot.

## Issue suite

**This repo (CannObserv/index)**, roughly in order:

1. D21: the collector unit, its pipeline (keeps, drops, redaction), storage
   and caps, the ACL port, check-in findings, knob and runbook. Tests first.
2. Pilot on `co-index`. Measure, tune the drops, record sizes. Gates 7's
   telemetry half and 8.
3. Markers log and its convention; first entry is "collection on".
4. `scripts/usage_report.py`: counts, rule classifier, ambiguous loop,
   agreement against the operator's on-host labels, efficiency tables,
   `data/` git-ignored. Synthetic rule fixtures in CI.
4a. The operator labels the first sample (a task, not code), once
   enough data exists. Gates 4's classification tables.
5. Store-side link check into the check-in.

**gregoryfoster/skills** (issues, never commits):

6. `socraticode-health.sh`: stub vs full clone, and empty or missing
   linked collections.
7. `init-socraticode`: when a link is added to `linkedProjects`, emit the
   host's stub step (it is a host step, so the skill names it rather than
   doing it); this half can start now. Document the user-scope telemetry
   block for cohorts that opt in; this half waits for the pilot's variable
   set. Filed under `GH_TOKEN_SKILLS`.

**Each cohort repo**, via `GH_TOKEN_<REPO>`:

8. The one-time audit and telemetry enable, after 2.

*Which repos, my call:* every tailnet peer that is a store client today,
which `tailscale status` lists as notifier, watcher, archiver, replicator,
broker, status, observo-primary (repo `observo`), power-map, co-processor
(repo `processor`) and address-validator. That is ten VMs, not D17's eight:
co-processor and address-validator joined after D17 was written, so issue
1 reads their tags from the live policy and amends D17 with them. `status`
gets the telemetry half only, since it has no collection and declares no
links. `address-validator`'s issue goes under `GH_TOKEN_ADDRESS_VALIDATOR`,
added 2026-11-04. The size projection is for ten VMs.

**Later, not MVP:** the controlled comparison, an unattended report on a
timer, a GCS copy, Qdrant access-log persistence.

## Left to the builder

Collector image and version; processor choice (filter, transform,
redaction) and config shape; rotation sizes and the total cap; markers
file name and format; read-sweep threshold; sampling method and the
agreement threshold method; the shape of the session-driven labelling
loop; report layout; the link check's cadence and cache; how private repo
configs are read within D18.
