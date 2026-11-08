#!/usr/bin/env bash
# #40 (D22): Qdrant's read-only API key, for the patching knob's recovery line,
# `qdrant-recovery.sh query`. Knob commands run as the knob user, never root
# (patching-hosts' knob.md), and the admin key is root-only. So a second key,
# Qdrant's service.read_only_api_key: in qdrant.env, which Qdrant reads at its
# start, and in qdrant-ro.key, which group qdrant-ro reads. Clients keep the
# admin key. Run from the checkout, never installed.
#
# Usage:
#   sudo bash scripts/qdrant-ro-key.sh mint USER   # first install and rotation alike
#
# It takes effect at Qdrant's next start, and any start takes it: restart in a
# window, with a recovery point first (deploy/README.md, "Read-only key").
# The key is written by redirection, so it reaches no argv and no output.
set -euo pipefail

DIR=/etc/socraticode
ENV_FILE="$DIR/qdrant.env"
KEY_FILE="$DIR/qdrant-ro.key"
GROUP=qdrant-ro
VAR=QDRANT__SERVICE__READ_ONLY_API_KEY

usage() { sed -n '/^# Usage:$/,/^#$/p' "$0" | sed '$d; s/^# \{0,1\}//' >&2; exit 2; }
refuse() { echo "qdrant-ro-key: $*" >&2; exit 2; }

mint() {
  local user=$1 key env_tmp key_tmp rc
  [ "$(id -u)" -eq 0 ] || refuse "mint needs root: sudo bash $0 mint $user"
  getent passwd "$user" >/dev/null || refuse "no user $user"
  [ "$user" != root ] || refuse "not root: knob commands never run as root, so root isn't the knob user"
  grep -q '^QDRANT__SERVICE__API_KEY=.' "$ENV_FILE" 2>/dev/null \
    || refuse "$ENV_FILE has no QDRANT__SERVICE__API_KEY: a read-only key goes beside the admin key, never alone"

  getent group "$GROUP" >/dev/null || groupadd --system "$GROUP"
  gpasswd -a "$user" "$GROUP" >/dev/null
  # 0750 root: the group enters it, and reads only what's its own.
  chgrp "$GROUP" "$DIR"
  chmod 0750 "$DIR"

  key=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')
  [[ $key =~ ^[0-9a-f]{64}$ ]] || refuse "couldn't mint a key"

  # Each file is written beside its target and renamed over it, mode and
  # group first: no reader sees half a file, or a key with the wrong mode.
  umask 077
  env_tmp=$(mktemp "$DIR/.qdrant.env.XXXXXX")
  key_tmp=$(mktemp "$DIR/.qdrant-ro.key.XXXXXX")
  trap 'rm -f "$env_tmp" "$key_tmp"' EXIT
  # grep -v exits 1 when it keeps no line, and 2 when it can't read: only
  # the 2 is a failure. Qdrant started without the admin key would 401 every
  # client, so the new file must still hold it.
  rc=0
  grep -v "^$VAR=" "$ENV_FILE" >"$env_tmp" || rc=$?
  [ "$rc" -le 1 ] || refuse "couldn't read $ENV_FILE (grep exit $rc): it is unchanged"
  printf '%s=%s\n' "$VAR" "$key" >>"$env_tmp"
  grep -q '^QDRANT__SERVICE__API_KEY=.' "$env_tmp" \
    || refuse "the new $ENV_FILE would have no QDRANT__SERVICE__API_KEY: it is unchanged"
  printf '%s\n' "$key" >"$key_tmp"
  chmod 0600 "$env_tmp"
  chgrp "$GROUP" "$key_tmp"
  chmod 0640 "$key_tmp"
  mv -f "$env_tmp" "$ENV_FILE"
  mv -f "$key_tmp" "$KEY_FILE"
  trap - EXIT

  echo "minted ${#key} chars: $VAR in $ENV_FILE, and $KEY_FILE (0640, group $GROUP, $user a member)"
  echo "Qdrant takes it at its next start: restart in a window, then the checks (deploy/README.md, \"Read-only key\")"
}

case "${1:-}" in
  mint) [ $# -eq 2 ] || usage; mint "$2" ;;
  *) usage ;;
esac
