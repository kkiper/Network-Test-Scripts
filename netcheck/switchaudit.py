"""Switch Port Audit: read the production switches' own tables (read-only).

Logs into each production switch with a read-only account and runs only
user-level show commands: port status, the MAC address table and CDP/LLDP
neighbours. The result shows which port every device is really on, including
devices without an IP address, as long as they send *something*.

Limit: a switch only learns a MAC from frames the device sends. A silent NIC
(e.g. a server with no OS and PXE off) never appears in a MAC table; only its
port's link and speed can be seen, and that is reported.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from .checker import (
    FAIL, MAC_DISCOVERED, MAC_MATCH, MAC_MISMATCH, MAC_NA, MAC_UNRESOLVED, PASS, SKIP, WARN,
    CheckResult, PortCheck,
)
from .cisco import (
    InterfaceStatus, MacEntry, Neighbor, normalize_hostname, normalize_interface,
    parse_cdp_neighbors_detail, parse_interfaces_status, parse_lldp_neighbors_detail,
    parse_mac_address_table, short_interface,
)
from .inventory import STATUS_UNUSED, Connection, InventoryError, format_speed

# Only these are ever sent to a production switch: all user-level (privilege 1).
AUDIT_COMMANDS = (
    "show interfaces status",
    "show mac address-table",
    "show cdp neighbors detail",
    "show lldp neighbors detail",
)
UPLINK_MAC_THRESHOLD = 8  # more MACs than this on a port: treat it as an uplink
UNLISTED = "UNLISTED"

SILENT_NOTE = ("A switch only learns MAC addresses from traffic, so a device that sends "
               "nothing (e.g. a server with no OS) shows only as a link.")


# ------------------------------------------------------------------ settings

@dataclass
class SwitchLogin:
    name: str               # the switch name used in the inventory rows
    host: str = ""
    username: str = ""
    device_type: str = "cisco_xe"
    ssh_port: int = 22

    def to_dict(self) -> dict:
        data: dict[str, Any] = {"host": self.host, "username": self.username}
        if self.device_type != "cisco_xe":
            data["device_type"] = self.device_type
        if self.ssh_port != 22:
            data["ssh_port"] = self.ssh_port
        return data


def logins_from_inventory(data: dict, connections: list[Connection]) -> dict[str, SwitchLogin]:
    """Logins from the inventory's "switches" section, plus every switch the rows name."""
    raw = data.get("switches") if isinstance(data, dict) else None
    if raw is not None and not isinstance(raw, dict):
        raise InventoryError('"switches" must be an object keyed by switch name')
    logins: dict[str, SwitchLogin] = {}
    for name, value in (raw or {}).items():
        if not isinstance(value, dict):
            raise InventoryError(f'"switches": "{name}" must be an object')
        try:
            logins[name] = SwitchLogin(
                name=name, host=str(value.get("host") or "").strip(),
                username=str(value.get("username") or "").strip(),
                device_type=str(value.get("device_type") or "cisco_xe"),
                ssh_port=int(value.get("ssh_port", 22)))
        except (TypeError, ValueError) as exc:
            raise InventoryError(f'"switches": "{name}": {exc}') from exc
    for conn in connections:
        if conn.switch and conn.switch not in logins:
            logins[conn.switch] = SwitchLogin(name=conn.switch)
    return dict(sorted(logins.items()))


# --------------------------------------------------------------- collection

@dataclass
class SwitchState:
    name: str
    statuses: dict[str, InterfaceStatus] = field(default_factory=dict)
    macs: dict[str, list[MacEntry]] = field(default_factory=dict)
    neighbors: dict[str, list[Neighbor]] = field(default_factory=dict)
    uplinks: dict[str, str] = field(default_factory=dict)  # port -> why it's an uplink


def find_uplinks(macs: dict[str, list[MacEntry]],
                 neighbors: dict[str, list[Neighbor]]) -> dict[str, str]:
    """Ports leading to other switches: their MACs belong to devices further away."""
    uplinks = {}
    for port, found in neighbors.items():
        switches = [n for n in found if n.is_switch]
        if switches:
            uplinks[port] = f"uplink to {normalize_hostname(switches[0].device_id) or 'a switch'}"
    for port, entries in macs.items():
        if port not in uplinks and len({e.mac for e in entries}) > UPLINK_MAC_THRESHOLD:
            uplinks[port] = f"{len({e.mac for e in entries})} MACs - probably an uplink"
    return uplinks


def collect(session, name: str) -> SwitchState:
    """Read one switch's tables. Sends only the show commands in AUDIT_COMMANDS."""
    statuses = parse_interfaces_status(session.send(AUDIT_COMMANDS[0]))
    macs = parse_mac_address_table(session.send(AUDIT_COMMANDS[1]))
    neighbors: dict[str, list[Neighbor]] = {}
    for neighbor in (parse_cdp_neighbors_detail(session.send(AUDIT_COMMANDS[2]))
                     + parse_lldp_neighbors_detail(session.send(AUDIT_COMMANDS[3]))):
        neighbors.setdefault(neighbor.local_interface, []).append(neighbor)
    return SwitchState(name=name, statuses=statuses, macs=macs, neighbors=neighbors,
                       uplinks=find_uplinks(macs, neighbors))


