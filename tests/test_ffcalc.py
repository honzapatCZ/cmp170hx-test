import json
import os
import shutil
import tempfile
import unittest

from ffcalc import cli, ffmacro, ffrecord, ffrows, ingest
from ffcalc.analyze import analyze
from ffcalc.dump import load_dump
from ffcalc.generate import plan
from ffcalc.optionmap import BY_KEY
from ffcalc.simulate import simulate


def write_rows_file(dump, path):
    with open(path, "w") as f:
        f.write("# physical FF row readout (CMD_READ), unburnt = 0\n")
        for r, v in sorted(dump["ff_rows"].items()):
            r = int(r, 0) if isinstance(r, str) else r
            f.write("%d %s\n" % (r, v))
    return path

SAMPLE = os.path.join(os.path.dirname(__file__), "..",
                      "samples", "blog_card_example.json")


class TestCodec(unittest.TestCase):
    def test_blog_example(self):
        d = ffrecord.decode(0x0fe805c1)
        self.assertEqual(d, {"chain": 1, "block": 23, "data": 0x0fe8})
        self.assertEqual(ffrecord.encode(1, 23, 0x0fe8), 0x0fe805c1)

    def test_replay_later_wins(self):
        rows = {222: ffrecord.encode(1, 5, 0xffff),
                223: ffrecord.encode(1, 5, 0x0f0f)}
        blocks = ffrecord.replay(rows)
        self.assertEqual(blocks[1][5], 0x0f0f)

    def test_iff_rejected(self):
        with self.assertRaises(ValueError):
            ffrecord.decode(0x12340021)


class TestDump(unittest.TestCase):
    def setUp(self):
        self.dump = load_dump(SAMPLE)

    def test_potential(self):
        rep = analyze(self.dump)
        self.assertEqual(rep["targets"]["fbp_disable"], 0x840)
        self.assertEqual(rep["usable_fbp"], 10)
        self.assertEqual(rep["fb_gib"], 80)
        self.assertEqual(rep["sm_count"], 112)
        self.assertTrue(rep["nvlink_ok"])
        self.assertTrue(rep["pcie_gen4_ok"])

    def test_defective_not_subset_warns(self):
        rep = analyze(self.dump, {"fbp_disable": 0x0})
        self.assertTrue(any("does not contain defective" in w
                            for w in rep["warnings"]))

    def test_gen_sim_roundtrip(self):
        obj = plan(self.dump)
        res = simulate(self.dump, obj)
        self.assertEqual(res["errors"], [])
        changed = {r["key"] for r in res["table"] if r["changed"]}
        self.assertIn("fbp_disable", changed)
        self.assertIn("tpc_gpc0_disable", changed)
        self.assertNotIn("pes_disable", changed)

    def test_block_merge_preserves_neighbors(self):
        obj = plan(self.dump)
        blk19 = [r for r in obj["burn"] if r["block"] == 19]
        self.assertEqual(len(blk19), 1)
        self.assertEqual(int(blk19[0]["new"], 0), 0x4200)

    def test_noop_fields_produce_no_rows(self):
        obj = plan(self.dump, only=["fbio_disable"])
        self.assertEqual(obj["burn"], [])

    def test_skip_and_only(self):
        obj = plan(self.dump, only=["fbp_disable"])
        self.assertTrue(all("fbp_disable" in r["fields"]
                            for r in obj["burn"]))
        obj2 = plan(self.dump, skip=["fbp_disable"])
        self.assertFalse(any("fbp_disable" in r["fields"]
                             for r in obj2["burn"]))

    def test_rows_after_last_used(self):
        obj = plan(self.dump)
        rows = [r["row"] for r in obj["burn"]]
        self.assertEqual(rows, sorted(rows))
        self.assertTrue(all(r >= 238 for r in rows))

    def test_overrides_flow_into_targets(self):
        obj = plan(self.dump, overrides={"fbp_disable": 0x0})
        self.assertTrue(any("does not contain defective" in w
                            for w in obj["warnings"]))

    def test_plan_refused_without_ff_rows(self):
        for rows in ({}, {222: 0x141, 223: 0x181}):
            dump = dict(self.dump, ff_rows=rows)
            with self.assertRaises(ffrows.FFRowsNotRead):
                plan(dump)

    def test_sim_refused_without_ff_rows(self):
        obj = plan(self.dump)
        res = simulate(dict(self.dump, ff_rows={}), obj)
        self.assertTrue(any("ff rows readout" in e for e in res["errors"]))

    def test_a100_rewrites_magic_whole_word(self):
        obj = plan(self.dump, a100=True)
        b46 = [r for r in obj["burn"] if r["block"] == 46]
        b47 = [r for r in obj["burn"] if r["block"] == 47]
        self.assertEqual(len(b46), 1)
        self.assertEqual(int(b46[0]["new"], 0), 0x8000)
        self.assertIn("magic_d", b46[0]["fields"])
        self.assertEqual(len(b47), 1)
        self.assertEqual(int(b47[0]["new"], 0), 0)
        self.assertIn("magic_d", b47[0]["fields"])
        self.assertEqual(obj["targets"]["magic_d"], "0x200000")
        self.assertTrue(any("EXPERIMENTAL" in w for w in obj["warnings"]))
        res = simulate(self.dump, obj)
        self.assertEqual(res["errors"], [])

    def test_a100_noop_when_magic_already_a100(self):
        rows = dict(self.dump["ff_rows"])
        free = sorted(int(r, 0) if isinstance(r, str) else r
                      for r, v in rows.items()
                      if (v if isinstance(v, int) else int(v, 0)) == 0)
        rows[free[0]] = ffrecord.encode(2, 46, 0x8000)
        rows[free[1]] = ffrecord.encode(2, 47, 0x0000)
        obj = plan(dict(self.dump, ff_rows=rows), a100=True)
        self.assertFalse([r for r in obj["burn"] if r["block"] in (46, 47)])

    def test_magic_not_touched_without_a100(self):
        obj = plan(self.dump)
        self.assertNotIn("magic_d", obj["targets"])
        self.assertFalse([r for r in obj["burn"] if r["block"] == 46])


