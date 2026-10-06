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
    DOM_LABELS, DOM_UNITS, NO_TRANSCEIVER, CableTest, DomReading, InterfaceStatus, Neighbor,
    expand_port_range, normalize_hostname, parse_cdp_neighbors_detail, parse_counters_errors,
    parse_interfaces_status, parse_lldp_neighbors_detail, parse_tdr, parse_transceiver_detail,
    same_interface, short_interface,
)
from .inventory import (
    MEDIA_FIBER, STATUS_UNUSED, Connection, InventoryError, format_speed,
)

MIN_EXPECTED_SPEED_MBPS = 1000
LINK_GRACE_S = 15      # stop waiting for a port whose link stays down this long
POLL_INTERVAL_S = 5
TDR_WAIT_S = 8
DEFAULT_FIBER_SOAK_S = 60  # how long a fiber link is watched for errors


@dataclass
class TestSwitchSettings:
    host: str = ""
    username: str = ""
    ports: list[str] = field(default_factory=list)  # copper test ports (canonical names)
    fiber_ports: list[str] = field(default_factory=list)  # SFP+ test ports for fiber runs
    fiber_soak: float = DEFAULT_FIBER_SOAK_S
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
            settings.ports = _port_list(raw.get("ports"))
            settings.fiber_ports = _port_list(raw.get("fiber_ports"))
            settings.fiber_soak = float(raw.get("fiber_soak", DEFAULT_FIBER_SOAK_S))
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
            **({"fiber_ports": [short_interface(p) for p in self.fiber_ports]}
               if self.fiber_ports else {}),
            **({"fiber_soak": self.fiber_soak}
               if self.fiber_soak != DEFAULT_FIBER_SOAK_S else {}),
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


def _port_list(value) -> list[str]:
    if not value:
        return []
    return expand_port_range(value if isinstance(value, str) else ",".join(str(p) for p in value))


@dataclass
class Assignment:
    """One test switch port patched to one unused run."""

    test_port: str  # canonical
    connection: Connection

    @property
    def test_port_short(self) -> str:
        return short_interface(self.test_port)

    @property
    def fiber(self) -> bool:
        return self.connection.media == MEDIA_FIBER


def plan_batches(
    connections: list[Connection], ports: list[str], fiber_ports: list[str] = ()
) -> tuple[list[list[Assignment]], list[CheckResult]]:
    """Split the unused connections into batches, one test port per run.

    Copper runs use ``ports`` and fiber runs use ``fiber_ports``; each batch
    holds up to one run per test port of each kind. Returns (batches, results
    for unused connections that can't be verified).
    """
    if not ports and not fiber_ports:
        raise ValueError("No test switch ports configured")
    copper, fiber, cannot = [], [], []
    for conn in connections:
        if conn.status != STATUS_UNUSED:
            continue
        is_fiber = conn.media == MEDIA_FIBER
        if not conn.switch_port:
            reason = "No switch port in the inventory - nothing to verify"
        elif is_fiber and not fiber_ports:
            reason = "Fiber run - no fiber (SFP+) test ports configured"
        elif not is_fiber and not ports:
            reason = "Copper run - no copper test ports configured"
        else:
            (fiber if is_fiber else copper).append(conn)
            continue
        cannot.append(CheckResult(conn, SKIP, None, None, MAC_NA, reason))

    def chunks(conns, test_ports):
        return [[Assignment(p, c) for p, c in zip(test_ports, conns[i:i + len(test_ports)])]
                for i in range(0, len(conns), len(test_ports))] if test_ports else []

    copper_batches, fiber_batches = chunks(copper, ports), chunks(fiber, fiber_ports)
    count = max(len(copper_batches), len(fiber_batches))
    batches = [(copper_batches[i] if i < len(copper_batches) else [])
               + (fiber_batches[i] if i < len(fiber_batches) else []) for i in range(count)]
    return batches, cannot


