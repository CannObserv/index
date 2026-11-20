#!/usr/bin/env bash
# ExecStart for otelcol.service (D21, #33). Same shape as qdrant-run.sh, same
# reason: resolving the address and publishing on it share a process.
set -euo pipefail

ADDR="$(/usr/local/bin/tailnet-bind.sh)"

docker rm -f otelcol >/dev/null 2>&1 || true

# The files hold Bash commands from every cohort VM, private repos' paths
# among them. This directory is what keeps other users out: the exporter
# creates its files 644 and has no option for it (0.165.0). Docker would
# create a missing mount source 755, and `install -d` also resets one that
# exists.
install -d -m 700 -o root -g root /var/lib/otelcol

# Published on the tailnet address alone, never 0.0.0.0. There is no ingest
# auth, on Ollama's terms (D4, D21): the Tailscale ACL bounds who can send,
# the port only accepts writes, and a forged event skews a count and reads
# nothing.
#
# Root, so the files are root's, with every capability dropped: it can't read
# another user's file or bind a low port. The root filesystem is read-only,
# and the one writable path is the data directory.
#
# 256 MiB against an idle 41 MiB (measured 2026-11-20): Qdrant and Ollama
# share this host's 8 GB (D1). GOMEMLIMIT makes Go collect before the kernel
# kills. No swap, so the limit is the limit.
#
# The image is a version and a digest, as Qdrant's is (#13). Changing this
# line is the update: deploy/README.md, "Collector".
exec docker run --rm --name otelcol \
  --user 0:0 \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --read-only \
  --memory 256m \
  --memory-swap 256m \
  --pids-limit 256 \
  --env GOMEMLIMIT=200MiB \
  --log-opt max-size=10m \
  --log-opt max-file=2 \
  --publish "${ADDR}:4318:4318" \
  --volume /usr/local/share/co-index/otelcol.yaml:/etc/otelcol-contrib/config.yaml:ro \
  --volume /var/lib/otelcol:/var/lib/otelcol \
  otel/opentelemetry-collector-contrib:0.165.0@sha256:e40d61f10c9a617cca1ffd1be4d7e2b3a4b0f0001798ff07e58cabe82e618cea