class TestFfRowsParsing(unittest.TestCase):
    def test_formats(self):
        rows = ffrows.parse_rows(
            "# comment\n"
            "222 0x0fe805c1\n"
            "0xde 0xfe805c1\n"
            "FFROW 223 141\n"
            "[ 1569.145419] FFROW 224 0x19801c1   # trailing comment\n"
            "486 0\n")
        self.assertEqual(rows[222], 0x0fe805c1)
        self.assertEqual(rows[223], 141)
        self.assertEqual(rows[224], 0x19801c1)
        self.assertEqual(rows[486], 0)

    def test_bad_lines(self):
        with self.assertRaises(ValueError):
            ffrows.parse_rows("222\n")
        with self.assertRaises(ValueError):
            ffrows.parse_rows("512 0\n")
        with self.assertRaises(ValueError):
            ffrows.parse_rows("222 0x100000000\n")
        with self.assertRaises(ValueError):
            ffrows.parse_rows("222 1\n222 2\n")
        ffrows.parse_rows("222 1\n222 0x1\n")

    def test_coverage_states(self):
        self.assertEqual(ffrows.coverage({"ff_rows": {}})[0], "missing")
        self.assertEqual(ffrows.coverage({"ff_rows": {"0": 5}})[0], "missing")
        self.assertEqual(ffrows.coverage({"ff_rows": {"222": 1}})[0],
                         "partial")
        full = dict((r, 0) for r in range(222, 487))
        self.assertEqual(ffrows.coverage({"ff_rows": full})[0], "complete")


class TestUuidMismatch(unittest.TestCase):
    def setUp(self):
        self.dump = load_dump(SAMPLE)

    def test_sim_rejects_foreign_plan(self):
        obj = plan(self.dump)
        obj["uuid"] = "GPU-00000000-0000-0000-0000-000000000000"
        self.dump["uuid"] = "GPU-11111111-1111-1111-1111-111111111111"
        res = simulate(self.dump, obj)
        self.assertTrue(any("uuid" in e for e in res["errors"]))

    def test_sim_accepts_matching_ids(self):
        self.dump["uuid"] = "GPU-11111111-1111-1111-1111-111111111111"
        obj = plan(self.dump)
        self.assertEqual(obj["uuid"], self.dump["uuid"])
        res = simulate(self.dump, obj)
        self.assertEqual([e for e in res["errors"] if "uuid" in e], [])


