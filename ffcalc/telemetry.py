"""Optional, read-only driver data for buyer summaries (standard library only)."""
import argparse
import csv
import ctypes
import io
import json
import os
import subprocess


FIELDS = (
    ("pci.bus_id", "pci_bus_id"),
    ("name", "name"),
    ("driver_version", "driver_version"),
    ("memory.total", "memory_total_mib"),
    ("temperature.gpu", "temperature_c"),
    ("power.draw", "power_draw_w"),
)


class _BBXTime(ctypes.Structure):
    _fields_ = [("timeRun", ctypes.c_uint)]


class _GPUUptime(ctypes.Structure):
    _fields_ = [("uptime", ctypes.c_uint)]


def _function(lib, name, args):
    function = getattr(lib, name)
    function.argtypes = args
    function.restype = ctypes.c_int
    return function


def collect_runtime():
    """Read per-GPU runtime using the driver's NVML library, if supported.

    BBX time counts cumulative driver-active seconds, an approximation of
    power-on time. GPU uptime is only the current session. Neither energy
    divided by current watts nor host uptime can establish lifetime hours.
    ABI: https://docs.nvidia.com/deploy/nvml-api/latest/api/group__nvmlDeviceQueries.html
    Both v1 structs contain one unsigned int, with no version member.
    """
    try:
        lib = ctypes.CDLL("nvml.dll" if os.name == "nt" else "libnvidia-ml.so.1")
        init = _function(lib, "nvmlInit_v2", [])
        shutdown = _function(lib, "nvmlShutdown", [])
        count_gpus = _function(lib, "nvmlDeviceGetCount_v2", [ctypes.POINTER(ctypes.c_uint)])
        get_handle = _function(lib, "nvmlDeviceGetHandleByIndex_v2",
                               [ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)])
        get_uuid = _function(lib, "nvmlDeviceGetUUID",
                             [ctypes.c_void_p, ctypes.POINTER(ctypes.c_char), ctypes.c_uint])
    except (OSError, AttributeError):
        return {}
    if init() != 0:
        return {}
    cards = {}
    try:
        count = ctypes.c_uint()
        if count_gpus(ctypes.byref(count)) != 0:
            return cards
        for index in range(count.value):
            handle = ctypes.c_void_p()
            uuid = ctypes.create_string_buffer(96)
            if get_handle(index, ctypes.byref(handle)) != 0 or \
                    get_uuid(handle, uuid, len(uuid)) != 0:
                continue
            identity = uuid.value.decode("ascii", errors="replace").lower()
            if not identity.startswith("gpu-"):
                continue
            for name, structure, field, key in (
                    ("nvmlDeviceGetBBXTimeData_v1", _BBXTime, "timeRun", "recorded_driver_seconds"),
                    ("nvmlDeviceGetGpuUptime_v1", _GPUUptime, "uptime", "gpu_uptime_seconds")):
                try:
                    get_time = _function(lib, name, [ctypes.c_void_p, ctypes.POINTER(structure)])
                except AttributeError:
                    continue
                data = structure()
                if get_time(handle, ctypes.byref(data)) == 0:
                    seconds = getattr(data, field)
                    if seconds != 0xffffffff:
                        cards.setdefault(identity, {})[key] = seconds
    finally:
        shutdown()
    return cards


def collect():
    """Return basic data keyed by UUID; unsupported queries remain absent.

    Query separately so one unsupported field cannot hide the others. UUID is
    included in every query to keep multi-GPU data associated with its card.
    """
    cards = {}
    for field, key in FIELDS:
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=uuid," + field,
                 "--format=csv,noheader,nounits"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                universal_newlines=True, timeout=5, check=False)
        except (OSError, subprocess.TimeoutExpired):
            break
        if result.returncode:
            continue
        for row in csv.reader(io.StringIO(result.stdout), skipinitialspace=True):
            if len(row) != 2:
                continue
            uuid, value = (part.strip() for part in row)
            if not uuid.lower().startswith("gpu-") or not value or \
                    value.lower() in ("n/a", "[n/a]", "not supported", "[not supported]"):
                continue
            cards.setdefault(uuid.lower(), {})[key] = value
    for uuid, runtime in collect_runtime().items():
        cards.setdefault(uuid, {}).update(runtime)
    return cards


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-o", "--out", required=True)
    args = ap.parse_args(argv)
    with io.open(args.out, "w", encoding="utf-8") as f:
        json.dump(collect(), f, indent=2, sort_keys=True)
    return 0


if __name__ == "__main__":
    main()