def preflight(session, settings: TestSwitchSettings) -> tuple[list[str], list[str]]:
    """Check the test switch is safe and ready. Returns (errors, warnings)."""
    errors: list[str] = []
    warnings: list[str] = []
    if not settings.ports and not settings.fiber_ports:
        errors.append("No test ports configured.")
    for port in set(settings.ports) & set(settings.fiber_ports):
        errors.append(f"{short_interface(port)} is listed as both a copper and a fiber test port.")

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
    for port in settings.ports + settings.fiber_ports:
        name = short_interface(port)
        status = statuses.get(port)
        fiber = port in settings.fiber_ports
        if status is None:
            errors.append(f"Test port {name} doesn't exist on the test switch.")
        elif fiber and status.status in NO_TRANSCEIVER:
            errors.append(f"Fiber test port {name} has no SFP+ module fitted.")
        elif status.status == "disabled":
            errors.append(f"Test port {name} is shut down - configure 'no shutdown'.")
        elif status.status == "err-disabled":
            errors.append(
                f"Test port {name} is err-disabled - 'shutdown' then 'no shutdown' it."
                + (" If it has a non-Cisco SFP+ module, the switch may have rejected it: see "
                   "'service unsupported-transceiver' in docs/test_switch_c9200.cfg."
                   if fiber else ""))
        elif not status.routed and not settings.allow_switchports:
            errors.append(
                f"Test port {name} is a switchport. Configure it with 'no switchport' so it "
                "can't bridge production ports together or trip BPDU guard "
                "(see docs/test_switch_c9200.cfg).")
        elif fiber and status.media_type and "10G" not in status.media_type.upper():
            warnings.append(f"Fiber test port {name} reports its module as "
                            f"'{status.media_type}', not a 10G SFP+.")

    if settings.fiber_ports and not errors:
        dom = parse_transceiver_detail(session.send("show interfaces transceiver detail"))
        missing = [short_interface(p) for p in settings.fiber_ports if p not in dom]
        if missing:
            warnings.append(
                f"No light-level (DOM) readings from the module in {', '.join(missing)}; "
                "optical levels can't be checked on those ports.")
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
        return f"up {format_speed(speed)}" if speed else "up"
    return {"notconnect": "down"}.get(status.status, status.status)


def _fmt(value: float, unit: str) -> str:
    return f"{value:g} {unit}"


