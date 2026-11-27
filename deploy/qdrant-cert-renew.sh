#!/usr/bin/env bash
# Renew the Qdrant TLS cert. Qdrant reloads it itself, within its cert_ttl (#52).
#
# Tailscale's certs are Let's Encrypt: 90 days. Tailscale renews automatically
# ONLY when it also owns the install location -- its own docs are explicit that
# a cert written to files by `tailscale cert` is the operator's to renew,
# because tailscaled has no idea where it was put or how to reload it.
#
# Without this timer the whole cohort's search dies on a date roughly three
# months out, with no warning and no failed unit: Qdrant keeps serving, and
# every client fails TLS verification instead. That is the exact failure class
# notifier#57 exists to remove, so it gets a timer AND a finding in the check-in.
set -euo pipefail

NAME="$(tailscale status --json | python3 -c 'import sys,json;print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
CRT=/etc/socraticode/tls/qdrant.crt
KEY=/etc/socraticode/tls/qdrant.key

before="$(sha256sum "$CRT" | cut -d' ' -f1)"

# Without --min-validity, `tailscale cert` hands back its cached cert while
# that is still valid, and once it is due (ARI's window, else two thirds of
# its life) renews it in the BACKGROUND: the call returns first. The script then saw no change and the
# new cert waited a week, which let index-checkin's tls:cert open (#49).
# With it, tailscaled renews synchronously whenever the cached cert has less
# than this left, and answers from its cache otherwise, so this stays safe to
# run on a cadence.
#
# Tied to index-checkin.sh's CERT_DAYS_MIN by tests/deploy/test_cert_renew.py:
# weekly with an hour's jitter, a renewal lands with between this less 7 days
# 1 hour and this left, which must stay at or above that line plus a day (the
# check-in floors to whole days, so it alerts under 22). And well under the
# CA's lifetime (90 days today): a cert issued with less than this would fail
# its own test, and every run would issue another.
MIN_VALIDITY_DAYS=30
tailscale cert --min-validity "$(( MIN_VALIDITY_DAYS * 24 ))h" \
  --cert-file "$CRT" --key-file "$KEY" "$NAME"
chmod 0644 "$CRT"
chmod 0640 "$KEY"

after="$(sha256sum "$CRT" | cut -d' ' -f1)"

if [ "$before" = "$after" ]; then
  echo "qdrant-cert-renew: unchanged, expires $(openssl x509 -in "$CRT" -noout -enddate | cut -d= -f2)"
  exit 0
fi

# No restart (#52). Qdrant reloads the pair from disk on the first handshake
# after its cert_ttl, which qdrant-run.sh sets to an hour, and keeps serving
# the old pair if the new one won't load. A restart cost about 19.5 s of
# search, unwindowed, and a start on a file it can't load exits. Whether the
# reload happened is index-checkin's tls:served, which reads what Qdrant
# serves.
echo "qdrant-cert-renew: renewed, expires $(openssl x509 -in "$CRT" -noout -enddate | cut -d= -f2); Qdrant reloads it within its cert_ttl"
