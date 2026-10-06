"""Load and validate the expected physical interconnect (JSON)."""

from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass, field
from typing import Any, Optional

from .mac import normalize_mac

STATUS_CONNECTED = "connected"
STATUS_UNUSED = "unused"
VALID_STATUSES = (STATUS_CONNECTED, STATUS_UNUSED)

KNOWN_FIELDS = (
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

    index: int  # 1-based position in the file's "connections" list
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
        return f"connection #{self.index}"


def read_json(path: str) -> Any:
    try:
        with open(path, encoding="utf-8-sig") as fh:
            return json.load(fh)
    except OSError as exc:
        raise InventoryError(f"Cannot open inventory file: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise InventoryError(
            f"{path}: invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc


def load_inventory(path: str) -> list[Connection]:
    """Read ``path`` and return its connections, raising InventoryError on problems.

    All problems found are reported together so the file can be fixed in one pass.
    """
    return parse_inventory(read_json(path), path)


def parse_inventory(data: Any, source: str = "inventory") -> list[Connection]:
    """Validate an already-loaded inventory document (see load_inventory)."""
    if not isinstance(data, dict) or "connections" not in data:
        raise InventoryError(f'{source}: top level must be an object with a "connections" list')
    entries = data["connections"]
    if not isinstance(entries, list):
        raise InventoryError(f'{source}: "connections" must be a list')
    if not entries:
        raise InventoryError(f"{source}: no connections defined")

    errors: list[str] = []
    connections: list[Connection] = []
    for index, entry in enumerate(entries, start=1):
        conn, entry_errors = _parse_entry(entry, index)
        errors.extend(entry_errors)
        if conn:
            connections.append(conn)

    errors.extend(_check_duplicates(connections))
    if errors:
        raise InventoryError(f"{source}: invalid inventory:\n  " + "\n  ".join(errors))
    return connections


def validate_entry(entry: Any, index: int = 1) -> list[str]:
    """Return the problems with a single connection entry (ignores duplicates)."""
    return _parse_entry(entry, index)[1]


def _text(entry: dict, key: str, errors: list[str]) -> str:
    """Return ``entry[key]`` as a stripped string; numbers are allowed (e.g. port 1)."""
    value = entry.get(key)
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        errors.append(f'"{key}" must be a string or number')
        return ""
    return str(value).strip()


def _parse_entry(entry: Any, index: int) -> tuple[Optional[Connection], list[str]]:
    where = f"connection #{index}"
    if not isinstance(entry, dict):
        return None, [f"{where}: must be an object"]
    type_errors: list[str] = []
    fields = {key: _text(entry, key, type_errors) for key in KNOWN_FIELDS}
    if fields["patch_panel"] or fields["panel_port"]:
        where += f" ({fields['patch_panel']}:{fields['panel_port']})"
    errors = [f"{where}: {e}" for e in type_errors]

    status = fields["status"].lower() or STATUS_CONNECTED
    if status not in VALID_STATUSES:
        errors.append(
            f"{where}: status '{fields['status']}' must be one of {', '.join(VALID_STATUSES)}"
        )

    ip = fields["ip"]
    if ip:
        try:
            ip = str(ipaddress.ip_address(ip))
        except ValueError:
            errors.append(f"{where}: '{ip}' is not a valid IP address")

    raw_mac = fields["expected_mac"]
    mac = normalize_mac(raw_mac)
    if raw_mac and mac is None:
        errors.append(f"{where}: '{raw_mac}' is not a valid MAC address")

    if status == STATUS_UNUSED and ip:
        errors.append(f"{where}: unused connection should not have an IP address")

    if errors:
        return None, errors

    conn = Connection(
        index=index,
        patch_panel=fields["patch_panel"],
        panel_port=fields["panel_port"],
        switch=fields["switch"],
        switch_port=fields["switch_port"],
        device=fields["device"],
        ip=ip,
        expected_mac=mac,
        status=status,
        notes=fields["notes"],
        extra={k: v for k, v in entry.items() if k not in KNOWN_FIELDS},
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
                    f"connection #{conn.index}: duplicate {name} '{shown}' "
                    f"(also on connection #{seen[value]})"
                )
            else:
                seen[value] = conn.index
    return errors
