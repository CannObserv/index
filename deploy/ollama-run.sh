#!/usr/bin/env bash
# ExecStart for ollama.service. Same shape as qdrant-run.sh, same reason:
# systemd reads every EnvironmentFile= before ExecStartPre= runs, so resolving
# and publishing have to share a process.
set -euo pipefail

ADDR="$(/usr/local/bin/tailnet-bind.sh)"

docker rm -f ollama >/dev/null 2>&1 || true

# socraticode/ollama-slim:latest, NOT ollama/ollama:latest.
#
# replicator#88 records that the ollama/ollama:latest tag is load-bearing --
# true on a host where SOCRATICODE manages the container, because
# ensureOllamaContainerReady guards on the tag's presence alone and an untag
# silently re-pulls 9.19GB. It does not apply here: on this host systemd owns
# the container and names the image explicitly, and every cohort client runs
# OLLAMA_MODE=external and so manages no container at all. Using our own tag
# keeps a locally-built image from impersonating upstream's.
#
# No API key exists for Ollama -- it has none to enable. Port 11434 is gated by
# the Tailscale ACL alone (#57 D4), which is written down rather than implied.
exec docker run --rm --name ollama \
  --publish "${ADDR}:11434:11434" \
  --volume /var/lib/ollama:/root/.ollama \
  socraticode/ollama-slim:latest serve
