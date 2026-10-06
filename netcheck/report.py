"""Console, CSV and JSON reporting of check results."""

from __future__ import annotations

import csv
import json
import os
from collections import Counter
from datetime import datetime, timezone
from typing import TextIO

from .checker import FAIL, MAC_DISCOVERED, PASS, SKIP, WARN, CheckResult
from .inventory import read_json

REPORT_COLUMNS = (
    "result",
    "patch_panel",
    "panel_port",
    "switch",
    "switch_port",
    "device",
    "ip",
    "expected_mac",
    "discovered_mac",
    "mac_check",
    "ping_replies",
    "avg_rtt_ms",
    "message",
)

_COLOURS = {PASS: "\033[32m", FAIL: "\033[31m", WARN: "\033[33m", SKIP: "\033[90m"}
_RESET = "\033[0m"


def result_row(res: CheckResult) -> dict:
    c = res.connection
    return {
        "result": res.result,
        "patch_panel": c.patch_panel,
        "panel_port": c.panel_port,
        "switch": c.switch,
        "switch_port": c.switch_port,
        "device": c.device,
        "ip": c.ip,
        "expected_mac": c.expected_mac or "",
        "discovered_mac": res.discovered_mac or "",
        "mac_check": res.mac_check,
        "ping_replies": f"{res.ping.replies}/{res.ping.sent}" if res.ping else "",
        "avg_rtt_ms": "" if not res.ping or res.ping.avg_rtt_ms is None else res.ping.avg_rtt_ms,
        "message": res.message,
    }


def summarize(results: list[CheckResult]) -> Counter:
    counts = Counter({PASS: 0, FAIL: 0, WARN: 0, SKIP: 0})
    counts.update(r.result for r in results)
    return counts


def print_table(results: list[CheckResult], out: TextIO, colour: bool = False) -> None:
    columns = ("result", "patch_panel", "panel_port", "switch", "switch_port",
               "device", "ip", "discovered_mac", "mac_check", "ping_replies", "message")
    headers = ("RESULT", "PANEL", "P-PORT", "SWITCH", "S-PORT",
               "DEVICE", "IP", "DISCOVERED MAC", "MAC CHECK", "PING", "DETAIL")
    rows = [[str(result_row(r)[col]) for col in columns] for r in results]
    # Don't let the free-text detail column drive the width calculation.
    widths = [max([len(h)] + [len(row[i]) for row in rows]) for i, h in enumerate(headers[:-1])]

    def fmt(cells: list[str], status: str = "") -> str:
        parts = [cells[i].ljust(widths[i]) for i in range(len(widths))] + [cells[-1]]
        if colour and status in _COLOURS:
            parts[0] = f"{_COLOURS[status]}{parts[0]}{_RESET}"
        return "  ".join(parts).rstrip()

    out.write(fmt(list(headers)) + "\n")
    out.write("  ".join("-" * w for w in widths) + "  " + "-" * len(headers[-1]) + "\n")
    for res, row in zip(results, rows):
        out.write(fmt(row, res.result) + "\n")

    counts = summarize(results)
    out.write(
        f"\nSummary: {len(results)} connections - "
        f"{counts[PASS]} pass, {counts[FAIL]} fail, {counts[WARN]} warn, {counts[SKIP]} skipped\n"
    )


def write_csv(results: list[CheckResult], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=REPORT_COLUMNS)
        writer.writeheader()
        for res in results:
            writer.writerow(result_row(res))


def write_json(results: list[CheckResult], path: str, inventory_path: str) -> None:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "inventory": os.path.abspath(inventory_path),
        "summary": dict(summarize(results)),
        "results": [result_row(r) for r in results],
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")


def write_report(results: list[CheckResult], path: str, inventory_path: str) -> None:
    if path.lower().endswith(".json"):
        write_json(results, path, inventory_path)
    else:
        write_csv(results, path)


def fill_discovered_macs(data: dict, results: list[CheckResult]) -> int:
    """Set ``expected_mac`` on inventory entries whose MAC was missing but discovered.

    ``data`` is the raw inventory document; it is modified in place. MACs that
    don't match are left alone. Returns the number of MACs filled in.
    """
    entries = data["connections"]
    filled = 0
    for res in results:
        if res.mac_check == MAC_DISCOVERED and res.discovered_mac:
            entries[res.connection.index - 1]["expected_mac"] = res.discovered_mac
            filled += 1
    return filled


def save_json(data: dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")


def write_baseline(results: list[CheckResult], inventory_path: str, path: str) -> int:
    """Write a copy of the inventory JSON with blank expected MACs filled from discovery.

    Everything else in the original file is preserved. Returns the number of
    MACs that were filled in. Review the file before adopting it as the new
    expected interconnect.
    """
    data = read_json(inventory_path)
    filled = fill_discovered_macs(data, results)
    save_json(data, path)
    return filled
