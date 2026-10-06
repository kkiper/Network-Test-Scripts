"""Discover devices on the local network and compare them with the inventory.

Pings every address in the chosen IPv4 subnets, then reads this computer's
ARP table. Devices that answered are compared with the expected interconnect:
unknown devices, devices that moved to a new IP and MAC conflicts are flagged.
Only devices on the same layer-2 segment (subnet/VLAN) as this computer can be
seen this way.
"""

from __future__ import annotations

import csv
import ipaddress
import json
import os
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from typing import Callable, Iterable, Optional

from .inventory import STATUS_UNUSED, Connection
from .mac import is_locally_administered, normalize_mac, read_arp_table
from .ping import PingResult, ping

EXPECTED = "EXPECTED"
MAC_CONFLICT = "MAC CONFLICT"
MOVED = "MOVED"
UNKNOWN = "UNKNOWN"
NOT_FOUND = "NOT FOUND"
CATEGORIES = (UNKNOWN, MOVED, MAC_CONFLICT, NOT_FOUND, EXPECTED)
UNEXPECTED = (UNKNOWN, MOVED, MAC_CONFLICT)

DEFAULT_MAX_HOSTS = 1024
DEFAULT_OUI_PATH = os.path.join(os.path.dirname(__file__), "data", "oui.csv")

REPORT_COLUMNS = ("category", "ip", "mac", "vendor", "reply", "inventory", "message")


@dataclass
class Found:
    ip: str
    mac: str
    replied: bool
    category: str
    message: str
    index: Optional[int] = None   # inventory row (1-based), if any
    inventory: str = ""           # e.g. "PP-A:1 Firewall-01"
    vendor: str = ""

    @property
    def reply(self) -> str:
        if self.replied:
            return "ping"
        return "ARP only" if self.mac else "none"


# ------------------------------------------------------------------- subnets

def is_netmask(text: str) -> bool:
    """True for a dotted subnet mask such as 255.255.255.0."""
    try:
        value = int(ipaddress.IPv4Address(text))
    except ValueError:
        return False
    inverted = ~value & 0xFFFFFFFF
    # A mask is a run of 1s followed by 0s, and starts with 255.
    return value >> 24 == 0xFF and inverted & (inverted + 1) == 0


def parse_subnets(text: str, max_hosts: int = DEFAULT_MAX_HOSTS) -> list[ipaddress.IPv4Network]:
    """Parse subnets into networks. Raises ValueError, with a hint, if invalid.

    Accepts CIDR ('192.168.1.0/24'), an address in the subnet ('192.168.1.20/24'),
    'address mask' pairs ('192.168.1.0 255.255.255.0' or '192.168.1.0/255.255.255.0'),
    and single addresses ('10.0.5.10').
    """
    networks: list[ipaddress.IPv4Network] = []
    parts = text.replace(",", " ").split()
    i = 0
    while i < len(parts):
        part = parts[i]
        # "192.168.1.0 255.255.255.0": an address followed by its mask.
        if "/" not in part and i + 1 < len(parts) and is_netmask(parts[i + 1]) \
                and not is_netmask(part):
            part = f"{part}/{parts[i + 1]}"
            i += 1
        i += 1
        if is_netmask(part.split("/")[0]):
            raise ValueError(
                f"'{part}' is a subnet mask, not a subnet. Enter the network your devices "
                "are on, e.g. 192.168.1.0/24 for addresses 192.168.1.x with mask "
                "255.255.255.0 (or type '192.168.1.0 255.255.255.0').")
        try:
            network = ipaddress.ip_network(part, strict=False)
        except ValueError as exc:
            raise ValueError(f"'{part}' is not a subnet (use e.g. 192.168.1.0/24)") from exc
        if network.version != 4:
            raise ValueError(f"'{part}': only IPv4 subnets can be swept")
        if network.network_address.is_unspecified or network.network_address.is_multicast:
            raise ValueError(f"'{part}' isn't a network devices can be on "
                             "(use e.g. 192.168.1.0/24)")
        if network not in networks:
            networks.append(network)
    if not networks:
        raise ValueError("enter at least one subnet, e.g. 192.168.1.0/24")
    total = sum(_host_count(n) for n in networks)
    if total > max_hosts:
        raise ValueError(f"that is {total} addresses; the limit is {max_hosts} "
                         "(use smaller subnets, or raise the limit)")
    return networks


def _host_count(network: ipaddress.IPv4Network) -> int:
    return network.num_addresses if network.prefixlen >= 31 else network.num_addresses - 2


def hosts(networks: Iterable[ipaddress.IPv4Network]) -> list[str]:
    """Every host address in ``networks`` (no network/broadcast addresses)."""
    seen: dict[str, None] = {}
    for network in networks:
        addresses = network.hosts() if network.prefixlen < 31 else iter(network)
        for address in addresses:
            seen[str(address)] = None
    return list(seen)


