---
title: Pin the embedding engine, detect a change, and re-embed the whole store when one is chosen
date: 2026-10-16
status: done
---

# Embedding engine changes (#11)

## Problem

A new Ollama release can change `nomic-embed-text`'s vectors while the model
digest, prefixes and dimensions stay the same. #11 measured exactly that
between 0.21.2 and 0.40.5: case-folding moved every chunk containing an
uppercase letter (median cosine 0.97, min 0.68 on a real index). SocratiCode
can't see the change: its index profile records model, prefix and dimensions,
not the engine. It also skips files whose hash hasn't changed. So after an
engine change, the store fills with mixed vectors and no error is raised.
D7 makes that worse here, because `linkedProjects` merges results across
collections by raw cosine. One stale collection mis-orders every cohort
search that touches it. `deploy/ollama-slim/Dockerfile` builds
`FROM ollama/ollama:latest`, so any rebuild can cross such a boundary without
anyone choosing to.

**Today's state, measured 2026-10-16.** No re-index is needed now:

- The engine is 0.34.0. It's the only version this host has ever run: five
  starts, and the journal goes back to first boot on 2026-09-11.
- 0.34.0 already case-folds (`embed(t) == embed(t.lower())`, bit-identical),
  so it's on 0.40.5's side of #11's change.
- 12 sampled chunks from each of the eight `codebase_*` collections,
  re-embedded on the live engine: cosine 1.000000, every one.
- 24 sampled chunks from each of the eight `context_*` collections: the
  same, 1.000000 every one. The text to rebuild is SocratiCode 1.14.0's
  artifact label, `context:<name>:<path.normalize(path)>`. Node keeps the
  trailing `/` of a directory artifact, and Python's `normpath` drops it. Until
  the rebuild matched that, three collections read 0.997–0.9999.
- A patching run never touches the image. #9's run restarted Ollama on the
  same image, which is safe because one engine is bit-identical from run to
  run. It listed the images as "outside apt, not assessed".

## Approach

The approach has three layers.

**(1) Pin.** `FROM ollama/ollama:0.34.0@sha256:684d…`. The local tag becomes
`socraticode/ollama-slim:0.34.0`, and `ollama-run.sh` names that tag. A drift
test holds the two to the same version.

