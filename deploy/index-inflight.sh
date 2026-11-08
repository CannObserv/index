#!/usr/bin/env bash
# #9, #41: the patching knob's `inflight`. Prints how many writes Qdrant
# served, and how many batches Ollama embedded, in the last five minutes;
# patching-hosts gates each step on 0.
#
# Each client indexes from its own node (D11), and this node is one of them
# for `index` (D20). Nothing on this host sees any of those runs except the
# store's own logs. A run pauses between batches, so five quiet minutes are
# evidence, not proof.
#
# Ollama's too, since #41: a run can embed for over five minutes between
# upserts, and Qdrant's writes alone read 0 through it (2026-11-08). Ollama
# runs one request at a time (n_slots = 1) and llama-server logs each input
# as it starts it, so the inputs between two embed requests' lines are one
# request's. A batch is a request of BATCH_INPUTS inputs or more, or one of
# BATCH_SECONDS or more; inputs past the last request line, as many, are a
# batch still running. A search embeds 1 input and the engine fingerprint
# (D19) 5, every 10 min, in about 1.1 s: over 7 days and 2,379 requests,
# neither crossed either line.
#
# The logs are read from the units' journals, never `docker logs` (#18): a
# knob command never runs as root, and D20 takes exedev out of group docker.
# Each run script runs `docker run` attached, so the server's stdout is the
# unit's, and exedev reads the system journal through group adm.
#
# Qdrant's writes only: a search or a retrieve is POST too, and a session
# searching is not an index run. Fails closed: when a log can't be read, it
# exits 1 and prints no count, so the gate refuses rather than reading an
# error as 0.
set -uo pipefail

# The name, not the address: Qdrant's certificate covers only it (D14).
QURL="https://index.taild0fb76.ts.net:6333"
OURL="http://index.taild0fb76.ts.net:11434"
BATCH_INPUTS=10
BATCH_SECONDS=10

unknown() {
  echo "index-inflight: $*, so in-flight work is unknown" >&2
  exit 1
}

# A silent log is not an idle one: Qdrant doesn't log /readyz, and above INFO
# it logs no request at all. So mark each log first, with a request it does
# log. Qdrant's: no key, so a 401, which also keeps the key out of a knob
# command. Ollama's: a path nothing serves, a 404 that loads no model. Until
# a marker shows, an empty window could be either.
marker="index-inflight/$$.$(date +%s%N)"
curl -s -o /dev/null --max-time 5 -A "$marker" "$QURL/collections" \
  || unknown "qdrant didn't answer the marker request"
curl -s -o /dev/null --max-time 5 "$OURL/$marker" \
  || unknown "ollama didn't answer the marker request"

# read_log VAR UNIT NEEDLE WHAT: the unit's last five minutes into VAR, once
# NEEDLE, its marker's line, shows. Without group adm, exedev still has its
# own user journal: journalctl then exits 0 and prints nothing at all, which
# the marker fails closed on.
read_log() {
  local -n _log=$1
  local unit=$2 needle=$3 what=$4 seen= _
  for _ in 1 2 3 4 5; do
    _log="$(journalctl -u "$unit" --since -5min -o cat --no-pager)" \
      || unknown "journalctl -u $unit failed"
    [ -n "$_log" ] && seen=1
    grep -qF -- "$needle" <<<"$_log" && return 0
    sleep 0.2
  done
  [ -n "$seen" ] \
    || unknown "the journal shows no lines from $unit (is $(id -un) in group adm? $what)"
  unknown "$unit's access log doesn't show the marker request"
}

# journald drops a unit's lines past its rate limit (10000 per 30s, scaled up
# by free disk; Qdrant's peak was 1389, 2026-10-27, Ollama's 554 in 30 s,
# 2026-11), and a dropped line would read as 0. It reports a drop, as the
# unit's own entry, when the unit's next line arrives after the interval: the
# marker at the latest, so a drop in the window shows before it, or the
# marker is dropped too. Matched whole, so a client can't fake one.
no_drop() {  # UNIT LOG
  local rc=0
  grep -qxE "Suppressed [0-9]+ messages from ${1//./\\.}" <<<"$2" || rc=$?
  case $rc in
    0) unknown "journald suppressed $1's lines in the window" ;;
    1) ;;
    *) unknown "grep failed on $1's log" ;;
  esac
}

read_log qlog qdrant.service "\"$marker\"" "is Qdrant logging above INFO?"
no_drop qdrant.service "$qlog"
# Qdrant's access log says nothing the marker didn't: an empty Ollama journal
# is the group, or Ollama logging nowhere.
read_log olog ollama.service "\"/$marker\"" "is ollama.service logging to the journal?"
no_drop ollama.service "$olog"

# The request line, as actix logs it: "METHOD /path?query HTTP/1.1".
write='"((PUT|PATCH|DELETE) /collections/|POST /collections/[^/ ?"]+/points/(delete|payload|payload/delete|payload/clear|vectors/delete|batch)[ ?])'
# grep exits 1 on no match, which is a count of 0; 2 is an error, and unknown.
writes=$(grep -cE "$write" <<<"$qlog") || [ $? -eq 1 ] || unknown "grep failed on qdrant's log"

# Ollama's request line, as gin logs it:
#   [GIN] 2026/11/08 - 19:14:24 | 200 |         1m20s |  172.17.0.1 | POST     "/api/embed"
# its time a Go duration. A client's path ends the line, so only the method
# and path field, whole, makes an embed: a path can't fake one. An input
# starts llama-server's line, where no client writes. Prints the batches, or
# "format" when a line isn't what was measured: an embed with no input in the
# window, or a time it can't read.
batches=$(LC_ALL=C awk -v k="$BATCH_INPUTS" -v s="$BATCH_SECONDS" '
  function secs(d,   v, num, unit) {
    gsub(/ /, "", d)
    if (d == "") return -1
    while (d != "") {
      if (!match(d, /^[0-9]+(\.[0-9]+)?/)) return -1
      num = substr(d, 1, RLENGTH); d = substr(d, RLENGTH + 1)
      if (d ~ /^h/) { v += num * 3600; d = substr(d, 2) }
      else if (d ~ /^ms/) { v += num / 1e3; d = substr(d, 3) }
      else if (d ~ /^ns/) { v += num / 1e9; d = substr(d, 3) }
      else if (d ~ /^us/) { v += num / 1e6; d = substr(d, 3) }
      else if (d ~ /^\xc2\xb5s/) { v += num / 1e6; d = substr(d, 4) }
      else if (d ~ /^m/) { v += num * 60; d = substr(d, 2) }
      else if (d ~ /^s/) { v += num; d = substr(d, 2) }
      else return -1
    }
    return v
  }
  /^slot launch_slot_: id +[0-9]+ \| task [0-9]+ \| processing task/ { n++; inputs++; next }
  /^\[GIN\] / {
    nf = split($0, f, "|")
    if (nf != 5 || f[5] !~ /^ +POST +"\/api\/embed(dings)?"$/) next
    embeds++
    t = secs(f[3])
    if (t < 0) bad = 1
    if (n >= k || t >= s) b++
    n = 0
  }
  END {
    if (n >= k) b++
    if (bad || (embeds && !inputs)) print "format"
    else print b + 0
  }
' <<<"$olog") || unknown "awk failed on ollama's log"
[ "$batches" != format ] \
  || unknown "ollama's log isn't in the format measured (#41): an embed with no input line, or a time that isn't a Go duration"

echo $((writes + batches))