# -------------------------------------------------------------------- audit

@dataclass
class Unlisted:
    """A port with link or traffic that no inventory row describes."""

    switch: str
    port: str
    link: str
    macs: list[str]
    neighbor: str = ""

    @property
    def port_short(self) -> str:
        return short_interface(self.port)


def _link_text(status: Optional[InterfaceStatus]) -> str:
    if status is None:
        return "missing"
    if status.link_up:
        return f"up {format_speed(status.speed_mbps)}".strip() if status.speed_mbps else "up"
    return {"notconnect": "down"}.get(status.status, status.status)


def _macs_on(state: SwitchState, port: str) -> list[str]:
    seen: dict[str, None] = {}
    for entry in state.macs.get(port, []):
        seen[entry.mac] = None
    return list(seen)


def _neighbor_text(state: SwitchState, port: str) -> str:
    return ", ".join(sorted({f"{normalize_hostname(n.device_id) or n.device_id} "
                             f"{short_interface(n.remote_port)}".strip()
                             for n in state.neighbors.get(port, [])}))


def audit(connections: list[Connection], states: dict[str, SwitchState],
          seen_macs: Optional[dict[int, str]] = None,
          ) -> tuple[list[CheckResult], list[Unlisted]]:
    """Compare the switches' tables with the inventory rows on those switches.

    ``seen_macs`` (row index -> MAC that answered at the row's IP, from a ping
    test) stands in for a row's missing expected_mac, so the port is checked too.
    """
    where_is: dict[str, tuple[str, str]] = {}  # mac -> (switch, port), uplinks excluded
    for name, state in states.items():
        for port in state.macs:
            if port not in state.uplinks:
                for mac in _macs_on(state, port):
                    where_is.setdefault(mac, (name, port))

    results: list[CheckResult] = []
    listed: dict[str, set[str]] = {name: set() for name in states}
    for conn in connections:
        state = states.get(conn.switch)
        port = normalize_interface(conn.switch_port)
        if state is None or not port:
            continue
        listed[conn.switch].add(port)
        results.append(_audit_row(conn, state, port, where_is,
                                  (seen_macs or {}).get(conn.index)))

    unlisted = []
    for name, state in states.items():
        for port in sorted(set(state.statuses) | set(state.macs), key=_port_key):
            if port in listed[name] or port in state.uplinks:
                continue
            status = state.statuses.get(port)
            macs = _macs_on(state, port)
            if macs or (status is not None and status.link_up):
                unlisted.append(Unlisted(name, port, _link_text(status), macs,
                                         _neighbor_text(state, port)))
    return results, unlisted


def _port_key(port: str) -> list:
    """Sort Gi1/0/7 before Gi1/0/12."""
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", port)]


def _audit_row(conn: Connection, state: SwitchState, port: str,
               where_is: dict[str, tuple[str, str]],
               seen_mac: Optional[str] = None) -> CheckResult:
    status = state.statuses.get(port)
    uplink = state.uplinks.get(port)
    macs = [] if uplink else _macs_on(state, port)
    check = PortCheck(test_port="", link=_link_text(status), seen_switch=state.name,
                      seen_port=short_interface(port), protocol="MAC table",
                      macs=", ".join(macs))
    neighbor = _neighbor_text(state, port)
    up = status is not None and status.link_up
    expected = conn.expected_mac or seen_mac
    label = (f"Expected MAC {expected}" if conn.expected_mac
             else f"MAC {expected} (answering at {conn.ip})")
    discovered: Optional[str] = None
    mac_check = MAC_NA

    if status is None:
        result, message = FAIL, f"Port {short_interface(port)} not found on {state.name}"
    elif conn.status == STATUS_UNUSED:
        if up or macs:
            result = FAIL
            message = f"Should be unused, but something is connected: link {check.link}"
            message += f", MAC(s) {', '.join(macs)}" if macs else ""
            message += f", neighbour {neighbor}" if neighbor else ""
        else:
            result, message = PASS, f"Empty as expected (link {check.link})"
    elif uplink and _expected_neighbor(conn, state, port):
        result = PASS
        message = f"Uplink to {_expected_neighbor(conn, state, port)} as expected"
    elif uplink:
        result, message = WARN, (f"This port is an {uplink}; its MACs belong to devices "
                                 "further away, so the device can't be confirmed here")
    elif expected and expected in macs:
        result, message, discovered, mac_check = (
            PASS, f"{label} seen on this port", expected, MAC_MATCH)
        if len(macs) > 1:
            message += f" (also {', '.join(m for m in macs if m != expected)})"
    elif expected and expected in where_is:
        sw, other = where_is[expected]
        result, mac_check = FAIL, MAC_MISMATCH
        message = f"{label} is on {sw} {short_interface(other)}, not here"
        if macs:
            message += f"; this port has {', '.join(macs)}"
    elif not up:
        result, mac_check = FAIL, MAC_UNRESOLVED
        message = f"No link on {short_interface(port)} ({check.link})"
    elif macs and expected:
        result, mac_check = FAIL, MAC_MISMATCH
        message = f"Different device: {', '.join(macs)} (expected {expected})"
    elif macs:
        discovered = macs[0]
        if len(macs) == 1:
            result, mac_check = PASS, MAC_DISCOVERED
            message = f"Link up; MAC {macs[0]} discovered (no expected MAC to compare)"
        else:
            result, mac_check = WARN, MAC_UNRESOLVED
            message = f"Several MACs on this port: {', '.join(macs)}"
            discovered = None
    else:
        result, mac_check = WARN, MAC_UNRESOLVED
        message = (f"Link up at {format_speed(status.speed_mbps) or 'unknown speed'} but no "
                   "traffic seen, so no MAC address: the device may have no OS or be silent. "
                   + SILENT_NOTE)

    if up and status.speed_mbps and conn.expected_speed and status.speed_mbps < conn.expected_speed:
        message += (f"; link is {format_speed(status.speed_mbps)}, expected "
                    f"{format_speed(conn.expected_speed)}")
        result = FAIL
    if neighbor and conn.status != STATUS_UNUSED and not uplink:
        message += f"; neighbour {neighbor}"
    return CheckResult(conn, result, None, discovered, mac_check, message, port_check=check)


