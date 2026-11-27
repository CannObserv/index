# co-index — Agent Guidelines

Be terse. Prefer fragments over full sentences. Skip filler and preamble. Lead with the answer or action.

## Project Overview

Deploy surface and operator tooling for **`co-index`**, the cohort's shared SocratiCode store: Qdrant (:6333, TLS, API key) and Ollama (:11434) on one VM, used by every cohort repo's `codebase_search`. Since D21 it also receives the cohort's Claude Code telemetry: an OpenTelemetry Collector (:4318, no auth).

**Nothing on a production path depends on it** (D9). An outage degrades search on the cohort's VMs and stops no service.

Moved out of CannObserv/notifier with history (notifier#90, #1). **A bare `#N` in history before that move, or `notifier#N` anywhere, is a notifier issue.** From here on, `#N` means this repo.

Design record, with every decision (D0–D22): [docs/plans/2026-09-11-shared-qdrant-vm-design.md](docs/plans/2026-09-11-shared-qdrant-vm-design.md). Host runbook: [deploy/README.md](deploy/README.md).

<!-- BEGIN socraticode-policy -->
## Code Exploration Policy

SocratiCode is the preferred semantic-search tool here once indexed (manifest
`.socraticodecontextartifacts.json`). Its MCP tools are **deferred** — schemas
load only after the `ToolSearch` prefetch that
`.claude/hooks/socraticode-reminder.sh` prints each session.

**Negative rule.** Use SocratiCode MCP tools first for semantic questions
("where is X", "how does Y work", "what depends on Z"). Reach for `grep`/`rg`
only on exact strings (error messages, log lines, known symbols). Reserve the
Explore subagent for path-pattern walks, not semantic search.

> **The dependency graph here is LOW-YIELD** — 2 edges across 34
> files, 69.7% unresolved (2026-11-07). `codebase_graph_query`,
> `codebase_impact` and `codebase_flow` answer empty rather than erroring, so
> **treat empty graph output as tool failure, not as absence** — "No dependency
> information found" means the resolver failed, never that nothing depends on
> the file. Use `rg -n 'from <module> import|import <module>|<script>\.sh'` instead:
> most of this repo is shell, which calls scripts by name.

| Goal | Tool |
|------|------|
| Where is X defined / how does Y work / what touches Z | `codebase_search` |
| Exact string or regex (errors, log lines, known symbols) | `grep` / `rg` |
| Imports/dependents of a file · blast radius of a change | `grep` / `rg` — graph low-yield |

Full tool table, prefetch hook, per-tool guidance: [`docs/SOCRATICODE.md`](docs/SOCRATICODE.md).
<!-- END socraticode-policy -->

## Development Methodology

TDD required. Red → Green → Refactor. No production code without a failing test first.

## Environment & Tooling

Python ≥3.12, uv ≥0.13.5 (`required-version`, #25), pytest, ruff. No Python package: the tests drive shell scripts as subprocesses. `test_otelcol_pipeline.py` also runs the pinned collector image (D21): it needs Docker, passwordless `sudo` and `zstd`, pulls the image once, and fails rather than skips on `co-index` and in CI.

`pre-commit` runs ruff only, never pytest. Only CI (`.github/workflows/ci.yml`: `attest`, `lint`, `test`) proves correctness. For a commit shipped through `push.sh` (`gate/local` = `success`, #50), `test` skips on its push run: the local gate is its only full run until `main`'s tip next gets a runner `test`, from the daily `schedule`, the skills hook's bump or a `workflow_dispatch`.

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run pre-commit install   # once per clone
```

## Layout

| Path | Role |
|---|---|
| `deploy/` | Everything installed on `co-index`: `qdrant.service`/`qdrant-run.sh`, `ollama.service`/`ollama-run.sh`/`ollama-slim/`, `qdrant-cert-renew.*` (D14), `index-checkin.*` (D10/D16) with `engine-fingerprint.*` (D19) and `link-check.py` over `cohort.json`, the cohort's one repo list (#38), `tailnet-bind.sh` (D3), `setup.sh.template` (first boot); the collector's `otelcol.service`/`otelcol-run.sh`/`otelcol.yaml` and `otelcol-retention.*` (D21); for patching (#9), `index-inflight.sh`, `qdrant-recovery.sh`, `apt-preferences.d/tailscale`; Node's apt source and pins (D20) |
| `.skills/patching-hosts` | The `patching-hosts` knob: windows, quiet range, services, health, in-flight gate, Tailscale and NodeSource origins. `datastore qdrant`: `recovery-point.sh` takes Qdrant's snapshot. `recovery`: `qdrant-recovery.sh query`, as `exedev`, through the read-only key (D22). `qdrant-recovery.sh` is for the steps the skill doesn't run, plus `check` after a step |
| `.skills/attest-local-gate` | `on` (#50): `shipping-work`'s `push.sh` runs `scripts/pre-ship.sh` on the clean, committed tree, pushes, and posts `gate/local` = `success` on the SHA. Read from the commit. Reference: `skills/shipping-work/references/attestation.md` |
| `scripts/install_qdrant_key.sh` | Installs the store's single API key into a *client* repo's `.claude/settings.local.json`. Key on stdin only, atomic 0600 write, refuses a target git would commit |
| `scripts/qdrant-ro-key.sh` | Mints and rotates Qdrant's read-only key (D22, #40): `qdrant.env` and `/etc/socraticode/qdrant-ro.key` (`0640 root:qdrant-ro`), for `qdrant-recovery.sh query` as the knob user. Root, run from the checkout. Takes effect at Qdrant's next restart: in a window |
| `scripts/reembed.py` | Re-embeds the store from its own payloads on an engine change (D19, #11). `--dry-run` is the gate and the drift check; `--apply` rewrites `dense` only. Procedure: deploy/README.md, "Engine change" |
| `scripts/nodesource.sh` | Installs Node from NodeSource (`install`, root, fingerprint-checked key) and checks the host against `deploy/` (`check`). Refuses while `exedev` is in group `docker` (D20). Run from the checkout |
| `scripts/pre-ship.sh` | `shipping-work`'s gate (#3): CI's steps, in CI's order, stops at the first failure. Dev-only; clients never run it |
| `docs/usage-markers.json` | Dated usage interventions and their reach (#35). Convention: [docs/usage-markers.md](docs/usage-markers.md) |
| `tests/deploy/`, `tests/report/` | Behaviour and drift tests for the above |
| `.socraticode.json`, `.socraticodeignore`, `.socraticodecontextartifacts.json` | This repo's own SocratiCode client (D20, #18): `projectId: index`, no linked projects, the store's `env` block and the pin in `.claude/settings.json`. Guarded by `tests/ci/test_socraticode_config.py`. Detail: [docs/SOCRATICODE.md](docs/SOCRATICODE.md) |
| `.github/dependabot.yml` | `github-actions` + `uv` (dev group), weekly, 7-day cooldown (#23). PRs land by fast-forward from a detached worktree, never via the deploy checkout's `main`: [docs/dependabot.md](docs/dependabot.md) |
| `tests/ci/` | Guards for the Dependabot config, for where CI and clones get uv (`test_uv_source.py`), and for the attestation's CI side (`test_ci_attestation.py`: every trigger, attested or not). `test_dependabot.py` is the `adopting-dependabot` asset: edit its ADAPT block only |

**Clients run `install_qdrant_key.sh` from a read-only clone of this repo** (public, no credential), never a copy: its tests pin exactly the properties a copy loses first. Procedure, and rotation: [docs/installing-the-key.md](docs/installing-the-key.md).

## Infrastructure

exe.dev VM `co-index` (`pdx`, 2 vCPU / 8 GB / 30 GB, proxy `private`), tailnet node `index`, `tag:index`. Clients reach `https://index.taild0fb76.ts.net:6333`. The short name fails TLS verification (D14).

| Unit | What | Check |
|---|---|---|
| `qdrant.service` | Docker, bound to the tailnet address only (D3) | collections + point counts |
| `ollama.service` | Docker, slim CPU image, `nomic-embed-text` (D5, D7) | `/api/tags` |
| `otelcol.service` | Docker, `otelcol-contrib` pinned by digest, OTLP/HTTP `:4318` on the tailnet address only; allowlisted JSONL in `/var/lib/otelcol`, root-only (D21) | the knob's `health` line; `otel:collector` |
| `otelcol-retention.timer` | daily 03:30 UTC: zstd, 90-day prune, 1 GiB cap | `sudo journalctl -u otelcol-retention`; `otel:retention` |
| `qdrant-cert-renew.timer` | weekly, `Persistent=true` | `systemctl list-timers` |
| `index-checkin.timer` | every 10 min → co-status's dead-man's timer (CannObserv/status) | a missed check-in alerts there |
| `cannobserv-disk-gc.timer` | the cohort disk GC, daily 05:15 UTC (#16). Provisioner's, installed by the `maintaining-host-disk` skill, not from `deploy/` | `sudo journalctl -u cannobserv-disk-gc` |
| `cannobserv-disk-headroom.timer` | every 10 min, `/` at 85/80 → `co-index-disk-headroom` (CannObserv/status#72) | a missed check-in alerts there |
| Node 26 (`nodejs`) | not a unit: NodeSource apt, for this repo's own SocratiCode client only. No unit or deploy script runs it (D20) | `bash scripts/nodesource.sh check` |

`qdrant.service`, `ollama.service` and `otelcol.service` are `Requires=docker.service`: **restarting Docker restarts all three.**

**Where this repo is worked on: `co-index` itself** (D15, which amends D8; D18; D20). One checkout of *this* repo. **Never** a client checkout or a commit to a client repo. Node is for this repo's own SocratiCode client only, and only once `exedev` is out of group `docker` (D20). This host may index `index` and nothing else (D11).

**Credentials:** `.env` (git-ignored, 0600) holds `GH_TOKEN`, scoped to this repo only, and one `GH_TOKEN_<REPO>` per cohort repo, for issues there only (D18): `GH_TOKEN="$GH_TOKEN_NOTIFIER" gh issue create -R CannObserv/notifier …`. Load with `set -a; . ./.env; set +a`. Plain `git push` works: a credential helper in this clone's `.git/config`, scoped to github.com, reads `GH_TOKEN` from `.env` when git asks, so the token is never in argv, a URL or a tracked file. The skills refresh hook pushes through it. `.git/config` isn't tracked, so a new clone sets it once:

```bash
git config credential.https://github.com.helper ''
git config --add credential.https://github.com.helper '!f() { test "$1" = get || exit 0; echo username=x-access-token; sed -n "s/^GH_TOKEN=//p" /home/exedev/index/.env | sed "s/^/password=/"; }; f'
```

## Conventions

**Commits:** `#<n> [type]: <description>`, or `[type]: <description>` without an issue. Types: feat, fix, refactor, docs, test, chore.

**Dates:** UTC; ISO 8601.

**Cross-repo:** issues only on sibling cohort repos, never commits.

**Landing** (`main` is linear, no PRs merged on GitHub). From a worktree on a topic branch, in one shell that ran `set -a; . /home/exedev/index/.env; set +a` (the worktree has no `.env`; `gh` posts the status):
1. `bash /home/exedev/index/skills/shipping-work/scripts/push.sh` — the gate, then the topic branch's push (no CI: `push` is `main`-only), then `gate/local` on the SHA. Exit 1 or 2 pushed nothing. Go on only after `Attestation: gate/local = success posted`; `ATTESTATION NOT POSTED` is still exit 0, and `test` will run.
2. `git push origin <sha>:refs/heads/main` — the fast-forward. Its push run finds the status already there and skips `test`.
3. `git push origin --delete <topic>`, then `git pull --ff-only` in the deploy checkout.

Never through the deploy checkout's `main`. A plain push attests nothing, so `test` runs: that's the route for Dependabot's commits ([docs/dependabot.md](docs/dependabot.md)), the skills hook, and a change whose gate is red on `co-index` only for host state that follows the landing (a `deploy/` change's `test_installed.py` parity; live embed tests while a client index run holds Ollama). Run the rest of the gate with those deselected, push by SHA, read `test` by job, then install. **Never post `gate/local` by hand**, and never make it a required status.

## Agent Skills

Vendored as submodules under `skills-vendor/` (gregoryfoster/skills, obra/superpowers), symlinked into `skills/` and `.claude/skills/`. Add or refresh with the `managing-skills` skill. A `SessionStart` hook (`.claude/settings.json`) refreshes them once per UTC day on `main`, commits and pushes the bump; log in `.git/skills-update.log`. `.skills/doctor.sh` repairs dangling skill symlinks (`git submodule update --init`); a fresh clone's first session fails its hook until it has run.
