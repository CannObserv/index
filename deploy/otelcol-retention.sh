#!/usr/bin/env bash
# The collector's storage line beyond rotation (D21, #33): compress, prune,
# cap. Run daily by otelcol-retention.timer, as root.
#
# The exporter (otelcol.yaml) writes plain JSONL and rotates by size. Its own
# `compression: zstd` frames each batch behind a length prefix, which
# `zstd -d` refuses (0.165.0), so the compression is done here: each rotated
# file becomes a standard stream, and `zstd -dc` gives the lines back.
#
# The compressed file goes into archive/, never beside the rotated one. The
# exporter's rotation library counts any <signal>-<UTC>-size.jsonl.zst in its
# own directory as one of its backups, and deletes all but the newest
# max_backups: with 20 planted, two rotations left 14 (0.165.0, 2026-11-20).
# It doesn't look inside a subdirectory.
#
# Any failure exits non-zero before the stamp is written. The check-in reads
# the stamp (`otel:retention`), so a run that fails, or a timer that stops,
# is heard instead of ending the 90-day prune silently.
set -euo pipefail

DIR=/var/lib/otelcol
ARCHIVE="$DIR/archive"
KEEP_DAYS=90
# 1 GiB. The design's estimate is 5-10 MB a day uncompressed for the cohort
# and about a tenth of that compressed: 45-90 MB for 90 days. So this is some
# 11 times the high end, and under a fifth of the 5.4 GB that `/` had left to
# its 80% alert on 2026-11-20. index-checkin.sh reports at half of it.
CAP_BYTES=$(( 1024 * 1024 * 1024 ))

# The exporter creates its files 644. Everything written here is 600.
umask 077

if [ ! -d "$DIR" ]; then
  echo "otelcol-retention: $DIR missing: otelcol.service creates it at its start" >&2
  exit 1
fi

install -d -m 700 "$ARCHIVE"

# Regular files in the directory itself, or in the archive, and only by
# name: a symlink is never followed and no other subdirectory entered.
own() { find "$DIR" -maxdepth 1 -type f "$@"; }
archived() { find "$ARCHIVE" -maxdepth 1 -type f "$@"; }

# What a run killed mid-compression left behind.
archived -name '*.jsonl.zst.tmp' -delete

# A compressed file beside the rotated ones, whatever put it there: the
# exporter would delete it as a backup.
while IFS= read -r -d '' file; do
  mv -- "$file" "$ARCHIVE/"
done < <(own -name '*.jsonl.zst' -print0)

# 1. Compress each rotated file. The exporter names one
#    <signal>-<UTC>-size.jsonl and never writes to it again. Written under
#    another name and renamed, so no reader sees half a stream. The mtime is
#    carried over: the prune below counts from the last write, not from here.
compressed=0
while IFS= read -r -d '' file; do
  out="$ARCHIVE/${file##*/}.zst"
  zstd -q -19 -T1 -f -o "$out.tmp" -- "$file"
  touch -r "$file" "$out.tmp"
  mv "$out.tmp" "$out"
  rm -f -- "$file"
  compressed=$(( compressed + 1 ))
done < <(own -name '*-size.jsonl' -print0 | sort -z)

# 2. Prune at 90 days. A file goes whole, 90 days after its last write, so a
#    record can outlive that by one file's span: 4 MB (otelcol.yaml).
pruned=$(archived -name '*.jsonl.zst' -mmin "+$(( KEEP_DAYS * 24 * 60 ))" -print -delete | wc -l)

# 3. Hold the cap: oldest compressed file first. Never an active file, which
#    the exporter holds open and bounds itself.
total() { { own -printf '%s\n'; archived -printf '%s\n'; } | awk '{ sum += $1 } END { print sum + 0 }'; }
bytes=$(total)
capped=0
while IFS= read -r -d '' entry; do
  [ "$bytes" -gt "$CAP_BYTES" ] || break
  size=${entry#* } file=${size#* } size=${size%% *}
  rm -f -- "$file"
  echo "otelcol-retention: over the cap, deleted $file" >&2
  bytes=$(( bytes - size ))
  capped=$(( capped + 1 ))
done < <(archived -name '*.jsonl.zst' -printf '%T@ %s %p\0' | sort -z -n)
if [ "$bytes" -gt "$CAP_BYTES" ]; then
  echo "otelcol-retention: $bytes bytes, over the cap of $CAP_BYTES, with nothing left to delete" >&2
  exit 1
fi

# 4. The exporter's new files are 644 until here.
own -name '*.jsonl' -exec chmod 600 {} +
archived -exec chmod 600 {} +

touch "$DIR/.retention-ok"
echo "otelcol-retention: compressed $compressed, pruned $pruned, over the cap $capped, $bytes bytes"
