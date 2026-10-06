"""MAC address normalisation and discovery via the host's ARP / neighbour table."""

from __future__ import annotations

import platform
import re
import subprocess
from typing import Optional

_HEX_PAIR_SEP = re.compile(r"^[0-9a-fA-F]{1,2}([:-][0-9a-fA-F]{1,2}){5}$")
_HEX_12 = re.compile(r"^[0-9a-fA-F]{12}$")
# Matches MACs as printed by ip/arp on Linux, Windows and macOS (macOS drops
# leading zeros, e.g. "0:1a:2b:3:4:5").
_MAC_IN_TEXT = re.compile(
    r"(?<![0-9a-fA-F:-])([0-9a-fA-F]{1,2}(?:[:-][0-9a-fA-F]{1,2}){5})(?![0-9a-fA-F:-])"
)

_INVALID_MACS = {"00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"}


def normalize_mac(value: Optional[str]) -> Optional[str]:
    """Return ``value`` as lowercase ``aa:bb:cc:dd:ee:ff``, or None if not a MAC.

    Accepts colon/hyphen separated (with or without leading zeros), Cisco
    dotted (``aabb.ccdd.eeff``) and bare 12-digit hex forms.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if _HEX_PAIR_SEP.match(text):
        octets = re.split(r"[:-]", text)
        return ":".join(o.zfill(2) for o in octets).lower()
    bare = text.replace(".", "")
    if _HEX_12.match(bare):
        return ":".join(bare[i:i + 2] for i in range(0, 12, 2)).lower()
    return None


def _run(cmd: list[str], timeout: float = 5.0) -> str:
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout or ""


def _mac_from_lines(text: str, ip: str) -> Optional[str]:
    """Find the MAC on the line(s) of ``text`` that mention ``ip`` exactly."""
    ip_token = re.compile(r"(?<![\d.])" + re.escape(ip) + r"(?![\d.])")
    for line in text.splitlines():
        if not ip_token.search(line):
            continue
        match = _MAC_IN_TEXT.search(line)
        if match:
            mac = normalize_mac(match.group(1))
            if mac and mac not in _INVALID_MACS:
                return mac
    return None


def _proc_arp(device: Optional[str] = None) -> str:
    """/proc/net/arp, optionally only the lines for one device (last column)."""
    try:
        with open("/proc/net/arp", encoding="ascii") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return ""
    if device:
        lines = [l for l in lines if l.split() and l.split()[-1] == device]
    return "\n".join(lines)


def _lookup_linux(ip: str, iface=None) -> Optional[str]:
    out = _run(["ip", "neigh", "show", ip] + (["dev", iface.name] if iface is not None else []))
    for line in out.splitlines():
        # e.g. "192.168.1.10 dev eth0 lladdr aa:bb:cc:dd:ee:ff REACHABLE"
        match = re.search(r"\blladdr\s+(\S+)", line)
        if match:
            mac = normalize_mac(match.group(1))
            if mac and mac not in _INVALID_MACS:
                return mac
    # Fall back to /proc/net/arp when iproute2 is unavailable.
    return _mac_from_lines(_proc_arp(iface.name if iface is not None else None), ip)


def _lookup_windows(ip: str, iface=None) -> Optional[str]:
    # -N limits the output to the ARP table of the interface with that address.
    extra = ["-N", iface.address] if iface is not None and iface.address else []
    return _mac_from_lines(_run(["arp", "-a", ip] + extra), ip)


def _lookup_bsd(ip: str, iface=None) -> Optional[str]:
    if iface is not None:
        return parse_arp_table(_run(["arp", "-an", "-i", iface.name]), "darwin").get(ip)
    return _mac_from_lines(_run(["arp", "-n", ip]), ip)


def lookup_mac(ip: str, system: Optional[str] = None, iface=None) -> Optional[str]:
    """Return the MAC address the host has learned for ``ip``, or None.

    The entry only exists for hosts on the same layer-2 segment as this
    machine, and normally only after traffic (e.g. a ping) has been sent to it.
    With ``iface`` (a netif.Interface) only that interface's entries are used.
    """
    system = (system or platform.system()).lower()
    if system == "windows":
        return _lookup_windows(ip, iface)
    if system == "linux":
        return _lookup_linux(ip, iface)
    return _lookup_bsd(ip, iface)


# ---------------------------------------------------------------- whole table

_IPV4_IN_TEXT = re.compile(r"(?<![\d.])((?:\d{1,3}\.){3}\d{1,3})(?![\d.])")


def _usable(mac: Optional[str]) -> bool:
    """A unicast MAC worth reporting (not empty, broadcast or multicast)."""
    if not mac or mac in _INVALID_MACS:
        return False
    return not int(mac[:2], 16) & 1  # the multicast bit (01:00:5e..., 33:33...)


def is_locally_administered(mac: Optional[str]) -> bool:
    """True for randomised / private MACs (the locally administered bit is set)."""
    mac = normalize_mac(mac)
    return bool(mac) and bool(int(mac[:2], 16) & 2)


def parse_arp_table(text: str, system: str) -> dict[str, str]:
    """Parse neighbour-table output into {ip: mac} for unicast entries."""
    table: dict[str, str] = {}
    for line in text.splitlines():
        if system == "linux" and "lladdr" not in line and "0x" not in line:
            continue  # FAILED / INCOMPLETE entries from 'ip neigh'
        ip = _IPV4_IN_TEXT.search(line)
        mac = _MAC_IN_TEXT.search(line)
        if not (ip and mac):
            continue
        value = normalize_mac(mac.group(1))
        if _usable(value):
            table.setdefault(ip.group(1), value)
    return table


def read_arp_table(system: Optional[str] = None, iface=None) -> dict[str, str]:
    """Return this computer's ARP / neighbour table as {ip: mac}.

    With ``iface`` (a netif.Interface) only that interface's entries are returned.
    """
    system = (system or platform.system()).lower()
    if system == "windows":
        extra = ["-N", iface.address] if iface is not None and iface.address else []
        return parse_arp_table(_run(["arp", "-a"] + extra), system)
    if system == "linux":
        dev = ["dev", iface.name] if iface is not None else []
        table = parse_arp_table(_run(["ip", "-4", "neigh", "show"] + dev), system)
        if table:
            return table
        return parse_arp_table(_proc_arp(iface.name if iface is not None else None), system)
    return parse_arp_table(_run(["arp", "-an"] + (["-i", iface.name] if iface else [])), system)
