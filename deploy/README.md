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

## TLS is mandatory, not defence in depth (D14)

SocratiCode **refuses** to send `QDRANT_API_KEY` to a non-HTTPS, non-localhost
host:

```
QDRANT_API_KEY is set but http://index:6333 is not HTTPS.
Refusing to send the API key over a non-TLS connection.
```

So an API-key-gated Qdrant on a plaintext tailnet port cannot exist, however
well WireGuard encrypts the hop. The cert is Tailscale's own, for this node's
**full** MagicDNS name:

```bash
sudo tailscale cert --cert-file /etc/socraticode/tls/qdrant.crt \
                    --key-file  /etc/socraticode/tls/qdrant.key \
                    index.taild0fb76.ts.net
```

Requires **DNS → HTTPS Certificates** enabled for the tailnet. With it off the
error is `your Tailscale account does not support getting TLS certs`, which
reads like a plan limit and is not one.

**Every client must use `https://index.taild0fb76.ts.net:6333`.** The short
name is not in the certificate's SAN, so `https://index:6333` fails with
`no alternative certificate subject name matches target host name 'index'`.

### Renewal is ours, and its absence would be silent

Tailscale renews automatically only when it also owns the install location.
Its docs are explicit that a cert written to files by `tailscale cert` is the
operator's to renew, because tailscaled does not know where it was put or how
to reload it. Let's Encrypt means **90 days**.

Unattended, the cohort's search would stop on a date three months out with no
failed unit anywhere: Qdrant keeps serving, and every *client* fails
verification instead. Two defences, because that is the exact failure class
this whole issue exists to remove:

| | |
|---|---|
| `qdrant-cert-renew.timer` | weekly, `Persistent=true`. Restarts Qdrant **only if the file changed** — a renewed cert that nothing reloaded is indistinguishable from a renewal that worked, until it isn't. |
| `index-checkin.sh` | reports a cert inside 21 days of expiry as a finding, so a *stopped renewal timer* becomes an alert rather than a surprise. |

`Persistent=true` here and not on the check-in timer: a missed heartbeat is due
again in ten minutes, a missed renewal window closes for good.

## Contents

| | |
|---|---|
| `setup.sh.template` | first-boot script; `__TAILSCALE_KEY__` substituted at provision time. Joins the tailnet and nothing else. |
| `ollama-slim/Dockerfile` | CPU-only Ollama, 221 MB against 9.19 GB, embeddings bitwise identical to the stock image (CannObserv/replicator#88). |

**On `co-index` the image keeps its own tag, `socraticode/ollama-slim:latest`.**
replicator#88 records that the `ollama/ollama:latest` tag is load-bearing,
because `ensureOllamaContainerReady` guards on presence alone —

```js
const { stdout } = await run("docker", ["images", "--format", "{{.Repository}}:{{.Tag}}", OLLAMA_IMAGE]);
return stdout.includes("ollama/ollama");
```

— so an untag, or a `docker image prune -a`, silently re-pulls 9.19 GB. That is
true on a host where **SocratiCode** manages the container. It does not apply
here: systemd owns this container and `ollama-run.sh` names the image
explicitly, and every cohort client runs `OLLAMA_MODE=external` and manages no
container at all. Keeping our own tag stops a locally-built image from
impersonating upstream's.

**Measured on this host, 2026-09-11**, reproducing replicator#88 independently:
9.18 GB → **221 MB**, and against the same volume, model and prompt the two
images' embeddings are **bitwise identical** (`max abs diff 0.0`, cosine 1.0).
So the slim image is not a backend change and fragments nothing under D7.
Removing the stock image and pruning reclaimed **8.8 GB** (14 G → 5.2 G used).

## Not here yet

The systemd units, the Qdrant volume, and D10's check-in timer are Phase 3.
They are written against the host once it exists: a unit authored against an
unbuilt machine is a unit that does not match it.