def _expected_neighbor(conn: Connection, state: SwitchState, port: str) -> str:
    """The neighbouring switch on an uplink port, if the row's device names it."""
    for neighbor in state.neighbors.get(port, []):
        name = normalize_hostname(neighbor.device_id)
        if neighbor.is_switch and name and name in conn.device.lower():
            return name
    return ""


# ------------------------------------------------------- with a ping test

SEVERITY = {SKIP: 0, PASS: 1, WARN: 2, FAIL: 3}


def merge(ping_result: CheckResult, port_result: Optional[CheckResult]) -> CheckResult:
    """One row's ping test and switch port result as a single result (the worse one wins)."""
    if port_result is None:
        return ping_result
    if ping_result.result == SKIP:
        return port_result  # unused, or no IP: the switch is the only check
    pc = port_result.port_check
    where = f"{pc.seen_switch} {pc.seen_port}" if pc else "switch"
    result = max(ping_result.result, port_result.result, key=SEVERITY.__getitem__)
    return CheckResult(ping_result.connection, result, ping_result.ping,
                       ping_result.discovered_mac or port_result.discovered_mac,
                       ping_result.mac_check,
                       f"Ping: {ping_result.message}. Switch {where}: {port_result.message}",
                       port_check=pc)


def combine(ping_results: list[CheckResult], connections: list[Connection],
            states: dict[str, SwitchState], arp: Optional[dict[str, str]] = None,
            ) -> tuple[list[CheckResult], list[Unlisted]]:
    """Add the switch port check to each ping test result.

    ``connections`` is the whole inventory, so rows that weren't pinged (filters)
    aren't reported as unlisted ports. ``arp`` (ip -> mac, this computer's ARP
    table) gives an IP for rows that have none when their port's MAC is in it.
    """
    seen = {r.connection.index: r.discovered_mac for r in ping_results
            if r.discovered_mac and r.connection.ip}
    port_results, unlisted = audit(connections, states, seen)
    by_index = {r.connection.index: r for r in port_results}
    ip_of_mac: dict[str, str] = {}
    for ip, mac in sorted((arp or {}).items()):
        ip_of_mac.setdefault(mac, ip)
    merged = []
    for res in ping_results:
        port = by_index.get(res.connection.index)
        if (port is not None and not res.connection.ip and port.discovered_mac
                and port.discovered_mac in ip_of_mac):
            port.message += (f"; it answers at {ip_of_mac[port.discovered_mac]} - "
                             "add this IP to the inventory")
        merged.append(merge(res, port))
    return merged, unlisted


def new_entry(item: Unlisted) -> dict:
    """An inventory row for an unlisted port."""
    entry: dict[str, Any] = {"switch": item.switch, "switch_port": item.port_short}
    if len(item.macs) == 1:
        entry["expected_mac"] = item.macs[0]
    entry["status"] = "connected"
    entry["notes"] = (f"Found by Switch Port Audit on {datetime.now():%Y-%m-%d}"
                      + (f" (MACs: {', '.join(item.macs)})" if len(item.macs) > 1 else "")
                      + (f" (neighbour {item.neighbor})" if item.neighbor else ""))
    return entry
