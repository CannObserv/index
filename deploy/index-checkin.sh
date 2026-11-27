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

# Sourced, not exported: no probe below needs them in its environment.
. /etc/socraticode/status.env

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

# `fault` is co-status's `metadata.fault` (#10, CannObserv/status#28): an
# `alert` whose key differs from the open fault's reports at once, so a new
# finding mid-fault is heard. `check:subject` only -- the message carries ages,
# codes and percentages, and a key holding one would report every tick. The
# checks run in a fixed order, so the key needs no sort. A finding that clears
# mid-fault changes the key too: by design that reports the rest at once, and
# co-status sends no *cleared* for the set it replaces (its spec, F2).
# Counted here, not after (#48): the findings are one line, so `grep -c`
# over them was never more than 1. Not `((count++))`, which returns 1 from 0:
# under `set -e` the first finding would end the script.
findings="" fault="" count=0
add() {
  findings="${findings}${findings:+,}{\"check\":\"$1\",\"subject\":\"$2\",\"message\":\"$3\"}"
  fault="${fault}${fault:+,}\"$1:$2\""
  count=$(( count + 1 ))
}

# Qdrant answering AND still refusing an unauthenticated read. A store that
# went open is as much a defect as one that went down, and only one of the two
# is visible from a plain health check.
#
# Every key goes to curl as a header FILE (#8). An argument is readable by
# every user on this host for as long as the call runs: its /proc has no
# hidepid. printf is a builtin, so the key is on no process's argv.
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
  -H @<(printf 'api-key: %s\n' "$QKEY") "${QURL}/collections" || echo 000)
[ "$code" = "200" ] || add qdrant collections "authenticated read returned ${code}"
open=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "${QURL}/collections" || echo 000)
[ "$open" = "401" ] || add qdrant auth "unauthenticated read returned ${open}, expected 401"

# The cohort's declared links (#38): each sibling a repo's .socraticode.json
# links must name a codebase_<projectId> that holds points. Only once the
# store answered: a down store is qdrant:collections, one finding. The key
# goes as its file's path. link-check.py caches GitHub's copies for hours, so
# this fetches nothing on most ticks. Exit 1 is one `subject<TAB>message` line
# per finding; 2 is unknown, no finding (Qdrant blinked since the read above);
# anything else, or 1 with nothing said, is the check broken, and heard.
# Its own budgets expire as unknown well inside the `timeout`, which bounds
# the rest (CR 4): the unit has no start timeout, and a hang here would
# silence the whole check-in.
if [ "$code" = "200" ]; then
  rc=0
  links=$(timeout -k 5 120 /usr/local/bin/link-check.py check "$QURL" /etc/socraticode/qdrant.key \
    /usr/local/share/co-index/cohort.json /var/cache/co-index/links.json) || rc=$?
  if [ "$rc" -eq 1 ] && [ -n "$links" ]; then
    while IFS=$'\t' read -r subject message; do
      add links "$subject" "$message"
    done <<< "$links"
  elif [ "$rc" -ne 0 ] && [ "$rc" -ne 2 ]; then
    add links check "link check exited ${rc}"
  fi
fi

# Ollama, and the one model the cohort's vectors are comparable under (D7).
curl -sf --max-time 10 "http://${ADDR}:11434/api/tags" -o /tmp/index-checkin-tags.json \
  || add ollama tags "not reachable"
if [ -f /tmp/index-checkin-tags.json ]; then
  if grep -q 'nomic-embed-text' /tmp/index-checkin-tags.json; then
    # The same model can embed differently under a new engine, and no client
    # can tell (#11). Exit 1 is drift, its line the message. Exit 2 is
    # unknown, no finding: a timeout behind a client's batch is not drift.
    rc=0
    drift=$(/usr/local/bin/engine-fingerprint.py check "http://${ADDR}:11434" \
      /usr/local/share/co-index/engine-fingerprint.json) || rc=$?
    [ "$rc" -ne 1 ] || add ollama engine "${drift:-fingerprint check failed}"
  else
    add ollama model "nomic-embed-text absent"
  fi
  rm -f /tmp/index-checkin-tags.json