def primary_address(iface=None) -> Optional[str]:
    """This computer's address: the wired interface's if given, else the default route's."""
    if iface is not None:
        return str(iface.ipv4[0]) if iface.ipv4 else None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 9))  # documentation address; nothing is sent
            address = sock.getsockname()[0]
    except OSError:
        return None
    return None if address.startswith(("0.", "127.")) else address


def suggest_subnets(connections: Iterable[Connection],
                    fallback: Optional[str] = None) -> list[str]:
    """The /24 subnets containing the inventory's IPv4 addresses.

    If the inventory has none, the subnet of ``fallback`` (normally this
    computer's own address, e.g. '192.168.1.240/23') is suggested instead; a
    bare address is taken as a /24.
    """
    subnets: dict[str, None] = {}
    for conn in connections:
        try:
            address = ipaddress.ip_address(conn.ip)
        except ValueError:
            continue
        if address.version == 4:
            subnets[str(ipaddress.ip_network(f"{address}/24", strict=False))] = None
    if not subnets and fallback:
        text = fallback if "/" in fallback else f"{fallback}/24"
        subnets[str(ipaddress.ip_network(text, strict=False))] = None
    return list(subnets)


def off_subnet_warning(networks: list[ipaddress.IPv4Network],
                       local_ips: Iterable[str], iface=None) -> Optional[str]:
    """Warn when this computer has no address in any of the swept subnets."""
    if set(local_ips):
        return None
    mine = primary_address(iface)
    if mine:
        own = ipaddress.ip_interface(mine if "/" in mine else f"{mine}/24")
        where = f"Its wired interface ({iface.name}) is" if iface is not None else "This computer is"
        hint = f" {where} {own.ip}, so you may want {own.network}."
    else:
        hint = ""
    return ("This computer doesn't have an address in "
            f"{', '.join(map(str, networks))}. Devices there may answer ping, but their MAC "
            "addresses can't be read, and devices that block ping won't be found." + hint)


def local_addresses(networks: Iterable[ipaddress.IPv4Network], iface=None) -> set[str]:
    """This computer's own address on each network (if it has one there).

    With ``iface`` only that interface's addresses count.
    """
    if iface is not None:
        return {str(a.ip) for a in iface.ipv4 if any(a.ip in n for n in networks)}
    found = set()
    for network in networks:
        target = str(next(iter(network.hosts()), network.network_address))
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect((target, 9))  # no packet is sent for UDP connect()
                address = sock.getsockname()[0]
        except OSError:
            continue
        if ipaddress.ip_address(address) in network:
            found.add(address)
    return found


# --------------------------------------------------------------------- sweep

def sweep(
    networks: list[ipaddress.IPv4Network],
    ping_fn: Callable[..., PingResult] = ping,
    timeout_s: float = 0.5,
    workers: int = 64,
    progress: Optional[Callable[[int, int], None]] = None,
    stop_event: Optional[threading.Event] = None,
) -> dict[str, bool]:
    """Ping every host once; returns {ip: replied}. Stop skips hosts not yet pinged."""
    addresses = hosts(networks)
    done = 0
    lock = threading.Lock()

    def probe(ip: str) -> tuple[str, bool]:
        nonlocal done
        replied = False
        if not (stop_event is not None and stop_event.is_set()):
            replied = ping_fn(ip, count=1, timeout_s=timeout_s).reachable
        with lock:
            done += 1
            if progress:
                progress(done, len(addresses))
        return ip, replied

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return dict(pool.map(probe, addresses))


# ------------------------------------------------------------------ classify

def load_oui(path: str) -> dict[str, str]:
    """Load the IEEE 'oui.csv' registry into {'aa:bb:cc': vendor}. Missing file -> {}."""
    vendors: dict[str, str] = {}
    try:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            for row in csv.reader(fh):
                if len(row) >= 3 and len(row[1]) == 6:
                    prefix = row[1].lower()
                    vendors[":".join(prefix[i:i + 2] for i in (0, 2, 4))] = row[2].strip()
    except OSError:
        return {}
    return vendors


def vendor_of(mac: str, vendors: Optional[dict[str, str]]) -> str:
    if not mac:
        return ""
    if vendors and mac[:8] in vendors:
        return vendors[mac[:8]]
    return "random/private MAC" if is_locally_administered(mac) else ""


def _describe(conn: Connection) -> str:
    where = f"{conn.patch_panel}:{conn.panel_port}" if conn.patch_panel or conn.panel_port else ""
    return " ".join(p for p in (where, conn.device) if p) or f"connection #{conn.index}"


