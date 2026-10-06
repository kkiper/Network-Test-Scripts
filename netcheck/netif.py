"""Find this computer's wired Ethernet interfaces, so tests only use the wired port.

Windows, Linux and macOS are supported using standard OS tools only. Wi-Fi,
Bluetooth and virtual adapters are never chosen.
"""

from __future__ import annotations

import ipaddress
import json
import os
import platform
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Optional

ANY = "any"  # --interface any: let the OS choose (old behaviour, not recommended)


class InterfaceError(Exception):
    """No usable wired interface, or the one asked for doesn't exist."""


class AmbiguousInterface(InterfaceError):
    """Several wired interfaces are connected; the user has to pick one."""

    def __init__(self, candidates: list["Interface"]):
        self.candidates = candidates
        names = "\n".join(f"  {c.label}" for c in candidates)
        super().__init__("More than one wired Ethernet interface is connected - choose one:\n"
                         + names)


@dataclass
class Interface:
    name: str                       # eth0 / en5 / Windows alias such as "Ethernet 2"
    description: str = ""           # adapter model, e.g. "Realtek USB GbE Family Controller"
    wired: bool = False
    up: bool = False
    ipv4: list = field(default_factory=list)  # ipaddress.IPv4Interface, link-local excluded
    index: Optional[int] = None     # Windows interface index
    mac: str = ""

    @property
    def address(self) -> Optional[str]:
        return str(self.ipv4[0].ip) if self.ipv4 else None

    @property
    def network(self) -> Optional[ipaddress.IPv4Network]:
        return self.ipv4[0].network if self.ipv4 else None

    @property
    def label(self) -> str:
        addresses = ", ".join(str(i) for i in self.ipv4) or "no IPv4 address"
        return f"{self.name} - {addresses}" + (f" ({self.description})" if self.description else "")


def _ipv4(address: str, prefix) -> Optional[ipaddress.IPv4Interface]:
    try:
        value = ipaddress.IPv4Interface(f"{address}/{prefix}")
    except ValueError:
        return None
    return None if value.ip.is_link_local or value.ip.is_loopback else value


def _run(cmd: list[str], timeout: float = 10.0) -> str:
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              check=False, **kwargs)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout or ""


# ------------------------------------------------------------------ Windows

_WINDOWS_SCRIPT = (
    "$a = @(Get-NetAdapter -Physical | Select-Object Name, InterfaceDescription, ifIndex, "
    "Status, PhysicalMediaType, MacAddress); "
    "$i = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | "
    "Select-Object InterfaceIndex, IPAddress, PrefixLength); "
    "@{adapters=$a; addresses=$i} | ConvertTo-Json -Depth 3 -Compress"
)


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def parse_windows(text: str) -> list[Interface]:
    """Parse the JSON from the PowerShell query in _WINDOWS_SCRIPT."""
    try:
        data = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError:
        return []
    addresses: dict[int, list] = {}
    for entry in _as_list(data.get("addresses")):
        value = _ipv4(str(entry.get("IPAddress", "")), entry.get("PrefixLength", 32))
        if value is not None:
            addresses.setdefault(int(entry.get("InterfaceIndex", -1)), []).append(value)
    result = []
    for adapter in _as_list(data.get("adapters")):
        index = int(adapter.get("ifIndex", -1))
        media = str(adapter.get("PhysicalMediaType") or "")
        result.append(Interface(
            name=str(adapter.get("Name", "")),
            description=str(adapter.get("InterfaceDescription", "")),
            wired=media == "802.3",
            up=str(adapter.get("Status", "")).lower() == "up",
            ipv4=addresses.get(index, []),
            index=index,
            mac=str(adapter.get("MacAddress", "")),
        ))
    return result


def _list_windows() -> list[Interface]:
    return parse_windows(_run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                               _WINDOWS_SCRIPT], timeout=20))


# -------------------------------------------------------------------- Linux

def parse_ip_addr(text: str) -> dict[str, list]:
    """Parse 'ip -4 -o addr show' into {ifname: [IPv4Interface, ...]}."""
    result: dict[str, list] = {}
    for line in text.splitlines():
        match = re.match(r"^\d+:\s+(\S+?)(?:@\S+)?\s+inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)", line)
        if match:
            value = _ipv4(match.group(2), match.group(3))
            if value is not None:
                result.setdefault(match.group(1), []).append(value)
    return result


