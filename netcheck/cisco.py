"""Parsing helpers for Cisco IOS / IOS XE command output."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

# Canonical interface type -> (short form, accepted lowercase aliases).
_INTERFACE_TYPES = {
    "TwentyFiveGigE": ("Twe", ("twe", "twentyfivegige", "twentyfivegigabitethernet")),
    "TwoGigabitEthernet": ("Tw", ("tw", "twogigabitethernet")),
    "TenGigabitEthernet": ("Te", ("te", "ten", "tengige", "tengigabitethernet")),
    "FiveGigabitEthernet": ("Fi", ("fi", "fivegigabitethernet", "fivegige")),
    "FortyGigabitEthernet": ("Fo", ("fo", "fortygige", "fortygigabitethernet")),
    "HundredGigE": ("Hu", ("hu", "hundredgige", "hundredgigabitethernet")),
    "AppGigabitEthernet": ("Ap", ("ap", "appgigabitethernet")),
    "GigabitEthernet": ("Gi", ("gi", "gig", "ge", "gigabitethernet")),
    "FastEthernet": ("Fa", ("fa", "fastethernet")),
    "Ethernet": ("Eth", ("e", "et", "eth", "ethernet")),
    "Port-channel": ("Po", ("po", "port-channel", "portchannel")),
}
_ALIASES = {alias: name for name, (_s, aliases) in _INTERFACE_TYPES.items() for alias in aliases}
_IFACE = re.compile(r"^\s*([A-Za-z][A-Za-z-]*)\s*(\d+(?:/\d+)*(?:\.\d+)?)\s*$")


def normalize_interface(name: Optional[str]) -> Optional[str]:
    """Return the full canonical interface name, e.g. 'Gi1/0/5' -> 'GigabitEthernet1/0/5'.

    Returns None if ``name`` doesn't look like an interface name.
    """
    if not name:
        return None
    match = _IFACE.match(name)
    if not match:
        return None
    prefix, numbers = match.group(1).lower(), match.group(2)
    canonical = _ALIASES.get(prefix)
    if canonical is None:
        candidates = [n for n in _INTERFACE_TYPES if n.lower().startswith(prefix)]
        if len(candidates) != 1:
            return None
        canonical = candidates[0]
    return canonical + numbers


def short_interface(name: Optional[str]) -> str:
    """Return the short display form, e.g. 'GigabitEthernet1/0/5' -> 'Gi1/0/5'."""
    canonical = normalize_interface(name)
    if canonical is None:
        return name or ""
    for full, (short, _aliases) in _INTERFACE_TYPES.items():
        if canonical.startswith(full) and canonical[len(full):][:1].isdigit():
            return short + canonical[len(full):]
    return canonical


def same_interface(a: Optional[str], b: Optional[str]) -> bool:
    na, nb = normalize_interface(a), normalize_interface(b)
    if na and nb:
        return na == nb
    return bool(a) and bool(b) and a.strip().lower() == b.strip().lower()


def normalize_hostname(name: Optional[str]) -> str:
    """Compare-friendly hostname: drop any serial in brackets and the domain name."""
    if not name:
        return ""
    name = re.sub(r"\(.*?\)", "", name).strip()
    return name.split(".", 1)[0].lower()


def expand_port_range(text: str) -> list[str]:
    """Expand 'Gi1/0/1-4, Gi1/0/10' into canonical interface names.

    Raises ValueError for anything that isn't a valid interface or range.
    """
    ports: list[str] = []
    for part in re.split(r"[,\s]+", text.strip()):
        if not part:
            continue
        match = re.match(r"^(.*?)(\d+)-(\d+)$", part)
        if match:
            base, first, last = match.group(1), int(match.group(2)), int(match.group(3))
            if last < first:
                raise ValueError(f"'{part}': range end is before its start")
            names = [f"{base}{n}" for n in range(first, last + 1)]
        else:
            names = [part]
        for name in names:
            canonical = normalize_interface(name)
            if canonical is None:
                raise ValueError(f"'{name}' is not a valid interface name")
            if canonical not in ports:
                ports.append(canonical)
    return ports


# --------------------------------------------------------------------- CDP/LLDP


@dataclass
class Neighbor:
    protocol: str            # "CDP" or "LLDP"
    local_interface: str     # canonical name of our port
    device_id: str           # remote hostname as advertised
    remote_port: str         # remote port as advertised
    platform: str = ""
    ip: str = ""


def _not_enabled(text: str) -> bool:
    return bool(re.search(r"%\s*(CDP|LLDP) is not enabled", text, re.IGNORECASE))


def parse_cdp_neighbors_detail(text: str) -> list[Neighbor]:
    """Parse 'show cdp neighbors detail'."""
    if _not_enabled(text):
        return []
    neighbors = []
    for block in re.split(r"^-{5,}\s*$", text, flags=re.MULTILINE):
        device = re.search(r"^Device ID:\s*(.+?)\s*$", block, re.MULTILINE)
        iface = re.search(
            r"^Interface:\s*([^,]+?),\s*Port ID \(outgoing port\):\s*(.+?)\s*$",
            block, re.MULTILINE)
        if not (device and iface):
            continue
        platform = re.search(r"^Platform:\s*([^,]+)", block, re.MULTILINE)
        ip = re.search(r"IP(?:v4)? address:\s*(\S+)", block)
        neighbors.append(Neighbor(
            protocol="CDP",
            local_interface=normalize_interface(iface.group(1)) or iface.group(1),
            device_id=device.group(1),
            remote_port=iface.group(2),
            platform=platform.group(1).strip() if platform else "",
            ip=ip.group(1) if ip else "",
        ))
    return neighbors


def parse_lldp_neighbors_detail(text: str) -> list[Neighbor]:
    """Parse 'show lldp neighbors detail' (IOS / IOS XE)."""
    if _not_enabled(text):
        return []
    neighbors = []
    for block in re.split(r"^-{5,}\s*$", text, flags=re.MULTILINE):
        local = re.search(r"^Local Intf:\s*(\S+)", block, re.MULTILINE)
        if not local:
            continue
        port_id = re.search(r"^Port id:\s*(.+?)\s*$", block, re.MULTILINE)
        port_desc = re.search(r"^Port Description:\s*(.+?)\s*$", block, re.MULTILINE)
        system = re.search(r"^System Name:\s*(.+?)\s*$", block, re.MULTILINE)
        chassis = re.search(r"^Chassis id:\s*(.+?)\s*$", block, re.MULTILINE)
        ip = re.search(r"IP(?:v4)?:\s*(\d+\.\d+\.\d+\.\d+)", block)
        # Port id is usually the interface name, but some devices send a MAC;
        # fall back to the port description in that case.
        remote = port_id.group(1) if port_id else ""
        if not normalize_interface(remote) and port_desc and normalize_interface(port_desc.group(1)):
            remote = port_desc.group(1)
        name = system.group(1) if system and system.group(1) != "- not advertised" else ""
        neighbors.append(Neighbor(
            protocol="LLDP",
            local_interface=normalize_interface(local.group(1)) or local.group(1),
            device_id=name or (chassis.group(1) if chassis else ""),
            remote_port=remote,
            ip=ip.group(1) if ip else "",
        ))
    return neighbors


# ------------------------------------------------------------ interface status

_STATUS_WORDS = ("connected", "notconnect", "disabled", "err-disabled", "inactive",
                 "suspended", "monitoring", "sfpAbsent", "xcvrAbsent", "noXcvr",
                 "notconnec", "faulty")
# A lookahead, so matches can overlap: a description such as "spare disabled
# one" must not hide the real status that follows it.
_STATUS_RE = re.compile(
    r"(?=\s(" + "|".join(re.escape(w) for w in _STATUS_WORDS) + r")\s+(\S+)\s+(\S+)\s+(\S+))")


@dataclass
class InterfaceStatus:
    interface: str  # canonical
    name: str       # description
    status: str
    vlan: str
    duplex: str
    speed: str

    @property
    def link_up(self) -> bool:
        return self.status == "connected"

    @property
    def routed(self) -> bool:
        return self.vlan.lower() == "routed"

    @property
    def speed_mbps(self) -> Optional[int]:
        """Negotiated speed in Mb/s ('a-1000' -> 1000, '10G' -> 10000), or None."""
        match = re.search(r"(\d+)\s*([GM]?)", self.speed, re.IGNORECASE)
        if not match:
            return None
        value = int(match.group(1))
        return value * 1000 if match.group(2).upper() == "G" else value


def parse_interfaces_status(text: str) -> dict[str, InterfaceStatus]:
    """Parse 'show interfaces status' into {canonical interface: status}."""
    result = {}
    for line in text.splitlines():
        if not line.strip() or line.startswith("Port "):
            continue
        port = line.split()[0]
        canonical = normalize_interface(port)
        if canonical is None:
            continue
        matches = list(_STATUS_RE.finditer(line))
        if not matches:
            continue
        match = matches[-1]  # rightmost, in case the description contains a status word
        result[canonical] = InterfaceStatus(
            interface=canonical,
            name=line[len(port):match.start()].strip(),
            status=match.group(1),
            vlan=match.group(2),
            duplex=match.group(3),
            speed=match.group(4),
        )
    return result


# ----------------------------------------------------------------------- TDR


@dataclass
class CableTest:
    interface: str
    pairs: list[tuple[str, str, str]] = field(default_factory=list)  # (pair, length, status)

    @property
    def ok(self) -> bool:
        return bool(self.pairs) and all(s.lower() == "normal" for _p, _l, s in self.pairs)

    def summary(self) -> str:
        if not self.pairs:
            return "unavailable"
        if self.ok:
            lengths = [int(l) for _p, l, _s in self.pairs if l.isdigit()]
            return f"OK ({max(lengths)} m)" if lengths else "OK"
        faults = [f"pair {p} {s}" + (f" at {l} m" if l.isdigit() else "")
                  for p, l, s in self.pairs if s.lower() != "normal"]
        return "; ".join(faults)


_TDR_PAIR = re.compile(
    r"Pair\s+([A-D])\s+(\d+|N/A)\s*(?:\+/-\s*\d+\s*meters?)?\s+(?:Pair\s+[A-D]|N/A)\s+(\S.*?)\s*$",
    re.IGNORECASE)


def parse_tdr(text: str) -> dict[str, CableTest]:
    """Parse 'show cable-diagnostics tdr interface ...' output."""
    results: dict[str, CableTest] = {}
    current: Optional[CableTest] = None
    for line in text.splitlines():
        first = line.split()[0] if line.strip() and not line[0].isspace() else ""
        canonical = normalize_interface(first) if first else None
        if canonical:
            current = results.setdefault(canonical, CableTest(canonical))
        match = _TDR_PAIR.search(line)
        if match and current is not None:
            current.pairs.append((match.group(1).upper(), match.group(2), match.group(3)))
    return results
