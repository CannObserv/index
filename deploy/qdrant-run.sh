#!/usr/bin/env bash
# ExecStart for qdrant.service. Resolving and publishing MUST share a process.
#
# systemd reads every EnvironmentFile= before ExecStartPre= runs, so no
# pre-step can put the tailnet address into the environment ExecStart would
# see. notifier's serve.sh exists for exactly this reason (#43); this is the
# same shape with docker in place of uvicorn.
set -euo pipefail

ADDR="$(/usr/local/bin/tailnet-bind.sh)"

# --rm plus a force-remove pre-step: a container left behind by SIGKILL would
# otherwise make every subsequent start fail on the name.
docker rm -f qdrant >/dev/null 2>&1 || true

# The API key travels by --env-file, never as --env on the command line: argv
# is visible to every user on the host through /proc and to `docker inspect`.
#
# Published on the tailnet address alone -- never 0.0.0.0, which on this host
# would also mean exe.dev's internal 10.42.0.0/16. gRPC 6334 is deliberately
# not published: the client speaks REST.
# TLS is not belt-and-braces on top of WireGuard: SocratiCode REFUSES to send
# QDRANT_API_KEY to a non-HTTPS, non-localhost host, so without this the key
# and the store cannot both exist (#57 D14). The cert is Tailscale's, for this
# node's full MagicDNS name -- the short name `index` is not in its SAN, so
# every client must use https://index.taild0fb76.ts.net:6333.
#
# These three are not secret and so live here rather than in the env-file,
# which exists to keep the API key out of argv and `docker inspect`.
exec docker run --rm --name qdrant \
  --env-file /etc/socraticode/qdrant.env \
  --env QDRANT__SERVICE__ENABLE_TLS=1 \
  --env QDRANT__TLS__CERT=/tls/qdrant.crt \
  --env QDRANT__TLS__KEY=/tls/qdrant.key \
  --publish "${ADDR}:6333:6333" \
  --volume /var/lib/qdrant:/qdrant/storage \
  --volume /etc/socraticode/tls:/tls:ro \
  qdrant/qdrant:v1.17.0
