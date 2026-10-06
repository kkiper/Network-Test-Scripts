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


def _lookup_linux(ip: str) -> Optional[str]:
    out = _run(["ip", "neigh", "show", ip])
    for line in out.splitlines():
        # e.g. "192.168.1.10 dev eth0 lladdr aa:bb:cc:dd:ee:ff REACHABLE"
        match = re.search(r"\blladdr\s+(\S+)", line)
        if match:
            mac = normalize_mac(match.group(1))
            if mac and mac not in _INVALID_MACS:
                return mac
    # Fall back to /proc/net/arp when iproute2 is unavailable.
    try:
        with open("/proc/net/arp", encoding="ascii") as fh:
            return _mac_from_lines(fh.read(), ip)
    except OSError:
        return None


def _lookup_windows(ip: str) -> Optional[str]:
    return _mac_from_lines(_run(["arp", "-a", ip]), ip)


def _lookup_bsd(ip: str) -> Optional[str]:
    return _mac_from_lines(_run(["arp", "-n", ip]), ip)


def lookup_mac(ip: str, system: Optional[str] = None) -> Optional[str]:
    """Return the MAC address the host has learned for ``ip``, or None.

    The entry only exists for hosts on the same layer-2 segment as this
    machine, and normally only after traffic (e.g. a ping) has been sent to it.
    """
    system = (system or platform.system()).lower()
    if system == "windows":
        return _lookup_windows(ip)
    if system == "linux":
        return _lookup_linux(ip)
    return _lookup_bsd(ip)
