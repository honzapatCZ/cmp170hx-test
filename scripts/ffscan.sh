#!/bin/bash
# Check installed CMP 170HX cards and print an opaque grade plus basic data.
# Usage: sudo bash scripts/ffscan.sh
# This build only provides card checks. Temporary collection files are removed.
set -euo pipefail
cd "$(dirname "$0")/.."
for argument in "$@"; do
    case "$argument" in
    -h|--help) sed -n '2,4p' "$0"; exit 0 ;;
    *) echo "Usage: sudo bash scripts/ffscan.sh" >&2; exit 2 ;;
    esac
done
umask 077
TMP_ROOT=$(cd -- "${TMPDIR:-/tmp}" && pwd -P)
WORK_DIR=$(mktemp -d "$TMP_ROOT/ffcheck.XXXXXXXX")
cleanup() {
    case "$WORK_DIR" in
        "$TMP_ROOT"/ffcheck.*) rm -rf -- "$WORK_DIR" ;;
    esac
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
KDIR="$WORK_DIR/kprobe"
K="/lib/modules/$(uname -r)/build"
PY=python3
mkdir -p "$KDIR"
$PY samples/gen_probe_c.py > "$KDIR/ffprobe.c"
printf 'obj-m += ffprobe.o\n' > "$KDIR/Makefile"
N=$(grep -c '{ 0x' "$KDIR/ffprobe.c")
if ! sudo make -C "$K" M="$KDIR" modules > "$WORK_DIR/build.log" 2>&1; then
    echo "Could not build the card reader. Check the compiler and running kernel headers." >&2
    exit 1
fi
if command -v nvidia-smi >/dev/null 2>&1; then
    nvidia-smi --query-gpu=pci.bus_id,uuid --format=csv,noheader 2>/dev/null \
        | tr 'A-F' 'a-f' | tr -d ' ' | sed 's/^00000000:/0000:/' \
        > "$WORK_DIR/uuid.csv" || true
else
    : > "$WORK_DIR/uuid.csv"
fi
$PY -m ffcalc.telemetry -o "$WORK_DIR/basic.json"
: > "$WORK_DIR/bar0map.tsv"
for dev in /sys/bus/pci/devices/*; do
    [ "$(cat "$dev/device" 2>/dev/null)" = "0x20c2" ] || continue
    bdf=$(basename "$dev")
    bar0=$(head -1 "$dev/resource" | cut -d' ' -f1)
    uuid=$(awk -F, -v b="$bdf" 'tolower($1)==b {print $2; exit}' "$WORK_DIR/uuid.csv")
    # The read-only probe returns EAGAIN to unload itself after reading.
    if ! sudo env LC_ALL=C insmod "$KDIR/ffprobe.ko" bar0="$bar0" 2> "$WORK_DIR/reader.err"; then
        if ! grep -q 'Resource temporarily unavailable' "$WORK_DIR/reader.err"; then
            echo "Could not read the card. Check permissions and kernel module loading." >&2
            exit 1
        fi
    fi
    LOG="$WORK_DIR/probe_${bdf//[:.]/_}.txt"
    if ! sudo dmesg > "$WORK_DIR/kernel.log" 2>/dev/null; then
        echo "Could not read the card results. Check kernel log permissions." >&2
        exit 1
    fi
    # Restrict results to this card and require a complete word capture.
    $PY - "$WORK_DIR/kernel.log" "$bar0" "$N" "$LOG" <<'PYLOG'
import pathlib
import sys
from ffcalc.ingest import LINE, parse_log
base, expected = int(sys.argv[2], 0), int(sys.argv[3])
matching = []
for line in pathlib.Path(sys.argv[1]).read_text(errors="replace").splitlines():
    match = LINE.search(line)
    if match and base <= int(match.group(2), 16) < base + 0x830000:
        matching.append(line)
# Ignore older captures (including physical-row reads from previous versions).
entries = parse_log("\n".join(matching[-expected:]))
if len(entries) != expected:
    sys.exit("Could not obtain a complete card reading. Collect again.")
with open(sys.argv[4], "w") as output:
    for name, addr, value in entries:
        output.write("FFPROBE %s %08x %08x\n" % (name, addr, value))
PYLOG
    printf '%s\t%s\t%s\n' "$bdf" "$bar0" "$uuid" >> "$WORK_DIR/bar0map.tsv"
done
$PY -m ffcalc scan --dir "$WORK_DIR"
