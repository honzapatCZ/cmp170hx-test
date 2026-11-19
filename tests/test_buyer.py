import contextlib
import io
import json
import os
import re
import tempfile
import unittest
import ctypes
import subprocess
import sys
import types
from unittest import mock

from ffcalc import burntool, capture, cli, ingest, telemetry
from ffcalc.buyer import format_summary
from ffcalc.dump import load_dump


SAMPLE = os.path.join(os.path.dirname(__file__), "..", "samples",
                      "blog_card_example.json")


class TestBuyerReport(unittest.TestCase):
    def setUp(self):
        self.dump = load_dump(SAMPLE)
        # The historical synthetic sample enables some flagged TPCs. Use
        # coherent current masks for grade tests; test contradictions explicitly.
        words = self.dump["option_words"]
        for i in range(8):
            words["tpc_gpc%d_disable" % i] |= words["tpc_gpc%d_defective" % i]

    def test_summary_has_only_an_opaque_grade_and_no_technical_details(self):
        self.dump.update(uuid="GPU-private", fingerprint="private-hash",
                         bdf="0000:05:00.0")
        text = format_summary(self.dump)
        for expected in ("Memory grade: B",
                         "Power-on hours: unavailable", "Stability was not tested"):
            self.assertIn(expected, text)
        for private in ("GPU-private", "private-hash", "0000:05:00.0",
                        "0x", "fbp_disable", "FF rows", "burn",
                        "64", "80", "96", "112", "SMs", "potential", "unlock"):
            self.assertNotIn(private, text)

    def test_missing_defects_do_not_imply_a_perfect_bin(self):
        words = self.dump["option_words"]
        words.pop("fbp_defective")
        text = format_summary(self.dump)
        self.assertIn("Memory grade: Unavailable", text)

    def test_grade_policy_and_boundaries(self):
        for usable, expected in ((12, "A"), (11, "B"), (10, "B"), (9, "C"),
                                 (8, "C"), (7, "D"), (6, "D"), (5, "E"), (0, "E")):
            mask = (1 << (12 - usable)) - 1
            self.dump["option_words"] = {"fbp_defective": mask, "fbp_disable": mask}
            text = format_summary(self.dump)
            self.assertIn("Memory grade: " + expected, text)
            self.assertNotIn("GiB", text)

    def test_compute_grade_boundaries_without_revealing_sm_counts(self):
        for sms, expected in ((128, "A"), (120, "A"), (118, "B"), (112, "B"),
                               (110, "C"), (108, "C"), (106, "D"), (96, "D"),
                               (94, "E"), (0, "E")):
            words = self.dump["option_words"]
            words["gpc_defective"] = words["gpc_disable"] = 0
            disabled = 64 - sms // 2
            for i in range(8):
                mask = (1 << min(disabled, 8)) - 1
                disabled -= min(disabled, 8)
                words["tpc_gpc%d_defective" % i] = mask
                words["tpc_gpc%d_disable" % i] = mask
            text = format_summary(self.dump)
            self.assertIn("Compute grade: " + expected, text)
            self.assertNotIn("SMs", text)
            self.assertNotIn("%d SM" % sms, text)

    def test_missing_compute_data_does_not_hide_memory_grade(self):
        words = self.dump["option_words"]
        del words["tpc_gpc3_defective"]
        text = format_summary(self.dump)
        self.assertIn("Compute grade: Unavailable", text)
        self.assertIn("Memory grade: B", text)
        self.assertIn("NVLink grade: A", text)
        words["gpc_defective"] = words["gpc_disable"] = 1 << 3
        self.assertIn("Compute grade: D", format_summary(self.dump))

    def test_compute_inconsistency_is_flagged_in_its_own_category(self):
        words = self.dump["option_words"]
        words["tpc_gpc3_disable"] = 0
        text = format_summary(self.dump)
        self.assertIn("Compute grade: Unavailable", text)
        self.assertIn("Memory grade: B", text)
        self.assertIn("Readout is inconsistent", text)
        words["tpc_gpc3_disable"] = 0xffffffff
        self.assertIn("Compute grade: Unavailable", format_summary(self.dump))

    def test_nvlink_and_experimental_auxiliary_grades(self):
        words = self.dump["option_words"]
        for mask, expected in ((0, "A"), (1, "B"), (3, "C"), (7, "E")):
            words["nvlink_defective"] = mask
            self.assertIn("NVLink grade: " + expected, format_summary(self.dump))
        words["sys_pipe_disable"] = 0xff
        for mask, expected in ((0, "A"), (1, "B"), (0xff, "E")):
            words["sys_pipe_defective"] = mask
            self.assertIn("Auxiliary features grade: %s (experimental)" % expected,
                          format_summary(self.dump))
        del words["nvlink_defective"]
        del words["sys_pipe_defective"]
        text = format_summary(self.dump)
        self.assertIn("NVLink grade: Unavailable", text)
        self.assertIn("Auxiliary features grade: Unavailable", text)

    def test_pcie_requires_all_flags_and_is_only_a_configuration_grade(self):
        self.assertIn("PCIe grade: Unavailable", format_summary(self.dump))
        for flag in ("disable_gen3_speed_bit1", "disable_gen3_speed_bit9",
                     "pcie_magic_gen4_dis", "pcie_boot_gen23_dis", "pcie_boot_gen3_dis"):
            self.dump["option_words"][flag] = 0
        self.assertIn("PCIe grade: A", format_summary(self.dump))
        self.dump["option_words"]["pcie_magic_gen4_dis"] = 1
        self.assertIn("PCIe grade: B", format_summary(self.dump))
        self.dump["option_words"]["pcie_magic_gen4_dis"] = 2
        self.assertIn("PCIe grade: Unavailable", format_summary(self.dump))

    def test_invalid_masks_and_unverified_geometry_are_not_a_grade(self):
        self.dump["option_words"]["fbp_defective"] = 0xffffffff
        self.assertIn("Memory grade: Unavailable", format_summary(self.dump))
        self.dump["option_words"]["fbp_defective"] = 0
        text = format_summary(self.dump)
        self.assertNotIn("96 GiB", text)
        self.assertIn("Memory grade: A", text)
        self.dump["sku"] = "10gb"
        self.assertIn("Memory grade: Unavailable", format_summary(self.dump))

    def test_inconsistent_current_and_bin_are_flagged(self):
        self.dump["option_words"]["fbp_disable"] = 0
        text = format_summary(self.dump)
        self.assertIn("Readout is inconsistent", text)
        self.assertNotIn("80 GiB", text)

    def test_basic_data_is_optional_and_whitelisted(self):
        self.dump["basic"] = {
            "name": "NVIDIA CMP\n170HX", "driver_version": "610.0",
            "memory_total_mib": "65536", "temperature_c": "42",
            "power_draw_w": "[Not Supported]", "uuid": "secret",
            "serial": "secret", "power_on_hours": "12345",
        }
        text = format_summary(self.dump)
        self.assertIn("Model: NVIDIA CMP 170HX", text)
        self.assertIn("65536 MiB", text)
        self.assertIn("42 C", text)
        self.assertNotIn("Power draw:", text)
        self.assertNotIn("secret", text)
        self.assertNotIn("12345", text)
        for invalid in ("nan", "inf", "-4"):
            self.dump["basic"]["temperature_c"] = invalid
            self.assertNotIn("Temperature:", format_summary(self.dump))

    def test_report_only_provides_summary(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(["report", SAMPLE]), 0)
        self.assertIn("Memory grade: B", output.getvalue())
        self.assertNotIn("fbp_disable", output.getvalue())

    def test_summary_refuses_custom_targets(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                cli.main(["report", SAMPLE, "--set", "fbp_disable=0"])
        self.assertEqual(cm.exception.code, 2)

    def test_old_modes_and_switches_are_unavailable(self):
        for args in (["gen"], ["sim"], ["verify"], ["mergeff"],
                     ["report", SAMPLE, "--technical"],
                     ["scan", "--dir", ".", "--a100"],
                     ["scan", "--dir", ".", "--plans", "plans"]):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as cm:
                    cli.main(args)
            self.assertEqual(cm.exception.code, 2)
        for entry in (burntool.main, capture.main):
            with mock.patch.object(capture, "open_bar0") as open_bar, \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(entry(["--yes"]), 2)
                open_bar.assert_not_called()

    def test_buyer_scan_does_not_plan_save_or_reuse_old_data(self):
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with mock.patch.object(ingest, "read_collected", return_value=iter([self.dump])), \
                    mock.patch.object(ingest, "save_dump") as save, \
                    contextlib.redirect_stdout(output):
                rc = cli.main(["scan", "--dir", directory])
            self.assertEqual(rc, 0)
            save.assert_not_called()
            self.assertEqual(os.listdir(directory), [])
            self.assertIn("card 1", output.getvalue())
            self.assertNotIn("fingerprint", output.getvalue())

    def test_recorded_hours_are_labeled_and_session_is_not_lifetime(self):
        self.dump["basic"] = {"recorded_driver_seconds": 360000, "gpu_uptime_seconds": 7200}
        text = format_summary(self.dump)
        self.assertIn("Power-on hours: ~100.0 h (recorded driver-active time)", text)
        self.assertIn("Current GPU session: 2.0 h", text)
        del self.dump["basic"]["recorded_driver_seconds"]
        text = format_summary(self.dump)
        self.assertIn("Power-on hours: unavailable", text)
        self.assertNotIn("Power-on hours: ~2", text)
        self.dump["basic"]["recorded_driver_seconds"] = 0xffffffff
        self.assertIn("Power-on hours: unavailable", format_summary(self.dump))

    def test_card_reader_contains_no_write_or_row_commands(self):
        generator = os.path.join(os.path.dirname(SAMPLE), "gen_probe_c.py")
        source = subprocess.check_output([sys.executable, generator], universal_newlines=True)
        self.assertIn("readl(", source)
        for forbidden in ("writel(", "ffrows", "CMD_READ", "ffprobe_read_rows"):
            self.assertNotIn(forbidden, source)

    def test_live_log_extractor_selects_each_gpu_and_ignores_old_row_reads(self):
        script = os.path.join(os.path.dirname(SAMPLE), "..", "scripts", "ffscan.sh")
        with open(script, encoding="utf-8") as f:
            parser = re.search(r"<<'PYLOG'\n(.*?)\nPYLOG", f.read(), re.S).group(1)
        with open(os.path.join(os.path.dirname(SAMPLE), "ffprobe_all.txt")) as f:
            fresh = f.read()
        with tempfile.TemporaryDirectory() as directory:
            log = os.path.join(directory, "kernel.log")
            with open(log, "w") as f:
                f.write("FFPROBE ffrow.222 fa820008 deadbeef\n" + fresh)
            for base, expected in (("0xfa000000", 0x852), ("0xf9000000", 0x2d),
                                   ("0xfb000000", 0x852)):
                output = os.path.join(directory, "probe.txt")
                result = subprocess.run([sys.executable, "-c", parser, log, base, "61", output],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        universal_newlines=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                with open(output) as f:
                    entries = ingest.parse_log(f.read())
                self.assertEqual(len(entries), 61)
                self.assertEqual(dict((n, v) for n, _, v in entries)["fbp_disable.status"], expected)

    def test_saved_basic_data_matches_uuid_not_card_order(self):
        with tempfile.TemporaryDirectory() as directory:
            with open(os.path.join(directory, "bar0map.tsv"), "w") as f:
                f.write("0000:05:00.0\t0xfa000000\tGPU-ABC\n")
                f.write("0000:06:00.0\t0xfb000000\tGPU-DEF\n")
            for bdf in ("0000_05_00_0", "0000_06_00_0"):
                with open(os.path.join(directory, "probe_%s.txt" % bdf), "w") as f:
                    f.write("FFPROBE fbp_disable.status fa820488 00000852\n")
            with open(os.path.join(directory, "basic.json"), "w") as f:
                json.dump({"gpu-def": {"temperature_c": "55"},
                           "gpu-abc": {"temperature_c": "42"}}, f)
            dumps = list(ingest.read_collected(directory))
            self.assertEqual([d["basic"]["temperature_c"] for d in dumps], ["42", "55"])
            path = ingest.save_dump(os.path.join(directory, "cards"), dumps[0])
            self.assertEqual(load_dump(path)["basic"]["temperature_c"], "42")


class TestDriverData(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(telemetry, "collect_runtime", return_value={})
        patch.start()
        self.addCleanup(patch.stop)

    def test_unsupported_field_does_not_hide_other_fields_or_mix_gpus(self):
        def query(command, **kwargs):
            field = command[1].split(",", 1)[1]
            if field == "power.draw":
                return mock.Mock(returncode=1, stdout="")
            if field == "temperature.gpu":
                return mock.Mock(returncode=0, stdout="GPU-B, 55\nGPU-A, 42\n")
            return mock.Mock(returncode=0, stdout='GPU-A, "NVIDIA, model"\nGPU-B, [N/A]\n')
        with mock.patch.object(telemetry.subprocess, "run", side_effect=query):
            cards = telemetry.collect()
        self.assertEqual(cards["gpu-a"]["name"], "NVIDIA, model")
        self.assertEqual(cards["gpu-a"]["temperature_c"], "42")
        self.assertEqual(cards["gpu-b"]["temperature_c"], "55")
        self.assertNotIn("power_draw_w", cards["gpu-a"])

    def test_missing_driver_and_timeout_are_optional(self):
        for error in (OSError("not installed"), telemetry.subprocess.TimeoutExpired("nvidia-smi", 5)):
            with mock.patch.object(telemetry.subprocess, "run", side_effect=error):
                self.assertEqual(telemetry.collect(), {})

    def test_runtime_merges_by_uuid_even_without_smi(self):
        with mock.patch.object(telemetry.subprocess, "run", side_effect=OSError()), \
                mock.patch.object(telemetry, "collect_runtime", return_value={
                    "gpu-b": {"recorded_driver_seconds": 7200}}):
            self.assertEqual(telemetry.collect(), {"gpu-b": {"recorded_driver_seconds": 7200}})


class TestNVMLRuntime(unittest.TestCase):
    def _library(self):
        def count(out):
            out._obj.value = 3
            return 0

        def handle(index, out):
            out._obj.value = index + 1
            return 0

        def uuid(device, out, length):
            out.value = ("GPU-%d" % device.value).encode("ascii")
            return 0

        def recorded(device, out):
            if device.value == 2:
                return 3  # NVML_ERROR_NOT_SUPPORTED
            out._obj.timeRun = 360000 if device.value == 1 else 0xffffffff
            return 0

        def uptime(device, out):
            out._obj.uptime = device.value * 3600
            return 0

        return types.SimpleNamespace(
            nvmlInit_v2=mock.Mock(return_value=0), nvmlShutdown=mock.Mock(return_value=0),
            nvmlDeviceGetCount_v2=mock.Mock(side_effect=count),
            nvmlDeviceGetHandleByIndex_v2=mock.Mock(side_effect=handle),
            nvmlDeviceGetUUID=mock.Mock(side_effect=uuid),
            nvmlDeviceGetBBXTimeData_v1=mock.Mock(side_effect=recorded),
            nvmlDeviceGetGpuUptime_v1=mock.Mock(side_effect=uptime))

    def test_real_abi_and_per_card_supported_counters(self):
        self.assertEqual(ctypes.sizeof(telemetry._BBXTime), ctypes.sizeof(ctypes.c_uint))
        lib = self._library()
        with mock.patch.object(telemetry.ctypes, "CDLL", return_value=lib):
            cards = telemetry.collect_runtime()
        self.assertEqual(cards, {
            "gpu-1": {"recorded_driver_seconds": 360000, "gpu_uptime_seconds": 3600},
            "gpu-2": {"gpu_uptime_seconds": 7200},
            "gpu-3": {"gpu_uptime_seconds": 10800}})
        lib.nvmlShutdown.assert_called_once_with()

    def test_old_driver_missing_optional_symbols(self):
        lib = self._library()
        del lib.nvmlDeviceGetBBXTimeData_v1
        with mock.patch.object(telemetry.ctypes, "CDLL", return_value=lib):
            cards = telemetry.collect_runtime()
        self.assertEqual(cards["gpu-1"], {"gpu_uptime_seconds": 3600})
        del lib.nvmlDeviceGetGpuUptime_v1
        with mock.patch.object(telemetry.ctypes, "CDLL", return_value=lib):
            self.assertEqual(telemetry.collect_runtime(), {})

    def test_missing_library_failed_init_and_failed_enumeration(self):
        with mock.patch.object(telemetry.ctypes, "CDLL", side_effect=OSError()):
            self.assertEqual(telemetry.collect_runtime(), {})
        lib = self._library()
        lib.nvmlInit_v2.return_value = 9
        with mock.patch.object(telemetry.ctypes, "CDLL", return_value=lib):
            self.assertEqual(telemetry.collect_runtime(), {})
        lib.nvmlShutdown.assert_not_called()
        lib.nvmlInit_v2.return_value = 0
        lib.nvmlDeviceGetCount_v2.side_effect = None
        lib.nvmlDeviceGetCount_v2.return_value = 9
        with mock.patch.object(telemetry.ctypes, "CDLL", return_value=lib):
            self.assertEqual(telemetry.collect_runtime(), {})
        lib.nvmlShutdown.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
