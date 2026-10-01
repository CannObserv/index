#!/usr/bin/env bash
# D10: report this host's liveness to co-status's dead-man's timer (notifier#56, notifier#83).
#
# The point is not this script's findings. It is that co-status alerts when this
# script STOPS running at all -- notifier#57's second argument is that the last
# SocratiCode install died silently for nine days because the only thing
# watching it reported into a log nobody read. A store whose absence is an
# alert is the fix.
#
# `status` is this host's own judgement, and `variables` is opaque to co-status
# -- that division is the API boundary this service refuses to cross.
set -euo pipefail

set -a
. /etc/socraticode/status.env
set +a

# Both, before any probe: the file is written by hand, and an empty key would
# otherwise go out as an empty header, a 401 that exits 0.
for var in STATUS_MONITOR_ID STATUS_API_KEY; do
  if [ -z "${!var:-}" ]; then
    echo "index-checkin: ${var} unset in /etc/socraticode/status.env" >&2
    exit 1
  fi
done

ADDR="$(/usr/local/bin/tailnet-bind.sh)"
QKEY="$(cat /etc/socraticode/qdrant.key)"
# Qdrant is HTTPS since D14, and the cert's SAN is the FULL MagicDNS name only
# -- https://index:6333 fails verification. Same URL every client uses.
QURL="https://index.taild0fb76.ts.net:6333"

findings=""
add() { findings="${findings}${findings:+,}{\"check\":\"$1\",\"subject\":\"$2\",\"message\":\"$3\"}"; }

# Qdrant answering AND still refusing an unauthenticated read. A store that
# went open is as much a defect as one that went down, and only one of the two
# is visible from a plain health check.
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -H "api-key: ${QKEY}" "${QURL}/collections" || echo 000)
[ "$code" = "200" ] || add qdrant collections "authenticated read returned ${code}"
open=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "${QURL}/collections" || echo 000)
[ "$open" = "401" ] || add qdrant auth "unauthenticated read returned ${open}, expected 401"

# Ollama, and the one model the cohort's vectors are comparable under (D7).
curl -sf --max-time 10 "http://${ADDR}:11434/api/tags" -o /tmp/index-checkin-tags.json \
  || add ollama tags "not reachable"
if [ -f /tmp/index-checkin-tags.json ]; then
  grep -q 'nomic-embed-text' /tmp/index-checkin-tags.json \
    || add ollama model "nomic-embed-text absent"
  rm -f /tmp/index-checkin-tags.json
fi

# The TLS cert expires in 90 days and qdrant-cert-renew.timer is what keeps it
# alive. If that timer ever stops, Qdrant keeps serving happily and every
# CLIENT fails verification instead -- a healthy-looking server and a dead
# cohort. So the expiry is reported here too, well before it bites.
days_left=$(( ( $(date -d "$(openssl x509 -in /etc/socraticode/tls/qdrant.crt -noout -enddate | cut -d= -f2)" +%s) - $(date +%s) ) / 86400 ))
[ "$days_left" -gt 21 ] || add tls cert "certificate expires in ${days_left} day(s)"

# Headroom, because a full disk stops an index without stopping a container.
free_pct=$(df --output=pcent / | tail -1 | tr -dc '0-9')
[ "$free_pct" -lt 90 ] || add disk root "root filesystem ${free_pct}% used"

# `|| true` is load-bearing, not defensive noise. Under `set -o pipefail` a
# grep that matches nothing fails the whole pipeline and `set -e` kills the
# script -- and "matches nothing" is precisely the HEALTHY case. Without this
# the check-in succeeded only when there was something to complain about, so a
# perfectly well host would have gone silent and tripped its own dead-man's
# timer. Found by running this script for the first time on a green host.
count=$(printf '%s' "$findings" | grep -c '"check"' || true)
if [ "$count" -eq 0 ]; then status=ok; else status=alert; fi

payload=$(printf '{"status":"%s","variables":{"source":"co-index","finding_count":%s,"findings":[%s]}}' \
  "$status" "$count" "$findings")

# co-status took over the dead-man's timers (notifier#83, CannObserv/status#2). Same
# path, body and monitor id as notifier's; only the host and the key changed.
# The credentials kept notifier's names through that switch, and took
# co-status's once co-index had its own repo (#8).
printf '%s' "$payload" | curl -sS --max-time 20 -X POST \
  "http://status:9000/api/v1/monitors/${STATUS_MONITOR_ID}/checkin" \
  -H "X-API-Key: ${STATUS_API_KEY}" \
  -H 'Content-Type: application/json' \
  --data-binary @- \
  -o /tmp/index-checkin-resp.json -w "index-checkin: %{http_code} sent=${status} findings=${count}\n"

python3 -c "import json;d=json.load(open('/tmp/index-checkin-resp.json'));print('next_deadline_at',d.get('next_deadline_at'))" 2>/dev/null || true
rm -f /tmp/index-checkin-resp.json
