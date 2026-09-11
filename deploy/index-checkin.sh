#!/usr/bin/env bash
# D10: report this host's liveness to notifier's dead-man's timer (#56).
#
# The point is not this script's findings. It is that notifier alerts when this
# script STOPS running at all -- #57's second argument is that the last
# SocratiCode install died silently for nine days because the only thing
# watching it reported into a log nobody read. A store whose absence is an
# alert is the fix.
#
# `status` is this host's own judgement, and `variables` is opaque to notifier
# -- that division is the API boundary this service refuses to cross.
set -euo pipefail

set -a
. /etc/socraticode/notifier.env
set +a

MONITOR_ID="${NOTIFIER_MONITOR_ID:-}"
if [ -z "$MONITOR_ID" ]; then
  echo "index-checkin: NOTIFIER_MONITOR_ID unset in /etc/socraticode/notifier.env" >&2
  exit 1
fi

ADDR="$(/usr/local/bin/tailnet-bind.sh)"
QKEY="$(cat /etc/socraticode/qdrant.key)"

findings=""
add() { findings="${findings}${findings:+,}{\"check\":\"$1\",\"subject\":\"$2\",\"message\":\"$3\"}"; }

# Qdrant answering AND still refusing an unauthenticated read. A store that
# went open is as much a defect as one that went down, and only one of the two
# is visible from a plain health check.
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -H "api-key: ${QKEY}" "http://${ADDR}:6333/collections" || echo 000)
[ "$code" = "200" ] || add qdrant collections "authenticated read returned ${code}"
open=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "http://${ADDR}:6333/collections" || echo 000)
[ "$open" = "401" ] || add qdrant auth "unauthenticated read returned ${open}, expected 401"

# Ollama, and the one model the cohort's vectors are comparable under (D7).
curl -sf --max-time 10 "http://${ADDR}:11434/api/tags" -o /tmp/index-checkin-tags.json \
  || add ollama tags "not reachable"
if [ -f /tmp/index-checkin-tags.json ]; then
  grep -q 'nomic-embed-text' /tmp/index-checkin-tags.json \
    || add ollama model "nomic-embed-text absent"
  rm -f /tmp/index-checkin-tags.json
fi

# Headroom, because a full disk stops an index without stopping a container.
free_pct=$(df --output=pcent / | tail -1 | tr -dc '0-9')
[ "$free_pct" -lt 90 ] || add disk root "root filesystem ${free_pct}% used"

count=$(printf '%s' "$findings" | grep -o '"check"' | wc -l | tr -d ' ')
if [ "$count" -eq 0 ]; then status=ok; else status=alert; fi

payload=$(printf '{"status":"%s","variables":{"source":"co-index","finding_count":%s,"findings":[%s]}}' \
  "$status" "$count" "$findings")

printf '%s' "$payload" | curl -sS --max-time 20 -X POST \
  "http://notifier:9000/api/v1/monitors/${MONITOR_ID}/checkin" \
  -H "X-API-Key: ${NOTIFIER_API_KEY}" \
  -H 'Content-Type: application/json' \
  --data-binary @- \
  -o /tmp/index-checkin-resp.json -w 'index-checkin: %{http_code} status=%s\n'

python3 -c "import json;d=json.load(open('/tmp/index-checkin-resp.json'));print('next_deadline_at',d.get('next_deadline_at'))" 2>/dev/null || true
rm -f /tmp/index-checkin-resp.json
