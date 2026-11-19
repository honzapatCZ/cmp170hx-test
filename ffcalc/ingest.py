"""Ingest ffprobe.ko output into UUID-keyed card dumps.

Input is a collected directory produced by scripts/ffscan.sh:

    collected/
      bar0map.tsv                 bdf <TAB> bar0_phys <TAB> nvidia-smi-uuid
      probe_0000_05_00_0.txt      FFPROBE lines for that GPU (from dmesg)

The card identity is the nvidia-smi GPU UUID (chip-derived, survives
reboot and FF burns). `fingerprint` is a burn-invariant hash over the
*_DEFECTIVE words + SBFE bit, used to verify identity when nvidia-smi
is unavailable (e.g. on the glitch bench).
"""
import hashlib
import io
import json
import os
import re

LINE = re.compile(r"FFPROBE (\S+) ([0-9a-fA-F]+) ([0-9a-fA-F]+)")

_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_MAP = os.path.join(_HERE, "data", "ga100-fuses.json")


def load_map(path=None):
    with io.open(path or DEFAULT_MAP, encoding="utf-8") as f:
        return json.load(f)


def sanitize(bdf):
    return re.sub(r"[.:]", "_", bdf)


def parse_log(text):
    """FFPROBE log text -> list of (name, addr, value), later lines win."""
    out = {}
    order = []
    for line in text.splitlines():
        m = LINE.search(line)
        if not m:
            continue
        name = m.group(1)
        if name not in out:
            order.append(name)
        out[name] = (int(m.group(2), 16), int(m.group(3), 16))
    return [(n, out[n][0], out[n][1]) for n in order]


def fingerprint(entries):
    h = hashlib.sha256()
    for name, _addr, val in sorted(entries):
        if name.startswith("defective.") or \
                name == "info.secured_burn_feature_enable":
            h.update(("%s=%08x\n" % (name, val)).encode("ascii"))
    return h.hexdigest()[:16]


FFROW = re.compile(r"ffrow\.(\d+)$")


def build_dump(m, entries, sku="8gb", bdf=None, uuid=None, source=None):
    raw = dict((name, val) for name, _addr, val in entries)
    ff_rows = {}
    for name, val in raw.items():
        mm = FFROW.match(name)
        if mm:
            ff_rows[int(mm.group(1))] = val
    words = {}
    for key, loc in m["fields"].items():
        fused = raw.get(key + ".fused")
        status = raw.get(key + ".status")
        cur = status if status is not None else fused
        if cur is None:
            continue
        words[key] = cur
        if fused is not None and status is not None and fused != status:
            words[key + "__fused"] = fused
    for key, spec in m["bit_fields"].items():
        w = raw.get("bitfield." + key)
        if w is not None:
            words[key] = (w >> spec["bit"]) & 1
    for key in m["defective"]:
        w = raw.get("defective." + key)
        if w is not None:
            words[key] = w
    info = {}
    for name in m["info"]:
        w = raw.get("info." + name)
        if w is not None:
            info[name] = w
    dump = {"sku": sku, "ff_rows": ff_rows, "option_words": words,
            "info": info, "source": source or "ffprobe.ko",
            "read_only": not ff_rows}
    if bdf:
        dump["bdf"] = bdf
    if uuid:
        dump["uuid"] = uuid
    dump["fingerprint"] = fingerprint(entries)
    return dump


def read_collected(dirpath, m=None, sku="8gb"):
    """Yield dumps for every GPU row in collected/bar0map.tsv."""
    m = m or load_map()
    basic_path = os.path.join(dirpath, "basic.json")
    basic = {}
    if os.path.exists(basic_path):
        try:
            with io.open(basic_path, encoding="utf-8") as f:
                saved_basic = json.load(f)
            if isinstance(saved_basic, dict):
                basic = dict((k.lower(), v) for k, v in saved_basic.items()
                             if isinstance(v, dict))
        except (OSError, ValueError):
            pass  # Optional driver data must not block the card analysis.
    path = os.path.join(dirpath, "bar0map.tsv")
    with io.open(path, encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 2:
                continue
            bdf, bar0 = parts[0], parts[1]
            uuid = parts[2] if len(parts) > 2 else None
            log = os.path.join(dirpath, "probe_%s.txt" % sanitize(bdf))
            if not os.path.exists(log):
                continue
            with io.open(log, encoding="utf-8", errors="replace") as lf:
                entries = parse_log(lf.read())
            dump = build_dump(m, entries, sku=sku, bdf=bdf, uuid=uuid,
                              source="ffprobe.ko@%s" % bar0)
            if uuid and uuid.lower() in basic:
                dump["basic"] = basic[uuid.lower()]
            yield dump


def card_id(dump):
    """Filesystem-safe unique id for a card: UUID without prefix, or fp."""
    uuid = dump.get("uuid") or ""
    if uuid.startswith("GPU-"):
        return uuid[4:]
    return uuid or "fp-" + dump.get("fingerprint", "unknown")


def save_dump(outdir, dump):
    if not os.path.isdir(outdir):
        os.makedirs(outdir)
    path = os.path.join(outdir, card_id(dump) + ".json")
    with io.open(path, "w", encoding="utf-8") as f:
        json.dump(dump, f, indent=2, sort_keys=True)
    return path
