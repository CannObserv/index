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
#        qdrant-recovery.sh query           a search alone, read-only key
#
# `query` is the knob's recovery line (#40), run as the knob user, never root,
# under timeout -k 10 60 (a TERM at 60 s, a KILL 10 s after it), with only its
# exit code recorded: by apply.sh after a step that restarted Qdrant, and by
# probe.sh --post-boot. So it reads the read-only key (group qdrant-ro, D22),
# takes no counts file (the skill checks counts), and holds itself to 20 s of
# limits: the list and each code collection's info share 10 s, then a 5 s
# scroll and a 5 s search.
#
# query calls no Ollama (#46). Ollama serves embeds from one queue, a
# client's index run holds it for minutes, and the line failed on a healthy
# Qdrant. It scrolls one point of the largest code collection with its stored
# vector, searches with that vector, and needs the point back at the top. A
# tie at the top score counts: duplicate chunks share a vector, and Qdrant
# orders no tie. So it proves that Qdrant ranks by vector after a restart,
# not that Ollama embeds: ollama.service has its own health line
# (/api/tags), and check still embeds.
#
# check's embed waits EMBED_T seconds (60), up to 3 tries: Ollama serialises
# embeds, so a client's index run can queue it behind a batch. Set it after
# sudo (sudo EMBED_T=180 ...): sudo drops it from the caller's env.
#
# snapshot and check are root, because the API key is 0600 root. A key
# reaches curl as a header file from a process substitution, never as an
# argument: /proc has no hidepid here.
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

# "<collection> <points>" per line, sorted by name; exact counts. check runs
# it in $(...), where bash drops set -e, so each call fails it by name (#43):
# a failed count printed "<collection> " and a failed list nothing, and check
# read either as a missing collection. curl's own line, above, says why.
counts() {
  local out names c n
  out=$(q 30 "$QURL/collections") \
    || die "counts: listing the collections failed (curl exit $?)${out:+: $out}"
  names=$(json '"\n".join(sorted(c["name"] for c in d["result"]["collections"]))' <<<"$out" 2>/dev/null) \
    || die "counts: the collection list answered no list: $out"
  for c in $names; do
    out=$(q 30 -X POST -H 'Content-Type: application/json' -d '{"exact": true}' \
      "$QURL/collections/$c/points/count") \
      || die "counts: counting $c failed (curl exit $?)${out:+: $out}"
    n=$(json 'd["result"]["count"]' <<<"$out" 2>/dev/null) && [[ $n =~ ^[0-9]+$ ]] \
      || die "counts: counting $c answered no count: $out"
    printf '%s %s\n' "$c" "$n"
  done
}

total() { awk '{ s += $2 } END { print s + 0 }'; }

# embed LIMIT TRIES WAY-OUT: check's query embedding, as JSON. Only a timeout
# is retried: it's a queue, not a fault. curl reads --max-time 0 as no limit,
# so check refuses one first. WAY-OUT ends the timeout's message, naming
# what is open to the caller (#43). query embeds nothing (#46).
embed() {
  local t=$1 tries=$2 way=$3 i out rc said
  for ((i = 1; i <= tries; i++)); do
    rc=0
    out=$(curl -sS --fail-with-body --max-time "$t" "$OURL/api/embed" \
      -d '{"model": "nomic-embed-text", "input": "search_query: how is the API key installed"}') \
      || rc=$?
    [ "$rc" -eq 28 ] || break
    echo "qdrant-recovery: embed: no answer in $t s (try $i of $tries)" >&2
  done
  [ "$tries" -eq 1 ] && said="1 try" || said="$tries tries"
  [ "$rc" -ne 28 ] \
    || die "embed: no answer in $t s, $said: a client's index run may be ahead of it in Ollama's queue; $way"
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
  local utc before now name start dest
  need_key
  utc=$(date -u +%Y%m%dT%H%M%SZ)
  umask 077
  before="$OUT/qdrant-counts-before-$utc.txt"
  # All the counts or no file: a partial one would read, later, as a baseline
  # that lost collections.
  now=$(counts) || exit
  printf '%s' "${now:+$now$'\n'}" >"$before"
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
  now=$(counts) || exit
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
  emb=$(embed "$EMBED_T" 3 "retry later, or longer: sudo EMBED_T=<seconds> qdrant-recovery.sh check <counts file>")
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
    | json '"\n".join(sorted(c["name"] for c in d["result"]["collections"] if c["name"].startswith("codebase_")))' 2>/dev/null) \
    || die "query: listing the collections failed"
  for c in $names; do
    within t 5
    out=$(q "$t" "$QURL/collections/$c" | json "f'{d[\"result\"].get(\"points_count\") or 0} {($VEC) or \"-\"}'" 2>/dev/null) \
      || die "query: reading $c failed"
    printf '%s %s\n' "$c" "$out"
  done
}

