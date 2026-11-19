#!/usr/bin/env python3
"""Legacy BAR helpers. The raw-capture command is disabled in this build."""
import io
import mmap
import os
import struct
import sys


BAR0_LEN = 0x1000000

_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_MAP = os.path.join(_HERE, "data", "ga100-fuses.json")


def open_bar0(bdf=None):
    base = "/sys/bus/pci/devices"
    cands = []
    for dev in sorted(os.listdir(base)):
        vp = os.path.join(base, dev, "device")
        try:
            with io.open(vp) as f:
                dev_id = f.read().strip()
        except IOError:
            continue
        if dev_id != "0x20c2":
            continue
        cands.append(dev)
    if bdf:
        want = bdf if len(bdf.split(":")) == 3 else "0000:" + bdf
        cands = [c for c in cands if c == want] or [want]
    if not cands:
        raise SystemExit("no device 0x20c2 found")
    path = os.path.join(base, cands[0], "resource0")
    # Same access pattern cmpunlocker uses against a live nvidia driver:
    # fd opened O_RDWR, mapping ACCESS_WRITE. Word capture reads only;
    # --read-ff-rows writes just FUSEADDR/FUSECTRL (CMD_READ command).
    fd = os.open(path, os.O_SYNC | os.O_RDWR)
    try:
        length = os.path.getsize(path) or BAR0_LEN
        mm = mmap.mmap(fd, length, access=mmap.ACCESS_WRITE)
    finally:
        os.close(fd)
    return cands[0], mm


def rd(mm, addr):
    mm.seek(int(addr, 0))
    return struct.unpack("<I", mm.read(4))[0]


def wr(mm, addr, value):
    mm.seek(int(addr, 0))
    mm.write(struct.pack("<I", value & 0xffffffff))


def main(argv=None):
    print("This build only supports card checks. Run scripts/ffscan.sh.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
