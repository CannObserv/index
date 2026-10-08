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

if ! log="$(docker logs --since 5m qdrant 2>&1)"; then
  echo "index-inflight: docker logs qdrant failed, so in-flight work is unknown" >&2
  exit 1
fi

# The request line, as actix logs it: "METHOD /path?query HTTP/1.1".
write='"((PUT|PATCH|DELETE) /collections/|POST /collections/[^/ ?"]+/points/(delete|payload|payload/delete|payload/clear|vectors/delete|batch)[ ?])'
grep -cE "$write" <<<"$log" || true
