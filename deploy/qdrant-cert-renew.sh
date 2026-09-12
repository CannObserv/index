#!/usr/bin/env bash
# Renew the Qdrant TLS cert, and restart Qdrant only if it actually changed.
#
# Tailscale's certs are Let's Encrypt: 90 days. Tailscale renews automatically
# ONLY when it also owns the install location -- its own docs are explicit that
# a cert written to files by `tailscale cert` is the operator's to renew,
# because tailscaled has no idea where it was put or how to reload it.
#
# Without this timer the whole cohort's search dies on a date roughly three
# months out, with no warning and no failed unit: Qdrant keeps serving, and
# every client fails TLS verification instead. That is the exact failure class
# #57 exists to remove, so it gets a timer AND a finding in the check-in.
set -euo pipefail

NAME="$(tailscale status --json | python3 -c 'import sys,json;print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
CRT=/etc/socraticode/tls/qdrant.crt
KEY=/etc/socraticode/tls/qdrant.key

before="$(sha256sum "$CRT" | cut -d' ' -f1)"

# `tailscale cert` is a no-op well before expiry and re-issues inside the
# renewal window, so this is safe to run on a cadence.
tailscale cert --cert-file "$CRT" --key-file "$KEY" "$NAME"
chmod 0644 "$CRT"
chmod 0640 "$KEY"

after="$(sha256sum "$CRT" | cut -d' ' -f1)"

if [ "$before" = "$after" ]; then
  echo "qdrant-cert-renew: unchanged, expires $(openssl x509 -in "$CRT" -noout -enddate | cut -d= -f2)"
  exit 0
fi

# Qdrant reads the cert once at start. A renewed file on disk that nothing
# reloaded is indistinguishable from a renewal that worked, until it isn't.
echo "qdrant-cert-renew: renewed, restarting qdrant"
systemctl restart qdrant
echo "qdrant-cert-renew: now expires $(openssl x509 -in "$CRT" -noout -enddate | cut -d= -f2)"
