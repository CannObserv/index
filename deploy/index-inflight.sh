#!/usr/bin/env bash
# #9: the patching knob's `inflight`. Prints how many writes Qdrant served in
# the last five minutes; patching-hosts gates each step on 0.
#
# Each client indexes from its own node (D11), and this node is one of them
# for `index` (D20). Nothing on this host sees any of those runs except
# Qdrant's access log. A run pauses between embedding batches, so five quiet
# minutes are evidence, not proof.
#
# The log is read from qdrant.service's journal, never `docker logs` (#18):
# a knob command never runs as root, and D20 takes exedev out of group
# docker. qdrant-run.sh runs `docker run` attached, so Qdrant's stdout is the
# unit's, and exedev reads the system journal through group adm.
#
# Writes only: a search or a retrieve is POST too, and a session searching is
# not an index run. Fails closed: when the log can't be read, it exits 1 and
# prints no count, so the gate refuses rather than reading an error as 0.
set -uo pipefail

# The name, not the address: the certificate covers only it (D14).
QURL="https://index.taild0fb76.ts.net:6333"

unknown() {
  echo "index-inflight: $*, so in-flight work is unknown" >&2
  exit 1
}

# A silent log is not an idle one: Qdrant doesn't log /readyz, and above INFO
# it logs no request at all. So mark the log first, with a request it does
# log: no key, so a 401, which also keeps the key out of a knob command. Until
# the marker shows, an empty window could be either.
marker="index-inflight/$$.$(date +%s%N)"
curl -s -o /dev/null --max-time 5 -A "$marker" "$QURL/collections" \
  || unknown "qdrant didn't answer the marker request"

# Without group adm, exedev still has its own user journal: journalctl then
# exits 0 and prints nothing at all, which the marker fails closed on.
seen=
for _ in 1 2 3 4 5; do
  log="$(journalctl -u qdrant.service --since -5min -o cat --no-pager)" \
    || unknown "journalctl -u qdrant.service failed"
  [ -n "$log" ] && seen=1
  grep -qF "\"$marker\"" <<<"$log" && break
  log=
  sleep 0.2
done
[ -n "$seen" ] \
  || unknown "the journal shows no lines from qdrant.service (is $(id -un) in group adm? is Qdrant logging above INFO?)"
[ -n "$log" ] || unknown "qdrant's access log doesn't show the marker request"

# journald drops a unit's lines past its rate limit (10000 per 30s, scaled up
# by free disk; Qdrant's peak was 1389, 2026-10-27), and a dropped write would
# read as 0. It reports a drop, as the unit's own entry, when the unit's next
# line arrives after the interval: the marker at the latest, so a drop in the
# window shows before it, or the marker is dropped too. Matched whole, so a
# client's user agent can't fake one.
suppressed=0
grep -qxE 'Suppressed [0-9]+ messages from qdrant\.service' <<<"$log" || suppressed=$?
case $suppressed in
  0) unknown "journald suppressed qdrant.service's lines in the window" ;;
  1) ;;
  *) unknown "grep failed on qdrant's log" ;;
esac

# The request line, as actix logs it: "METHOD /path?query HTTP/1.1".
write='"((PUT|PATCH|DELETE) /collections/|POST /collections/[^/ ?"]+/points/(delete|payload|payload/delete|payload/clear|vectors/delete|batch)[ ?])'
# grep exits 1 on no match, which is a count of 0; 2 is an error, and unknown.
count=$(grep -cE "$write" <<<"$log") || [ $? -eq 1 ] || unknown "grep failed on qdrant's log"
echo "$count"