class FakeFuseBar(object):
    IDLE = 4 << 16

    def __init__(self, pmc=0x10000001, idle=True, re_idle=True,
                 debug=0, latch_ok=True):
        self.pmc = pmc
        self.idle = idle
        self.re_idle = re_idle
        self.debug = debug
        self.latch_ok = latch_ok
        self.addr = 77
        self.data = 0
        self.cmds = 0

    def rd(self, off):
        if off == ffmacro.PMC_BOOT_0:
            return self.pmc
        if off == ffmacro.FUSECTRL:
            return FakeFuseBar.IDLE if self.idle else 0xe0050000
        if off == ffmacro.FUSEADDR:
            return self.addr
        if off == ffmacro.FUSEDATA:
            return self.data
        if off == ffmacro.FUSEDEBUG:
            return self.debug
        return 0

    def wr(self, off, val):
        if off == ffmacro.FUSEADDR:
            self.addr = val if self.latch_ok else val + 1
        elif off == ffmacro.FUSECTRL and val == ffmacro.CMD_READ:
            self.cmds += 1
            self.idle = self.re_idle
            self.data = self.addr * 3 % 0x100000000


class TestFfMacro(unittest.TestCase):
    def test_full_read_restores_address(self):
        bar = FakeFuseBar()
        rows = ffmacro.capture_rows(bar.rd, bar.wr)
        self.assertEqual(len(rows), 512)
        self.assertEqual(rows[100], 300)
        self.assertEqual(bar.addr, 77)
        self.assertEqual(bar.cmds, 512)

    def test_partial_rows_survive_error(self):
        bar = FakeFuseBar(re_idle=False)
        clock_times = iter([0.0] * 4 + [0.5] * 100)
        out = {}
        with self.assertRaises(ffmacro.FuseControllerError):
            ffmacro.capture_rows(bar.rd, bar.wr, sleep=lambda s: None,
                                 clock=lambda: next(clock_times),
                                 rows=out)
        self.assertEqual(out, {})
        self.assertEqual(bar.addr, 77)  # FUSEADDR restored on failure

    def test_busy_controller_refuses_to_start(self):
        bar = FakeFuseBar(idle=False)
        with self.assertRaises(ffmacro.FuseControllerError) as cm:
            ffmacro.capture_rows(bar.rd, bar.wr)
        self.assertIn("busy", str(cm.exception))
        self.assertEqual(bar.cmds, 0)

    def test_debug_register_and_sentinel_and_latch(self):
        with self.assertRaises(ffmacro.FuseControllerError):
            ffmacro.capture_rows(FakeFuseBar(debug=1).rd, FakeFuseBar().wr)
        with self.assertRaises(ffmacro.FuseControllerError):
            ffmacro.capture_rows(FakeFuseBar(pmc=0).rd,
                                 FakeFuseBar().wr)
        bar = FakeFuseBar(latch_ok=False)
        with self.assertRaises(ffmacro.FuseControllerError) as cm:
            ffmacro.capture_rows(bar.rd, bar.wr)
        self.assertIn("latch", str(cm.exception))

    def test_generated_probe_c_carries_protocol(self):
        src = ffmacro.probe_c_source()
        self.assertNotIn("@", src)
        for needle in ("ffprobe_read_rows", "ffprobe_idle",
                       "writel(1, h + 8519680)",  # CMD_READ -> FUSECTRL
                       "h + 8519684",             # FUSEADDR
                       "h + 8519688"):            # FUSEDATA
            self.assertIn(needle, src)


ALLLOG = os.path.join(os.path.dirname(__file__), "..",
                      "samples", "ffprobe_all.txt")
UUIDS = {
    0xfa000000: ("0000:05:00.0",
                 "GPU-aaaaaaaa-1111-2222-3333-000000000005"),
    0xf9000000: ("0000:06:00.0",
                 "GPU-aaaaaaaa-1111-2222-3333-000000000006"),
    0xfb000000: ("0000:09:00.0",
                 "GPU-aaaaaaaa-1111-2222-3333-000000000009"),
}


