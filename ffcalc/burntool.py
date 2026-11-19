#!/usr/bin/env python3
"""Legacy library routines. The programming command is disabled in this build."""
import io
import json
import sys

from . import ffmacro
from .dump import load_dump
from .simulate import simulate


def cmd_status(rd, log=print):
    st = ffmacro.status(rd)
    for name in sorted(st):
        log("%-16s 0x%08x" % (name, st[name]))
    if ffmacro.is_poisoned(st["FUSECTRL"]):
        log("=> POISONED: stop reads, cold-boot the card")
        return 3
    log("=> write window: %s" % ("OPEN" if ffmacro.in_write_window(st)
                                 else "closed"))
    return 0


def cmd_burn(rd, wr, plan_path, dump_path, rows=None, log=print,
             sleep=None, clock=None, force=False):
    with io.open(plan_path, encoding="utf-8") as f:
        plan = json.load(f)
    dump = load_dump(dump_path)
    res = simulate(dump, plan)
    for w in res["warnings"]:
        log("warn: %s" % w)
    if res["errors"]:
        for e in res["errors"]:
            log("REFUSED: %s" % e)
        return 1
    wanted = None
    if rows:
        wanted = {int(r, 0) for r in rows.split(",")}
        unknown = wanted - {rec["row"] for rec in plan["burn"]}
        if unknown:
            log("REFUSED: rows %s not in plan" % sorted(unknown))
            return 1
    kw = {}
    if sleep is not None:
        kw["sleep"] = sleep
    if clock is not None:
        kw["clock"] = clock

    st = ffmacro.status(rd)
    if ffmacro.is_poisoned(st["FUSECTRL"]):
        log("REFUSED: controller poisoned (FUSECTRL=0x%08x); cold-boot first"
            % st["FUSECTRL"])
        return 3
    if not force and not ffmacro.in_write_window(st):
        log("REFUSED: not in write window: %s"
            % (ffmacro.armed_problems(rd, wr) or "idle/CRC/EFP/DSOV check"))
        return 1

    log("CMD_SENSE ...")
    ffmacro.sense(rd, wr, **kw)
    log("CMD_SENSE ok (DSOV=0, inside window)")
    already = set()
    if not force:
        missing = {rec["row"] for rec in plan["burn"]} - \
                  {r for r in dump["ff_rows"]}
        if missing:
            log("REFUSED: dump does not cover rows %s" % sorted(missing))
            return 1
        for rec in plan["burn"]:
            live = ffmacro.read_row(rd, wr, rec["row"], **kw)
            snap = dump["ff_rows"].get(rec["row"])
            if live == snap:
                continue
            if live == int(rec["record"], 0):
                log("row %d already holds this record; will skip "
                    "(interrupted run? resuming is safe)" % rec["row"])
                already.add(rec["row"])
                continue
            log("REFUSED: row %d changed since snapshot "
                "(snapshot 0x%08x, live 0x%08x) -- regenerate the plan"
                % (rec["row"], snap, live))
            return 1
        log("snapshot matches card (%d rows re-read, %d already burned)"
            % (len(plan["burn"]), len(already)))

    try:
        log("arm (SBFE shadow + WDATA_WR_SECURE) ...")
        ffmacro.arm(rd, wr)
        done = 0
        for rec in plan["burn"]:
            if wanted is not None and rec["row"] not in wanted:
                continue
            if rec["row"] in already:
                done += 1
                log("row %3d: already burned, skipped (%s)"
                    % (rec["row"], ",".join(rec.get("fields", []))))
                continue
            record = int(rec["record"], 0)
            got = ffmacro.program_row(rd, wr, rec["row"], record, **kw)
            done += 1
            log("row %3d: burned 0x%08x ok (%s)"
                % (rec["row"], got, ",".join(rec.get("fields", []))))
    except ffmacro.FuseControllerError as exc:
        log("ABORT: %s" % exc)
        log("treat the card as suspect: stop all BAR0 reads, cold-boot, "
            "then re-check with `status` (docs/BURNING.md)")
        return 3
    st = ffmacro.status(rd)
    log("post: FUSECTRL=0x%08x FUSEDEBUG=0x%x CRC=0x%x DSOV=%d"
        % (st["FUSECTRL"], st["FUSEDEBUG"], st["CRC_STATUS"],
           st["DSOV_STATUS"]))
    log("done: %d/%d records. Cold boot now, then re-run ffscan.sh: "
        "fingerprint must be unchanged, plan must shrink"
        % (done, len(plan["burn"])))
    return 0


def main(argv=None):
    print("This build only supports card checks. Run scripts/ffscan.sh.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
