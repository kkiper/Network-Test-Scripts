"""GUI window for the read-only Switch Port Audit of the production switches."""

from __future__ import annotations

import csv
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Optional

from .checker import FAIL, PASS, WARN
from .inventory import InventoryError, parse_inventory
from .report import result_row
from .switch import SwitchError, connect_to
from .switchaudit import (
    SILENT_NOTE, UNLISTED, SwitchLogin, audit, collect, logins_from_inventory, new_entry,
)

COLUMNS = {
    "switch": ("Switch", 105, "w"),
    "port": ("Port", 80, "w"),
    "row": ("Inventory Row", 170, "w"),
    "link": ("Link", 90, "w"),
    "macs": ("MACs Seen", 200, "w"),
    "result": ("Result", 80, "center"),
    "detail": ("Detail", 470, "w"),
}
ROW_STYLES = {
    PASS: {"background": "#d9f2d9"},
    FAIL: {"background": "#f8d0d0"},
    WARN: {"background": "#fbefc4"},
    UNLISTED: {"background": "#fbefc4"},
}


class AuditWindow(tk.Toplevel):
    """Log into each production switch read-only and compare its tables with the inventory."""

    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.title("Switch Port Audit (read-only)")
        self.geometry("1250x660")
        self.minsize(900, 450)
        self.worker: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.events: "queue.Queue" = queue.Queue()
        self.results = []
        self.unlisted = []
        self.rows: dict[str, object] = {}  # tree iid -> CheckResult or Unlisted

        self._build_switches()
        self._build_table()
        self._build_actions()
        self.protocol("WM_DELETE_WINDOW", self.close)

    # ------------------------------------------------------------------ layout

    def _connections(self) -> list:
        try:
            return parse_inventory(self.app.data, "inventory")
        except InventoryError:
            return []

    def _build_switches(self) -> None:
        note = ttk.Label(self, foreground="#8a5a1c", justify="left", wraplength=1200,
                         padding=(8, 8, 8, 0), text=(
            "Logs into each production switch with a READ-ONLY account and sends only 'show' "
            "commands (port status, MAC address table, CDP/LLDP neighbours) - nothing is "
            "changed. It shows which port each device is on, including devices without an "
            "IP address. " + SILENT_NOTE))
        note.pack(fill="x")

        frame = ttk.LabelFrame(self, text="Production switches", padding=8)
        frame.pack(fill="x", padx=8, pady=8)
        try:
            logins = logins_from_inventory(self.app.data, self._connections())
        except InventoryError:
            logins = {}
        self.switch_vars: dict[str, dict[str, tk.Variable]] = {}
        headers = ("Audit", "Switch", "Management IP", "Username", "Password")
        for col, text in enumerate(headers):
            ttk.Label(frame, text=text, font=("TkDefaultFont", 9, "bold")).grid(
                row=0, column=col, sticky="w", padx=(0, 12))
        for row, login in enumerate(logins.values(), start=1):
            values = {
                "enabled": tk.BooleanVar(value=bool(login.host)),
                "host": tk.StringVar(value=login.host),
                "username": tk.StringVar(value=login.username),
                "password": tk.StringVar(),
            }
            self.switch_vars[login.name] = values
            ttk.Checkbutton(frame, variable=values["enabled"]).grid(row=row, column=0)
            ttk.Label(frame, text=login.name).grid(row=row, column=1, sticky="w", padx=(0, 12))
            ttk.Entry(frame, textvariable=values["host"], width=18).grid(
                row=row, column=2, sticky="w", padx=(0, 12), pady=2)
            ttk.Entry(frame, textvariable=values["username"], width=16).grid(
                row=row, column=3, sticky="w", padx=(0, 12), pady=2)
            ttk.Entry(frame, textvariable=values["password"], width=16, show="•").grid(
                row=row, column=4, sticky="w", padx=(0, 12), pady=2)
        hint_row = len(logins) + 1
        for col, text in ((2, "e.g. 192.168.1.2"), (3, "read-only account"), (4, "never saved")):
            ttk.Label(frame, text=text, foreground="#666666", font=("TkDefaultFont", 9)).grid(
                row=hint_row, column=col, sticky="w")
        if not logins:
            ttk.Label(frame, text="No switches are named in the inventory yet.").grid(
                row=1, column=0, columnspan=5, sticky="w")
        self.run_button = ttk.Button(frame, text="Run Audit", style="Run.TButton",
                                     command=self.run)
        self.run_button.grid(row=1, column=5, rowspan=max(1, len(logins)), padx=(12, 0),
                             sticky="ns")
        self.progress_label = ttk.Label(self, text="", padding=(8, 0))
        self.progress_label.pack(fill="x")

    def _build_table(self) -> None:
        frame = ttk.Frame(self, padding=8)
        frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(frame, columns=list(COLUMNS), show="headings",
                                 selectmode="extended")
        for col, (heading, width, anchor) in COLUMNS.items():
            self.tree.heading(col, text=heading)
            self.tree.column(col, width=width, anchor=anchor, stretch=(col == "detail"))
        for tag, opts in ROW_STYLES.items():
            self.tree.tag_configure(tag, **opts)
        yscroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        xscroll = ttk.Scrollbar(frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

    def _build_actions(self) -> None:
        bar = ttk.Frame(self, padding=(8, 0, 8, 8))
        bar.pack(fill="x")
        ttk.Button(bar, text="Close", command=self.close).pack(side="right")
        ttk.Button(bar, text="Export...", command=self.export).pack(side="right", padx=4)
        self.add_button = ttk.Button(bar, text="Add Selected Unlisted to Inventory",
                                     command=self.add_selected)
        self.add_button.pack(side="right", padx=4)

    # ------------------------------------------------------------------- run

    def running(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def chosen(self) -> list[tuple[SwitchLogin, str]]:
        picked = []
        for name, values in self.switch_vars.items():
            if values["enabled"].get():
                login = SwitchLogin(name=name, host=values["host"].get().strip(),
                                    username=values["username"].get().strip())
                picked.append((login, values["password"].get()))
        return picked

    def save_logins(self) -> None:
        """Remember host/username per switch in the inventory (never passwords)."""
        switches = dict(self.app.data.get("switches") or {})
        for name, values in self.switch_vars.items():
            host, user = values["host"].get().strip(), values["username"].get().strip()
            if host or user or name in switches:
                entry = dict(switches.get(name) or {})
                entry.update({"host": host, "username": user})
                switches[name] = entry
        if switches and self.app.data.get("switches") != switches:
            self.app.data["switches"] = switches
            self.app.changed(clear_all=False)

    def run(self) -> None:
        if self.running():
            return
        picked = self.chosen()
        if not picked:
            messagebox.showinfo("Switch Port Audit", "Tick at least one switch to audit.",
                                parent=self)
            return
        missing = [login.name for login, _pw in picked if not login.host or not login.username]
        if missing:
            messagebox.showerror("Switch Port Audit",
                                 f"Enter the management IP and username for: {', '.join(missing)}",
                                 parent=self)
            return
        iface = self.app.require_iface(parent=self)
        if iface is None:
            return
        self.save_logins()
        connections = self._connections()

        def work():
            states = {}
            for login, password in picked:
                self.events.put(("progress", f"Reading {login.name} ({login.host})..."))
                try:
                    session = connect_to(login.host, login.username, password,
                                         login.device_type, login.ssh_port, iface)
                    try:
                        states[login.name] = collect(session, login.name)
                    finally:
                        session.close()
                except SwitchError as exc:
                    self.events.put(("error", f"{login.name}: {exc}"))
                    return
            results, unlisted = audit(connections, states)
            self.events.put(("done", results, unlisted))

        self.run_button.configure(state="disabled")
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()
        self.after(100, self.poll)

    def poll(self) -> None:
        if not self.winfo_exists():
            return
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "progress":
                    self.progress_label.configure(text=event[1])
                elif event[0] == "error":
                    self.progress_label.configure(text="Audit failed")
                    messagebox.showerror("Switch Port Audit", event[1], parent=self)
                else:
                    _kind, self.results, self.unlisted = event
                    self.app.add_results(self.results)
                    self.show()
        except queue.Empty:
            pass
        if self.running() or not self.events.empty():
            self.after(100, self.poll)
        else:
            self.run_button.configure(state="normal")

    def show(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self.rows = {}
        for number, res in enumerate(self.results):
            pc, conn = res.port_check, res.connection
            iid = f"r{number}"
            self.rows[iid] = res
            label = conn.device or f"{conn.patch_panel}:{conn.panel_port}".strip(":")
            self.tree.insert("", "end", iid=iid, tags=(res.result,), values=[
                conn.switch, pc.seen_port if pc else conn.switch_port,
                f"#{conn.index} {label}", pc.link if pc else "", pc.macs if pc else "",
                res.result, res.message])
        for number, item in enumerate(self.unlisted):
            iid = f"u{number}"
            self.rows[iid] = item
            detail = "Not in the inventory" + (f"; neighbour {item.neighbor}" if item.neighbor
                                               else "")
            if not item.macs:
                detail += " - link only, no traffic seen"
            self.tree.insert("", "end", iid=iid, tags=(UNLISTED,), values=[
                item.switch, item.port_short, "", item.link, ", ".join(item.macs) or "none",
                UNLISTED, detail])
        counts = {r: sum(x.result == r for x in self.results) for r in (PASS, FAIL, WARN)}
        self.progress_label.configure(
            text=f"Audit done: {counts[PASS]} pass, {counts[FAIL]} fail, {counts[WARN]} warn, "
                 f"{len(self.unlisted)} unlisted port(s). Results are also in the main window.")

    # ---------------------------------------------------------------- actions

    def add_selected(self) -> None:
        if self.running() or self.app.running():
            return
        chosen = [self.rows[iid] for iid in self.tree.selection()
                  if iid.startswith("u") and iid in self.rows]
        if not chosen:
            messagebox.showinfo("Add to inventory", "Select one or more UNLISTED ports first.",
                                parent=self)
            return
        for item in chosen:
            self.app.entries.append(new_entry(item))
            self.unlisted.remove(item)
        self.app.changed(clear_all=False)
        self.show()
        messagebox.showinfo(
            "Add to inventory",
            f"Added {len(chosen)} port(s) to the inventory. In the main window, double-click "
            "each new row to fill in its patch panel, device name and IP, then Save.",
            parent=self)

    def export(self) -> None:
        if not self.results and not self.unlisted:
            messagebox.showinfo("Export", "Run an audit first.", parent=self)
            return
        path = filedialog.asksaveasfilename(
            parent=self, title="Export switch port audit", defaultextension=".csv",
            initialfile="switch_port_audit.csv", filetypes=[("CSV (Excel)", "*.csv")])
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(["result", "switch", "port", "inventory_row", "link", "macs",
                                 "detail"])
                for res in self.results:
                    row = result_row(res)
                    writer.writerow([res.result, res.connection.switch, row["seen_port"],
                                     res.connection.index, row["link"], row["macs_seen"],
                                     res.message])
                for item in self.unlisted:
                    writer.writerow([UNLISTED, item.switch, item.port_short, "", item.link,
                                     ", ".join(item.macs), item.neighbor])
        except OSError as exc:
            messagebox.showerror("Export failed", str(exc), parent=self)
            return
        self.progress_label.configure(text=f"Results written to {path}")

    def close(self) -> None:
        if self.running():
            messagebox.showinfo("Busy", "Wait for the audit to finish before closing.",
                                parent=self)
            return
        self.app.audit_window = None
        self.destroy()
