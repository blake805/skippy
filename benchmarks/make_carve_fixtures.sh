#!/bin/sh
# Targets for verifying containerised extraction by hand. See ADR 0019's "What this does
# not claim": CI has no VM, so the live behaviour of unblob inside our restrictions is
# checked here rather than asserted in a test.
#
# Built rather than committed, for the same reason as benchmarks/updater.c: a checked-in
# binary is opaque in review and goes stale against the toolchain.
#
# Usage: sh benchmarks/make_carve_fixtures.sh /tmp/carve
set -eu

OUT="${1:-/tmp/carve}"
mkdir -p "$OUT"
WORK="$OUT/.build"
rm -rf "$WORK"
mkdir -p "$WORK/payload/bin" "$WORK/payload/etc"

# --- 1. a firmware-shaped image: padding, a gzipped tar, more padding -------
# The shape that matters is a container that is not at offset zero, since finding the
# start offset in a blob is the thing carving does that `tar -x` cannot.

cat > "$WORK/agent.c" <<'EOF'
#include <stdio.h>
#include <string.h>
static const char key[] = "PROVISIONING-KEY-DO-NOT-SHIP";
int check_token(const char *t) { return strcmp(t, key) == 0; }
int main(void) { puts("agent"); return check_token("x"); }
EOF
cc -O1 "$WORK/agent.c" -o "$WORK/payload/bin/agent"
printf 'root:x:0:0:root:/root:/bin/sh\n' > "$WORK/payload/etc/passwd"
printf 'admin_password=hunter2\nupdate_url=http://example.invalid/fw\n' \
    > "$WORK/payload/etc/config.ini"

tar -czf "$WORK/payload.tar.gz" -C "$WORK/payload" .

{
    # A plausible header, then padding, so the gzip does not start at offset 0.
    printf '1LPK'
    dd if=/dev/zero bs=1 count=1020 2>/dev/null
    cat "$WORK/payload.tar.gz"
    dd if=/dev/zero bs=1 count=2048 2>/dev/null
} > "$OUT/firmware.bin"

# --- 2. an archive that tries to escape the extraction directory ------------
# unblob should block and report this; our job is to surface the report rather than
# swallow it, because an image that attempts this has said something about itself.

mkdir -p "$WORK/slip"
printf 'you should never see this outside the quarantine\n' > "$WORK/slip/escape.txt"
# GNU-style traversal entry. bsdtar on macOS refuses to write '..' paths directly, so the
# member name is set with -s rather than by creating the path.
tar -cf "$OUT/slip.tar" -C "$WORK/slip" -s '|escape.txt|../../../../tmp/skippy-escaped.txt|' escape.txt

# --- 3. a decompression bomb ------------------------------------------------
# 512 MB of zeros in a few hundred KB. Needs no vulnerability at all, just a ratio, and
# it is as plausible an accident in our own build output as an attack.

dd if=/dev/zero bs=1m count=512 2>/dev/null | gzip -9 > "$OUT/bomb.gz"

rm -rf "$WORK"

echo "fixtures in $OUT:"
for f in firmware.bin slip.tar bomb.gz; do
    printf '  %-14s %s bytes\n' "$f" "$(wc -c < "$OUT/$f" | tr -d ' ')"
done
