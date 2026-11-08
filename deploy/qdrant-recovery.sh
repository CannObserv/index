#!/usr/bin/env bash
# #9, #30: Qdrant's recovery point and check for the steps patching-hosts
# doesn't run: a Docker restart, a Qdrant upgrade, an engine change
# (deploy/README.md). The owner's rule is a snapshot, off the node, before any
# step that stops Qdrant: a re-index is expensive.
#
# A patching run needs no snapshot from here: the knob's datastore line has
# recovery-point.sh take it. Its counts file is BEFORE's format, so `check`
# takes it after a step for the one thing the skill's own check doesn't run:
# a real query.
#
#   sudo qdrant-recovery.sh snapshot        counts, then a full snapshot
#   sudo qdrant-recovery.sh check BEFORE    counts against BEFORE, and a query
#        qdrant-recovery.sh query           the query alone, read-only key
#
# `query` is the knob's recovery line (#40): apply.sh runs it as the knob
# user, never root, after a step that restarted Qdrant and after the reboot,
# under a 60 s limit, and records only its exit code. So it reads the
# read-only key (group qdrant-ro, D22), takes no counts file (the skill
# checks counts), and holds itself to 45 s of limits: the list and each code
# collection's info share 10 s, then one embed of 30 s, then a 5 s search.
#
# The query's embed waits EMBED_T seconds (60), up to 3 tries: Ollama
# serialises embeds, so a client's index run can queue it behind a batch. Set
# it after sudo (sudo EMBED_T=180 ...): sudo drops it from the caller's env.
#
# snapshot and check are root, because the API key is 0600 root. It reaches curl as a header file
# from a process substitution, never as an argument: /proc has no hidepid here.
set -euo pipefail

# The name, not the address: the certificate covers only it (D14).
QURL="https://index.taild0fb76.ts.net:6333"
OURL="http://index.taild0fb76.ts.net:11434"
KEY_FILE=/etc/socraticode/qdrant.key
RO_KEY_FILE=/etc/socraticode/qdrant-ro.key
OUT=/var/backups

die() {
  echo "qdrant-recovery: $*" >&2
  exit 1
}
usage() { die "usage: qdrant-recovery.sh snapshot | check BEFORE-COUNTS | query"; }
need_key() { [ -r "$KEY_FILE" ] || die "can't read $KEY_FILE: run it through sudo"; }

key() { printf 'api-key: %s\n' "$(cat "$KEY_FILE")"; }
# q LIMIT ARGS...: each caller sets its own limit. None comes from the
# environment (#39): curl reads --max-time 0 as no limit at all.
q() {
  local t=$1
  shift
  curl -sS --fail-with-body --max-time "$t" -H @<(key) "$@"
}
json() { python3 -c "import json, sys; d = json.load(sys.stdin); print($1)"; }

# "<collection> <points>" per line, sorted by name; exact counts.
counts() {
  local names c n
  names=$(q 30 "$QURL/collections" | json '"\n".join(sorted(c["name"] for c in d["result"]["collections"]))')
  for c in $names; do
    n=$(q 30 -X POST -H 'Content-Type: application/json' -d '{"exact": true}' \
      "$QURL/collections/$c/points/count" | json 'd["result"]["count"]')
    printf '%s %s\n' "$c" "$n"
  done
}

total() { awk '{ s += $2 } END { print s + 0 }'; }

# embed LIMIT TRIES: the query's embedding, as JSON. Only a timeout is
# retried: it's a queue, not a fault. curl reads --max-time 0 as no limit, so
# check refuses one first.
embed() {
  local t=$1 tries=$2 i out rc
  for ((i = 1; i <= tries; i++)); do
    rc=0
    out=$(curl -sS --fail-with-body --max-time "$t" "$OURL/api/embed" \
      -d '{"model": "nomic-embed-text", "input": "search_query: how is the API key installed"}') \
      || rc=$?
    [ "$rc" -eq 28 ] || break
    echo "qdrant-recovery: embed: no answer in $t s (try $i of $tries)" >&2
  done
  [ "$rc" -ne 28 ] \
    || die "embed: no answer in $t s, $tries tries: a client's index run may be ahead of it in Ollama's queue; retry later, or longer: sudo EMBED_T=<seconds> qdrant-recovery.sh check <counts file>"
  [ "$rc" -eq 0 ] || die "embed failed (curl exit $rc): $out"
  json 'json.dumps(d["embeddings"][0])' <<<"$out" 2>/dev/null \
    || die "embed answered no embedding: $out"
}

