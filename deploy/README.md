# `co-index` — the cohort's shared index host

Design: [`docs/plans/2026-09-11-shared-qdrant-vm-design.md`](../docs/plans/2026-09-11-shared-qdrant-vm-design.md).
Issue: [CannObserv/notifier#57](https://github.com/CannObserv/notifier/issues/57).

One VM running Qdrant and Ollama for every cohort VM (D17), so the vector
store survives what took the per-VM installs out: everything that vanished
lived under `$HOME` or in Docker's local state, everything that survived is in
git.

**Nothing on a production path depends on this host** (D9). An outage degrades
`codebase_search` on the cohort's VMs and stops no service. The degrade is `grep`.

## Phase 2 — provisioning

### Operator prerequisites

Neither is present in this repo's `.env` today, and both are operator-only.
Phase 2 cannot start without them.

| | |
|---|---|
| `EXE_API_TOKEN` | scoped to at least `new` and `whoami`. `ssh exe.dev whoami` from this host returns `Permission denied (publickey)` — there is no registered identity here, so the HTTPS `exec` path is the one available to an agent. |
| `TAILSCALE_KEY_INDEX` | **single-tag `tag:index`, pre-approved, non-ephemeral, minted before the first join.** A multi-tag key applies all its tags and cannot be narrowed with `--advertise-tags`. Tags bind at registration: `tailscale up --reset` does not retag (notifier#43 F1). |

Also before `new`: confirm `tag:index` is declared in `tagOwners`, and add
both ACL blocks below. **A peer is visible only through an `acls` rule** — an
`ssh` block alone leaves the node out of the netmap, where the missing rule
presents as a DNS failure rather than a permission denial.

### ACL

Steady state. The store's clients are every cohort VM (D17); `tag:index` is a
`src` in one rule, the check-in to co-status (D10, D16).

```jsonc
{
  "tagOwners": { "tag:index": ["autogroup:admin"] },
  "acls": [
    { "action": "accept",
      "src": ["tag:notifier", "tag:watcher", "tag:archiver", "tag:replicator",
              "tag:broker", "tag:status", "tag:observo-primary", "tag:power-map"],
      "dst": ["tag:index:6333,11434"] },
    { "action": "accept", "src": ["tag:index"], "dst": ["tag:status:9000"] }
  ]
}
```

Build phase only (D13). Added with the steady state, **removed at the end of
Phase 3**, together with `--ssh` on the node (`sudo tailscale set --ssh=false`).
Edge and `ssh` block removed 2026-09-29, `--ssh` 2026-10-01; a rebuild adds all
three again. Removing the rules does not turn `--ssh` off. Check afterwards:
`tailscale status` prints no health warning, and `tailscale debug prefs` shows
`"RunSSH": false`.

```jsonc
{
  // D13: BUILD PHASE ONLY. Without the acls rule the ssh block is never consulted.
  "acls": [
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
| `ollama-slim/Dockerfile` | CPU-only Ollama, 221 MB against 9.19 GB, embeddings bitwise identical to the stock image (CannObserv/replicator#88). Its base is pinned to a version and a digest (#11): changing it is an [engine change](#engine-change). |
| `qdrant.service`, `ollama.service` | the long-running units. A clean `systemctl stop qdrant` records success (`SuccessExitStatus=143`) and an out-of-band SIGTERM (`docker stop qdrant`) still restarts it (`Restart=always`, #4). Ollama exits 0 on SIGTERM and has `Restart=always` for the same case (#5). Stop either with `systemctl stop`, which never restarts. The start limit sits in `[Unit]`, the only place systemd reads it. |
| `qdrant-run.sh`, `ollama-run.sh` | their `ExecStart=`: `docker run` with the publish bound to the tailnet address only (D3). Qdrant's image is pinned to a version and a digest, with telemetry off (#13): changing it is a [Qdrant upgrade](#qdrant-upgrade). |
| `tailnet-bind.sh` | prints this node's tailnet address, waiting up to 60 s for `tailscaled` to assign one. Called by both run scripts and the check-in. |
| `qdrant-cert-renew.{sh,service,timer}` | weekly TLS renewal (D14, above). |
| `index-checkin.{sh,service,timer}` | every 10 min, reports to co-status's dead-man's timer (D10). Its credentials are `STATUS_API_KEY` and `STATUS_MONITOR_ID` in `/etc/socraticode/status.env` (`0600 root:root`, never in git); without either it exits 1 before probing anything (#8). co-status reports a run of `alert` check-ins once, and *has cleared* on the next `ok` (CannObserv/status#28). Each check-in sends its findings' `check:subject` list, in the checks' fixed order, as `metadata.fault`, so a change in the set reports at once rather than waiting for the clear (#10). |
| `needrestart.conf.d/index.conf` | needrestart lists restarts and never performs them (#2), so an apt run cannot bounce Docker, and Qdrant and Ollama with it. Install it before any apt run (Installing a change, below). `sudo needrestart -m u -r l -b` then prints `Disabling Ubuntu mode` and restarts nothing. |
| `engine-fingerprint.{py,json}` | the check-in's engine check (#11). The `.json` holds five probes (code, an artifact label, uppercase XML, a mixed-case and a lowercase query) and the vectors the pinned engine gave them. A probe below cosine 0.99999 is the `ollama:engine` finding. A probe that couldn't be embedded is no finding, because a timeout behind a client's batch isn't drift. Recapture with `python3 -I deploy/engine-fingerprint.py capture http://index.taild0fb76.ts.net:11434 deploy/engine-fingerprint.json`, only as part of an [engine change](#engine-change). |
| `index-inflight.sh` | the patching knob's `inflight` (#9, #41): prints how many writes Qdrant served and how many batches Ollama embedded in the last 5 min, so a step waits out a client's index run, this host's own included (D20). A batch is an embed request of 10 inputs or more, or of 10 s or more, or 10 inputs since the last request (one still running); a search is 1 input and the engine fingerprint 5. Ollama's count is #41's: a run can embed for over 5 min between upserts. It reads `qdrant.service`'s and `ollama.service`'s journals, which `exedev` reads through group `adm`, never `docker logs` (#18). It first logs one request in each, Qdrant's unauthenticated with a unique user agent, Ollama's to a unique path nothing serves, and exits 1, printing no count, unless both markers show: a silent log isn't an idle one. It also exits 1 when journald reports dropping either unit's lines in the window, or when Ollama's lines aren't in the format measured. |
| `qdrant-recovery.sh` | Qdrant's recovery point and check for the steps `patching-hosts` doesn't run: a Docker restart, a Qdrant upgrade, an engine change (#9, #30). A patching run takes no snapshot here: the knob's `datastore qdrant` line has `recovery-point.sh` take it. `sudo qdrant-recovery.sh snapshot` writes the point counts and a full snapshot, copied **out of the container** (`--rm` discards its snapshots directory), mode 600, with the sha256 for the owner's off-node copy. `sudo qdrant-recovery.sh check <counts file>` after a step that stopped Qdrant: fails on a collection gone or emptied, or a real query with no hits. A patching run's counts file, from its recovery point, works too, and adds the real query the skill's check doesn't run. The query's embed waits `EMBED_T` seconds (60), up to 3 tries on a timeout only: Ollama serialises embeds, so a client's index run can queue it. Set it after `sudo` (`sudo EMBED_T=180 qdrant-recovery.sh check …`): `EMBED_T=180 sudo …` loses it to sudo's `env_reset`, and the check runs at 60 s. `qdrant-recovery.sh query`, with no `sudo`, is the query alone (#40), for the knob user, through the [read-only key](#read-only-key): no counts file, the largest `codebase_` collection by its info's `points_count`, then one embed and a search. It keeps to 45 s of limits, under a knob command's 60: the list and the info calls share 10 s (5 s at most a call), the embed has 30 s and one try, and the search 5 s. It reads no `EMBED_T`. Exit 0 means hits. |
| `apt-preferences.d/tailscale` | scopes `pkgs.tailscale.com`, which the knob follows: the site at 100, below Ubuntu, and `tailscale` and its keyring at 600 (#9). |
| `apt-sources.list.d/nodesource.sources`, `apt-preferences.d/nodesource` | Node 26 from NodeSource, for this repo's own SocratiCode client and nothing else (D20, #18): the `node_26.x` source, and pins that hold the site at 100 and `nodejs` at 600. The knob follows `deb.nodesource.com`. Installed and checked by `scripts/nodesource.sh`, never by the lines below: apt can't use the source before the key is in place. See [Node](#node). |

**The disk GC is not in `deploy/`** (#16). It is Provisioner's cohort GC and headroom sensor, pulled and installed by the vendored `maintaining-host-disk` skill, which also updates and removes it. Never edit the installed copy. Its units are `cannobserv-disk-gc.timer` (daily) and `cannobserv-disk-headroom.timer` (every 10 min), and its journald drop-in caps the journal at 512M. This host's own files are in `/etc/cannobserv/disk-gc/`:
- `disk-gc.conf`: the template's tiers plus `worktrees` and `docker`. `docker` is on because `ollama-slim` is built here; it never removes a container, a volume or a tagged image. The lines are 85/80, and patching-hosts reads them as its floor.
- `checkin.key`: the `co-index` tenant's key, copied from `status.env`.

`/`'s headroom pages on `co-index-disk-headroom`, not on the check-in.

**On `co-index` the image keeps its own tag, `socraticode/ollama-slim:<version>`**, the version the Dockerfile pins (#11).
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
impersonating upstream's. Built, from the checkout's root:

```bash
sudo docker build --pull --no-cache -t socraticode/ollama-slim:0.34.0 deploy/ollama-slim
uv run pytest tests/deploy/test_ollama_image.py
```

**`--pull --no-cache`, so the CA bundle is today's (#13).** The `ollama`
stage is pinned by digest, so this never changes `/usr/bin/ollama`, and a
rebuild at the same version is no engine change. `ubuntu:24.04` and its
`ca-certificates` aren't pinned. The image's Go 1.26 trusts its bundle as
given, so a stale base or a cached apt layer keeps a root distrusted since
the build: the 2026-10-17 build kept `emSign ECC Root CA - G3` after the
eMudhra compromise (CannObserv/processor#66). The test parses the built
image's bundle and fails on any root it lists as distrusted. Then restart
`ollama.service`, and `engine-fingerprint.py check` passes: it compares
vectors, not image IDs.

Docker loads its own roots once per process, so after a host store fix,
**restart `docker.service` before any pull**. That restarts `qdrant` and
`ollama` too (`Requires=docker.service`): take `qdrant-recovery.sh snapshot`
first and run `check` after.

**Measured on this host, 2026-09-11**, reproducing replicator#88 independently:
9.18 GB → **221 MB**, and against the same volume, model and prompt the two
images' embeddings are **bitwise identical** (`max abs diff 0.0`, cosine 1.0).
So the slim image is not a backend change and fragments nothing under D7.
Removing the stock image and pruning reclaimed **8.8 GB** (14 G → 5.2 G used).

## Read-only key

Qdrant has a second key, read-only, for the patching knob's `recovery` line (D22, #40). Knob commands run as the knob user, never root, and the admin key is root-only. The read-only key lists collections, reads their info, queries and counts. Every write gets 403. It lives in `qdrant.env`, beside the admin key, and in `/etc/socraticode/qdrant-ro.key`, `0640 root:qdrant-ro`. `/etc/socraticode` is `0750 root:qdrant-ro`, and `exedev` is the group's member. **It never goes to a client**: they keep the admin key ([installing-the-key.md](../docs/installing-the-key.md)).

**Qdrant reads it only at a start, and any start takes it**: the Monday cert renewal, a crash, a Docker restart. So mint it in the window you restart in, never ahead of one. Minting and rotating are the same command:

```bash
index-inflight.sh                               # 0, or wait
sudo qdrant-recovery.sh snapshot                # the recovery point (the owner's rule)
sudo bash scripts/qdrant-ro-key.sh mint exedev  # from the checkout
sudo systemctl restart qdrant
```

Then check it, in this order:
- `sudo qdrant-recovery.sh check <the snapshot's counts file>`: the admin key still works, and nothing was lost.
- `time sudo runuser -u exedev -- timeout -k 10 60 sh -c '/usr/local/bin/qdrant-recovery.sh query'`: the knob's own route (`knob.md`, `_probe-lib.sh`'s `knob_cmd`). Exit 0, well under 60 s.
- A write with the read-only key gets 403. This one is a no-op with any key: a collection that doesn't exist. The admin key would answer `200` with `"result":false` (measured on v1.20.0):
  ```bash
  sudo bash -c 'curl -sS -o /dev/null -w "%{http_code}\n" -X DELETE \
    -H @<(printf "api-key: %s\n" "$(cat /etc/socraticode/qdrant-ro.key)") \
    https://index.taild0fb76.ts.net:6333/collections/ro-key-probe'   # 403
  ```
- One client's `codebase_search`.

A rotation takes the same steps, and touches no client.

## Installing a change

Every file in `deploy/` except this README, `setup.sh.template` and
`ollama-slim/Dockerfile` is installed as a file. After any change to `deploy/`,
from the checkout's root:

```bash
sudo install -m 755 deploy/*.sh deploy/*.py /usr/local/bin/
sudo install -D -m 644 deploy/engine-fingerprint.json /usr/local/share/co-index/engine-fingerprint.json
sudo install -m 644 deploy/*.service deploy/*.timer /etc/systemd/system/
sudo install -D -m 644 deploy/needrestart.conf.d/index.conf /etc/needrestart/conf.d/index.conf
sudo install -D -m 644 deploy/apt-preferences.d/tailscale /etc/apt/preferences.d/tailscale
sudo systemctl daemon-reload
uv run pytest tests/deploy/test_installed.py
```

**`install`, never `cp`.** `install` replaces the file, so a running script
keeps reading the old copy. `cp` overwrites it in place, and bash reads a
script as it runs.

A **new** unit with an `[Install]` section also needs
`sudo systemctl enable --now <unit>`: installed and loaded, a timer never fires.

`daemon-reload` restarts no container and fires no timer. A changed script
takes effect at its next start. A change to how Qdrant or Ollama starts
takes effect at the unit's next restart, which is a separate decision.

`test_installed.py` maps every tracked file in `deploy/` to its install path.
On co-index it fails on any file that differs, is missing, has the wrong mode
or owner, or is not loaded, and on any unit with `[Install]` that is not enabled
and running, and prints the command that fixes it. Everywhere, CI included, it
fails on a new file in `deploy/` until the file is mapped, and on install lines
above that no longer install exactly what is mapped.

## Node

Node is client-side only: SocratiCode for this repo, on this host (D20). No
store unit or deploy script runs `node`, `npx` or `npm`, and
`test_nodesource.py` fails on any that does. The line is pinned in
`deploy/apt-sources.list.d/nodesource.sources` (`node_26.x`); moving it is a
deliberate edit, with `EXPECTED_MAJOR` in the test.

**`exedev` is out of group `docker` first** (D20). That means the database
and every running process: one started before the change keeps the group, and
so does everything it starts. The install refuses while either holds it. On
2026-11-06, after `sudo gpasswd -d exedev docker`, three still did (#18):

- **the VS Code server.** Reloading the window doesn't restart it. Run
  "Remote-SSH: Kill VS Code Server on Host…" from the client, or
  `pkill -u exedev -f '\.vscode-server'` from an ssh shell, then reconnect.
- **an orphaned VS Code CLI** (`code-<hash> … agent host`, parent PID 1),
  from an older build. Stop it with `kill <pid>`.
- **the user manager**, which linger keeps up since boot, with its session
  D-Bus: `sudo systemctl restart user@1000.service`. A session started by
  exe.dev's `guestd` sits in `init.scope`, outside it, and survives.

Then `id -nG` shows no `docker`, and `ps -u exedev -o pid=,supgid=` lists
no process holding its gid (`test_host_access.py`).

```bash
sudo bash scripts/nodesource.sh install   # refuses a key that isn't NodeSource's
bash scripts/nodesource.sh check          # key, source, pins, candidate, /usr/bin/node
```

The key's fingerprint is `6F71 F525 2828 41EE DAF8  51B4 2F59 B5F9 9B1B E0B4`
(CannObserv/watcher#296, and fetched again on 2026-11-06). Updates come in
the knob's maintenance lane, as `origin deb.nodesource.com follow`. Out of
cycle, re-run `install`. To remove it:

```bash
sudo apt-get purge nodejs
sudo rm /etc/apt/sources.list.d/nodesource.sources /etc/apt/preferences.d/nodesource /etc/apt/keyrings/nodesource.gpg
sudo apt-get update
```

Removing it also means removing the knob's `origin` line, in the same change.

## Engine change

**An Ollama upgrade can change every stored vector, and nothing else
notices** (#11, D19). 0.40.5 case-folds input that 0.21.2 didn't, which
moved every chunk holding an uppercase letter (median cosine 0.97, min 0.68),
with model, prefixes and dimensions unchanged. SocratiCode's index profile
records none of that, and it re-embeds only files whose hash changed. So
after a swap the store is silently mixed, and under D7 one stale collection
mis-orders every cross-repo search. That's why the base image is pinned,
and why the check-in's `ollama:engine` finding exists. A patching run never
touches the image (the knob's `owner` line).

**Upgrade only for a reason** (a fix, speed, a model). When there is one,
inside a knob window:

1. **Measure the candidate, read-only.** Pin the new version and digest in
   `ollama-slim/Dockerfile`, build it under its own tag (the build line in
   Contents, with the new version), and start it on loopback beside the live
   one:

   ```bash
   sudo docker run -d --rm --name ollama-candidate -p 127.0.0.1:11435:11434 \
     -v /var/lib/ollama/models:/root/.ollama/models:ro socraticode/ollama-slim:<new> serve
   sudo cat /etc/socraticode/qdrant.key \
     | python3 -I scripts/reembed.py --ollama http://127.0.0.1:11435 --dry-run --sample 24
   ```

2. **No drift** (every collection `PASS`): set the tag in `ollama-run.sh`,
   install, and restart `ollama.service`. Then recapture the fingerprint (its
   row in Contents), commit, and install it. You're done.
3. **Drift:** the store is re-embedded from its own payloads, with no client
   involved:
   - **A day ahead,** file an issue on each cohort repo with its
     `GH_TOKEN_<REPO>`: search degrades in the window, and no indexing until
     it closes.
   - **The gate, before the window, on the live engine:** `reembed.py
     --dry-run --batch 8 --gate-out <gate.json>`. Every collection must
     `PASS`. A collection that fails gets an issue asking its owner for
     `codebase_remove` + `codebase_index` after the swap. Never run
     `--apply` on it.
   - **In the window:** check `index-inflight.sh` reads 0, then run `sudo
     qdrant-recovery.sh snapshot`. Keep the old image's tag. Set the new tag
     in `ollama-run.sh`, install, restart `ollama.service`, then run
     `reembed.py --apply --gate <gate.json>`.
   - **Verify:** `reembed.py --dry-run --sample 24` passes on the new engine,
     and `qdrant-recovery.sh check <counts>` passes. Recapture the
     fingerprint, commit, and install it.
   - **Rollback:** the old tag in `ollama-run.sh`, plus the snapshot.

**Throughput, measured** (D6's timed figure, 2026-10-17): the full gate on
0.34.0 ran `--dry-run --batch 8` over the whole store, 42,449 points in 16
collections. It took **28,976 s (8.0 h), about 0.68 s per point**: 0.66 for
code, 0.98 for artifacts, which are longer chunks. Every point read cosine
1.000000. That's more than one 6.5-hour window. Either the window grows, or
`--apply` runs per collection (`--collection`) across windows, and the
cross-repo merge stays mixed in between. #11 measured 0.40.5 at 2.1× faster.
Re-embedding from empty costs the same embeddings, plus each client's
chunking.

`reembed.py` rebuilds each point's text as SocratiCode 1.14.0 did: the
prefix, then `relativePath` (code) or `context:<name>:<path.normalize(path)>`
(artifacts, where Node keeps a directory's trailing `/`), then the content,
cut at `contextLength` UTF-16 code units. A newer SocratiCode that changes
any of that fails the gate. That failure is the gate working, not a bug in
the gate.

## Qdrant upgrade

**The image is a version and a digest** (`qdrant-run.sh`, #13), because
Qdrant migrates storage on start and only from the previous minor version.
So an upgrade goes **one minor version at a time**, each started on the
store until it is ready. A pull of a newer tag is never an upgrade. Upgrade
for a reason (a fix, or a CA bundle, as in #13). Inside a knob window:

1. **Rehearse offline,** from a fresh `sudo qdrant-recovery.sh snapshot`:
   restore it into a loopback candidate on the current version, then start
   that storage on each minor version's latest patch in turn. At each step,
   compare the collections, exact point counts, `status: green`, and exact
   dense and sparse top-10s against the first step, and check for `WARN` or
   `ERROR` in the log. RRF ties reorder from run to run, so compare hybrid
   results as sets.

   ```bash
   sudo mkdir -p /var/tmp/qdrant-rehearsal
   sudo docker run -d --name qdrant-rehearsal -p 127.0.0.1:16333:6333 \
     -e QDRANT__TELEMETRY_DISABLED=true -v /var/tmp/qdrant-rehearsal:/qdrant/storage \
     -v /var/backups/qdrant-<utc>.snapshot:/snap/full.snapshot:ro \
     qdrant/qdrant:<current> ./entrypoint.sh --storage-snapshot /snap/full.snapshot
   # then, per step: sudo docker rm -f qdrant-rehearsal, and sudo docker run the same
   # storage on the next version, without --storage-snapshot
   ```

2. **In the window:** `index-inflight.sh` reads 0, then take a fresh
   snapshot. `sudo systemctl stop qdrant.service`, then copy the stopped
   store for rollback: `sudo cp -a /var/lib/qdrant /var/lib/qdrant.pre-<new>`.
   Start each intermediate version on the store, on loopback, until
   `/readyz` answers, then stop it:

   ```bash
   sudo docker run -d --rm --name qdrant-step -p 127.0.0.1:16333:6333 \
     --env-file /etc/socraticode/qdrant.env -e QDRANT__TELEMETRY_DISABLED=true \
     -v /var/lib/qdrant:/qdrant/storage qdrant/qdrant:<step>
   ```

   Pin the new version and its digest in `qdrant-run.sh`, install it, then
   `sudo systemctl start qdrant.service`.
3. **Verify:** `qdrant-recovery.sh check <counts>`, `uv run pytest
   tests/deploy/test_qdrant_run.py tests/deploy/test_installed.py`, and the
   next check-in.
4. **Rollback:** stop the unit, put `/var/lib/qdrant.pre-<new>` back as
   `/var/lib/qdrant`, restore the old pin, and start the unit. Delete the copy
   once the next check-ins are clean.

**Measured, 2026-10-23** (#13, v1.17.0 → v1.18.3 → v1.19.2 → v1.20.0): 41
collections, 48,477 points. Each step was ready in 4–6 s with no `WARN` or
`ERROR`, counts and statuses were unchanged, and the exact dense and sparse
top-10s were identical in all 16 collections. SocratiCode's calls (create,
payload index, upsert, retrieve, filtered scroll, delete, hybrid RRF query)
all work on 1.20.0. SocratiCode 1.14 through 1.16 pin `@qdrant/js-client-rest`
`~1.18.0`, two minor versions behind the server, but build it with
`checkCompatibility: false`, so the gap goes unchecked and unlogged. The
upgrade's evidence is the calls above, not the client's version check.
