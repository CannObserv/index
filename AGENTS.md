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
| `tests/deploy/` | Behaviour and drift tests for the above |

**Clients run `install_qdrant_key.sh` from a read-only clone of this repo** (public, no credential). They do not vendor it: its tests pin exactly the properties a copy loses first.

## Infrastructure

exe.dev VM `co-index` (`pdx`, 2 vCPU / 8 GB / 30 GB, proxy `private`), tailnet node `index`, `tag:index`. Clients reach `https://index.taild0fb76.ts.net:6333`. The short name fails TLS verification (D14).

| Unit | What | Check |
|---|---|---|
| `qdrant.service` | Docker, bound to the tailnet address only (D3) | collections + point counts |
| `ollama.service` | Docker, slim CPU image, `nomic-embed-text` (D5, D7) | `/api/tags` |
| `qdrant-cert-renew.timer` | weekly, `Persistent=true` | `systemctl list-timers` |
| `index-checkin.timer` | every 10 min → co-status's dead-man's timer (CannObserv/status) | a missed check-in alerts there |

`qdrant.service` and `ollama.service` are `Requires=docker.service`: **restarting Docker restarts both.**

**Where this repo is worked on: `co-index` itself** (D15, which amends D8). One checkout of *this* repo and a token scoped to it. **Never** a client checkout, a client-repo credential, or Node on this host. Nothing here indexes (D11).

## Conventions

**Commits:** `#<n> [type]: <description>`, or `[type]: <description>` without an issue. Types: feat, fix, refactor, docs, test, chore.

**Dates:** UTC; ISO 8601.

**Cross-repo:** issues only on sibling cohort repos, never commits.