class TestIngest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(ALLLOG) as f:
            text = f.read()
        cls.bybar = {}
        for line in text.splitlines():
            m = ingest.LINE.search(line)
            if m:
                bar = int(m.group(2), 16) & 0xff000000
                cls.bybar.setdefault(bar, []).append(line)

    def _collect_dir(self, uuids=None):
        d = tempfile.mkdtemp(prefix="ffcalc-collect-")
        self.addCleanup(shutil.rmtree, d, True)
        with open(os.path.join(d, "bar0map.tsv"), "w") as f:
            for bar, (bdf, uuid) in sorted((uuids or UUIDS).items()):
                f.write("%s\t0x%08x\t%s\n" % (bdf, bar, uuid))
                with open(os.path.join(d, "probe_%s.txt"
                                        % ingest.sanitize(bdf)), "w") as lf:
                    lf.write("\n".join(self.bybar[bar]))
        return d

    def test_grouping(self):
        self.assertEqual(sorted(self.bybar), sorted(UUIDS))
        for lines in self.bybar.values():
            self.assertEqual(len(lines), 61)

    def test_dumps_distinct_uuids_and_fingerprints(self):
        m = ingest.load_map()
        dumps = list(ingest.read_collected(self._collect_dir(), m))
        self.assertEqual(len(dumps), 3)
        ids = {d["uuid"] for d in dumps}
        fps = {d["fingerprint"] for d in dumps}
        self.assertEqual(len(ids), 3)
        self.assertEqual(len(fps), 3)
        for d in dumps:
            want = 0x2d if d["bdf"] == "0000:06:00.0" else 0x840
            self.assertEqual(d["option_words"]["fbp_defective"], want)
            self.assertEqual(ingest.card_id(d), d["uuid"][4:])

    def test_card_id_falls_back_to_fingerprint(self):
        d = {"uuid": None, "fingerprint": "deadbeef00000000"}
        self.assertEqual(ingest.card_id(d), "fp-deadbeef00000000")

    def _ffrow_lines(self):
        return ["[ 1569.9] FFPROBE ffrow.%d 08200008 %08x"
                % (r, 0x0fe805c1 if r == 222 else 0) for r in range(512)]

    def test_cmd_read_log_fills_ff_rows(self):
        m = ingest.load_map()
        entries = ingest.parse_log("\n".join(self._ffrow_lines()))
        dump = ingest.build_dump(m, entries)
        self.assertEqual(ffrows.coverage(dump)[0], "complete")
        self.assertEqual(dump["ff_rows"][222], 0x0fe805c1)
        self.assertFalse(dump["read_only"])

    def test_scan_is_an_opaque_report_even_with_full_rows(self):
        import contextlib
        import io
        d = self._collect_dir()
        for bdf, _uuid in UUIDS.values():
            path = os.path.join(d, "probe_%s.txt" % ingest.sanitize(bdf))
            with open(path, "a") as f:
                f.write("\n" + "\n".join(self._ffrow_lines()))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(["scan", "--dir", d]), 0)
        report = output.getvalue()
        self.assertEqual(report.count("Memory grade: B"), 2)
        self.assertEqual(report.count("Memory grade: C"), 1)
        self.assertEqual(report.count("Compute grade: A"), 2)
        self.assertEqual(report.count("Compute grade: C"), 1)
        for hidden in ("GiB", "SMs", "0x", "fingerprint", "burn", "GPU-", "potential"):
            self.assertNotIn(hidden, report)

    def test_scan_without_rows_succeeds_without_changing_collection(self):
        import contextlib
        import io
        d = self._collect_dir()
        before = {}
        for name in os.listdir(d):
            with open(os.path.join(d, name), "rb") as f:
                before[name] = f.read()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["scan", "--dir", d]), 0)
        self.assertEqual(set(os.listdir(d)), set(before))
        for name, content in before.items():
            with open(os.path.join(d, name), "rb") as f:
                self.assertEqual(f.read(), content)


if __name__ == "__main__":
    unittest.main()
