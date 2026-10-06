"""Command line for discovering devices that aren't in the inventory."""

from __future__ import annotations

import argparse
import shutil
import sys
from typing import Optional

from . import __version__
from .discover import (
    DEFAULT_MAX_HOSTS, DEFAULT_OUI_PATH, EXPECTED, UNEXPECTED, UNKNOWN, discover, found_row,
    load_oui, local_addresses, new_entry, off_subnet_warning, parse_subnets, primary_address,
    suggest_subnets, summarize, write_discovery,
)
from .inventory import InventoryError, parse_inventory, read_json
from .report import save_json

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_BAD_INPUT = 2

_COLOURS = {"UNKNOWN": "\033[33m", "MOVED": "\033[33m", "MAC CONFLICT": "\033[31m",
            "NOT FOUND": "\033[90m", "EXPECTED": "\033[32m"}
_RESET = "\033[0m"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="discover",
        description=(
            "Find devices on the local network that aren't in the expected interconnect: "
            "ping every address in the given subnets, read this computer's ARP table and "
            "compare with the inventory. Only devices on the same subnet/VLAN as this "
            "computer can be found. Make sure scanning is permitted on the network."
        ),
    )
    parser.add_argument("inventory", nargs="?",
                        help="JSON inventory to compare with (optional)")
    parser.add_argument("-s", "--subnet", action="append", metavar="CIDR",
                        help="subnet to sweep, e.g. 192.168.1.0/24 or '192.168.1.0 "
                             "255.255.255.0' (repeatable; default: the /24s of the "
                             "inventory's IP addresses, else this computer's /24)")
    parser.add_argument("-t", "--timeout", type=float, default=0.5,
                        help="seconds to wait for each ping reply (default 0.5)")
    parser.add_argument("-w", "--workers", type=int, default=64,
                        help="addresses pinged in parallel (default 64)")
    parser.add_argument("--max-hosts", type=int, default=DEFAULT_MAX_HOSTS,
                        help=f"refuse to sweep more addresses than this (default "
                             f"{DEFAULT_MAX_HOSTS})")
    parser.add_argument("--all", action="store_true",
                        help="also list devices that are as expected")
    parser.add_argument("--oui", metavar="CSV", default=DEFAULT_OUI_PATH,
                        help="IEEE oui.csv for vendor names (optional)")
    parser.add_argument("-o", "--output", help="write a report file (.csv or .json)")
    parser.add_argument("--add-unknown", metavar="JSON",
                        help="write a copy of the inventory with the unknown devices added")
    parser.add_argument("--no-colour", "--no-color", action="store_true",
                        help="disable coloured output")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def print_found(found, out, colour: bool) -> None:
    headers = ("STATUS", "IP", "MAC", "VENDOR", "REPLY", "INVENTORY", "DETAIL")
    keys = ("category", "ip", "mac", "vendor", "reply", "inventory", "message")
    rows = [[str(found_row(f)[k]) for k in keys] for f in found]
    widths = [max([len(h)] + [len(r[i]) for r in rows]) for i, h in enumerate(headers[:-1])]

    def fmt(cells, category=""):
        parts = [cells[i].ljust(widths[i]) for i in range(len(widths))] + [cells[-1]]
        if colour and category in _COLOURS:
            parts[0] = f"{_COLOURS[category]}{parts[0]}{_RESET}"
        return "  ".join(parts).rstrip()

    out.write(fmt(list(headers)) + "\n")
    out.write("  ".join("-" * w for w in widths) + "  " + "-" * len(headers[-1]) + "\n")
    for f, row in zip(found, rows):
        out.write(fmt(row, f.category) + "\n")


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    data, connections = None, []
    if args.inventory:
        try:
            data = read_json(args.inventory)
            connections = parse_inventory(data, args.inventory)
        except InventoryError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_BAD_INPUT
    if args.add_unknown and data is None:
        print("error: --add-unknown needs an inventory file", file=sys.stderr)
        return EXIT_BAD_INPUT

    subnet_text = " ".join(args.subnet or suggest_subnets(connections,
                                                          fallback=primary_address()))
    if not subnet_text:
        print("error: give the subnet(s) to sweep with --subnet, e.g. 192.168.1.0/24",
              file=sys.stderr)
        return EXIT_BAD_INPUT
    try:
        networks = parse_subnets(subnet_text, args.max_hosts)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT
    if args.timeout <= 0 or args.workers < 1:
        print("error: --timeout must be > 0 and --workers >= 1", file=sys.stderr)
        return EXIT_BAD_INPUT
    if not shutil.which("ping"):
        print("error: the 'ping' command was not found on this system", file=sys.stderr)
        return EXIT_BAD_INPUT

    warning = off_subnet_warning(networks, local_addresses(networks))
    if warning:
        print(f"warning: {warning}", file=sys.stderr)
    subnets = [str(n) for n in networks]
    total = sum(n.num_addresses for n in networks)
    print(f"Sweeping {', '.join(subnets)} (up to {total} addresses). Only devices on this "
          "computer's subnet/VLAN can be seen.", file=sys.stderr)

    def progress(done: int, count: int) -> None:
        if done == count or done % 16 == 0:
            print(f"\r  {done}/{count} addresses pinged", end="", file=sys.stderr, flush=True)

    found = discover(connections, networks, timeout_s=args.timeout, workers=args.workers,
                     vendors=load_oui(args.oui), progress=progress)
    print("\n", file=sys.stderr)

    shown = found if args.all else [f for f in found if f.category != EXPECTED]
    colour = not args.no_colour and sys.stdout.isatty()
    if shown:
        print_found(shown, sys.stdout, colour)
    else:
        print("Nothing unexpected found.")
    counts = summarize(found)
    print("\nSummary: " + ", ".join(f"{n} {c.lower()}" for c, n in counts.items()))

    if args.output:
        write_discovery(found, args.output, subnets)
        print(f"Report written to {args.output}")
    if args.add_unknown:
        unknown = [f for f in found if f.category == UNKNOWN]
        data["connections"].extend(new_entry(f) for f in unknown)
        save_json(data, args.add_unknown)
        print(f"Inventory with {len(unknown)} unknown device(s) added written to "
              f"{args.add_unknown} - fill in their patch panel and switch port.")

    return EXIT_UNEXPECTED if any(f.category in UNEXPECTED for f in found) else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
