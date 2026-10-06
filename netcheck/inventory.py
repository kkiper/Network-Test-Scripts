"""Load and validate the expected physical interconnect (CSV)."""

from __future__ import annotations

import csv
import ipaddress
from dataclasses import dataclass, field
from typing import Optional

from .mac import normalize_mac

STATUS_CONNECTED = "connected"
STATUS_UNUSED = "unused"
VALID_STATUSES = (STATUS_CONNECTED, STATUS_UNUSED)

REQUIRED_COLUMNS = ("status",)
KNOWN_COLUMNS = (
    "patch_panel",
    "panel_port",
    "switch",
    "switch_port",
    "device",
    "ip",
    "expected_mac",
    "status",
    "notes",
)


class InventoryError(Exception):
    """Raised when the expected-interconnect file is invalid."""


@dataclass
class Connection:
    """One expected link: patch panel port <-> switch port <-> device."""

    line: int
    patch_panel: str = ""
    panel_port: str = ""
    switch: str = ""
    switch_port: str = ""
    device: str = ""
    ip: str = ""
    expected_mac: Optional[str] = None
    status: str = STATUS_CONNECTED
    notes: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        if self.device:
            return self.device
        if self.patch_panel or self.panel_port:
            return f"{self.patch_panel}:{self.panel_port}"
        return f"line {self.line}"


def load_inventory(path: str) -> list[Connection]:
    """Read ``path`` and return its connections, raising InventoryError on problems.

    All problems found are reported together so the file can be fixed in one pass.
    """
    try:
        fh = open(path, newline="", encoding="utf-8-sig")
    except OSError as exc:
        raise InventoryError(f"Cannot open inventory file: {exc}") from exc

    headers: Optional[list[str]] = None
    errors: list[str] = []
    connections: list[Connection] = []
    with fh:
        reader = csv.reader(fh)
        for cells in reader:
            # Skip blank lines and comment lines (first cell starts with '#').
            if not any(c.strip() for c in cells) or cells[0].lstrip().startswith("#"):
                continue
            if headers is None:
                headers = [c.strip().lower() for c in cells]
                missing = [c for c in REQUIRED_COLUMNS if c not in headers]
                if missing:
                    raise InventoryError(
                        f"{path}: missing required column(s): {', '.join(missing)}"
                    )
                continue
            row = {h: (cells[i].strip() if i < len(cells) else "") for i, h in enumerate(headers)}
            conn, row_errors = _parse_row(row, reader.line_num)
            errors.extend(row_errors)
            if conn:
                connections.append(conn)

    if headers is None:
        raise InventoryError(f"{path}: file is empty")

    errors.extend(_check_duplicates(connections))
    if errors:
        raise InventoryError(f"{path}: invalid inventory:\n  " + "\n  ".join(errors))
    if not connections:
        raise InventoryError(f"{path}: no connections defined")
    return connections


def _parse_row(row: dict, line: int) -> tuple[Optional[Connection], list[str]]:
    errors = []
    status = row.get("status", "").lower() or STATUS_CONNECTED
    if status not in VALID_STATUSES:
        errors.append(
            f"line {line}: status '{row.get('status')}' must be one of {', '.join(VALID_STATUSES)}"
        )

    ip = row.get("ip", "")
    if ip:
        try:
            ip = str(ipaddress.ip_address(ip))
        except ValueError:
            errors.append(f"line {line}: '{ip}' is not a valid IP address")

    raw_mac = row.get("expected_mac", "")
    mac = normalize_mac(raw_mac)
    if raw_mac and mac is None:
        errors.append(f"line {line}: '{raw_mac}' is not a valid MAC address")

    if status == STATUS_UNUSED and ip:
        errors.append(f"line {line}: unused connection should not have an IP address")

    if errors:
        return None, errors

    conn = Connection(
        line=line,
        patch_panel=row.get("patch_panel", ""),
        panel_port=row.get("panel_port", ""),
        switch=row.get("switch", ""),
        switch_port=row.get("switch_port", ""),
        device=row.get("device", ""),
        ip=ip,
        expected_mac=mac,
        status=status,
        notes=row.get("notes", ""),
        extra={k: v for k, v in row.items() if k not in KNOWN_COLUMNS},
    )
    return conn, []


def _check_duplicates(connections: list[Connection]) -> list[str]:
    errors = []
    keys = {
        "IP address": lambda c: c.ip,
        "MAC address": lambda c: c.expected_mac,
        "patch panel port": lambda c: (c.patch_panel, c.panel_port)
        if c.patch_panel and c.panel_port else None,
        "switch port": lambda c: (c.switch, c.switch_port)
        if c.switch and c.switch_port else None,
    }
    for name, key in keys.items():
        seen: dict = {}
        for conn in connections:
            value = key(conn)
            if not value:
                continue
            if value in seen:
                shown = ":".join(value) if isinstance(value, tuple) else value
                errors.append(
                    f"line {conn.line}: duplicate {name} '{shown}' (also on line {seen[value]})"
                )
            else:
                seen[value] = conn.line
    return errors