**(2) Detect.** A committed fixture of probe texts (code, prose, uppercase,
lowercase) and their 0.34.0 vectors. `index-checkin.sh` embeds the probes and
reports the fault `ollama:engine` (the #10 key) when any cosine falls
below 0.99999. That catches an unpinned or accidental change within 10
minutes, whatever caused it.

**(3) Change the engine on purpose, store-wide, in one window.**

1. Run the candidate on a loopback side port.
2. Measure drift against a sample from every collection.
3. If there's no drift, swap the image and stop there.
4. If there is drift, **co-index re-embeds every point itself, from its stored
   payload.** Each point already carries the exact text SocratiCode embedded:
   - code: `search_document: <relativePath>\n<content>`;
   - artifacts: `search_document: context:<name>:<path>\n<content>`.

   The `dense` vector is overwritten in place. IDs, payloads and BM25 text are
   untouched, and no client has to act.

A collection whose text can't be rebuilt exactly falls back to its owner's
`codebase_remove` + `codebase_index`, which goes out as an issue on that repo.

## Tradeoffs / alternatives

- **Each client re-indexes its own repo** (`codebase_remove`/`codebase_index`
  plus the `context_` pair). Rejected as the default:
  - It takes eight sessions on eight VMs in one window, and every one has to
    finish.
  - Until the last one finishes, D7's cross-collection merge is mixed.
  - Kept as the fallback for any collection that fails the rebuild gate.
- **Host drops every collection and lets clients rebuild from empty.** It
  fails loudly, which is better than failing silently. Rejected because each
  repo's search goes dark until its owner acts, and a watcher writing into a
  dropped collection may recreate a partial one that looks indexed.
- **Blue/green: a second Ollama, each client switches when ready.** Rejected:
  collection names are fixed (D12 forbids a prefix), so there's no second set
  to fill, and the switch-over mixes engines across collections.
- **Let it mix and drift out as files change.** Rejected. #11 measured it, and
  unchanged files never re-embed.
- **Never upgrade.** That's what pinning makes the default. Ollama here is
  tailnet-only and unauthenticated by design (D4), so an upgrade needs a
  reason (a fix, speed, a model). Nobody needs to track releases.

## Steps

1. **Pin (TDD).**
   - A test fails while the Dockerfile's `FROM` lacks a version and a digest,
     or while `ollama-run.sh`'s tag differs from that version.
   - Pin both, rebuild, and restart `ollama.service`.
   - Verified when step 2's fingerprint still matches.
2. **Fingerprint (TDD).**
   - `deploy/engine-fingerprint.json`, captured from the live 0.34.0 before
     the rebuild.
   - A check-in test: matching probes send `ok`, a drifted probe sends the
     fault `engine:fingerprint`.
   - Install, then see the next check-in report `ok`.
   *Revised in execution:* the key is `ollama:engine`, matching the other
   `ollama:*` checks. Exit 2 (couldn't embed, e.g. a timeout behind a
   client's batch) is no finding.
3. *Revised in execution: steps 3 and 4 are one script, `scripts/reembed.py`.*
   `python3 -I` takes the script's directory off `sys.path`, so two scripts
   couldn't share the text rebuild without a package. A drift check is a
   dry run on a sample (`--dry-run --sample N`).

   Originally: **`scripts/engine-drift.py`** (stdlib, `python3 -I`). This generalises
   today's sampler: rebuild each point's text, embed it on a given Ollama URL,
   and report cosine against the stored vector for each collection.
   - Tested against a stubbed Qdrant and Ollama.
   - Run against the live engine, every collection must read ≥ 0.999999
     (the rebuild gate).
4. **`scripts/reembed.py`.**
   - Scroll each collection, rebuild the text, embed on the target engine, and
     `PUT /points/vectors` the `dense` vector only.
   - `--dry-run` writes nothing and reports the gate and the time taken.
   - It refuses a collection that's `in-progress` in `socraticode_metadata`,
     or that has written in the last 5 minutes (`index-inflight.sh`).
   - Tested against stubs.
   - A full dry run on 0.34.0 gives D6's missing timed figure.
5. **Runbook and knob.**
   - `deploy/README.md` gains an "Engine change" section with the procedure
     below.
   - D19 in the design record allows the host-side re-embed (see the open
     questions).
   - The knob gains `owner socraticode/ollama-slim self`, so a patching run
     reports the image as ours and never upgrades it.
6. **Procedure, when an upgrade is chosen**, inside a knob window:
   1. Run the candidate on `127.0.0.1:11435` and run `engine-drift.py`
      against it.
   2. **No drift:** pin the new version, swap, refresh the fingerprint, and
      stop there.
   3. **Drift:** file a heads-up issue on each cohort repo, using
      `GH_TOKEN_<REPO>`, at least a day ahead: "search degraded in the window;
      don't index".
   4. Take a recovery point: `qdrant-recovery.sh snapshot`, plus the old image
      kept under its tag.
   5. Swap the image, then run `reembed.py`.
   6. Run `engine-drift.py` against the new engine: every collection must read
      ≥ 0.999999.
   7. Then `qdrant-recovery.sh check`, and refresh the fingerprint.
   8. **Rollback:** the old tag plus the snapshot.
7. **Answer #11** with the findings and this plan's link, then close it when
   steps 1–5 ship.

## Open questions / risks

- **D11 says "nothing here indexes."** A host-side re-embed writes to
  collections their own hosts write to. Is a D19 amendment acceptable when
  it's limited to an owner-approved window, changes only the `dense` vector,
  and is gated on exact reproduction? Without it, step 6 drift falls back to
  eight client re-indexes.
- **The rebuild depends on SocratiCode's text format.** If 1.16 or a later
  version changes `prepareDocumentText` or the artifact label, the rebuild
  gate fails and that collection falls back. The gate is the guard, not
  trust. Today's trailing-slash miss shows a near-miss reads as 0.999, not as
  an error. So the gate is cosine ≥ 0.999999 for every point in a full dry
  run, not a sample's median.
- **Throughput is unmeasured.** The store holds about 48k chunks on 2 vCPU,
  with `OLLAMA_NUM_PARALLEL=1`. If step 4's dry run doesn't fit one window,
  either the re-embed runs per collection across windows (accepting mixing
  between collections for days), or the window grows.
- **A client indexing during the window** writes old-engine vectors before the
  swap and new-engine ones after it. The in-flight refusal and the heads-up
  issue are the only guards. A late writer's points still pass the post-swap
  check, because they're new-engine.
- **Tolerance.** Today, 0–2 of every 24 stored vectors aren't bit-identical
  to a fresh embedding but still read cosine 1.000000. So "exact" can't be the
  test. The test is cosine ≥ 0.999999 for the gate, and 0.99999 for the
  fingerprint. #11's smallest real drift was 0.88 for a query and 0.68 for a
  chunk, far below either.
