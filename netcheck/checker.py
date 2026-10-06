"""Run the live checks for each expected connection and grade the results."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from dataclasses import dataclass
from typing import Callable, Optional

from .inventory import STATUS_UNUSED, Connection
from .mac import lookup_mac
from .ping import PingResult, ping

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"
SKIP = "SKIP"

MAC_MATCH = "MATCH"
MAC_MISMATCH = "MISMATCH"
MAC_DISCOVERED = "DISCOVERED"  # found, but no expected MAC to compare against
MAC_UNRESOLVED = "UNRESOLVED"
MAC_NA = "N/A"


@dataclass
class PortCheck:
    """What the test switch saw on the port patched to an unused run."""

    test_port: str
    link: str = ""
    seen_switch: str = ""
    seen_port: str = ""
    protocol: str = ""
    cable_test: str = ""
    optics: str = ""   # fiber: transceiver light levels
    errors: str = ""   # fiber: receive errors seen while the link was watched


@dataclass
class CheckResult:
    connection: Connection
    result: str
    ping: Optional[PingResult]
    discovered_mac: Optional[str]
    mac_check: str
    message: str
    port_check: Optional[PortCheck] = None


def evaluate(
    conn: Connection, ping_result: Optional[PingResult], discovered_mac: Optional[str]
) -> CheckResult:
    """Grade one connection from its ping result and the MAC found for its IP."""
    if conn.status == STATUS_UNUSED:
        return CheckResult(
            conn, SKIP, None, None, MAC_NA,
            "Unused port - nothing to ping (verify it with the test switch)",
        )
    if not conn.ip:
        return CheckResult(conn, SKIP, None, None, MAC_NA, "No IP address defined - cannot ping")

    assert ping_result is not None
    if discovered_mac is None:
        mac_check = MAC_UNRESOLVED
    elif conn.expected_mac is None:
        mac_check = MAC_DISCOVERED
    elif discovered_mac == conn.expected_mac:
        mac_check = MAC_MATCH
    else:
        mac_check = MAC_MISMATCH

    if mac_check == MAC_MISMATCH:
        # A wrong MAC is a failure whether or not ping answered: some other
        # device is using this IP.
        return CheckResult(
            conn, FAIL, ping_result, discovered_mac, mac_check,
            f"MAC mismatch: expected {conn.expected_mac}, found {discovered_mac}",
        )

    if not ping_result.reachable:
        reason = ping_result.error or "no ping reply"
        if mac_check == MAC_MATCH:
            # Answered ARP but not ICMP: the device is there, probably firewalled.
            return CheckResult(
                conn, WARN, ping_result, discovered_mac, mac_check,
                f"{reason}, but expected MAC answered ARP (ICMP likely blocked)",
            )
        if mac_check == MAC_DISCOVERED:
            return CheckResult(
                conn, WARN, ping_result, discovered_mac, mac_check,
                f"{reason}, but a device answered ARP with {discovered_mac} (ICMP likely blocked)",
            )
        return CheckResult(conn, FAIL, ping_result, discovered_mac, mac_check, f"Unreachable: {reason}")

    if mac_check == MAC_MATCH:
        return CheckResult(conn, PASS, ping_result, discovered_mac, mac_check, "Reachable, MAC matches")
    if mac_check == MAC_DISCOVERED:
        return CheckResult(
            conn, PASS, ping_result, discovered_mac, mac_check,
            "Reachable, MAC discovered (no expected MAC to compare)",
        )
    return CheckResult(
        conn, WARN, ping_result, discovered_mac, mac_check,
        "Reachable, but MAC not in ARP table (device may be on another subnet "
        "or this is a local interface)",
    )


def check_connection(
    conn: Connection,
    count: int = 2,
    timeout_s: float = 1.0,
    ping_fn: Callable[..., PingResult] = ping,
    mac_fn: Callable[[str], Optional[str]] = lookup_mac,
) -> CheckResult:
    if conn.status == STATUS_UNUSED or not conn.ip:
        return evaluate(conn, None, None)
    ping_result = ping_fn(conn.ip, count=count, timeout_s=timeout_s)
    # The ping (successful or not) triggers ARP, so the neighbour table is
    # populated now if the host is on our segment.
    return evaluate(conn, ping_result, mac_fn(conn.ip))


def check_all(
    connections: list[Connection],
    count: int = 2,
    timeout_s: float = 1.0,
    workers: int = 16,
    ping_fn: Callable[..., PingResult] = ping,
    mac_fn: Callable[[str], Optional[str]] = lookup_mac,
    progress: Optional[Callable[[CheckResult], None]] = None,
    stop_event: Optional[threading.Event] = None,
    iface=None,
) -> list[CheckResult]:
    """Check every connection in parallel; results keep the inventory order.

    Setting ``stop_event`` makes connections that haven't started yet return
    a SKIP "Cancelled" result; checks already in progress finish normally.
    With ``iface`` (a netif.Interface) pings and MAC lookups only use that
    interface.
    """
    if iface is not None:
        if ping_fn is ping:
            ping_fn = partial(ping, iface=iface)
        if mac_fn is lookup_mac:
            mac_fn = partial(lookup_mac, iface=iface)

    def run(conn: Connection) -> CheckResult:
        if stop_event is not None and stop_event.is_set():
            res = CheckResult(conn, SKIP, None, None, MAC_NA, "Cancelled")
        else:
            res = check_connection(conn, count, timeout_s, ping_fn, mac_fn)
        if progress:
            progress(res)
        return res

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return list(pool.map(run, connections))
