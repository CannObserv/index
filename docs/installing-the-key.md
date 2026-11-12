# Installing the store key into a client repo

The SocratiCode store on `co-index` is gated by a **single global** Qdrant
`service.api_key` (CannObserv/notifier#57). There is no key list, no per-client identity and no
per-collection scope, so every cohort VM holds the same secret and a leak
anywhere is a rotation everywhere.

The store has one other key, read-only, and it never leaves `co-index`: it's
the patching knob's, for a real query after a restart (D22, [the runbook](../deploy/README.md#read-only-key)).
Never install it on a client. A client with it can't index.

`scripts/install_qdrant_key.sh` merges it into a repo's git-ignored
`.claude/settings.local.json`. **The key arrives on stdin** — never argv, which
is visible in `ps` to every other process for the life of the call and lands in
the caller's shell history.

**It refuses any target git would commit** (CannObserv/notifier#68). Check the repo first:

```bash
git -C <repo> check-ignore -v .claude/settings.local.json   # must print a rule
```

Do not assume the rule is there because a sibling repo has it. CannObserv/broker
had none, is public, and four of the five cohort repos carrying the rule made
the assertion read as true right up to the exception. The script distinguishes
three failures because they have three remedies: **not a work tree** (the check
cannot be made), **tracked** (`.gitignore` does not apply to tracked paths, so
the key may already be in history — untrack *and* rotate), and **not ignored**
(add the rule). It asks `git check-ignore` whatever the rule's source, since a
global `core.excludesfile` genuinely does prevent a commit from that VM. Run from an operator machine, which is the only host
able to reach both ends (exe.dev VMs are isolated from each other, and D13's
`tag:index:22` edge was removed on 2026-09-29):

```bash
# 1. a read-only clone of this repo on the target (public: no credential).
#    Run it from the clone, never a copy: its tests pin what a copy loses.
ssh <vm>.exe.xyz 'git clone -q https://github.com/CannObserv/index.git ~/index || git -C ~/index pull -q'

# 2. pipe the key across, host to host — it touches no disk on the way
ssh co-index.exe.xyz \
    "sudo sed -n 's/^QDRANT__SERVICE__API_KEY=//p' /etc/socraticode/qdrant.env" \
  | ssh <vm>.exe.xyz 'bash ~/index/scripts/install_qdrant_key.sh ~/<repo>'
```

Expect `installed 64 chars`. **Any other length is a truncated transfer**, which
401s exactly like a wrong key — the length is the only cheap discriminator.

**Reload the editor window before the first index** (#42). A SocratiCode server
reads the key in `.claude/settings.local.json` and the `env` block in
`.claude/settings.json` only when it starts. It reads `.socraticode.json` live.
So once the key and the commit carrying the `env` block are both in the target
checkout, whether the block was written there or pulled, reload the editor
window or restart Claude Code, trusting the folder if it asks. Whichever of the
two arrives second, the reload comes after it.

Then verify on the target with `codebase_health`. It must report
`Qdrant mode: external` and name the external Qdrant and Ollama rather than a
container, before the first `codebase_index`. `000` on both a keyed and unkeyed
probe is the ACL, not the key, and presents as DNS failure.

A server that predates the block fails silently on a host that still has its
local containers (CannObserv/usa-wa#556). It stays in managed mode, so its
`codebase_index` writes the new collection names into the **local** store and
holds the project's locks in `/tmp/socraticode-locks`. A driver `index` run
then waits on that live server's locks until its ceiling (120 minutes on
usa-wa), and nothing reaches co-index. The reload kills the old server, its
locks go stale, and the new one comes up external and takes them. Where the
local store is gone (Docker purged, say), the same mistake fails loudly.

**Never run the installer under `bash -x`.** Tracing a script that touches a
credential writes the value to stdout; that is how two were leaked during CannObserv/notifier#57.

Rotation has no overlap window — Qdrant holds one `api_key`, so every client
401s from the restart until it is updated. Sequence: mint, write
`/etc/socraticode/qdrant.env`, `systemctl restart qdrant`, then step 2 against
every VM, then reload each client's editor window: a running server keeps the
key it started with. Not while an index is running: a half-written collection
outlives the outage.
