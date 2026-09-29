#!/usr/bin/env bash
# Print this host's Tailscale address, waiting for tailscaled to assign one.
#
# Ported from CannObserv/notifier scripts/tailnet_bind.sh (#43 D3, #57 D3).
# Both containers publish on this address alone, never 0.0.0.0, so the store is
# unreachable from exe.dev's internal 10.42.0.0/16, from the exe.dev HTTPS
# proxy, and from the internet. The Tailscale ACL is what decides access.
#
# Two rules it exists to hold, both learned in CannObserv/observo:
#
#   1. Probe /proc/net/fib_trie, never a netlink client. observo#479 found a
#      wait that used `ip addr`, which a unit sandbox SIGSYS-kills while
#      blocking AF_NETLINK -- silently, so the probe still exited 0. Every
#      start then paid a blind sleep while appearing to work. Reading a proc
#      file needs no socket.
#
#   2. Time out non-zero. There is nothing safe to fall back to: a store bound
#      to 0.0.0.0 is reachable from the exe.dev internal network, which is the
#      whole thing the tailnet-only bind exists to prevent. Exhausting the wait
#      fails the start and lets Restart=on-failure retry.
#
# DELIBERATELY NO ESCAPE-HATCH VARIABLE. notifier's copy carries
# NOTIFIER_BIND_HOST for CI, which has no tailnet, and pays for it with a test
# asserting the variable reaches no unit and no env file. This host has no CI
# and no such need, so the hazard is simply absent rather than guarded.
set -euo pipefail

WAIT_SECONDS="${INDEX_TAILNET_WAIT_SECONDS:-60}"
POLL_SECONDS=1

# The Tailscale CGNAT range is 100.64.0.0/10 -- second octet 64 through 127.
# Matched on the address the kernel marks LOCAL, so a peer's tailnet route can
# never be read as this host's own address.
read_tailnet_address() {
  awk '
    /^Local:/                                  { in_local = 1 }
    in_local && $1 == "|--" && $2 ~ /^100\./   { candidate = $2; next }
    in_local && candidate != "" {
      if ($0 ~ /\/32 host LOCAL/) { print candidate; exit }
      candidate = ""
    }
  ' /proc/net/fib_trie |
  awk -F. '$2 >= 64 && $2 <= 127 { print; exit }'
}

deadline=$(( SECONDS + WAIT_SECONDS ))
announced=0
while :; do
  if address="$(read_tailnet_address)" && [[ -n "$address" ]]; then
    (( announced )) && echo "tailnet-bind: address ready: $address" >&2
    echo "$address"
    exit 0
  fi
  if (( SECONDS >= deadline )); then
    cat >&2 <<MSG
tailnet-bind: no Tailscale address after ${WAIT_SECONDS}s.

The containers publish on the tailnet address alone, so there is nothing safe
to fall back to -- refusing to start rather than binding an address reachable
from somewhere it should not be.

  systemctl status tailscaled
  tailscale status
MSG
    exit 1
  fi
  if (( ! announced )); then
    echo "tailnet-bind: waiting up to ${WAIT_SECONDS}s for tailscaled to assign an address" >&2
    announced=1
  fi
  sleep "$POLL_SECONDS"
done
