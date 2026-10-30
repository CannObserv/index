# co-index — Agent Guidelines

Be terse. Prefer fragments over full sentences. Skip filler and preamble. Lead with the answer or action.

## Project Overview

Deploy surface and operator tooling for **`co-index`**, the cohort's shared SocratiCode store: Qdrant (:6333, TLS, API key) and Ollama (:11434) on one VM, used by every cohort repo's `codebase_search`.

**Nothing on a production path depends on it** (D9). An outage degrades search on the cohort's VMs and stops no service.

Moved out of CannObserv/notifier with history (notifier#90, #1). **A bare `#N` in history before that move, or `notifier#N` anywhere, is a notifier issue.** From here on, `#N` means this repo.

Design record, with every decision (D0–D16): [docs/plans/2026-09-11-shared-qdrant-vm-design.md](docs/plans/2026-09-11-shared-qdrant-vm-design.md). Host runbook: [deploy/README.md](deploy/README.md).

## Development Methodology

TDD required. Red → Green → Refactor. No production code without a failing test first.

## Environment & Tooling

Python ≥3.12, uv, pytest, ruff. No Python package: the tests drive shell scripts as subprocesses.

`pre-commit` runs ruff only, never pytest. Only CI (`.github/workflows/ci.yml`: `lint`, `test`) proves correctness.

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run pre-commit install   # once per clone
```

## Layout

| Path | Role |
|---|---|
| `deploy/` | Everything installed on `co-index`: `qdrant.service`/`qdrant-run.sh`, `ollama.service`/`ollama-run.sh`/`ollama-slim/`, `qdrant-cert-renew.*` (D14), `index-checkin.*` (D10/D16) with `engine-fingerprint.*` (D19), `tailnet-bind.sh` (D3), `setup.sh.template` (first boot); for patching (#9), `index-inflight.sh`, `qdrant-recovery.sh`, `apt-preferences.d/tailscale` |
| `.skills/patching-hosts` | The `patching-hosts` knob: windows, quiet range, services, health, in-flight gate, Tailscale origin. Qdrant isn't expressible as a datastore: snapshot it with `qdrant-recovery.sh` |
| `scripts/install_qdrant_key.sh` | Installs the store's single API key into a *client* repo's `.claude/settings.local.json`. Key on stdin only, atomic 0600 write, refuses a target git would commit |
| `scripts/reembed.py` | Re-embeds the store from its own payloads on an engine change (D19, #11). `--dry-run` is the gate and the drift check; `--apply` rewrites `dense` only. Procedure: deploy/README.md, "Engine change" |
| `scripts/pre-ship.sh` | `shipping-work`'s gate (#3): CI's steps, in CI's order, stops at the first failure. Dev-only; clients never run it |
| `tests/deploy/` | Behaviour and drift tests for the above |
| `.github/dependabot.yml` | `github-actions` + `uv` (dev group), weekly, 7-day cooldown (#23). PRs land by fast-forward from a detached worktree, never via the deploy checkout's `main`: [docs/dependabot.md](docs/dependabot.md) |
| `tests/ci/` | Guards for the Dependabot config. `test_dependabot.py` is the `adopting-dependabot` asset: edit its ADAPT block only |

**Clients run `install_qdrant_key.sh` from a read-only clone of this repo** (public, no credential), never a copy: its tests pin exactly the properties a copy loses first. Procedure, and rotation: [docs/installing-the-key.md](docs/installing-the-key.md).

## Infrastructure

exe.dev VM `co-index` (`pdx`, 2 vCPU / 8 GB / 30 GB, proxy `private`), tailnet node `index`, `tag:index`. Clients reach `https://index.taild0fb76.ts.net:6333`. The short name fails TLS verification (D14).

| Unit | What | Check |
|---|---|---|
| `qdrant.service` | Docker, bound to the tailnet address only (D3) | collections + point counts |
| `ollama.service` | Docker, slim CPU image, `nomic-embed-text` (D5, D7) | `/api/tags` |
| `qdrant-cert-renew.timer` | weekly, `Persistent=true` | `systemctl list-timers` |
| `index-checkin.timer` | every 10 min → co-status's dead-man's timer (CannObserv/status) | a missed check-in alerts there |
| `cannobserv-disk-gc.timer` | the cohort disk GC, daily 05:15 UTC (#16). Provisioner's, installed by the `maintaining-host-disk` skill, not from `deploy/` | `sudo journalctl -u cannobserv-disk-gc` |
| `cannobserv-disk-headroom.timer` | every 10 min, `/` at 85/80 → `co-index-disk-headroom` (CannObserv/status#72) | a missed check-in alerts there |

`qdrant.service` and `ollama.service` are `Requires=docker.service`: **restarting Docker restarts both.**

**Where this repo is worked on: `co-index` itself** (D15, which amends D8; D18). One checkout of *this* repo. **Never** a client checkout, a commit to a client repo, or a Node toolchain on this host (VS Code's private runtime is not one, D15). Nothing here indexes (D11).

**Credentials:** `.env` (git-ignored, 0600) holds `GH_TOKEN`, scoped to this repo only, and one `GH_TOKEN_<REPO>` per cohort repo, for issues there only (D18): `GH_TOKEN="$GH_TOKEN_NOTIFIER" gh issue create -R CannObserv/notifier …`. Load with `set -a; . ./.env; set +a`. Plain `git push` works: a credential helper in this clone's `.git/config`, scoped to github.com, reads `GH_TOKEN` from `.env` when git asks, so the token is never in argv, a URL or a tracked file. The skills refresh hook pushes through it. `.git/config` isn't tracked, so a new clone sets it once:

```bash
git config credential.https://github.com.helper ''
git config --add credential.https://github.com.helper '!f() { test "$1" = get || exit 0; echo username=x-access-token; sed -n "s/^GH_TOKEN=//p" /home/exedev/index/.env | sed "s/^/password=/"; }; f'
```

## Conventions

**Commits:** `#<n> [type]: <description>`, or `[type]: <description>` without an issue. Types: feat, fix, refactor, docs, test, chore.

**Dates:** UTC; ISO 8601.

**Cross-repo:** issues only on sibling cohort repos, never commits.

## Agent Skills

Vendored as submodules under `skills-vendor/` (gregoryfoster/skills, obra/superpowers), symlinked into `skills/` and `.claude/skills/`. Add or refresh with the `managing-skills` skill. A `SessionStart` hook (`.claude/settings.json`) refreshes them once per UTC day on `main`, commits and pushes the bump; log in `.git/skills-update.log`. `.skills/doctor.sh` repairs dangling skill symlinks (`git submodule update --init`); a fresh clone's first session fails its hook until it has run.