def assess_optics(readings: Optional[dict[str, DomReading]]) -> tuple[str, str, list[str]]:
    """Grade a transceiver's light levels against its own thresholds.

    Returns (level 'ok'/'warn'/'alarm'/'unknown', short summary, problems).
    """
    if not readings:
        return "unknown", "no light-level readings", []
    problems: list[str] = []
    level = "ok"
    for name, reading in readings.items():
        grade = reading.level()
        if grade == "ok":
            continue
        if grade == "alarm" or level == "ok":
            level = grade
        label, unit = DOM_LABELS[name], DOM_UNITS[name]
        if reading.value is None:
            problems.append(f"{label}: none" if name != "rx_power" else "no light received")
            continue
        for limit, word in ((reading.low_alarm, "low alarm"), (reading.low_warn, "low warning"),
                            (reading.high_alarm, "high alarm"), (reading.high_warn, "high warning")):
            if limit is None:
                continue
            if (word.startswith("low") and reading.value < limit
                    or word.startswith("high") and reading.value > limit):
                problems.append(f"{label} {_fmt(reading.value, unit)} is "
                                f"{'below' if word.startswith('low') else 'above'} the {word} "
                                f"limit ({_fmt(limit, unit)})")
                break
    parts = []
    rx = readings.get("rx_power")
    if rx is not None and rx.value is not None:
        floor = rx.low_warn if rx.low_warn is not None else rx.low_alarm
        margin = f", {rx.value - floor:.1f} dB margin" if floor is not None else ""
        parts.append(f"Rx {rx.value:g} dBm{margin}")
    elif rx is not None:
        parts.append("Rx no light")
    tx = readings.get("tx_power")
    if tx is not None and tx.value is not None:
        parts.append(f"Tx {tx.value:g} dBm")
    return level, "; ".join(parts) or "readings unavailable", problems


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
    optics: Optional[dict[str, DomReading]] = None,
    link_errors: Optional[int] = None,
    soak: float = 0,
) -> CheckResult:
    conn = assignment.connection
    check = PortCheck(test_port=assignment.test_port_short, link=_link_text(status))
    if cable is not None:
        check.cable_test = cable.summary()
    optics_level, optics_summary, optics_problems = assess_optics(optics)
    if assignment.fiber:
        check.optics = optics_summary
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
    elif not status.link_up and assignment.fiber:
        rx = (optics or {}).get("rx_power")
        if optics and (rx is None or rx.value is None or rx.level() == "alarm"):
            result, message = FAIL, (
                "No link and no light received from the production switch - the fiber is "
                "probably reversed: swap the P and S strands at one end. Otherwise check the "
                "connectors are clean and fully seated and the fiber isn't broken")
        elif optics:
            result, message = FAIL, (
                f"Light received (Rx {rx.value:g} dBm) but no link - the strand from the test "
                "switch to the production switch may be broken or dirty, or the two modules "
                "don't match (both must be 10GBASE-SR)")
        else:
            result, message = FAIL, (
                f"No link - check the fiber at {conn.connect_point}: the P and S strands may be "
                "reversed (swap them at one end), or a connector is dirty or not seated")
    elif not status.link_up:
        result, message = FAIL, (
            f"No link - check the test cable at {conn.connect_point}; the production port "
            "may be shut down, or the run is faulty")
    else:
        result, message = WARN, (
            f"Link up but no CDP/LLDP heard within {timeout:g}s - CDP may be disabled on the "
            "production switch; port not verified")

    speed = status.speed_mbps if status is not None and status.link_up else None
    if speed and conn.expected_speed and speed < conn.expected_speed:
        message += (f"; link is {format_speed(speed)}, expected "
                    f"{format_speed(conn.expected_speed)}")
        result = FAIL
    elif speed and not conn.expected_speed and speed < MIN_EXPECTED_SPEED_MBPS:
        message += f"; link only {format_speed(speed)} - possible cable fault"
        result = WARN if result == PASS else result
    elif speed and result == PASS and (assignment.fiber or conn.expected_speed):
        message += f" at {format_speed(speed)}"

    if assignment.fiber and status is not None and status.link_up:
        if optics_level == "unknown":
            message += "; light levels not available from the module (not checked)"
            result = WARN if result == PASS else result
        elif optics_problems:
            message += "; " + "; ".join(optics_problems)
            if optics_level == "alarm":
                result = FAIL
            elif result == PASS:
                result = WARN
        else:
            message += f"; light levels OK ({optics_summary})"
        if link_errors is not None:
            check.errors = f"{link_errors} in {soak:g}s"
            if link_errors:
                message += f"; {link_errors} receive error(s) during the {soak:g}s check"
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

    fiber = [a for a in batch if a.fiber]
    soak = settings.fiber_soak if fiber else 0
    start = clock()
    # Fiber links are also watched for errors for ``soak`` seconds once heard.
    deadline = start + settings.cdp_timeout + soak
    down_since: dict[str, float] = {}
    up_since: dict[str, float] = {}
    first_errors: dict[str, int] = {}
    last_errors: dict[str, int] = {}
    while True:
        now = clock()
        statuses = parse_interfaces_status(session.send("show interfaces status"))
        neighbors = _neighbors(session)
        if fiber:
            counters = parse_counters_errors(session.send("show interfaces counters errors"))
        states: dict[str, str] = {}
        waiting = False
        for a in batch:
            port = a.test_port
            status = statuses.get(port)
            states[port] = interim_state(status, neighbors.get(port, []))
            if status is not None and status.link_up:
                up_since.setdefault(port, now)
                if a.fiber and port in counters:
                    first_errors.setdefault(port, counters[port])
                    last_errors[port] = counters[port]
            else:
                up_since.pop(port, None)
            if neighbors.get(port):
                left = soak - (now - up_since[port]) if a.fiber and port in up_since else 0
                if left > 0:
                    waiting = True
                    states[port] += f" - watching for errors ({left:.0f}s)"
                continue
            if status is not None and not status.link_up:
                down_since.setdefault(port, now)
                if now - down_since[port] < LINK_GRACE_S:
                    waiting = True
            else:
                down_since.pop(port, None)
                waiting = True
        if progress:
            progress(states, max(0.0, deadline - now))
        if not waiting or now >= deadline or (stop_event is not None and stop_event.is_set()):
            break
        sleep(POLL_INTERVAL_S)

    stopped = stop_event is not None and stop_event.is_set()
    optics: dict = {}
    if fiber:
        optics = parse_transceiver_detail(session.send("show interfaces transceiver detail"))

    cables: dict[str, CableTest] = {}
    copper = [a.test_port for a in batch if not a.fiber]
    if settings.cable_test and copper and not stopped:
        if progress:
            progress({a.test_port: "Running cable test..." for a in batch if not a.fiber}, 0)
        cables = run_cable_tests(session, copper, sleep)

    results = []
    for a in batch:
        port = a.test_port
        errors = (last_errors[port] - first_errors[port]) if port in last_errors else None
        watched = (clock() - up_since[port]) if port in up_since else 0
        results.append(evaluate_port(
            a, statuses.get(port), neighbors.get(port, []), cables.get(port),
            settings.cdp_timeout, optics=optics.get(port) if a.fiber else None,
            link_errors=errors if a.fiber else None, soak=min(watched, soak) if a.fiber else 0))
    return results
