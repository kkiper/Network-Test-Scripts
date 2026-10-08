"""Command line for the read-only Switch Port Audit of the production switches."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from typing import Optional

from . import __version__, netif
from .checker import FAIL
from .inventory import InventoryError, parse_inventory, read_json
from .report import print_table, write_report
from .switch import SwitchError, connect_to
from .switchaudit import SILENT_NOTE, audit, collect, logins_from_inventory

EXIT_OK = 0
EXIT_PROBLEMS = 1
EXIT_BAD_INPUT = 2
PASSWORD_ENV = "NETCHECK_SWITCH_PASSWORD"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switch_audit",
        description=(
            "Log into the production switches with a READ-ONLY account and compare their "
            "port status, MAC address tables and CDP/LLDP neighbours with the inventory: "
            "which port each device is on, devices without an IP address, empty ports and "
            "unlisted devices. Only 'show' commands are sent. " + SILENT_NOTE),
        epilog=(f"The password is prompted for once per switch, or read from ${PASSWORD_ENV} "
                "(used for every switch). Switch addresses and usernames come from the "
                'inventory\'s "switches" section, or --host/--username for a single switch.'),
    )
    parser.add_argument("inventory", nargs="?",
                        help="JSON file describing the expected interconnect")
    parser.add_argument("--switch", action="append", metavar="NAME",
                        help="audit only this switch (repeatable; default: all with an address)")
    parser.add_argument("--host", help="address of the switch (with a single --switch)")
    parser.add_argument("--username", help="read-only username (overrides the inventory)")
    parser.add_argument("-o", "--output", help="write a report file (.csv or .json)")
    parser.add_argument("--no-colour", "--no-color", action="store_true",
                        help="disable coloured output")
    netif.add_cli_arguments(parser)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    iface, code = netif.resolve_cli_interface(args)
    if code is not None:
        return code
    if not args.inventory:
        parser.error("the inventory file is required")
    try:
        data = read_json(args.inventory)
        connections = parse_inventory(data, args.inventory)
        logins = logins_from_inventory(data, connections)
    except InventoryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT

    if args.switch:
        unknown = [s for s in args.switch if s not in logins]
        if unknown:
            print(f"error: no switch called {', '.join(unknown)} in the inventory "
                  f"(known: {', '.join(logins) or 'none'})", file=sys.stderr)
            return EXIT_BAD_INPUT
        logins = {name: logins[name] for name in args.switch}
    if args.host:
        if len(logins) != 1:
            print("error: --host needs exactly one --switch", file=sys.stderr)
            return EXIT_BAD_INPUT
        next(iter(logins.values())).host = args.host
    if args.username:
        for login in logins.values():
            login.username = args.username

    todo = [login for login in logins.values() if login.host]
    skipped = [name for name, login in logins.items() if not login.host]
    if skipped:
        print(f"note: no address for {', '.join(skipped)} - not audited "
              '(add it to the inventory\'s "switches" section)', file=sys.stderr)
    if not todo:
        print("error: no switch addresses to audit", file=sys.stderr)
        return EXIT_BAD_INPUT

    states = {}
    for login in todo:
        password = os.environ.get(PASSWORD_ENV) or getpass.getpass(
            f"Password for {login.username or '?'}@{login.host} ({login.name}): ")
        print(f"Reading {login.name} ({login.host})...", file=sys.stderr)
        try:
            session = connect_to(login.host, login.username, password, login.device_type,
                                 login.ssh_port, iface)
        except SwitchError as exc:
            print(f"error: {login.name}: {exc}", file=sys.stderr)
            return EXIT_BAD_INPUT
        try:
            states[login.name] = collect(session, login.name)
        except SwitchError as exc:
            print(f"error: {login.name}: {exc}", file=sys.stderr)
            return EXIT_BAD_INPUT
        finally:
            session.close()

    results, unlisted = audit(connections, states)
    colour = not args.no_colour and sys.stdout.isatty()
    if results:
        print_table(results, sys.stdout, colour=colour)
    if unlisted:
        print("\nPorts with a link or traffic that the inventory doesn't list:")
        for item in unlisted:
            macs = ", ".join(item.macs) or "no MAC seen"
            extra = f"  neighbour {item.neighbor}" if item.neighbor else ""
            print(f"  UNLISTED  {item.switch} {item.port_short:<10} link {item.link:<12} "
                  f"{macs}{extra}")
    print(f"\n{SILENT_NOTE}")
    if args.output:
        write_report(results, args.output, args.inventory)
        print(f"Report written to {args.output}")
    problems = any(r.result == FAIL for r in results) or bool(unlisted)
    return EXIT_PROBLEMS if problems else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
