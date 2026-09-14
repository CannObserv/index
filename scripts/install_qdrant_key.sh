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
# docs/SOCRATICODE.md and CannObserv/notifier#57.
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

target="$repo/.claude/settings.local.json"
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