def classify(
    connections: list[Connection],
    networks: list[ipaddress.IPv4Network],
    replies: dict[str, bool],
    arp: dict[str, str],
    local_ips: Iterable[str] = (),
    vendors: Optional[dict[str, str]] = None,
) -> list[Found]:
    """Compare what answered with the inventory."""
    local = set(local_ips)

    def inside(ip: str) -> bool:
        try:
            address = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(address in n for n in networks)

    expected = [c for c in connections if c.status != STATUS_UNUSED and c.ip]
    by_ip = {c.ip: c for c in expected}
    by_mac = {c.expected_mac: c for c in expected if c.expected_mac}

    answered = {ip for ip, ok in replies.items() if ok} | {ip for ip in arp if inside(ip)}
    answered -= local
    results: list[Found] = []
    for ip in sorted(answered, key=ipaddress.ip_address):
        mac = normalize_mac(arp.get(ip)) or ""
        replied = replies.get(ip, False)
        note = "" if replied else " (replied to ARP only - ping is blocked)"
        known = by_ip.get(ip)
        if known is not None:
            if mac and known.expected_mac and mac != known.expected_mac:
                category = MAC_CONFLICT
                message = (f"Expected {known.expected_mac} here, but {mac} answered - a "
                           "different device is using this IP")
            else:
                category = EXPECTED
                message = "In the inventory" + (" (MAC not recorded yet)"
                                                if not known.expected_mac else "")
            row = known
        elif mac and mac in by_mac:
            row = by_mac[mac]
            category = MOVED
            message = f"Known device now at this IP; the inventory has it at {row.ip}"
        else:
            row = None
            category = UNKNOWN
            message = "Not in the inventory" + ("" if mac else
                                                " (MAC unknown - is it on another subnet?)")
        results.append(Found(
            ip=ip, mac=mac, replied=replied, category=category, message=message + note,
            index=row.index if row else None, inventory=_describe(row) if row else "",
            vendor=vendor_of(mac, vendors)))

    for conn in expected:
        if inside(conn.ip) and conn.ip not in answered and conn.ip not in local:
            results.append(Found(
                ip=conn.ip, mac="", replied=False, category=NOT_FOUND,
                message="In the inventory but didn't answer ping or ARP",
                index=conn.index, inventory=_describe(conn)))
    results.sort(key=lambda f: (CATEGORIES.index(f.category), ipaddress.ip_address(f.ip)))
    return results


def discover(
    connections: list[Connection],
    networks: list[ipaddress.IPv4Network],
    ping_fn: Callable[..., PingResult] = ping,
    arp_fn: Callable[[], dict[str, str]] = read_arp_table,
    timeout_s: float = 0.5,
    workers: int = 64,
    vendors: Optional[dict[str, str]] = None,
    progress: Optional[Callable[[int, int], None]] = None,
    stop_event: Optional[threading.Event] = None,
    local_ips: Optional[Iterable[str]] = None,
    iface=None,
) -> list[Found]:
    """Sweep ``networks``, read the ARP table and classify every device found.

    With ``iface`` (a netif.Interface) the sweep and the ARP table only use
    that interface.
    """
    if iface is not None:
        if ping_fn is ping:
            ping_fn = partial(ping, iface=iface)
        if arp_fn is read_arp_table:
            arp_fn = partial(read_arp_table, iface=iface)
    replies = sweep(networks, ping_fn, timeout_s, workers, progress, stop_event)
    local = local_addresses(networks, iface) if local_ips is None else local_ips
    return classify(connections, networks, replies, arp_fn(), local, vendors)


def summarize(found: list[Found]) -> dict[str, int]:
    counts = {c: 0 for c in CATEGORIES}
    for f in found:
        counts[f.category] += 1
    return counts


# ------------------------------------------------------------------- output

def new_entry(found: Found) -> dict:
    """An inventory entry for a newly discovered device."""
    entry = {"ip": found.ip}
    if found.mac:
        entry["expected_mac"] = found.mac
    entry["status"] = "connected"
    entry["notes"] = f"Found by Discover on {datetime.now():%Y-%m-%d}" + (
        f" ({found.vendor})" if found.vendor else "")
    return entry


def found_row(f: Found) -> dict:
    return {"category": f.category, "ip": f.ip, "mac": f.mac, "vendor": f.vendor,
            "reply": f.reply, "inventory": f.inventory, "message": f.message}


def write_discovery(found: list[Found], path: str, subnets: list[str]) -> None:
    if path.lower().endswith(".json"):
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "subnets": subnets,
            "summary": summarize(found),
            "devices": [dict(found_row(f), index=f.index) for f in found],
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
            fh.write("\n")
        return
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=REPORT_COLUMNS)
        writer.writeheader()
        for f in found:
            writer.writerow(found_row(f))

