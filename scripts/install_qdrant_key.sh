#!/usr/bin/env bash
# Install the shared Qdrant API key into a repo's .claude/settings.local.json.
#
#   ssh co-index.exe.xyz "sudo sed -n 's/^QDRANT__SERVICE__API_KEY=//p' \
#       /etc/socraticode/qdrant.env" \
#     | ssh <vm>.exe.xyz 'bash ~/install_qdrant_key.sh ~/<repo>'
#
# THE KEY ARRIVES ON STDIN. Never as an argument: argv is visible in `ps` to
# every other process on the box for the life of the call, and lands in the
# caller's shell history. No shell variable here ever holds it either — it goes
# from stdin straight into python, so it is never in this script's environment.
#
# Never run this under `bash -x`. Tracing a script that touches a credential is
# how two were leaked during #57; the trace writes the value to stdout, and from
# there to whatever was capturing it.
#
# The store on co-index is gated by a single global Qdrant service.api_key —
# no key list, no per-client identity, no per-collection scope. Every cohort VM
# holds the same secret, so a leak anywhere is a rotation everywhere. See
# CannObserv/notifier's docs/reference/shared-store.md, and #57 there.
set -euo pipefail
umask 077

repo="${1:-}"
if [ -z "$repo" ]; then
  echo "usage: install_qdrant_key.sh <repo-dir>   (key on stdin)" >&2
  exit 2
fi
if [ ! -d "$repo" ]; then
  echo "no such directory: $repo" >&2
  exit 2
fi

rel=".claude/settings.local.json"
target="$repo/$rel"

# Refuse anything git would commit. CannObserv/broker#17 asserted this file
# "is git-ignored"; in CannObserv/broker it was not, and that repo is PUBLIC
# (notifier#68). Four of five cohort repos carried the rule, so the assertion
# read as true right up to the exception that would have put the cohort's one
# Qdrant key a `git add -A` from GitHub. Per #57 that is a rotation on every
# VM, with no overlap window.
#
# Three distinct failures, three remedies, so they do not share a message. The
# checks run BEFORE mkdir below: a refusal must leave nothing behind.
#
# `check-ignore` is asked whatever the SOURCE of the rule. A global
# core.excludesfile really does stop a commit from this VM, which is the whole
# of this script's responsibility. Pinning the rule to the repo's own
# .gitignore is the repo's job (broker#18); enforcing it here would refuse a
# target that is in fact safe.
if ! git -C "$repo" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "$repo is not a git work tree - refusing to write the shared key" >&2
  echo "  the check that this file cannot be committed cannot be made here" >&2
  exit 2
fi
if git -C "$repo" ls-files --error-unmatch "$rel" >/dev/null 2>&1; then
  echo "$repo/$rel is TRACKED by git - refusing to write the shared key" >&2
  echo "  .gitignore does not apply to tracked paths, so adding a rule fixes" >&2
  echo "  nothing. The key may already be in history: untrack it, and rotate" >&2
  echo "  the store key on co-index and every cohort VM (#57)." >&2
  exit 2
fi
if ! git -C "$repo" check-ignore -q "$rel"; then
  echo "$repo/$rel is not ignored - refusing to write the shared key" >&2
  echo "  add '$rel' to $repo/.gitignore, then re-run." >&2
  echo "  verify with: git -C $repo check-ignore -v $rel" >&2
  exit 2
fi

mkdir -p "$repo/.claude"

# The merge, the atomic replace and the mode all happen in python: a shell
# redirect would truncate the destination before the new content exists, so a
# crash mid-write would leave a half-key that 401s exactly like a wrong one.
TARGET="$target" python3 -c '
import json, os, pathlib, sys

key = sys.stdin.read().strip()
if not key:
    sys.exit("empty key on stdin - nothing written")

p = pathlib.Path(os.environ["TARGET"])
try:
    existing = json.loads(p.read_text() or "{}") if p.exists() else {}
except json.JSONDecodeError as exc:
    sys.exit(f"{p} is not valid JSON, refusing to overwrite it: {exc}")
if not isinstance(existing, dict):
    sys.exit(f"{p} is not a JSON object, refusing to overwrite it")

existing.setdefault("env", {})["QDRANT_API_KEY"] = key

# Write beside the target, chmod, then rename: the replace is atomic and the
# file is never briefly group- or world-readable.
tmp = p.with_name(p.name + ".tmp")
tmp.write_text(json.dumps(existing, indent=2) + "\n")
tmp.chmod(0o600)
tmp.replace(p)

# Length, never the value. A truncated transfer 401s just like a wrong key;
# this is the only cheap discriminator between the two.
print(f"installed {len(key)} chars into {p}", file=sys.stderr)
'
