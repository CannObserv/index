# Installing the store key into a client repo

The SocratiCode store on `co-index` is gated by a **single global** Qdrant
`service.api_key` (CannObserv/notifier#57). There is no key list, no per-client identity and no
per-collection scope, so every cohort VM holds the same secret and a leak
anywhere is a rotation everywhere.

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
able to reach both ends: exe.dev VMs are isolated from each other.

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

Verify on the target with `codebase_health`, which must name the external Qdrant
and Ollama rather than a container. `000` on both a keyed and unkeyed probe is
the ACL, not the key, and presents as DNS failure.

**Never run the installer under `bash -x`.** Tracing a script that touches a
credential writes the value to stdout; that is how two were leaked during CannObserv/notifier#57.

Rotation has no overlap window — Qdrant holds one `api_key`, so every client
401s from the restart until it is updated. Sequence: mint, write
`/etc/socraticode/qdrant.env`, `systemctl restart qdrant`, then step 2 against
every VM. Not while an index is running: a half-written collection outlives the
outage.
