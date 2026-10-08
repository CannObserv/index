#!/usr/bin/env bash
# #9: the patching knob's `inflight`. Prints how many writes Qdrant served in
# the last five minutes; patching-hosts gates each step on 0.
#
# Clients index from their own nodes (D11), so nothing on this host sees an
# index run except Qdrant's access log. A run pauses between embedding batches,
# so five quiet minutes are evidence, not proof.
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

for _ in 1 2 3 4 5; do
  log="$(docker logs --since 5m qdrant 2>&1)" || unknown "docker logs qdrant failed"
  grep -qF "\"$marker\"" <<<"$log" && break
  log=
  sleep 0.2
done
[ -n "$log" ] || unknown "qdrant's access log doesn't show the marker request"

# The request line, as actix logs it: "METHOD /path?query HTTP/1.1".
write='"((PUT|PATCH|DELETE) /collections/|POST /collections/[^/ ?"]+/points/(delete|payload|payload/delete|payload/clear|vectors/delete|batch)[ ?])'
# grep exits 1 on no match, which is a count of 0; 2 is an error, and unknown.
count=$(grep -cE "$write" <<<"$log") || [ $? -eq 1 ] || unknown "grep failed on qdrant's log"
echo "$count"