# A scroll's answer on stdin, the vector's name ("" for an unnamed one) in
# argv: the first point's id, then its stored vector as JSON. Exit 3: no
# point list; 4: no point; 5: the point holds no such vector (its id printed).
PICK='import json, sys
try:
    pts = json.load(sys.stdin)["result"]["points"]
    assert isinstance(pts, list)
except Exception:
    sys.exit(3)
if not pts:
    sys.exit(4)
v = pts[0].get("vector")
if sys.argv[1]:
    v = v.get(sys.argv[1]) if isinstance(v, dict) else None
print(pts[0].get("id"))
if not isinstance(v, list) or not v:
    sys.exit(5)
print(json.dumps(v))'

# A search's answer on stdin, the scrolled id in argv: "<hits> <top id>".
# Exit 0 when the id ties at the top score, 6 when it doesn't, 3 on no
# point list.
RANK='import json, sys
try:
    pts = json.load(sys.stdin)["result"]["points"]
    top = pts[0]["score"] if pts else 0
    tied = [str(p["id"]) for p in pts if abs(p["score"] - top) <= 1e-6 * max(1, abs(top))]
except Exception:
    sys.exit(3)
print(len(pts), pts[0]["id"] if pts else "-")
sys.exit(0 if sys.argv[1] in tied else 6)'

query() {
  local deadline now c vec body out rc pt id r hits top
  KEY_FILE=$RO_KEY_FILE
  [ -r "$KEY_FILE" ] || die "can't read $KEY_FILE: run it as a member of group qdrant-ro, or through sudo"
  deadline=$((SECONDS + 10))
  now=$(sizes)
  read -r c _ vec < <(sort -k2 -n <<<"$now" | tail -1)
  [ -n "${c:-}" ] || die "no codebase_ collection to query"
  [ "$vec" != "-" ] || vec=""

  # One point and its stored vector, the one the search goes through.
  body=$(python3 -c 'import json, sys
print(json.dumps({"limit": 1, "with_payload": False, "with_vector": [sys.argv[1]] if sys.argv[1] else True}))' "$vec")
  out=$(q 5 -X POST -H 'Content-Type: application/json' -d "$body" "$QURL/collections/$c/points/scroll") \
    || die "query: scrolling $c failed (curl exit $?)${out:+: $out}"
  rc=0
  pt=$(python3 -c "$PICK" "$vec" <<<"$out" 2>/dev/null) || rc=$?
  case $rc in
    0) ;;
    4) die "query: $c has no point to search with" ;;
    5) die "query: $c's point $pt holds no ${vec:-unnamed} vector to search with" ;;
    *) die "query: scrolling $c answered no points: $out" ;;
  esac
  id=${pt%%$'\n'*}

  # 10 deep, so a few duplicates tied at the top can't push the point out.
  body=$(python3 -c 'import json, sys
q = {"query": json.loads(sys.argv[1]), "limit": 10}
if sys.argv[2]:
    q["using"] = sys.argv[2]
print(json.dumps(q))' "${pt#*$'\n'}" "$vec")
  out=$(q 5 -X POST -H 'Content-Type: application/json' -d "$body" "$QURL/collections/$c/points/query") \
    || die "query: searching $c failed (curl exit $?)${out:+: $out}"
  rc=0
  r=$(python3 -c "$RANK" "$id" <<<"$out" 2>/dev/null) || rc=$?
  case $rc in
    0 | 6) read -r hits top <<<"$r" ;;
    *) die "query: searching $c answered no points: $out" ;;
  esac
  if [ "$hits" -eq 0 ]; then
    echo "query $c: 0 hits"
    die "query: no hits"
  fi
  [ "$rc" -eq 0 ] \
    || die "query: $c's top hit is $top, not the scrolled point $id: the search answers, but doesn't rank by the vector"
  echo "query $c: $hits hits, the scrolled point $id at the top"
  echo "query: ok"
}

case "${1:-}" in
  snapshot) [ $# -eq 1 ] || usage; snapshot ;;
  check) [ $# -eq 2 ] || usage; check "$2" ;;
  query) [ $# -eq 1 ] || usage; query ;;
  *) usage ;;
esac