# search COLLECTION VECTOR EMBEDDING LIMIT: how many points a 3-point query
# returns, through VECTOR ("" for an unnamed one).
search() {
  local body
  body=$(python3 -c 'import json, sys
q = {"query": json.loads(sys.argv[1]), "limit": 3}
if sys.argv[2]:
    q["using"] = sys.argv[2]
print(json.dumps(q))' "$3" "$2")
  q "$4" -X POST -H 'Content-Type: application/json' -d "$body" \
    "$QURL/collections/$1/points/query" | json 'len(d["result"]["points"])'
}

# The vector a collection's config names, or "" for an unnamed one.
VEC='"" if "size" in (v := d["result"]["config"]["params"]["vectors"]) else next(iter(v))'

snapshot() {
  local utc before name start dest
  need_key
  utc=$(date -u +%Y%m%dT%H%M%SZ)
  umask 077
  before="$OUT/qdrant-counts-before-$utc.txt"
  counts >"$before"
  echo "counts: $before ($(wc -l <"$before") collections, $(total <"$before") points)"

  start=$(date +%s)
  name=$(q 1800 -X POST "$QURL/snapshots?wait=true" | json 'd["result"]["name"]')
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
  q 300 -X DELETE "$QURL/snapshots/$name?wait=true" >/dev/null
  stat -c '%a %s %n' "$dest"
  sha256sum "$dest"
}

check() {
  local before=$1 now c n m vec emb hits bad=0
  EMBED_T=${EMBED_T:-60}
  [[ $EMBED_T =~ ^[1-9][0-9]*$ ]] || die "EMBED_T must be a whole number of seconds, 1 or more: not '$EMBED_T'"
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
  vec=$(q 30 "$QURL/collections/$c" | json "$VEC")
  emb=$(embed "$EMBED_T" 3)
  hits=$(search "$c" "$vec" "$emb" 30)
  echo "query $c: $hits hits"
  [ "$hits" -gt 0 ] || bad=1

  [ "$bad" -eq 0 ] || die "check failed"
  echo "check: ok"
}

# within VAR CAP: VAR := a call's limit, CAP seconds at most and never past
# $deadline. Out of time is a failure, not a call with no limit.
within() {
  local left=$((deadline - SECONDS))
  [ "$left" -ge 1 ] || die "query: Qdrant's list and collection info took over 10 s"
  printf -v "$1" '%s' $((left < $2 ? left : $2))
}

# "<collection> <points> <vector>" per codebase_ collection, its size from
# its info: exact counts of every collection took 55.6 s on 51 (#39). It runs
# in $(...), where bash drops set -e, so each call fails it explicitly: a
# skipped collection would query the wrong one.
sizes() {
  local t names c out
  within t 5
  names=$(q "$t" "$QURL/collections" \
    | json '"\n".join(sorted(c["name"] for c in d["result"]["collections"] if c["name"].startswith("codebase_")))') \
    || die "query: listing the collections failed"
  for c in $names; do
    within t 5
    out=$(q "$t" "$QURL/collections/$c" | json "f'{d[\"result\"].get(\"points_count\") or 0} {($VEC) or \"-\"}'") \
      || die "query: reading $c failed"
    printf '%s %s\n' "$c" "$out"
  done
}

query() {
  local deadline now c vec emb hits
  KEY_FILE=$RO_KEY_FILE
  [ -r "$KEY_FILE" ] || die "can't read $KEY_FILE: run it as a member of group qdrant-ro, or through sudo"
  deadline=$((SECONDS + 10))
  now=$(sizes)
  read -r c _ vec < <(sort -k2 -n <<<"$now" | tail -1)
  [ -n "${c:-}" ] || die "no codebase_ collection to query"
  [ "$vec" != "-" ] || vec=""
  emb=$(embed 30 1)
  hits=$(search "$c" "$vec" "$emb" 5)
  echo "query $c: $hits hits"
  [ "$hits" -gt 0 ] || die "query: no hits"
  echo "query: ok"
}

case "${1:-}" in
  snapshot) [ $# -eq 1 ] || usage; snapshot ;;
  check) [ $# -eq 2 ] || usage; check "$2" ;;
  query) [ $# -eq 1 ] || usage; query ;;
  *) usage ;;
esac
