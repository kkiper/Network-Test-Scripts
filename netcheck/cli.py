"""Command-line entry point."""

from __future__ import annotations

import argparse
import shutil
import sys
import threading
from typing import Optional

from . import __version__
from .checker import FAIL, check_all
from .inventory import InventoryError, load_inventory
from .report import print_table, write_baseline, write_report

EXIT_OK = 0
EXIT_FAILURES = 1
EXIT_BAD_INPUT = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="interconnect_test",
        description=(
            "Compare a live local network against its expected physical interconnect: "
            "ping each connected device and verify its MAC address."
        ),
    )
    parser.add_argument("inventory", help="CSV file describing the expected interconnect")
    parser.add_argument("-c", "--count", type=int, default=2,
                        help="ping packets per device (default: 2)")
    parser.add_argument("-t", "--timeout", type=float, default=1.0,
                        help="seconds to wait for each ping reply (default: 1.0)")
    parser.add_argument("-w", "--workers", type=int, default=16,
                        help="devices to test in parallel (default: 16)")
    parser.add_argument("-o", "--output",
                        help="write a report file (.csv or .json, chosen by extension)")
    parser.add_argument("--write-baseline", metavar="CSV",
                        help="write a copy of the inventory with blank expected_mac "
                             "values filled in from discovered MACs")
    parser.add_argument("--switch", action="append", metavar="NAME",
                        help="only test connections on this switch (repeatable)")
    parser.add_argument("--patch-panel", action="append", metavar="NAME",
                        help="only test connections on this patch panel (repeatable)")
    parser.add_argument("--no-colour", "--no-color", action="store_true",
                        help="disable coloured output")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="don't print per-device progress")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.count < 1 or args.timeout <= 0 or args.workers < 1:
        print("error: --count and --workers must be >= 1 and --timeout > 0", file=sys.stderr)
        return EXIT_BAD_INPUT

    try:
        connections = load_inventory(args.inventory)
    except InventoryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT

    if args.switch:
        wanted = {s.lower() for s in args.switch}
        connections = [c for c in connections if c.switch.lower() in wanted]
    if args.patch_panel:
        wanted = {p.lower() for p in args.patch_panel}
        connections = [c for c in connections if c.patch_panel.lower() in wanted]
    if not connections:
        print("error: no connections match the given filters", file=sys.stderr)
        return EXIT_BAD_INPUT

    if any(c.ip and c.status != "unused" for c in connections) and not shutil.which("ping"):
        print("error: the 'ping' command was not found on this system", file=sys.stderr)
        return EXIT_BAD_INPUT

    total = len(connections)
    done = 0
    lock = threading.Lock()

    def progress(res) -> None:
        nonlocal done
        with lock:
            done += 1
            if not args.quiet:
                print(f"  [{done}/{total}] {res.result:<4} {res.connection.label}",
                      file=sys.stderr, flush=True)

    print(f"Testing {total} connection(s) from {args.inventory} ...", file=sys.stderr)
    results = check_all(connections, count=args.count, timeout_s=args.timeout,
                        workers=args.workers, progress=progress)
    print(file=sys.stderr)

    colour = not args.no_colour and sys.stdout.isatty()
    print_table(results, sys.stdout, colour=colour)

    if args.output:
        write_report(results, args.output, args.inventory)
        print(f"Report written to {args.output}")
    if args.write_baseline:
        filled = write_baseline(results, args.write_baseline)
        print(f"Baseline written to {args.write_baseline} ({filled} MAC(s) filled in)")

    return EXIT_FAILURES if any(r.result == FAIL for r in results) else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