def _read(path: str) -> str:
    try:
        with open(path, encoding="ascii", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def parse_linux(sys_class_net: str, ip_addr_text: str) -> list[Interface]:
    """Build interfaces from a /sys/class/net tree plus 'ip -4 -o addr show' output."""
    addresses = parse_ip_addr(ip_addr_text)
    result = []
    try:
        names = sorted(os.listdir(sys_class_net))
    except OSError:
        return []
    for name in names:
        base = os.path.join(sys_class_net, name)
        physical = os.path.exists(os.path.join(base, "device"))
        wireless = (os.path.exists(os.path.join(base, "wireless"))
                    or os.path.exists(os.path.join(base, "phy80211")))
        ethernet = _read(os.path.join(base, "type")) == "1"
        state = _read(os.path.join(base, "operstate"))
        up = state == "up" or (state == "unknown" and _read(os.path.join(base, "carrier")) == "1")
        result.append(Interface(
            name=name, wired=physical and ethernet and not wireless, up=up,
            ipv4=addresses.get(name, []), mac=_read(os.path.join(base, "address"))))
    return result


def _ioctl_ipv4(name: str) -> list:
    """Primary IPv4 address/netmask via ioctl, for systems without the 'ip' command."""
    try:
        import fcntl
        import socket
        import struct
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            request = struct.pack("256s", name.encode()[:15])
            address = socket.inet_ntoa(fcntl.ioctl(sock.fileno(), 0x8915, request)[20:24])
            netmask = socket.inet_ntoa(fcntl.ioctl(sock.fileno(), 0x891B, request)[20:24])
    except (OSError, ImportError):
        return []
    value = _ipv4(address, netmask)
    return [value] if value is not None else []


def _list_linux() -> list[Interface]:
    interfaces = parse_linux("/sys/class/net", _run(["ip", "-4", "-o", "addr", "show"]))
    for iface in interfaces:
        if iface.wired and not iface.ipv4:
            iface.ipv4 = _ioctl_ipv4(iface.name)
    return interfaces


# -------------------------------------------------------------------- macOS

_NOT_WIRED = re.compile(r"wi-?fi|airport|bluetooth|bridge|iphone|ipad|vpn|modem|firewire",
                        re.IGNORECASE)


def parse_hardware_ports(text: str) -> list[tuple[str, str]]:
    """Parse 'networksetup -listallhardwareports' into [(port name, device), ...]."""
    ports, current = [], None
    for line in text.splitlines():
        if line.startswith("Hardware Port:"):
            current = line.split(":", 1)[1].strip()
        elif line.startswith("Device:") and current is not None:
            ports.append((current, line.split(":", 1)[1].strip()))
            current = None
    return ports


def parse_ifconfig(text: str) -> tuple[bool, list]:
    """(up, [IPv4Interface]) from 'ifconfig <dev>' output on macOS."""
    up = bool(re.search(r"status:\s*active", text))
    addresses = []
    for match in re.finditer(r"\binet (\d+\.\d+\.\d+\.\d+) netmask (0x[0-9a-fA-F]+)", text):
        prefix = bin(int(match.group(2), 16)).count("1")
        value = _ipv4(match.group(1), prefix)
        if value is not None:
            addresses.append(value)
    return up, addresses


def _list_macos() -> list[Interface]:
    result = []
    for port, device in parse_hardware_ports(_run(["networksetup", "-listallhardwareports"])):
        up, addresses = parse_ifconfig(_run(["ifconfig", device]))
        result.append(Interface(name=device, description=port, wired=not _NOT_WIRED.search(port),
                                up=up, ipv4=addresses))
    return result


# ------------------------------------------------------------------- choose

def list_interfaces(system: Optional[str] = None) -> list[Interface]:
    """Every network adapter the OS reports (wired or not)."""
    system = (system or platform.system()).lower()
    if system == "windows":
        return _list_windows()
    if system == "linux":
        return _list_linux()
    return _list_macos()


def wired_candidates(system: Optional[str] = None) -> list[Interface]:
    """Wired Ethernet interfaces that are connected and have an IPv4 address."""
    return [i for i in list_interfaces(system) if i.wired and i.up and i.ipv4]


def _matches(iface: Interface, wanted: str) -> bool:
    wanted = wanted.strip().lower()
    return wanted in {iface.name.lower(), iface.description.lower(), str(iface.index)} or \
        any(str(a.ip) == wanted for a in iface.ipv4)


def choose_interface(name: Optional[str] = None,
                     candidates: Optional[list[Interface]] = None) -> Optional[Interface]:
    """Pick the wired interface to use.

    ``name`` may be an interface name, Windows alias, description, index or one
    of its IPv4 addresses. ``"any"`` returns None, meaning "let the OS choose".
    """
    if name and name.strip().lower() == ANY:
        return None
    candidates = wired_candidates() if candidates is None else candidates
    if name:
        for iface in candidates:
            if _matches(iface, name):
                return iface
        available = "\n".join(f"  {c.label}" for c in candidates) or "  (none)"
        raise InterfaceError(f"No connected wired interface matches '{name}'. Wired "
                             f"interfaces with an IPv4 address:\n{available}")
    if not candidates:
        raise InterfaceError(
            "No wired Ethernet connection with an IPv4 address was found. Plug the laptop's "
            "Ethernet port into the network and give it an address (e.g. 192.168.1.240/24). "
            "Wi-Fi is never used by this tool.")
    if len(candidates) > 1:
        raise AmbiguousInterface(candidates)
    return candidates[0]


# --------------------------------------------------------------------- CLI

def add_cli_arguments(parser) -> None:
    parser.add_argument("--interface", metavar="NAME",
                        help="wired interface to use (name, alias or IP address); needed only "
                             "if more than one is connected. 'any' lets the OS choose "
                             "(not recommended)")
    parser.add_argument("--list-interfaces", action="store_true",
                        help="list the connected wired interfaces and exit")


def resolve_cli_interface(args, out=None, err=None) -> tuple[Optional[Interface], Optional[int]]:
    """Handle --list-interfaces / --interface. Returns (interface, exit code or None)."""
    out = out or sys.stdout
    err = err or sys.stderr
    if getattr(args, "list_interfaces", False):
        candidates = wired_candidates()
        if not candidates:
            out.write("No connected wired interface with an IPv4 address.\n")
        for iface in candidates:
            out.write(iface.label + "\n")
        return None, 0
    try:
        iface = choose_interface(getattr(args, "interface", None))
    except InterfaceError as exc:
        hint = "\nUse --interface NAME to choose." if isinstance(exc, AmbiguousInterface) else ""
        err.write(f"error: {exc}{hint}\n")
        return None, 2
    if iface is None:
        err.write("warning: --interface any - the OS chooses the interface (Wi-Fi may be used)\n")
    else:
        err.write(f"Using wired interface {iface.label}\n")
    return iface, None
