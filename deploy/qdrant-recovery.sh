#!/usr/bin/env bash
# #9: Qdrant's recovery point for a patching run, and the check after a step
# that stopped it. patching-hosts has no knob line for Qdrant, so its
# recovery-point.sh takes nothing here. The owner's rule is a snapshot, off the
# node, before any step that stops Qdrant: a re-index is expensive.
#
#   sudo qdrant-recovery.sh snapshot        counts, then a full snapshot
#   sudo qdrant-recovery.sh check BEFORE    counts against BEFORE, and a query
#
# Root, because the API key is 0600 root. It reaches curl as a header file
# from a process substitution, never as an argument: /proc has no hidepid here.
set -euo pipefail

# The name, not the address: the certificate covers only it (D14).
QURL="https://index.taild0fb76.ts.net:6333"
OURL="http://index.taild0fb76.ts.net:11434"
KEY_FILE=/etc/socraticode/qdrant.key
OUT=/var/backups

die() {
  echo "qdrant-recovery: $*" >&2
  exit 1
}
usage() { die "usage: qdrant-recovery.sh snapshot | check BEFORE-COUNTS"; }
need_key() { [ -r "$KEY_FILE" ] || die "can't read $KEY_FILE: run it through sudo"; }

key() { printf 'api-key: %s\n' "$(cat "$KEY_FILE")"; }
q() { curl -sS --fail-with-body --max-time "${T:-30}" -H @<(key) "$@"; }
json() { python3 -c "import json, sys; d = json.load(sys.stdin); print($1)"; }

# "<collection> <points>" per line, sorted by name; exact counts.
counts() {
  local names c n
  names=$(q "$QURL/collections" | json '"\n".join(sorted(c["name"] for c in d["result"]["collections"]))')
  for c in $names; do
    n=$(q -X POST -H 'Content-Type: application/json' -d '{"exact": true}' \
      "$QURL/collections/$c/points/count" | json 'd["result"]["count"]')
    printf '%s %s\n' "$c" "$n"
  done
}

total() { awk '{ s += $2 } END { print s + 0 }'; }

snapshot() {
  local utc before name start dest
  need_key
  utc=$(date -u +%Y%m%dT%H%M%SZ)
  umask 077
  before="$OUT/qdrant-counts-before-$utc.txt"
  counts >"$before"
  echo "counts: $before ($(wc -l <"$before") collections, $(total <"$before") points)"

  start=$(date +%s)
  name=$(T=1800 q -X POST "$QURL/snapshots?wait=true" | json 'd["result"]["name"]')
  echo "snapshot: $name in $(($(date +%s) - start)) s"

  # Out of the container: its storage is a volume, its snapshots directory
  # isn't, and --rm discards that at the next restart. It must read back as a
  # tar, or it's no recovery point.
  dest="$OUT/qdrant-$utc.snapshot"
  if ! docker cp "qdrant:/qdrant/snapshots/$name" - | tar -xO >"$dest" \
    || ! tar -tf "$dest" >/dev/null 2>&1; then
    rm -f "$dest"
    die "copying $name out of the container failed; it's still inside, under /qdrant/snapshots"
  fi
  T=300 q -X DELETE "$QURL/snapshots/$name?wait=true" >/dev/null
  stat -c '%a %s %n' "$dest"
  sha256sum "$dest"
}

check() {
  local before=$1 now c n m vec emb body hits bad=0
  need_key
  [ -r "$before" ] || die "can't read $before"
  now=$(counts)
  echo "collections: $(wc -l <"$before") before, $(grep -c . <<<"$now") now;" \
    "points: $(total <"$before") before, $(total <<<"$now") now"

  # A client's index run adds and deletes points while the run goes on, so a
  # count may move either way. A collection gone, or emptied, is a loss.
  while read -r c n; do
    m=$(awk -v c="$c" '$1 == c { print $2 }' <<<"$now")
    if [ -z "$m" ]; then
      echo "  missing: $c (had $n)"
      bad=1
    elif [ "$m" -eq 0 ] && [ "$n" -gt 0 ]; then
      echo "  emptied: $c (had $n)"
      bad=1
    elif [ "$m" != "$n" ]; then
      echo "  changed: $c $n -> $m"
    fi
  done <"$before"

  # A real query, not /readyz: an Ollama embedding against the largest code
  # collection, through the vector its config names.
  c=$(awk '$1 ~ /^codebase_/' <<<"$now" | sort -k2 -n | tail -1 | cut -d' ' -f1)
  [ -n "$c" ] || die "no codebase_ collection to query"
  vec=$(q "$QURL/collections/$c" \
    | json '"" if "size" in (v := d["result"]["config"]["params"]["vectors"]) else next(iter(v))')
  emb=$(curl -sS --fail-with-body --max-time 60 "$OURL/api/embed" \
    -d '{"model": "nomic-embed-text", "input": "search_query: how is the API key installed"}' \
    | json 'json.dumps(d["embeddings"][0])')
  body=$(python3 -c 'import json, sys
q = {"query": json.loads(sys.argv[1]), "limit": 3}
if sys.argv[2]:
    q["using"] = sys.argv[2]
print(json.dumps(q))' "$emb" "$vec")
  hits=$(q -X POST -H 'Content-Type: application/json' -d "$body" \
    "$QURL/collections/$c/points/query" | json 'len(d["result"]["points"])')
  echo "query $c: $hits hits"
  [ "$hits" -gt 0 ] || bad=1

  [ "$bad" -eq 0 ] || die "check failed"
  echo "check: ok"
}

case "${1:-}" in
  snapshot) [ $# -eq 1 ] || usage; snapshot ;;
  check) [ $# -eq 2 ] || usage; check "$2" ;;
  *) usage ;;
esac
