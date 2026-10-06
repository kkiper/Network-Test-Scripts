"""Verify unused patch panel runs using a test switch and CDP/LLDP.

A test switch port is patched into the far end of each unused run. When the
link comes up, the production switch advertises itself over CDP (or LLDP), so
the test switch learns exactly which production switch and port the run lands
on. Only the test switch is accessed; production switches are not touched.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .checker import FAIL, MAC_NA, PASS, SKIP, WARN, CheckResult, PortCheck
from .cisco import (
    CableTest, InterfaceStatus, Neighbor, expand_port_range, normalize_hostname,
    parse_cdp_neighbors_detail, parse_interfaces_status,
    parse_lldp_neighbors_detail, parse_tdr, same_interface, short_interface,
)
from .inventory import STATUS_UNUSED, Connection, InventoryError

MIN_EXPECTED_SPEED_MBPS = 1000
LINK_GRACE_S = 15      # stop waiting for a port whose link stays down this long
POLL_INTERVAL_S = 5
TDR_WAIT_S = 8


@dataclass
class TestSwitchSettings:
    host: str = ""
    username: str = ""
    ports: list[str] = field(default_factory=list)  # canonical interface names
    device_type: str = "cisco_xe"
    ssh_port: int = 22
    cdp_timeout: float = 90
    clear_tables: bool = True
    cable_test: bool = False
    allow_switchports: bool = False

    @classmethod
    def from_inventory(cls, data: dict) -> "TestSwitchSettings":
        """Read the optional "test_switch" section of an inventory document."""
        raw = data.get("test_switch") if isinstance(data, dict) else None
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise InventoryError('"test_switch" must be an object')
        settings = cls()
        try:
            settings.host = str(raw.get("host") or "").strip()
            settings.username = str(raw.get("username") or "").strip()
            ports = raw.get("ports") or []
            settings.ports = expand_port_range(ports if isinstance(ports, str)
                                               else ",".join(str(p) for p in ports))
            settings.device_type = str(raw.get("device_type") or "cisco_xe")
            settings.ssh_port = int(raw.get("ssh_port", 22))
            settings.cdp_timeout = float(raw.get("cdp_timeout", 90))
            settings.clear_tables = bool(raw.get("clear_tables", True))
            settings.cable_test = bool(raw.get("cable_test", False))
            settings.allow_switchports = bool(raw.get("allow_switchports", False))
        except (TypeError, ValueError) as exc:
            raise InventoryError(f'"test_switch": {exc}') from exc
        return settings

    def to_dict(self) -> dict:
        """Settings to store in the inventory (never includes passwords)."""
        data: dict[str, Any] = {
            "host": self.host,
            "username": self.username,
            "ports": [short_interface(p) for p in self.ports],
            "cdp_timeout": self.cdp_timeout,
            "cable_test": self.cable_test,
        }
        if not self.clear_tables:
            data["clear_tables"] = False
        if self.allow_switchports:
            data["allow_switchports"] = True
        if self.device_type != "cisco_xe":
            data["device_type"] = self.device_type
        if self.ssh_port != 22:
            data["ssh_port"] = self.ssh_port
        return data


@dataclass
class Assignment:
    """One test switch port patched to one unused run."""

    test_port: str  # canonical
    connection: Connection

    @property
    def test_port_short(self) -> str:
        return short_interface(self.test_port)


def plan_batches(
    connections: list[Connection], ports: list[str]
) -> tuple[list[list[Assignment]], list[CheckResult]]:
    """Split the unused connections into batches, one test port per run.

    Returns (batches, results for unused connections that can't be verified).
    """
    if not ports:
        raise ValueError("No test switch ports configured")
    todo, cannot = [], []
    for conn in connections:
        if conn.status != STATUS_UNUSED:
            continue
        if conn.switch_port:
            todo.append(conn)
        else:
            cannot.append(CheckResult(conn, SKIP, None, None, MAC_NA,
                                      "No switch port in the inventory - nothing to verify"))
    batches = [
        [Assignment(port, conn) for port, conn in zip(ports, todo[i:i + len(ports)])]
        for i in range(0, len(todo), len(ports))
    ]
    return batches, cannot


def preflight(session, settings: TestSwitchSettings) -> tuple[list[str], list[str]]:
    """Check the test switch is safe and ready. Returns (errors, warnings)."""
    errors: list[str] = []
    warnings: list[str] = []
    if not settings.ports:
        errors.append("No test ports configured.")

    if "not enabled" in session.send("show cdp neighbors").lower():
        if "not enabled" in session.send("show lldp neighbors").lower():
            errors.append("Neither CDP nor LLDP is enabled on the test switch "
                          "(configure 'cdp run').")
        else:
            warnings.append("CDP is disabled on the test switch; only LLDP will be used, "
                            "which Cisco switches don't send by default.")
    if settings.clear_tables and not session.privileged:
        errors.append("Privileged (enable) access is needed to clear the CDP/LLDP tables "
                      "between batches. Enter the enable secret, or turn off table clearing.")

    statuses = parse_interfaces_status(session.send("show interfaces status"))
    for port in settings.ports:
        name = short_interface(port)
        status = statuses.get(port)
        if status is None:
            errors.append(f"Test port {name} doesn't exist on the test switch.")
        elif status.status == "disabled":
            errors.append(f"Test port {name} is shut down - configure 'no shutdown'.")
        elif status.status == "err-disabled":
            errors.append(f"Test port {name} is err-disabled - 'shutdown' then 'no shutdown' it.")
        elif not status.routed and not settings.allow_switchports:
            errors.append(
                f"Test port {name} is a switchport. Configure it with 'no switchport' so it "
                "can't bridge production ports together or trip BPDU guard "
                "(see docs/test_switch_c9200.cfg).")
    return errors, warnings


def _neighbors(session) -> dict[str, list[Neighbor]]:
    found: dict[str, list[Neighbor]] = {}
    for neighbor in (parse_cdp_neighbors_detail(session.send("show cdp neighbors detail"))
                     + parse_lldp_neighbors_detail(session.send("show lldp neighbors detail"))):
        found.setdefault(neighbor.local_interface, []).append(neighbor)
    return found


def _link_text(status: Optional[InterfaceStatus]) -> str:
    if status is None:
        return "missing"
    if status.link_up:
        speed = status.speed_mbps
        return f"up {speed} Mb/s" if speed else "up"
    return {"notconnect": "down"}.get(status.status, status.status)


def interim_state(status: Optional[InterfaceStatus], neighbors: list[Neighbor]) -> str:
    if neighbors:
        n = neighbors[0]
        return f"Heard {normalize_hostname(n.device_id) or n.device_id} {short_interface(n.remote_port)}"
    if status is not None and status.link_up:
        return "Link up - waiting for CDP/LLDP..."
    return "Waiting for link..."


def run_cable_tests(session, ports: list[str], sleep: Callable[[float], None]) -> dict[str, CableTest]:
    """Run TDR on each port (briefly drops the link) and return the results."""
    for port in ports:
        session.run(f"test cable-diagnostics tdr interface {short_interface(port)}")
    sleep(TDR_WAIT_S)
    results: dict[str, CableTest] = {}
    for port in ports:
        cmd = f"show cable-diagnostics tdr interface {short_interface(port)}"
        output = session.send(cmd)
        if "in progress" in output.lower():
            sleep(TDR_WAIT_S)
            output = session.send(cmd)
        results.update(parse_tdr(output))
    return results


def evaluate_port(
    assignment: Assignment,
    status: Optional[InterfaceStatus],
    neighbors: list[Neighbor],
    cable: Optional[CableTest] = None,
    timeout: float = 0,
) -> CheckResult:
    conn = assignment.connection
    check = PortCheck(test_port=assignment.test_port_short, link=_link_text(status))
    if cable is not None:
        check.cable_test = cable.summary()
    expected = f"{conn.switch} {conn.switch_port}".strip()

    def host_ok(n: Neighbor) -> bool:
        return not conn.switch or normalize_hostname(n.device_id) == normalize_hostname(conn.switch)

    if neighbors:
        matching = [n for n in neighbors
                    if host_ok(n) and same_interface(n.remote_port, conn.switch_port)]
        seen = matching[0] if matching else neighbors[0]
        check.seen_switch = normalize_hostname(seen.device_id) or seen.device_id
        check.seen_port = short_interface(seen.remote_port)
        check.protocol = seen.protocol
        where = f"{check.seen_switch} {check.seen_port}"
        if matching:
            result, message = PASS, f"Verified via {seen.protocol}: patched to {where}"
        else:
            result, message = FAIL, f"Patched to {where}, expected {expected}"
        others = {(normalize_hostname(n.device_id), short_interface(n.remote_port))
                  for n in neighbors} - {(check.seen_switch, check.seen_port)}
        if others:
            result = FAIL
            message += "; also heard " + ", ".join(f"{h} {p}" for h, p in sorted(others))
    elif status is None:
        result, message = FAIL, f"Test port {check.test_port} not found on the test switch"
    elif status.status == "err-disabled":
        result, message = FAIL, f"Test port {check.test_port} went err-disabled"
    elif not status.link_up:
        result, message = FAIL, (
            f"No link - check the test cable at {conn.connect_point}; the production port "
            "may be shut down, or the run is faulty")
    else:
        result, message = WARN, (
            f"Link up but no CDP/LLDP heard within {timeout:g}s - CDP may be disabled on the "
            "production switch; port not verified")

    if status is not None and status.link_up and status.speed_mbps \
            and status.speed_mbps < MIN_EXPECTED_SPEED_MBPS:
        message += f"; link only {status.speed_mbps} Mb/s - possible cable fault"
        result = WARN if result == PASS else result
    if cable is not None and cable.pairs and not cable.ok:
        message += f"; cable test: {check.cable_test}"
        result = WARN if result == PASS else result
    return CheckResult(conn, result, None, None, MAC_NA, message, port_check=check)


def verify_batch(
    session,
    batch: list[Assignment],
    settings: TestSwitchSettings,
    progress: Optional[Callable[[dict[str, str], float], None]] = None,
    stop_event: Optional[threading.Event] = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> list[CheckResult]:
    """Verify one cabled-up batch. ``progress(states, seconds_left)`` reports interim state."""
    if settings.clear_tables:
        # Forget neighbours from the previous batch so nothing stale is matched.
        session.run("clear cdp table")
        session.run("clear lldp table")

    start = clock()
    deadline = start + settings.cdp_timeout
    down_since: dict[str, float] = {}
    while True:
        now = clock()
        statuses = parse_interfaces_status(session.send("show interfaces status"))
        neighbors = _neighbors(session)
        waiting = False
        for a in batch:
            status = statuses.get(a.test_port)
            if neighbors.get(a.test_port):
                continue
            if status is not None and not status.link_up:
                down_since.setdefault(a.test_port, now)
                if now - down_since[a.test_port] < LINK_GRACE_S:
                    waiting = True
            else:
                down_since.pop(a.test_port, None)
                waiting = True
        if progress:
            progress({a.test_port: interim_state(statuses.get(a.test_port),
                                                 neighbors.get(a.test_port, []))
                      for a in batch}, max(0.0, deadline - now))
        if not waiting or now >= deadline or (stop_event is not None and stop_event.is_set()):
            break
        sleep(POLL_INTERVAL_S)

    cables: dict[str, CableTest] = {}
    if settings.cable_test and not (stop_event is not None and stop_event.is_set()):
        if progress:
            progress({a.test_port: "Running cable test..." for a in batch}, 0)
        cables = run_cable_tests(session, [a.test_port for a in batch], sleep)

    return [evaluate_port(a, statuses.get(a.test_port), neighbors.get(a.test_port, []),
                          cables.get(a.test_port), settings.cdp_timeout)
            for a in batch]
