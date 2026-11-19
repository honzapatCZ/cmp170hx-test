import io
import json


def _h(v):
    if isinstance(v, int):
        return v
    return int(str(v), 0)


def load_dump(path):
    with io.open(path, encoding="utf-8") as f:
        raw = json.load(f)
    sku = raw.get("sku", "8gb")
    if sku not in ("8gb", "10gb"):
        raise ValueError("sku must be 8gb or 10gb, got %r" % sku)
    rows = {}
    for k, v in (raw.get("ff_rows") or {}).items():
        row = int(k, 0)
        if not 0 <= row <= 511:
            raise ValueError("fuse row out of range: %s" % k)
        rows[row] = _h(v)
    words = {}
    for k, v in (raw.get("option_words") or {}).items():
        words[k] = _h(v)
    out = {"sku": sku, "ff_rows": rows, "option_words": words,
           "source": path}
    for extra in ("uuid", "fingerprint", "bdf", "info", "basic"):
        if extra in raw:
            out[extra] = raw[extra]
    return out
