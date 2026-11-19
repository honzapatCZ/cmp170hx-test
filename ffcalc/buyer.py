"""Shareable card check. Raw binning figures and permanent IDs stay internal."""
import math
import re

from .optionmap import SM_PER_TPC, TOTAL_FBP, TOTAL_GPC, TPC_PER_GPC, popcount


PCIE_FLAGS = ("disable_gen3_speed_bit1", "disable_gen3_speed_bit9",
              "pcie_magic_gen4_dis", "pcie_boot_gen23_dis", "pcie_boot_gen3_dis")


def _mask(words, key, width=TOTAL_FBP):
    value = words.get(key)
    if isinstance(value, int) and 0 <= value < (1 << width):
        return value
    return None


def _defect_mask(words, current_key, defect_key, width):
    defects = _mask(words, defect_key, width)
    if defects is None:
        return None, False
    current = _mask(words, current_key, width)
    if current_key in words and (current is None or current & defects != defects):
        return None, True
    return defects, False


def _band(value, thresholds):
    for minimum, grade in thresholds:
        if value >= minimum:
            return grade
    return "E"


def _memory_grade(dump):
    """Ordinal classification only; not a promise of usable capacity.

    Private policy: A >= 96, B >= 80, C >= 64, D >= 48, otherwise E
    for the supported 8gb geometry. Do not print these thresholds.
    """
    if dump["sku"] != "8gb":
        return "Unavailable", False
    words = dump["option_words"]
    defects, inconsistent = _defect_mask(words, "fbp_disable", "fbp_defective", TOTAL_FBP)
    if defects is None:
        return "Unavailable", inconsistent
    blocks = TOTAL_FBP - popcount(defects)
    return _band(blocks, ((12, "A"), (10, "B"), (8, "C"), (6, "D"))), False


def _compute_grade(words):
    """Grade the SM floor after physical defects, never assuming missing masks are zero."""
    gpc, inconsistent = _defect_mask(words, "gpc_disable", "gpc_defective", TOTAL_GPC)
    if gpc is None:
        return "Unavailable", inconsistent
    sms = 0
    for i in range(TOTAL_GPC):
        if gpc & (1 << i):
            continue
        tpc, inconsistent = _defect_mask(words, "tpc_gpc%d_disable" % i,
                                         "tpc_gpc%d_defective" % i, TPC_PER_GPC)
        if tpc is None:
            return "Unavailable", inconsistent
        sms += (TPC_PER_GPC - popcount(tpc)) * SM_PER_TPC
    return _band(sms, ((120, "A"), (112, "B"), (108, "C"), (96, "D"))), False


def _nvlink_grade(words):
    defects, inconsistent = _defect_mask(words, "nvlink_disable", "nvlink_defective", 3)
    if defects is None:
        return "Unavailable", inconsistent
    return {0: "A", 1: "B", 2: "C", 3: "E"}[popcount(defects)], False


def _pcie_grade(words):
    # These flags do not establish an achievable link generation or bandwidth.
    flags = [_mask(words, key, 1) for key in PCIE_FLAGS]
    if any(value is None for value in flags):
        return "Unavailable", False
    return ("B" if any(flags) else "A"), False


def _auxiliary_grade(words):
    # One shared, experimentally interpreted mask; not independent feature tests.
    defects, inconsistent = _defect_mask(words, "sys_pipe_disable", "sys_pipe_defective", 8)
    if defects is None:
        return "Unavailable", inconsistent
    return ("A" if defects == 0 else "E" if defects == 0xff else "B"), False


def _grades(dump):
    words = dump["option_words"]
    results = (
        ("Memory", _memory_grade(dump)),
        ("Compute", _compute_grade(words)),
        ("NVLink", _nvlink_grade(words)),
        ("PCIe", _pcie_grade(words)),
        ("Auxiliary features", _auxiliary_grade(words)),
    )
    return results


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _text(value):
    if not isinstance(value, str):
        return None
    return " ".join("".join(c if c.isprintable() else " "
                            for c in value).split())[:80]


def _pci_address(value):
    if not isinstance(value, str):
        return None
    address = value.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{4,8}:[0-9a-f]{2}:[01][0-9a-f]\.[0-7]", address):
        return None
    domain, location = address.split(":", 1)
    return "%04x:%s" % (int(domain, 16), location)


def format_summary(dump, card_number=None):
    grades = _grades(dump)
    basic = dump.get("basic")
    if not isinstance(basic, dict):
        basic = {}
    title = "CMP 170HX - card check"
    if card_number is not None:
        title += " (card %d)" % card_number
    lines = [title, "=" * len(title), ""]
    address = _pci_address(dump.get("bdf")) or _pci_address(basic.get("pci_bus_id"))
    lines.append("PCIe address: %s" % (address or "unavailable"))
    index = basic.get("gpu_index")
    if index is not None and re.fullmatch(r"[0-9]+", str(index)):
        lines.append("NVIDIA GPU index: %d" % int(index))
    lines.append("")
    for label, (grade, _) in grades:
        suffix = " (experimental)" if label == "Auxiliary features" and grade != "Unavailable" else ""
        lines.append("%s grade: %s%s" % (label, grade, suffix))
    lines.append("")
    model = _text(basic.get("name"))
    if model:
        lines.append("Model: %s" % model)
    for key, label, unit in (("memory_total_mib", "Installed memory", "MiB"),
                             ("temperature_c", "Temperature", "C"),
                             ("power_draw_w", "Power draw", "W")):
        value = _number(basic.get(key))
        if value is not None:
            lines.append("%s: %g %s" % (label, value, unit))
    driver = _text(basic.get("driver_version"))
    if driver:
        lines.append("Driver: %s" % driver)

    recorded = _number(basic.get("recorded_driver_seconds"))
    session = _number(basic.get("gpu_uptime_seconds"))
    if recorded is not None and recorded < 0xffffffff:
        lines.append("Power-on hours: ~%.1f h (recorded driver-active time)" % (recorded / 3600))
    else:
        lines.append("Power-on hours: unavailable")
    if session is not None and session < 0xffffffff:
        lines.append("Current GPU session: %.1f h (since driver initialization)" % (session / 3600))
    if not basic:
        lines.append("Basic driver data: unavailable in this capture")
    if any(inconsistent for _, (_, inconsistent) in grades):
        lines.append("Readout is inconsistent; collect the card data again.")
    elif any(grade == "Unavailable" for _, (grade, _) in grades):
        lines.append("Some categories lack sufficient verified data for a grade.")
    lines.extend(["", "Grades classify hardware configuration; functionality was not tested.",
                  "Stability was not tested."])
    return "\n".join(lines)