fi

# The TLS cert expires in 90 days and qdrant-cert-renew.timer is what keeps it
# alive. If that timer ever stops, Qdrant keeps serving happily and every
# CLIENT fails verification instead -- a healthy-looking server and a dead
# cohort. So the expiry is reported here too, well before it bites. The renew
# script's MIN_VALIDITY_DAYS renews before this line (test_cert_renew.py).
CERT_DAYS_MIN=21
days_left=$(( ( $(date -d "$(openssl x509 -in /etc/socraticode/tls/qdrant.crt -noout -enddate | cut -d= -f2)" +%s) - $(date +%s) ) / 86400 ))
[ "$days_left" -gt "$CERT_DAYS_MIN" ] || add tls cert "certificate expires in ${days_left} day(s)"

# The collector (D21, #33): an empty export, on the port clients send to. It
# is answered 200 and writes nothing. Down is a finding, never unknown:
# nothing queues in front of it the way a client's batch does in front of
# Ollama's embed. curl prints 000 itself when it can't connect.
ocode=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
  -H 'Content-Type: application/json' -d '{}' "http://${ADDR}:4318/v1/logs") || true
[ "$ocode" = "200" ] || add otel collector "empty export returned ${ocode:-nothing}"

# Its files. otelcol-retention.sh holds the directory to 1 GiB by deleting
# the oldest: half of that is over five times the design's estimate for 90 days,
# so a flooding client or a wrong estimate is heard before anything is
# deleted. This isn't `/`'s headroom, which stays the disk GC's (below).
OTEL_BYTES_MAX=$(( 512 * 1024 * 1024 ))
obytes=$(du -sb /var/lib/otelcol 2>/dev/null | cut -f1) || true
[ "${obytes:-0}" -le "$OTEL_BYTES_MAX" ] \
  || add otel bytes "$(( obytes / 1048576 )) MiB on disk, over $(( OTEL_BYTES_MAX / 1048576 ))"

# The retention run stamps its success. The timer is daily, so 36 hours is
# one run missed. Unheard, a dead timer ends the 90-day prune and the cap.
ostamp=$(stat -c %Y /var/lib/otelcol/.retention-ok 2>/dev/null) || ostamp=0
ohours=$(( ( $(date +%s) - ostamp ) / 3600 ))
if [ "$ostamp" -eq 0 ]; then
  add otel retention "otelcol-retention has never succeeded"
elif [ "$ohours" -ge 36 ]; then
  add otel retention "otelcol-retention last succeeded ${ohours} h ago"
fi

# No headroom check here: a full disk stops an index without stopping a
# container, so `/` pages on the cohort disk GC's own sensor and monitor,
# co-index-disk-headroom (#16). One here too would page one disk twice.

if [ "$count" -eq 0 ]; then status=ok; else status=alert; fi

payload=$(printf '{"status":"%s","variables":{"source":"co-index","finding_count":%s,"findings":[%s]},"metadata":{"fault":[%s]}}' \
  "$status" "$count" "$findings" "$fault")

# co-status took over the dead-man's timers (notifier#83, CannObserv/status#2). Same
# path, body and monitor id as notifier's; only the host and the key changed.
# The credentials kept notifier's names through that switch, and took
# co-status's once co-index had its own repo (#8).
printf '%s' "$payload" | curl -sS --max-time 20 -X POST \
  "http://status:9000/api/v1/monitors/${STATUS_MONITOR_ID}/checkin" \
  -H @<(printf 'X-API-Key: %s\n' "$STATUS_API_KEY") \
  -H 'Content-Type: application/json' \
  --data-binary @- \
  -o /tmp/index-checkin-resp.json -w "index-checkin: %{http_code} sent=${status} findings=${count}\n"

python3 -c "import json;d=json.load(open('/tmp/index-checkin-resp.json'));print('next_deadline_at',d.get('next_deadline_at'))" 2>/dev/null || true
rm -f /tmp/index-checkin-resp.json
