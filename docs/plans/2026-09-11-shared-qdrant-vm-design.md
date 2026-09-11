# A shared index VM for the cohort — `co-index`

**Status:** approved — D1 (`co-index`, 2 vCPU / 8 GB / 30 GB) and D6 settled by the operator,
2026-09-11. Phases 0–1 in progress; Phase 2 needs an explicit go before `new` runs
**Issue:** [#57](https://github.com/CannObserv/notifier/issues/57)
**Cohort:** the fifth VM, and the first that is not a service. Pattern from
[notifier#43](https://github.com/CannObserv/notifier/issues/43); phase spine and
provisioning lessons from
[replicator#88](https://github.com/CannObserv/replicator/issues/88)
**Measurements:** [#57 comment](https://github.com/CannObserv/notifier/issues/57#issuecomment-5640555349)
— Q1–Q5 answered against `socraticode@1.13.3`'s source

## Problem

Two failures, one cause.

**Cross-repo search is gone.** `SOCRATICODE_LINKED_PROJECTS` points a project at
sibling checkouts **by absolute path**. That worked only while all four services
shared `/home/exedev` on one box. #43 moved notifier out; broker, archiver and
replicator followed. No VM has a sibling checkout any more, and nothing
announced the loss.

**The install itself is ephemeral.** Measured on this VM today: no `node`, no
`~/.claude/plugins`, no Qdrant image, no Qdrant volume. `.git/socraticode-health.log`
has five entries and every one of them says `node not on PATH — skipped`, the
oldest 2026-08-31. The daily hook reported it five times and repaired nothing,
by design (#21). The last measured index is the 2026-08-22 figure still written
into `docs/SOCRATICODE.md`.

Everything that vanished lived under `$HOME` or in Docker's local state.
Everything that survived is in git. **The durable half belongs off the service
VM.**

And the rebuild cost is paid four times: a first index pulls the Qdrant image,
the Ollama image and `nomic-embed-text` before embedding starts, then spends
~75 min per ~1100 files on a 2-vCPU box — four times over, for four disjoint
indexes that cannot see each other.

## What the measurements changed

Read the [#57 comment](https://github.com/CannObserv/notifier/issues/57#issuecomment-5640555349)
for the citations. Four results move this design:

1. **Nothing is persisted locally.** Six Qdrant collections per project — code
   chunks, dependency graph, context artifacts, and three symbol-graph shards —
   plus one instance-global `socraticode_metadata`. The only disk writes in the
   package are a lockfile and a log. Centralizing Qdrant centralizes *all* of
   it, not embeddings only. The issue's hedge is struck.
2. **Cross-repo search needs a checkout on disk, but not an indexed one.**
   `loadLinkedProjects()` gates on `fs.existsSync`; the collection it then
   addresses comes from the linked repo's own `projectId`, not from its path. A
   bare read-only clone is enough.
3. **Node is required wherever sessions run**, because the MCP server is a local
   `npx` subprocess. Docker is what a shared store removes, not the toolchain.
   That decides Q4 against shape (b), which had no other argument.
4. **Cross-collection results are ordered by raw cosine**, so two collections
   embedded by different models are silently mis-ordered rather than rejected.
   D7 is load-bearing.

## Decisions

| # | Decision | Rationale |
|---|---|---|
| **D0** | **Every cohort repo commits `.socraticode.json` with a fixed `projectId`.** Lands **first**, needs no VM. Notifier by commit; the other three by issue. | Without it the project id is `sha256(abs_path)[:12]`, so every git worktree is a separate project with its own six collections and its own ~75-minute first index — this repo uses worktrees routinely. It is also the durable fix for the path fragility that opened this issue, and it makes the shared store legible: `codebase_notifier`, not `codebase_a1b2c3d4e5f6`. |
| **D1** | **exe.dev VM `co-index`, tailnet `index`, `tag:index`, `pdx`**, **2 vCPU / 8 GB / 30 GB**, proxy `private`. Never `set-region`. If exe.dev refuses the name, **stop and ask** — do not improvise. | The cohort `co-<name>` pattern with a bare tailnet name. Named for the role, not the product, so swapping Qdrant later does not make the node name a lie. **2 vCPU is a deliberate acceptance, not an oversight:** embedding is CPU-bound and now shared, so concurrent first-indexes queue behind each other. That cost is paid once per repo and is unattended; RAM and disk are where a shared store actually grows, hence 8 GB / 30 GB against the cohort's 4/20. Revisit only if steady-state incremental indexing — not the first index — is observed to queue. |
| **D2** | **Single-tag Tailscale key, tag set before the first join.** | Tags bind at device registration; `tailscale up --reset` does not retag, and a multi-tag key cannot be narrowed with `--advertise-tags` (#43 F1). |
| **D3** | **Both services bind this host's tailnet address alone**, never `0.0.0.0`, published that way by Docker (`-p <tailnet-ip>:6333:6333`). The address is resolved in the same process that binds. | Same reasoning as `notifier.service`, and the same trap: systemd reads every `EnvironmentFile=` before `ExecStartPre` runs, so no pre-step can put the address into the environment `ExecStart` sees. Reuse `scripts/tailnet_bind.sh` (`/proc/net/fib_trie`, never `ip addr`; non-zero on timeout). |
| **D4** | **Qdrant on :6333 with `QDRANT_API_KEY` set. Ollama on :11434 with no authentication, and that is written down.** | Tailnet-only is not authorization. The asymmetry is real: Ollama has no auth to enable, and `/api/pull` lets any cohort node pull an arbitrary model onto the shared box. Accepted with the note rather than proxied — a path-filtering reverse proxy costs more than the exposure is worth on a node with no production role (D9). |
| **D5** | **One shared Ollama, `OLLAMA_MODE=external` set explicitly on every client**, running replicator#88's slim CPU-only image retagged `ollama/ollama:latest`. | The default `OLLAMA_MODE` is `auto`, which falls back to a **Docker** pull when it finds no local Ollama — on a VM with no Docker that is a slow, confusing failure. The slim image is 221 MB against 9.19 GB and its embeddings are bitwise identical to the fat image's, so it forces no re-index and fragments no vectors. Its Dockerfile lands in `deploy/index/ollama-slim/`, answering the "say where it belongs" in replicator#88. **The `ollama/ollama:latest` tag is load-bearing** — `ensureOllamaContainerReady` guards on presence only, so an untag or a `docker image prune -a` silently re-pulls 9.19 GB. |
| **D6** | **Persistent volume, and the restore path is a re-index — not a backup.** Success is a **timed** from-empty re-index in the runbook, not an exercised restore. | The entire store is derived from four git repos. A volume keeps a reboot from costing hours; engineering backup-and-restore for derived data buys less than it costs. Operator's call — this is a deliberate reduction of the acceptance criteria as filed. |
| **D7** | **One embedding backend, pinned explicitly cohort-wide:** `EMBEDDING_MODEL=nomic-embed-text`, `EMBEDDING_DIMENSIONS=768`, set in each VM's environment rather than left to the provider default. | Cross-collection merge orders by raw cosine, which is comparable only within one model. Mixed models give **silently mis-ordered** cross-repo results — no error. Pinning both values makes the shared premise explicit at each client instead of implicit in one library default. |
| **D8** | **Shape (a): each service VM indexes its own repo.** `co-index` holds no checkout, no repo credential and no Node. Sibling repos are **read-only HTTPS clones** under `/home/exedev`, and `linkedProjects` is committed as `["../archiver", "../broker", "../replicator", "../watcher"]` — relative, so it is portable. | Q4. Shape (b)'s one advantage — a single toolchain — does not exist, because Node is needed wherever sessions run. (b) also wants read access to all four repos and a checkout-refresh job. Relative linked paths mean a VM missing a sibling clone simply searches less; **the health hook reports resolved-vs-configured**, so that degrade is announced rather than silent. |
| **D9** | **Nothing on any production path depends on `co-index`.** It is developer tooling. An outage degrades search on four VMs and stops no service. | Scopes D4's accepted exposure, D6's dropped backup, and the ACL below. Worth stating because a fifth always-on VM otherwise looks like a fifth thing that can take the cohort down. |
| **D10** | **`co-index` checks in to notifier's dead-man's timer (#56)** on a systemd timer. | The second argument in #57 is that a per-VM install fails *silently*. A store whose absence is announced is the fix, and this repo already runs the mechanism. It makes `tag:index` a `src` in exactly one rule — the same amendment `tag:broker` took in broker#3. |
| **D11** | **One host per `projectId`, enforced by convention.** | The index lock is `os.tmpdir()/socraticode-locks/<projectId>-<op>` — **host-local**. A shared Qdrant gives no shared lock, so two hosts indexing one project write the same collections concurrently with nothing stopping them. Natural under D8; written down so nobody later adds a helpful central re-index cron. |
| **D13** | **A temporary `tag:notifier → tag:index:22` admin edge, and `--ssh` at join, both removed at the end of Phase 3.** | D1 says `tag:index` exposes no service port and the host is administered over public `ssh co-index.exe.xyz`. That is the steady state, and it is not reachable from a session on another exe.dev VM — exe.dev VMs are isolated from each other, which is the premise the tailnet exists to answer. Without a build-phase edge there is no host from which to run Phase 3. Replicator#88 D5 is the proven shape, including the trap it names: Tailscale SSH needs **both** an `acls` rule and an `ssh` block — without the network rule the node never appears in the peer's netmap and the `ssh` rule is never consulted, which presents as a DNS failure rather than a permission denial. |
| **D12** | **`QDRANT_COLLECTION_PREFIX` is never set, and `SOCRATICODE_BRANCH_AWARE` is never `"true"`.** Asserted by a test, like `NOTIFIER_BIND_HOST`. | The prefix is prepended to the global `socraticode_metadata` collection too, so one VM setting it splits the cohort namespace silently. Branch-awareness appends the branch to the project id: a fresh six-collection set per branch. Both are the same failure class — a widening or a fragmenting that every health check still calls green. |

## Design

### Client configuration

Six variables, identical on all four VMs except the key:

```
QDRANT_MODE=external
QDRANT_URL=http://index:6333          # explicit; see the trap below
OLLAMA_MODE=external
OLLAMA_URL=http://index:11434
EMBEDDING_MODEL=nomic-embed-text
EMBEDDING_DIMENSIONS=768
QDRANT_API_KEY=…                      # secret, per host, never committed
```

**`QDRANT_URL` must be set, not `QDRANT_HOST`.** Two reasons. `QDRANT_MODE=external`
refuses to start without it. And the fallback it would otherwise take is
`` `${QDRANT_API_KEY ? "https" : "http"}://${QDRANT_HOST}:${QDRANT_PORT}` `` —
an API key with no URL makes the client assume **https** against a plaintext
tailnet endpoint, and the resulting TLS error reads like a network fault.
`QDRANT_PORT` defaults to **16333** (the managed container's host-side mapping),
not to Qdrant's own 6333, which is the second half of the same trap.

**Delivery — settled by measurement, 2026-09-11.** Claude Code's `env` settings
key is documented as setting variables "for every session and its subprocesses",
naming MCP servers, hooks and Bash tool calls, and it is settable from any file.
The plugin's `.mcp.json` declares no `env` of its own, so the server inherits
exactly that. Therefore:

- the five non-secret values go in this repo's committed
  `.claude/settings.json` `env` block, where they are self-documenting and
  travel with the checkout;
- `QDRANT_API_KEY` goes in `.claude/settings.local.json`, already git-ignored.

**The caveat is a silent one, so it is written down rather than discovered.**
Most `env` values "apply only after each teammate trusts the folder". On a
freshly cloned cohort VM, an untrusted workspace means the block does not
apply, `QDRANT_MODE` reverts to `managed` and `OLLAMA_MODE` to `auto` — and
SocratiCode then tries to start **Docker containers** on a host that has none,
rather than reporting a missing configuration. Phase 4's gate therefore
includes trusting the folder and confirming `codebase_health` names the
external endpoints, not a container.

Rejected fallback, which needed none of that: `/etc/socraticode/env` sourced
from a profile snippet, key at 0600. It works unconditionally but puts the key
in every process's environment on the host and keeps the cohort's shared
configuration out of version control, where the trust gate above is the only
thing it buys.

### ACL

```jsonc
{
  "tagOwners": { "tag:index": ["autogroup:admin"] },
  "acls": [
    // The four service VMs are clients of the store. Two ports, nothing else.
    { "action": "accept",
      "src": ["tag:notifier", "tag:watcher", "tag:archiver", "tag:replicator"],
      "dst": ["tag:index:6333,11434"] },
    // D10: co-index checks in to notifier's dead-man's timer. Port 9000 only,
    // production key — same shape as tag:broker's rule, and the same reason.
    { "action": "accept", "src": ["tag:index"], "dst": ["tag:notifier:9000"] },
    // D13: BUILD PHASE ONLY. Removed at the end of Phase 3, with the ssh block.
    { "action": "accept", "src": ["tag:notifier"], "dst": ["tag:index:22"] }
  ],
  "ssh": [
    // D13: build phase only. Without the acls rule above this is never consulted.
    { "action": "accept", "src": ["tag:notifier", "autogroup:member"],
      "dst": ["tag:index"], "users": ["exedev", "root"] }
  ]
}
```

**In the steady state** `tag:index` opens no port 22: administer over the public
`ssh co-index.exe.xyz`, as this host does. The `:22` edge and the `ssh` block
above are D13's build-phase scaffolding and come out at the end of Phase 3. A peer is visible only through an `acls` rule — an `ssh`
block alone leaves the node absent from the netmap, where a missing rule looks
like a DNS failure rather than a permission denial (archiver#193, replicator#88).

**This inverts notifier's posture, and the doc has to say so.**
`docs/reference/tailscale.md` currently asserts *"notifier still lists no rule
with itself as a `src`: notifier initiates nothing across the tailnet —
verified, not assumed."* Both halves of that die here: notifier gains an
outbound edge to `index`, and `index` gains one back to notifier. The claim is
amended in Phase 5, not left standing.

### What is centralized, and what is not

| Capability | After this change |
|---|---|
| `codebase_search` on this repo | shared store |
| `codebase_search` **across cohort repos** | shared store, **`includeLinked: true`**, sibling clone present |
| `codebase_impact`, `graph_query`, `flow`, `symbol` | shared store, **single project always** |
| `codebase_context_search` | shared store, **single project always** |

`resolveLinkedCollections` has exactly one caller in the package. The honest
claim is **cross-repo semantic code search** — never cross-repo impact
analysis — and `docs/SOCRATICODE.md` says it in those words.

`includeLinked` defaults to **false**, so the `AGENTS.md` policy block must tell
agents to pass it for cross-repo questions. Without that line the capability
exists and nobody uses it.

## Testing

TDD, in the repo, before any VM exists:

- `tests/deploy/test_socraticode_config.py` — `.socraticode.json` parses, its
  `projectId` is `notifier`, and `linkedProjects` are relative (D0, D8).
- The `NOTIFIER_BIND_HOST` pattern extended to D12: neither
  `QDRANT_COLLECTION_PREFIX` nor `SOCRATICODE_BRANCH_AWARE=true` appears in any
  env file, unit, or settings file.
- `tests/deploy/test_index_unit.py` — the two container units order
  `After=tailscaled.service` with no `Wants=`, and publish to a resolved
  address rather than `0.0.0.0` (D3).

Unmeasurable from here and therefore a phase gate, not a test: that a shared
store answers a query at all, and that a linked search crosses repos.

**The health hook is vendored, so Phase 1's step 5 is not a local edit.**
`.claude/hooks/socraticode-health.sh` is a symlink into
`skills-vendor/gregoryfoster-skills/`, whose copy is upstream's. Changing what
it reports is either an upstream change (an issue on that repo, never a commit)
or a local override under `skills/init-socraticode/` that shadows the vendor
copy — the shape `brainstorming` and `shipping-work-python-fastapi` already
take here. An override forks a file that upstream will keep improving, so the
default is the upstream issue; the override is for the case where this repo
needs the behaviour before upstream ships it. Decided in Phase 1, recorded
here.

## Plan

Replicator#88's spine, with a Phase 0 that is pure profit whether or not the VM
is ever built.

### Phase 0 — `.socraticode.json` (no VM)

1. Commit it here. File the sibling issues (D0) — **issues, never commits**.
2. Re-index this repo against whatever store exists at the time and confirm the
   collections move to the new names. This alone fixes the worktree tax.

### Phase 1 — repo-side, lands before provisioning

3. The tests above, then the config they pin.
4. ~~Decide env delivery by measurement.~~ **Done** — settings `env`, with the
   workspace-trust caveat above. The block itself lands in Phase 4, not here:
   pointing this repo at `http://index:6333` before that host exists would
   commit a claim that is false for as long as Phase 2 takes.
5. ~~Teach `socraticode-health.sh` three things.~~ **Two, and both filed
   upstream** as [gregoryfoster/skills#281](https://github.com/gregoryfoster/skills/issues/281):
   past the manifest gate a missing `node` is a finding rather than a silent
   skip, and configured-vs-resolved `linkedProjects` is a daily line so D8's
   silent skip is announced. **The third was wrong and is not filed** —
   `codebase_health` already branches on `QDRANT_MODE` (`manage-tools.js:79`),
   reporting an endpoint and "Docker is not required for the vector database"
   rather than a stopped container, and the hook forwards that unchanged. An
   external-mode host is already handled end to end. Carried as a gap until
   upstream ships; no local override.

### Phase 2 — provision `co-index`

6. `new --name co-index --cpu 2 --memory 8GB --disk 30GB --setup-script …`.
   The shred sits in a **`trap`**, set first, so an abort at `tailscale up`
   cannot leave the key behind (archiver's did).
7. **No secret in a `tailscale ssh` argv** — argv lands in the node's journal
   (replicator#88). Everything sensitive travels on stdin.
8. Verify before any data: hostname, `tag:index`, `CorpDNS: true`, a **direct**
   path to each service node, and that no service node can open a connection
   the ACL does not list. Then reboot: same node id, same IPs, same tag.

### Phase 3 — build the store

9. Qdrant and the slim Ollama as systemd-managed containers on a persistent
   volume, bound per D3, API key per D4, `nomic-embed-text` pulled once.
10. Gate: `GET /collections` answers **with** the key and **401s without** it;
    unreachable from the exe.dev proxy and the internet; survives a reboot with
    the volume intact; `POST /api/embed` returns 768 dims.
11. D10's check-in timer, and one deliberate miss to prove the alert fires.

### Phase 4 — notifier adopts it

12. Node + the plugin back on this VM. Point at the store, index from empty,
    **time it**, and record the figure (D6).
13. Gate: `codebase_search` returns hits from this repo; `codebase_impact`
    answers non-empty; `codebase_context_search` finds all five artifacts.
14. Clone one sibling read-only and prove `includeLinked: true` crosses repos —
    **or** record Q1 as negative and restate the scope honestly. The failure
    mode is silence, so the negative case is proved by a collection that exists
    and still returns nothing, never by an empty result alone.

### Phase 5 — docs

15. `AGENTS.md`: the Code Exploration Policy gains `includeLinked` for
    cross-repo questions; Infrastructure gains the fifth node. Net-neutral
    against the context budget.
16. `docs/SOCRATICODE.md` **below the END marker only** — the generated half is
    do-not-hand-edit. New measured yield, the single-project limits, the
    re-index timing.
17. `docs/reference/tailscale.md`: the new node, the new rules, and the
    correction to the "initiates nothing" claim.

### Phase 6 — soak

18. ≥ 24 h, including a reboot of `co-index` with a service VM querying across
    it. Rollback until then is a local `QDRANT_MODE=managed` — the repos are
    the source of truth, so rollback costs a re-index and nothing else.

### Phase 7 — the cohort adopts

19. One issue per sibling repo: `.socraticode.json`, the six variables, a read-only
    sibling clone set, and the health-hook change. **Issues, not commits.**

## Risks

1. **Two hosts index one project.** The lock is host-local (D11). Convention is
   the only guard; a re-index cron is the realistic way it gets violated.
2. **A model swap silently re-orders every cross-repo result.** D7 pins both
   values; nothing upstream errors on mismatch.
3. **`docker image prune -a` re-pulls 9.19 GB.** The `:latest` tag on the slim
   image is load-bearing (D5).
4. **The store becomes a habit and then an outage.** Bounded by D9: dev tooling
   only, no production path. The degrade is `grep`.
5. **The shared embedder serializes four indexers.** Accepted at D1's 2 vCPU.
   It is a first-index cost, not a steady-state one: incremental updates embed
   only changed files. The trigger to revisit is *incremental* runs queueing.
6. **A fifth VM's own install decays the same way this one did.** D10 is the
   answer, and it is the thing #57 says was missing the first time.

## Success criteria

- [ ] Q1–Q3 answered by measurement and recorded in #57 **before** provisioning — **done**
- [ ] `.socraticode.json` committed here, sibling issues filed (D0)
- [ ] `co-index` in `pdx`, 2 vCPU / 8 GB / 30 GB, `tag:index`, survives a reboot
      with the same identity
- [ ] Qdrant reachable on the tailnet, **401 without the key**, unreachable from the
      exe.dev proxy and the internet
- [ ] Data volume survives a reboot; a **from-empty re-index is timed** and in the
      runbook (D6 — replaces the exercised-restore criterion as filed)
- [ ] `notifier` indexes into it; `codebase_search`, `codebase_impact` and
      `codebase_context_search` all answer
- [ ] Cross-repo search demonstrated with `includeLinked: true` against a second
      repo, **or** Q1 recorded negative and the scope restated in `docs/SOCRATICODE.md`
- [ ] `AGENTS.md` tells agents to pass `includeLinked` for cross-repo questions
- [ ] The health hook distinguishes a missing `node` from an expected missing
      container, and reports unresolved `linkedProjects`
- [ ] `docs/reference/tailscale.md` no longer claims notifier initiates nothing
- [ ] `co-index` checks in to a dead-man's timer, and a miss has been seen to fire
- [ ] Sibling repos have their own adoption issues — **not** commits

## Out of scope

- Retiring the per-VM Node toolchain. It is required wherever sessions run (Q5).
- Authenticating Ollama (D4), and any reverse proxy in front of it.
- Backup and restore of derived data (D6).
- Moving `deploy/index/` to the observo repo. Notifier owns it because notifier
  is the only cohort repo carrying SocratiCode config; revisit if a second
  non-service VM appears.
