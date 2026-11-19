"""Card checks only: every input produces the same shareable summary."""
import argparse
import sys

from . import ingest
from .buyer import format_summary
from .dump import load_dump


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ffcalc", description="CMP 170HX card checker")
    sub = ap.add_subparsers(dest="cmd", required=True)
    report = sub.add_parser("report", help="check a saved card capture")
    report.add_argument("dump")
    scan = sub.add_parser("scan", help="check cards in a saved collection")
    scan.add_argument("--dir", required=True, help="collection directory")
    scan.add_argument("--sku", default="8gb", choices=("8gb", "10gb"))
    args = ap.parse_args(argv)

    try:
        if args.cmd == "report":
            print(format_summary(load_dump(args.dump)))
            return 0
        count = 0
        for count, dump in enumerate(ingest.read_collected(args.dir, sku=args.sku), 1):
            if count > 1:
                print()
            print(format_summary(dump, card_number=count))
        if not count:
            print("No cards found.")
            return 1
        return 0
    except (OSError, ValueError, TypeError, KeyError):
        print("Could not read the card capture. Collect the card data again.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
