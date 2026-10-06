"""Cross-platform ICMP ping using the operating system's ping command."""

from __future__ import annotations

import math
import platform
import re
import subprocess
from dataclasses import dataclass
from typing import Optional

_RTT = re.compile(r"time\s*[=<]\s*([\d.]+)\s*ms", re.IGNORECASE)


@dataclass
class PingResult:
    reachable: bool
    replies: int
    sent: int
    avg_rtt_ms: Optional[float]
    error: Optional[str] = None


def build_ping_command(
    ip: str, count: int, timeout_s: float, system: Optional[str] = None, iface=None
) -> list[str]:
    """The OS ping command. ``iface`` (a netif.Interface) pins it to that interface."""
    system = (system or platform.system()).lower()
    source = iface.address if iface is not None else None
    if system == "windows":
        cmd = ["ping", "-n", str(count), "-w", str(int(timeout_s * 1000))]
        return cmd + (["-S", source] if source else []) + [ip]
    if system == "darwin":
        # macOS: -W is the per-reply wait in milliseconds; -b binds to an interface.
        cmd = ["ping", "-c", str(count), "-W", str(int(timeout_s * 1000))]
        return cmd + (["-b", iface.name] if iface is not None else []) + [ip]
    # Linux (iputils/busybox): -W is the per-reply wait in whole seconds.
    cmd = ["ping", "-c", str(count), "-W", str(max(1, math.ceil(timeout_s)))]
    return cmd + (["-I", iface.name] if iface is not None else []) + [ip]


def parse_ping_output(output: str, sent: int, system: Optional[str] = None) -> PingResult:
    system = (system or platform.system()).lower()
    if system == "windows":
        # Windows exits 0 even for "Destination host unreachable" replies sent
        # by a router, so only count lines that carry a TTL as real replies
        # (this also works with non-English Windows output).
        reply_lines = [line for line in output.splitlines() if "ttl=" in line.lower()]
    else:
        reply_lines = [
            line for line in output.splitlines()
            if "bytes from" in line.lower() and _RTT.search(line)
        ]
    rtts = []
    for line in reply_lines:
        match = _RTT.search(line)
        if match:
            rtts.append(float(match.group(1)))
    replies = len(reply_lines)
    return PingResult(
        reachable=replies > 0,
        replies=replies,
        sent=sent,
        avg_rtt_ms=round(sum(rtts) / len(rtts), 2) if rtts else None,
    )


def ping(ip: str, count: int = 2, timeout_s: float = 1.0, iface=None) -> PingResult:
    """Ping ``ip`` ``count`` times, waiting ``timeout_s`` seconds per reply.

    With ``iface`` (a netif.Interface) the ping only leaves through that interface.
    """
    cmd = build_ping_command(ip, count, timeout_s, iface=iface)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=count * (timeout_s + 1) + 5,
            check=False,
        )
    except FileNotFoundError:
        return PingResult(False, 0, count, None, "ping command not found")
    except subprocess.TimeoutExpired:
        return PingResult(False, 0, count, None, "ping command timed out")
    output = (proc.stdout or "") + (proc.stderr or "")
    return parse_ping_output(output, count)
