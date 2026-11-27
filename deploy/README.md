# `co-index` — the cohort's shared index host

Design: [`docs/plans/2026-09-11-shared-qdrant-vm-design.md`](../docs/plans/2026-09-11-shared-qdrant-vm-design.md).
Issue: [CannObserv/notifier#57](https://github.com/CannObserv/notifier/issues/57).

One VM running Qdrant and Ollama for every cohort VM (D17), so the vector
store survives what took the per-VM installs out: everything that vanished
lived under `$HOME` or in Docker's local state, everything that survived is in
git.

**Nothing on a production path depends on this host** (D9). An outage degrades
`codebase_search` on the cohort's VMs and stops no service. The degrade is `grep`.

It also receives the cohort's Claude Code telemetry (D21): an OpenTelemetry
Collector on `:4318`, described under [Collector](#collector). A dead
collector loses data and stops nothing.

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

Steady state. The store's clients are every cohort VM (D17), and they send
their telemetry to the collector on `:4318` (D21); `tag:index` is a `src` in
one rule, the check-in to co-status (D10, D16). **`:4318` is recorded ahead of
the live policy** (not yet live on 2026-11-20): the live rule gains it at
rollout, after the pilot (#34).

```jsonc
{
  "tagOwners": { "tag:index": ["autogroup:admin"] },
  "acls": [
    { "action": "accept",
      "src": ["tag:notifier", "tag:watcher", "tag:archiver", "tag:replicator",
              "tag:broker", "tag:status", "tag:observo-primary", "tag:power-map",
              "tag:processor", "tag:address-validator", "tag:provisioner",
              "tag:usa-wa"],
      "dst": ["tag:index:6333,11434,4318"] },
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
| `qdrant-cert-renew.timer` | weekly, `Persistent=true`. Calls `tailscale cert --min-validity 720h`, and logs `renewed` or `unchanged` with the file's expiry. **It doesn't restart Qdrant** (#52): Qdrant reloads the pair itself, below. |
| `index-checkin.sh` | reports a cert with less than 22 days left as a finding (`CERT_DAYS_MIN=21`, in whole days, floored): `tls:cert` for the file, so a *stopped renewal timer* becomes an alert rather than a surprise, and `tls:served` for the one Qdrant presents on `index.taild0fb76.ts.net:6333` when it isn't the file's, so does a *reload that never happened*. |

`Persistent=true` here and not on the check-in timer: a missed heartbeat is due
again in ten minutes, a missed renewal window closes for good.

**Reload, not restart (#52).** Qdrant re-reads `qdrant.crt` and `qdrant.key`
on the first TLS handshake after its `cert_ttl`, which `qdrant-run.sh` sets to
3600 s (the image's own default, pinned there). It opens both paths each time,
so it follows `tailscale cert`'s write: a temporary file in the same directory,
renamed over the old, the certificate first and then the key. Measured in a
throwaway container of the pinned image on 2026-11-27, with a 5 s TTL. A pair
replaced by rename, or written in place, was served within the TTL, with no
restart. On an empty, missing or non-PEM file, Qdrant logged `Failed to refresh
server TLS certificate, keeping current` and kept serving the old pair, where a
*start* on an empty key exits. The restart this replaced cost about 19.5 s of
search, unwindowed (measured 2026-11-23). `test_cert_renew.py` holds the TTL
inside the margin above: a renewal is served at most one TTL late, still 22
hours clear of `tls:served`'s line.

**When `tls:served` opens.** Qdrant serves a certificate that isn't the
file's, and it's inside the line.
1. `sudo docker logs qdrant 2>&1 | grep 'Failed to refresh'` names a file
   Qdrant can't load. Compare the expiries: `sudo openssl x509 -in
   /etc/socraticode/tls/qdrant.crt -noout -enddate`, and `openssl s_client
   -connect index.taild0fb76.ts.net:6333 -servername index.taild0fb76.ts.net
   </dev/null 2>/dev/null | openssl x509 -noout -enddate`.
2. Repair the files with `sudo systemctl start qdrant-cert-renew.service`,
   and wait an hour: Qdrant reloads at its next handshake past the TTL.
3. If it still serves the old certificate, `sudo systemctl restart qdrant`,
   in a window or with `index-inflight.sh` at 0. A start on a file it can't
   load exits, so do step 2 first.

Its message `no certificate read from the store` means the read failed while
the store answered 200 over the same URL: the check is broken, not the
certificate.

**Why `--min-validity` (#49).** Without it, `tailscale cert` returns the cached
certificate while it is valid and, once it is due (in ARI's suggested window,
else past two thirds of its life), renews it *in the background*: the call returns before the new certificate exists. The script
saw no change and the renewal waited a week, long enough for `tls:cert` to open
at 21 days (2026-11-16). With the flag, tailscaled renews **synchronously** when
less than that is left, and answers from its cache otherwise. The two numbers
are tied: weekly with an hour's jitter, a renewal lands with between 22 days
23 hours and 30 days left, 23 hours clear of the check-in's alert at 22.
`test_cert_renew.py` reads the timer and both scripts and fails if a change
breaks that, and refuses 29 days. The value must
also stay well under the CA's lifetime (90 days today): a certificate issued
with less left than the flag asks for fails its own test, and every run would
issue another.

**Known limits.**
- After a renewal Qdrant serves the old certificate for up to an hour, until
  the first handshake past its TTL. That certificate still has almost 23 days.
- **A mismatched pair loads.** Qdrant doesn't check that the key matches the
  certificate. If a reload falls in the few milliseconds between `tailscale
  cert`'s two renames (4 ms on 2026-11-23), Qdrant serves the new certificate
  with the old key and every handshake fails, with nothing in its log, until
  the next reload, within the hour. The odds are about one in a million a
  renewal. The check-in hears it as `qdrant:collections` within ten minutes.
  If it ever must end sooner, `sudo systemctl restart qdrant`, in a window or
  with `index-inflight.sh` at 0. The old restart couldn't hit it, but a start
  on such a pair breaks the same way.
- A failed synchronous renewal (ACME, DNS-01, a rate limit) fails the unit,
  which nothing watches. One that hangs is killed at `TimeoutStartSec=10min`,
  so the timer can start the next. The next weekly run tries again. If every one fails,
  the check-in's `tls:cert` opens at 22 days, three weeks before clients fail
  verification.

## Contents

| | |
|---|---|
| `setup.sh.template` | first-boot script; `__TAILSCALE_KEY__` substituted at provision time. Joins the tailnet and nothing else. |
| `ollama-slim/Dockerfile` | CPU-only Ollama, 221 MB against 9.19 GB, embeddings bitwise identical to the stock image (CannObserv/replicator#88). Its base is pinned to a version and a digest (#11): changing it is an [engine change](#engine-change). |
| `qdrant.service`, `ollama.service` | the long-running units. A clean `systemctl stop qdrant` records success (`SuccessExitStatus=143`) and an out-of-band SIGTERM (`docker stop qdrant`) still restarts it (`Restart=always`, #4). Ollama exits 0 on SIGTERM and has `Restart=always` for the same case (#5). Stop either with `systemctl stop`, which never restarts. The start limit sits in `[Unit]`, the only place systemd reads it. |
| `qdrant-run.sh`, `ollama-run.sh` | their `ExecStart=`: `docker run` with the publish bound to the tailnet address only (D3). Qdrant's image is pinned to a version and a digest, with telemetry off (#13): changing it is a [Qdrant upgrade](#qdrant-upgrade). |
| `otelcol.service`, `otelcol-run.sh`, `otelcol.yaml` | the telemetry collector (D21, #33): its unit, its `ExecStart=` and its pipeline. The image is pinned to a version and a digest: changing it is a [collector update](#collector). The config is mounted read-only from `/usr/local/share/co-index/otelcol.yaml`. |
| `otelcol-retention.{sh,service,timer}` | daily at 03:30 UTC, `Persistent=true`: compresses the collector's rotated files with zstd into `archive/`, prunes at 90 days, holds the directory to 1 GiB, sets every file `0600`, and stamps its success for the check-in. See [Collector](#collector). |
| `tailnet-bind.sh` | prints this node's tailnet address, waiting up to 60 s for `tailscaled` to assign one. Called by the three run scripts and the check-in. |
| `qdrant-cert-renew.{sh,service,timer}` | weekly TLS renewal (D14, above). |
| `index-checkin.{sh,service,timer}` | every 10 min, reports to co-status's dead-man's timer (D10). Its credentials are `STATUS_API_KEY` and `STATUS_MONITOR_ID` in `/etc/socraticode/status.env` (`0600 root:root`, never in git); without either it exits 1 before probing anything (#8). co-status reports a run of `alert` check-ins once, and *has cleared* on the next `ok` (CannObserv/status#28). Each check-in sends its findings' `check:subject` list, in the checks' fixed order, as `metadata.fault`, so a change in the set reports at once rather than waiting for the clear (#10). Three findings are the collector's (D21): `otel:collector`, `otel:bytes` and `otel:retention`, each under [Collector](#collector). The `links:` findings are `link-check.py`'s (#38), below. |
| `needrestart.conf.d/index.conf` | needrestart lists restarts and never performs them (#2), so an apt run cannot bounce Docker, and Qdrant, Ollama and the collector with it. Install it before any apt run (Installing a change, below). `sudo needrestart -m u -r l -b` then prints `Disabling Ubuntu mode` and restarts nothing. |
| `engine-fingerprint.{py,json}` | the check-in's engine check (#11). The `.json` holds five probes (code, an artifact label, uppercase XML, a mixed-case and a lowercase query) and the vectors the pinned engine gave them. A probe below cosine 0.99999 is the `ollama:engine` finding. A probe that couldn't be embedded is no finding, because a timeout behind a client's batch isn't drift. Recapture with `python3 -I deploy/engine-fingerprint.py capture http://index.taild0fb76.ts.net:11434 deploy/engine-fingerprint.json`, only as part of an [engine change](#engine-change). |
| `link-check.py`, `cohort.json` | the check-in's link check (#38). `cohort.json` is the cohort's one repo list: each repo with its D17 tag, `index` as `tag:index` (`test_cohort.py` holds it to D17, so a new cohort VM goes in both). For every link a repo's committed `.socraticode.json` declares, the check confirms the sibling's `projectId`, read from the sibling's own file, names a `codebase_<id>` with points (exact count). **Findings:** `links:<repo>/<sibling>` for no such collection, 0 points, a path no listed repo answers to, a sibling that commits no `projectId`, or one whose `projectId` is no collection name; `links:<repo>` for a file that isn't JSON or a JSON object, is over 64 KiB, or whose `linkedProjects` isn't a list of paths. No file, or no links, is no finding. An anonymous 404 can't tell no file from a repo gone private, renamed, or misspelt in the list: that repo's own links go unchecked, and a link into it reads as a sibling with no `projectId`. **The cache:** public repos' files come from `raw.githubusercontent.com`, anonymously, into `/var/cache/co-index/links.json` (`0644`: `fetched_at`, `attempted_at`, the `raw` base, each repo's `projectId` and `linkedProjects`). It's refreshed when 6 h old, or at once when it doesn't hold the list's public repos or base. A failed refresh (an error, a body cut short, 40 s spent, give or take a slow name lookup, which no budget can cut short) is unknown: the old cache stands, all or nothing, retried after 1 h, and a repo it doesn't hold yet is skipped. No good fetch in 24 h is `links:cache`. **observo and provisioner are private and hand-listed** in `cohort.json` with when and where they were read (no token: D18), so a change there goes unseen until someone re-reads it and edits the list. **Exits:** Qdrant refusing the connection, any HTTP error but 401, 403 and 404, an unreadable reply, a handshake cut short, or 30 s spent is exit 2, no finding (the check-in runs it only after its own read returned 200). A 401, a 403 or a certificate that fails verification is exit 3, since the check-in read the same URL and key a moment before: that, any other failure, and the check-in's `timeout 120`, are `links:check`. Read the cache with `jq . /var/cache/co-index/links.json`; delete it to force a refresh. |
| `index-inflight.sh` | the patching knob's `inflight` (#9, #41): prints how many writes Qdrant served and how many batches Ollama embedded in the last 5 min, so a step waits out a client's index run, this host's own included (D20). A batch is an embed request of 10 inputs or more, or of 10 s or more, or 10 inputs since the last request (one still running), or, when none of those counted, 50 inputs in the window from smaller requests (the re-embed gate's `--batch 8` stream); a search is 1 input and the engine fingerprint 5. Ollama's count is #41's: a run can embed for over 5 min between upserts. It reads `qdrant.service`'s and `ollama.service`'s journals, which `exedev` reads through group `adm`, never `docker logs` (#18). It first logs one request in each, Qdrant's unauthenticated with a unique user agent, Ollama's to a unique path nothing serves, and exits 1, printing no count, unless both markers show: a silent log isn't an idle one. It also exits 1 when journald reports dropping either unit's lines in the window, or when Ollama's lines aren't in the format measured. The patching skill records only the exit code: on exit 1, run it by hand, as `exedev`, and its stderr names the cause. |
| `qdrant-recovery.sh` | Qdrant's recovery point and check for the steps `patching-hosts` doesn't run: a Docker restart, a Qdrant upgrade, an engine change (#9, #30). A patching run takes no snapshot here: the knob's `datastore qdrant` line has `recovery-point.sh` take it. `sudo qdrant-recovery.sh snapshot` writes the point counts and a full snapshot, copied **out of the container** (`--rm` discards its snapshots directory), mode 600, with the sha256 for the owner's off-node copy. `sudo qdrant-recovery.sh check <counts file>` after a step that stopped Qdrant: fails on a collection gone or emptied, or a real query with no hits. A list or count call that fails stops it there, naming the call, and curl's exit when curl failed, never as a loss (#43). A patching run's counts file, from its recovery point, works too, and adds the real query the skill's check doesn't run. The query's embed waits `EMBED_T` seconds (60), up to 3 tries on a timeout only: Ollama serialises embeds, so a client's index run can queue it. Set it after `sudo` (`sudo EMBED_T=180 qdrant-recovery.sh check …`): `EMBED_T=180 sudo …` loses it to sudo's `env_reset`, and the check runs at 60 s. `qdrant-recovery.sh query`, with no `sudo`, is a search alone (#40), for the knob user, through the [read-only key](#read-only-key). It takes no counts file. It picks the largest `codebase_` collection by its info's `points_count`, scrolls one point with its stored vector, and searches with that vector. It calls no Ollama (#46): a client's index run holds Ollama's one queue for minutes, and the line used to fail on a healthy Qdrant. `ollama.service` has its own `health` line, and `check` still embeds. It keeps to 20 s of limits under a knob command's 60 (`timeout -k 10 60`): the list and the info calls share 10 s, 5 s at most a call, then the scroll has 5 s and the search 5 s. Exit 0 means the scrolled point came back at the top. A tie at the top score counts, because duplicate chunks share a vector. An empty collection, a point with no stored vector, or a search that ranks another point first fails by name. |
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
**restart `docker.service` before any pull**. That restarts `qdrant`,
`ollama` and `otelcol` too (`Requires=docker.service`): take
`qdrant-recovery.sh snapshot` first and run `check` after.

**Measured on this host, 2026-09-11**, reproducing replicator#88 independently:
9.18 GB → **221 MB**, and against the same volume, model and prompt the two
images' embeddings are **bitwise identical** (`max abs diff 0.0`, cosine 1.0).
So the slim image is not a backend change and fragments nothing under D7.
Removing the stock image and pruning reclaimed **8.8 GB** (14 G → 5.2 G used).

## Read-only key

Qdrant has a second key, read-only, for the patching knob's `recovery` line (D22, #40). Knob commands run as the knob user, never root, and the admin key is root-only. The read-only key lists collections, reads their info, queries and counts. Every write gets 403. It lives in `qdrant.env`, beside the admin key, and in `/etc/socraticode/qdrant-ro.key`, `0640 root:qdrant-ro`. `/etc/socraticode` is `0750 root:qdrant-ro`, and `exedev` is the group's member. **It never goes to a client**: they keep the admin key ([installing-the-key.md](../docs/installing-the-key.md)).

**Qdrant reads it only at a start, and any start takes it**: a crash, a Docker restart, a patching step. The cert renewal no longer restarts Qdrant (#52). So mint it in the window you restart in, never ahead of one. Minting and rotating are the same command:

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
sudo install -D -m 644 deploy/cohort.json /usr/local/share/co-index/cohort.json
sudo install -D -m 644 deploy/otelcol.yaml /usr/local/share/co-index/otelcol.yaml
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
takes effect at its next start. A change to how Qdrant, Ollama or the
collector starts, `otelcol.yaml` included, takes effect at the unit's next
restart, which is a separate decision.

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
   row in Contents), commit, and install it, and run `index-inflight.sh`: it
   must print a number. `format` means the new Ollama logs its requests or
   its inputs differently, and the gate refuses every patch step until it is
   measured again (#41). You're done.
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
     in `ollama-run.sh`, install, restart `ollama.service`, check
     `index-inflight.sh` prints a number, not `format` (as in 2), then run
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

## Collector

`otelcol.service` receives the cohort's Claude Code telemetry (D21, #33):
OTLP/HTTP on `:4318`, on the tailnet address only, with **no ingest auth**
(D4's terms: the ACL bounds who can send, the port only accepts writes).
Clients are turned on per VM, in `~/.claude/settings.json`, never from this
repo: the pilot is #34.

**What it writes.** `tool_result` and `api_request` events, and the
`claude_code.active_time.total` metric, each through an allowlist of
attributes. `deploy/otelcol.yaml` is the list, and
`tests/deploy/test_otelcol_pipeline.py` runs it in the pinned image. Every
other event, metric and attribute is dropped before the write, traces have no
pipeline (the port answers 404), and known token shapes are masked: shapes,
not secrets, so `user:pass@host` in a URL or a cloud provider's key is written
as sent. A kept value is cut at 4096 bytes, so a long `tool_parameters` or
`tool_input` may not parse as JSON: read it as text.

**Where.** `/var/lib/otelcol`, `0700 root:root`:

| File | What |
|---|---|
| `events.jsonl`, `metrics.jsonl` | the active files. One line per export, OTLP/JSON: `{"resourceLogs":[…]}` or `{"resourceMetrics":[…]}` |
| `events-<UTC>-size.jsonl` | rotated at 8 MB, waiting for the retention run. At most 8 per signal: the exporter deletes the oldest past that. One export over 8 MB is refused whole |
| `archive/events-<UTC>-size.jsonl.zst` | compressed by the retention run: a standard zstd stream, with the rotated file's mtime. In a subdirectory because the exporter deletes, as a backup of its own, any such file left beside the rotated ones |
| `.retention-ok` | the retention run's stamp, which the check-in reads |

```bash
sudo ls -la /var/lib/otelcol
sudo sh -c 'zstd -dcq /var/lib/otelcol/archive/events-*.jsonl.zst; cat /var/lib/otelcol/events-*-size.jsonl /var/lib/otelcol/events.jsonl' 2>/dev/null | wc -l
```

**The exporter creates its files `0644`** and has no option for it
(0.165.0). The directory is what keeps other users out, and
`otelcol-run.sh` resets it to `0700 root:root` at every start. The retention
run sets every file `0600`. A copy made elsewhere must not keep `0644`.

**Retention** (`otelcol-retention.timer`, daily 03:30 UTC):

- compresses each rotated file (`zstd -19`) into `archive/`, and removes the original;
- prunes a compressed file 90 days after its last write. A file goes whole,
  so a record can outlive 90 days by one file's span;
- holds the directory to **1 GiB** by deleting the oldest compressed files.
  The estimate is 0.5–1 MB a day compressed for the cohort (45–90 MB for 90
  days), so the cap is a backstop about 11 times the high end. The pilot's
  measured sizes replace the estimate (#34).

Run it by hand with `sudo systemctl start otelcol-retention.service`, and
read it with `sudo journalctl -u otelcol-retention -n 5`.

**The check-in's findings:**

| Finding | Means | Do |
|---|---|---|
| `otel:collector` | an empty export to `:4318` wasn't answered `200`. It proves the listener, not the write: a collector that refuses real exports (its directory unwritable, its memory limiter refusing) still answers an empty one (#47) | `systemctl status otelcol`, `sudo journalctl -u otelcol -n 50` |
| `otel:bytes` | the directory is over 512 MiB, half the cap | a client is flooding, or the estimate is wrong. `sudo du -sh /var/lib/otelcol`, then find the sender by `cohort.host` in the newest files. Past 1 GiB the oldest data goes |
| `otel:retention` | the retention run hasn't succeeded in 36 hours | `systemctl list-timers otelcol-retention.timer`, `sudo journalctl -u otelcol-retention -n 20`. Until it runs, nothing is pruned, and rotated files past the 8th are lost uncompressed |

**First install.** Outside a patching window, with `index-inflight.sh` at 0
(the pull and the start share the host with a client's index run). Qdrant and
Ollama don't restart: `systemctl show qdrant ollama -p Id -p InvocationID` and
`sudo docker inspect qdrant ollama --format '{{.Name}} {{.State.StartedAt}}'`
read the same before and after. Not `NRestarts`, which counts only
`Restart=`'s restarts: an explicit start, a `systemctl restart` or Docker's,
resets it to 0, so a healthy unit reads 0 before and after whether or not it
restarted (#53).

```bash
sudo docker pull "$(sed -n 's/^  \(otel\/opentelemetry-collector-contrib:.*\)$/\1/p' deploy/otelcol-run.sh)"
# then the lines under "Installing a change", and:
sudo systemctl enable --now otelcol.service otelcol-retention.timer
sudo ss -ltnp 'sport = :4318'                     # the tailnet address only
curl -sf -o /dev/null -w '%{http_code}\n' -H 'Content-Type: application/json' -d '{}' http://index:4318/v1/logs   # 200
sudo systemctl start otelcol-retention.service   # the first stamp
```

The retention run comes after the `200`: `enable --now` returns before the
unit has created `/var/lib/otelcol`, and a run that finds no directory fails.
It needs `zstd`, which the image ships (`initramfs-tools-core` depends on
it): a host built without it fails the run by name.

Install `index-checkin.sh` with the rest: its first run after that expects
the collector up and the stamp written, or it reports both.

**Update.** The image is a version and a digest (`otelcol-run.sh`), and a
patching run never touches it (the knob's `owner` line). Update for a reason:
a fix, or a CVE in the receiver. There is no storage to migrate, so it is one
restart:

1. Pin the new version and its digest in `otelcol-run.sh`. The digest is the
   tag's multi-platform index, from the registry (`sudo docker buildx
   imagetools inspect otel/opentelemetry-collector-contrib:<version>`).
2. `sudo docker pull` it (the line above), then `uv run pytest
   tests/deploy/test_otelcol_pipeline.py tests/deploy/test_otelcol_run.py`.
   The pipeline tests run the **new** image with the tracked config: a
   release that renames an attribute path, a function or the rotated files
   fails here, before it runs. Read the release's changes to the file
   exporter and to OTTL either way.
3. Commit, install, and `sudo systemctl restart otelcol.service`. Clients
   lose what they send in those seconds, and nothing else (D9).
4. `uv run pytest tests/deploy/test_installed.py`, the knob's `health` line,
   and the next check-in.

**Rollback:** the old pin, install, restart. The old image stays on disk
until it is removed by name: pulled by digest it isn't dangling, so neither
`sudo docker image prune` nor the disk GC's `docker` tier takes it. Once the
new one has run for a while, `sudo docker rmi
otel/opentelemetry-collector-contrib@sha256:<old digest>`.

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
