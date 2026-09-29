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
| `deploy/` | Everything installed on `co-index`: `qdrant.service`/`qdrant-run.sh`, `ollama.service`/`ollama-run.sh`/`ollama-slim/`, `qdrant-cert-renew.*` (D14), `index-checkin.*` (D10/D16), `tailnet-bind.sh` (D3), `setup.sh.template` (first boot) |
| `scripts/install_qdrant_key.sh` | Installs the store's single API key into a *client* repo's `.claude/settings.local.json`. Key on stdin only, atomic 0600 write, refuses a target git would commit |
| `scripts/pre-ship.sh` | `shipping-work`'s gate (#3): CI's steps, in CI's order, stops at the first failure. Dev-only; clients never run it |
| `tests/deploy/` | Behaviour and drift tests for the above |

**Clients run `install_qdrant_key.sh` from a read-only clone of this repo** (public, no credential), never a copy: its tests pin exactly the properties a copy loses first. Procedure, and rotation: [docs/installing-the-key.md](docs/installing-the-key.md).

## Infrastructure

exe.dev VM `co-index` (`pdx`, 2 vCPU / 8 GB / 30 GB, proxy `private`), tailnet node `index`, `tag:index`. Clients reach `https://index.taild0fb76.ts.net:6333`. The short name fails TLS verification (D14).

| Unit | What | Check |
|---|---|---|
| `qdrant.service` | Docker, bound to the tailnet address only (D3) | collections + point counts |
| `ollama.service` | Docker, slim CPU image, `nomic-embed-text` (D5, D7) | `/api/tags` |
| `qdrant-cert-renew.timer` | weekly, `Persistent=true` | `systemctl list-timers` |
| `index-checkin.timer` | every 10 min → co-status's dead-man's timer (CannObserv/status) | a missed check-in alerts there |

`qdrant.service` and `ollama.service` are `Requires=docker.service`: **restarting Docker restarts both.**

**Where this repo is worked on: `co-index` itself** (D15, which amends D8). One checkout of *this* repo and a token scoped to it. **Never** a client checkout, a client-repo credential, or a Node toolchain on this host (VS Code's private runtime is not one, D15). Nothing here indexes (D11).

**Credentials:** `.env` (git-ignored, 0600) holds `GH_TOKEN`, scoped to this repo only. Load with `set -a; . ./.env; set +a`. Push without putting the token in argv or a URL:

```bash
git -c credential.helper= -c 'credential.helper=!f() { echo username=x-access-token; echo "password=$GH_TOKEN"; }; f' push
```

## Conventions

**Commits:** `#<n> [type]: <description>`, or `[type]: <description>` without an issue. Types: feat, fix, refactor, docs, test, chore.

**Dates:** UTC; ISO 8601.

**Cross-repo:** issues only on sibling cohort repos, never commits.

## Agent Skills

Vendored as submodules under `skills-vendor/` (gregoryfoster/skills, obra/superpowers), symlinked into `skills/` and `.claude/skills/`. Add or refresh with the `managing-skills` skill. A fresh clone needs `git submodule update --init`.
