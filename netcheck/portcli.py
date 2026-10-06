"""Command line for verifying unused runs with the test switch."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from typing import Optional

from . import __version__
from .checker import FAIL, CheckResult
from .cisco import expand_port_range
from .inventory import InventoryError, parse_inventory, read_json
from .portverify import TestSwitchSettings, plan_batches, preflight, verify_batch
from .report import print_table, write_report
from .switch import SwitchError, connect

EXIT_OK = 0
EXIT_FAILURES = 1
EXIT_BAD_INPUT = 2

PASSWORD_ENV = "NETCHECK_SWITCH_PASSWORD"
SECRET_ENV = "NETCHECK_ENABLE_SECRET"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="port_verify",
        description=(
            "Verify that unused patch panel runs land on the expected production switch "
            "ports. A test switch port is patched to the far end of each run, and the "
            "production switch's CDP/LLDP advertisement shows where the run lands. Only "
            "the test switch is logged into."
        ),
        epilog=(f"The password is prompted for, or read from ${PASSWORD_ENV}; an enable "
                f"secret (if needed) from ${SECRET_ENV}. Settings not given on the command "
                'line come from the inventory\'s "test_switch" section.'),
    )
    parser.add_argument("inventory", help="JSON file describing the expected interconnect")
    parser.add_argument("--host", help="test switch management IP / hostname")
    parser.add_argument("--username", help="test switch SSH username")
    parser.add_argument("--ports", help="copper test ports to use, e.g. 'Gi1/0/1-23'")
    parser.add_argument("--fiber-ports",
                        help="SFP+ test ports for fiber runs, e.g. 'Te1/1/1-2'")
    parser.add_argument("--fiber-soak", type=float,
                        help="seconds to watch each fiber link for errors (default 60)")
    parser.add_argument("--timeout", type=float,
                        help="seconds to wait for CDP/LLDP per batch (default 90)")
    parser.add_argument("--cable-test", action="store_true", default=None,
                        help="also run a TDR cable test on each run")
    parser.add_argument("--no-clear", action="store_true",
                        help="don't clear the CDP/LLDP tables before each batch")
    parser.add_argument("--allow-switchports", action="store_true",
                        help="allow test ports that aren't routed ports (not recommended)")
    parser.add_argument("--switch", action="append", metavar="NAME",
                        help="only verify runs to this production switch (repeatable)")
    parser.add_argument("--patch-panel", action="append", metavar="NAME",
                        help="only verify runs on this patch panel (repeatable)")
    parser.add_argument("-o", "--output", help="write a report file (.csv or .json)")
    parser.add_argument("--no-colour", "--no-color", action="store_true",
                        help="disable coloured output")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def apply_args(settings: TestSwitchSettings, args) -> None:
    if args.host:
        settings.host = args.host
    if args.username:
        settings.username = args.username
    if args.ports:
        settings.ports = expand_port_range(args.ports)
    if args.fiber_ports:
        settings.fiber_ports = expand_port_range(args.fiber_ports)
    if args.fiber_soak is not None:
        settings.fiber_soak = args.fiber_soak
    if args.timeout is not None:
        settings.cdp_timeout = args.timeout
    if args.cable_test:
        settings.cable_test = True
    if args.no_clear:
        settings.clear_tables = False
    if args.allow_switchports:
        settings.allow_switchports = True


def print_plan(batch, number: int, total: int) -> None:
    print(f"\n=== Batch {number} of {total}: connect these test cables ===")
    rows = [("TEST PORT", "CONNECT TO", "EXPECTED SWITCH PORT")]
    rows += [(a.test_port_short, a.connection.connect_point,
              f"{a.connection.switch} {a.connection.switch_port}".strip()) for a in batch]
    widths = [max(len(r[i]) for r in rows) for i in range(2)]
    for row in rows:
        print(f"  {row[0].ljust(widths[0])}  ->  {row[1].ljust(widths[1])}   ({row[2]})")


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        data = read_json(args.inventory)
        connections = parse_inventory(data, args.inventory)
        settings = TestSwitchSettings.from_inventory(data)
        apply_args(settings, args)
    except (InventoryError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT

    if args.switch:
        wanted = {s.lower() for s in args.switch}
        connections = [c for c in connections if c.switch.lower() in wanted]
    if args.patch_panel:
        wanted = {p.lower() for p in args.patch_panel}
        connections = [c for c in connections if c.patch_panel.lower() in wanted]
    if not settings.ports and not settings.fiber_ports:
        print("error: no test switch ports given (use --ports, e.g. 'Gi1/0/1-23', and/or "
              "--fiber-ports, e.g. 'Te1/1/1-2')", file=sys.stderr)
        return EXIT_BAD_INPUT

    batches, results = plan_batches(connections, settings.ports, settings.fiber_ports)
    runs = sum(len(b) for b in batches)
    if not runs:
        print("No unused runs with a switch port to verify.")
        return EXIT_OK
    print(f"{runs} unused run(s) to verify in {len(batches)} batch(es) using test switch "
          f"{settings.host or '?'}.")

    password = os.environ.get(PASSWORD_ENV) or getpass.getpass(
        f"Password for {settings.username or '?'}@{settings.host or '?'}: ")
    try:
        session = connect(settings, password, os.environ.get(SECRET_ENV, ""))
    except SwitchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT

    try:
        if settings.clear_tables and not session.privileged:
            secret = getpass.getpass("Enable secret: ")
            try:
                session.enable(secret)
            except SwitchError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return EXIT_BAD_INPUT
        errors, warnings = preflight(session, settings)
        for warning in warnings:
            print(f"warning: {warning}")
        if errors:
            print("The test switch isn't ready:", file=sys.stderr)
            for error in errors:
                print(f"  - {error}", file=sys.stderr)
            return EXIT_BAD_INPUT

        for number, batch in enumerate(batches, start=1):
            print_plan(batch, number, len(batches))
            answer = input("Connect the cables above, then press Enter to verify "
                           "(s = skip batch, q = quit): ").strip().lower()
            if answer == "q":
                break
            if answer == "s":
                continue

            def progress(states: dict[str, str], seconds_left: float) -> None:
                pending = sum(not s.startswith("Heard") for s in states.values())
                print(f"\r  {len(states) - pending}/{len(states)} heard, "
                      f"{seconds_left:3.0f}s left ", end="", flush=True)

            batch_results = verify_batch(session, batch, settings, progress=progress)
            print("\n")
            print_table(batch_results, sys.stdout, colour=_colour(args))
            results.extend(batch_results)
            if number < len(batches):
                print("\nDisconnect the test cables before moving on.")
    except SwitchError as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
    finally:
        session.close()

    results.sort(key=lambda r: r.connection.index)
    _finish(results, args)
    return EXIT_FAILURES if any(r.result == FAIL for r in results) else EXIT_OK


def _colour(args) -> bool:
    return not args.no_colour and sys.stdout.isatty()


def _finish(results: list[CheckResult], args) -> None:
    if not results:
        return
    print("\n=== All verified runs ===")
    print_table(results, sys.stdout, colour=_colour(args))
    if args.output:
        write_report(results, args.output, args.inventory)
        print(f"Report written to {args.output}")


if __name__ == "__main__":
    sys.exit(main())
