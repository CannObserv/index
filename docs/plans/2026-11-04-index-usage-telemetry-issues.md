---
title: File the issue suite for index usage telemetry and setup checks
date: 2026-11-04
status: in progress — epic #32 (A–F #33–#38, G–H gregoryfoster/skills#573–#574); step 6 waits on #34
---

# File the issue suite for index usage telemetry and setup checks

## Problem

The approved design
([2026-11-04-index-usage-telemetry-design.md](2026-11-04-index-usage-telemetry-design.md))
ends in an issue suite across three places: this repo, gregoryfoster/skills,
and ten cohort repos. Its order and gates matter. The cohort rollout waits on
the pilot's measured size and variable set, and the classification tables
wait on the operator's labelling. Filed loosely, the issues would let a
cohort VM switch telemetry on before anyone knows what it costs on disk.

## Approach

File an **epic here** that holds the suite and its gates, then the children
in dependency order. Each child's body is drawn from its design section,
links the design, and states what it waits on. This repo's children and the
two upstream issues are filed now. The ten cohort issues are filed **after
the pilot closes**, carrying the measured variable set, so none of them goes
out with a guess. Every cross-repo issue uses its own `GH_TOKEN_<REPO>`
(D18).

## Tradeoffs / alternatives

- **File all ten cohort issues now, marked blocked.** Rejected: their core
  content (the client `env` block) is the pilot's to confirm, so each would
  need an edit later, in ten repos.
- **Cross-linked issues, no epic.** Rejected: the gates (pilot → rollout,
  labelling → classification tables) need one place to live, and the
  operator already runs burn-downs as epics (#22, `coordinating-epics`).
- **One upstream issue, not two.** Rejected: the health hook change (6) and
  the init-socraticode change (7) land in different files with different
  reviewers, and 7 is half-blocked on the pilot while 6 isn't.

## Steps

1. **Commit** the design and this plan: `[docs]: index usage telemetry —
   design and issue plan`. Push.
2. **Epic** on CannObserv/index: intent in two lines, a link to the design,
   the child list with gates, and the reach rule for markers. Children are
   added as they're filed.
3. **This repo's children**, in order, each linking the epic and design:
   - **A**, D21: collector unit, pipeline (keeps, drops, redaction), storage
     and caps, ACL `:4318` plus the co-processor and address-validator
     tags, check-in findings, knob, runbook, D21 into the design record.
   - **B**, Pilot on `co-index`: at least three days, sizes per event type,
     drop verification, VS Code extension check, Claude Code ≥ 2.1.310.
     Waits on A. Its close posts the confirmed variable set and the 90-day
     projection for ten VMs.
   - **C**, Markers log and convention; first entry "collection on". Waits
     on B.
   - **D**, `scripts/usage_report.py`: counts, rules, ambiguous loop,
     agreement, efficiency tables, `data/` git-ignored, synthetic fixtures.
     Can start once A defines the on-disk format.
   - **E**, Operator labels the first 100–150 calls. Waits on D plus enough
     data. Gates D's classification tables.
   - **F**, Store-side link check into `index-checkin.sh`. Independent.
4. **Upstream, gregoryfoster/skills**, under `GH_TOKEN_SKILLS`:
   - **G**, `socraticode-health.sh`: stub vs full clone (with size), and
     linked collections that are missing or empty. Independent.
   - **H**, `init-socraticode`: emit the host's stub step when a link is
     added (start now). Document the user-scope telemetry block (waits on
     B, which posts the variable set there when it closes).
5. **Verify the filing**: every child is linked from the epic, every
   cross-repo issue links back to it, and no cohort issue is filed yet.
6. **On B's close**: file the ten cohort issues (notifier, watcher,
   archiver, replicator, broker, status, observo, power-map, processor,
   address-validator), each via its own token. Each covers the one-time
   audit (stubs not clones, links resolve, `env` block applies, Claude Code
   version) and the telemetry enable with B's confirmed block. `status`
   gets the telemetry half only. Add them to the epic.

## Open questions / risks

- **#18 (SocratiCode on `co-index`) is still open**, so the pilot's sessions
  will make few `codebase_*` calls. That's fine for sizing, which is the
  pilot's job, but it means the first index-call counts come from the
  rollout, not the pilot.
- **Labels:** none assumed. Say if the epic or children should carry any.
- **`observo`'s tag** is `observo-primary`. The issue goes to the
  `CannObserv/observo` repo, under `GH_TOKEN_OBSERVO`.
