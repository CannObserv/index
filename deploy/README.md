# `co-index` — the cohort's shared index host

Design: [`docs/plans/2026-09-11-shared-qdrant-vm-design.md`](../../docs/plans/2026-09-11-shared-qdrant-vm-design.md).
Issue: [#57](https://github.com/CannObserv/notifier/issues/57).

One VM running Qdrant and Ollama for all four cohort services, so the vector
store survives what took the per-VM installs out: everything that vanished
lived under `$HOME` or in Docker's local state, everything that survived is in
git.

**Nothing on a production path depends on this host** (D9). An outage degrades
`codebase_search` on four VMs and stops no service. The degrade is `grep`.

## Phase 2 — provisioning

### Operator prerequisites

Neither is present in this repo's `.env` today, and both are operator-only.
Phase 2 cannot start without them.

| | |
|---|---|
| `EXE_API_TOKEN` | scoped to at least `new` and `whoami`. `ssh exe.dev whoami` from this host returns `Permission denied (publickey)` — there is no registered identity here, so the HTTPS `exec` path is the one available to an agent. |
| `TAILSCALE_KEY_INDEX` | **single-tag `tag:index`, pre-approved, non-ephemeral, minted before the first join.** A multi-tag key applies all its tags and cannot be narrowed with `--advertise-tags`. Tags bind at registration: `tailscale up --reset` does not retag (notifier#43 F1). |

Also before `new`: confirm `tag:index` is declared in `tagOwners`, and add the
ACL below. **A peer is visible only through an `acls` rule** — an `ssh` block
alone leaves the node out of the netmap, where the missing rule presents as a
DNS failure rather than a permission denial.

### ACL

```jsonc
{
  "tagOwners": { "tag:index": ["autogroup:admin"] },
  "acls": [
    { "action": "accept",
      "src": ["tag:notifier", "tag:watcher", "tag:archiver", "tag:replicator"],
      "dst": ["tag:index:6333,11434"] },
    { "action": "accept", "src": ["tag:index"], "dst": ["tag:notifier:9000"] },
    // D13: BUILD PHASE ONLY — removed at the end of Phase 3, with the ssh block.
    { "action": "accept", "src": ["tag:notifier"], "dst": ["tag:index:22"] }
  ],
  "ssh": [
    { "action": "accept", "src": ["tag:notifier", "autogroup:member"],
      "dst": ["tag:index"], "users": ["exedev", "root"] }
  ]
}
```

### Create the VM

Render `setup.sh.template` with the key — **to a 0600 file outside the repo**,
never into a `git add`able path — then:

```
new --name co-index --cpu 2 --memory 8GB --disk 30GB --setup-script <rendered>
```

2 vCPU is a deliberate acceptance (D1): embedding is CPU-bound and shared, so
concurrent *first* indexes queue. 8 GB / 30 GB against the cohort's 4/20
because RAM and disk are where a shared store actually grows.

**If exe.dev refuses the name `co-index`, stop and ask.** Do not improvise one —
archiver burned a provisioning round discovering that every `archiver*` name was
taken, and the name it settled on (`co-registrar`) is now load-bearing in two
repos' docs.

**Never run `set-region`.** `pdx` is not among its values, so any call moves
this node out of the region the rest of the cohort is in.

### Verify before any data lands

- `hostname` is `co-index`; the tailnet name is `index`, `tag:index`, non-expiring
- `CorpDNS: true` in `tailscale debug prefs`
- the path from each service node is **direct**, not DERP
- **no service node can open a connection the ACL does not list** — check the
  negative, not just the positive
- reboot: same node id, same IPs, same tag, `tailscaled` active, `NRestarts=0`

## Contents

| | |
|---|---|
| `setup.sh.template` | first-boot script; `__TAILSCALE_KEY__` substituted at provision time. Joins the tailnet and nothing else. |
| `ollama-slim/Dockerfile` | CPU-only Ollama, 221 MB against 9.19 GB, embeddings bitwise identical to the stock image (CannObserv/replicator#88). |

**The `ollama/ollama:latest` tag on the slim image is load-bearing.**
`ensureOllamaContainerReady` guards on presence only —

```js
const { stdout } = await run("docker", ["images", "--format", "{{.Repository}}:{{.Tag}}", OLLAMA_IMAGE]);
return stdout.includes("ollama/ollama");
```

— so build it, retag it `ollama/ollama:latest`, and leave it there. Untag it, or
run `docker image prune -a`, and the next index silently pulls 9.19 GB again.

## Not here yet

The systemd units, the Qdrant volume, and D10's check-in timer are Phase 3.
They are written against the host once it exists: a unit authored against an
unbuilt machine is a unit that does not match it.
