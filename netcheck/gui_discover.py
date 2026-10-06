"""GUI window for discovering devices that aren't in the inventory."""

from __future__ import annotations

import queue
import shutil
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Optional

from .discover import (
    DEFAULT_OUI_PATH, EXPECTED, MAC_CONFLICT, MOVED, NOT_FOUND, UNKNOWN, Found, discover,
    found_row, load_oui, new_entry, parse_subnets, suggest_subnets, summarize, write_discovery,
)
from .inventory import InventoryError, parse_inventory

COLUMNS = {
    "category": ("Status", 115, "center"),
    "ip": ("IP Address", 115, "w"),
    "mac": ("MAC Address", 135, "w"),
    "vendor": ("Vendor", 150, "w"),
    "reply": ("Reply", 70, "center"),
    "inventory": ("Inventory Row", 170, "w"),
    "message": ("Detail", 420, "w"),
}
ROW_STYLES = {
    UNKNOWN: {"background": "#fbefc4"},
    MOVED: {"background": "#fbefc4"},
    MAC_CONFLICT: {"background": "#f8d0d0"},
    NOT_FOUND: {"foreground": "#777777"},
    EXPECTED: {"background": "#d9f2d9"},
}
SUMMARY_COLOURS = {UNKNOWN: "#a06800", MOVED: "#a06800", MAC_CONFLICT: "#b00020",
                   NOT_FOUND: "#666666", EXPECTED: "#1e7b1e"}


class DiscoverWindow(tk.Toplevel):
    """Sweep subnets, list what answered, and add unknown devices to the inventory."""

    scan_confirmed = False  # asked once per session

    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.title("Discover Devices")
        self.geometry("1200x600")
        self.minsize(850, 400)
        self.worker: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.events: "queue.Queue" = queue.Queue()
        self.found: list[Found] = []
        self.subnets: list[str] = []
        self.vendors = load_oui(DEFAULT_OUI_PATH)

        self._build_settings()
        self._build_table()
        self._build_actions()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.show()

    # ------------------------------------------------------------------ layout

    def _build_settings(self) -> None:
        frame = ttk.LabelFrame(self, text="Sweep (only devices on this computer's subnet/VLAN "
                                          "can be found)", padding=8)
        frame.pack(fill="x", padx=8, pady=8)
        self.subnets_var = tk.StringVar(value=", ".join(suggest_subnets(self._connections())))
        self.timeout_var = tk.DoubleVar(value=0.5)
        self.workers_var = tk.IntVar(value=64)
        self.only_unexpected = tk.BooleanVar(value=True)

        ttk.Label(frame, text="Subnets:").pack(side="left")
        ttk.Entry(frame, textvariable=self.subnets_var, width=36).pack(side="left", padx=(4, 12))
        ttk.Label(frame, text="Timeout (s):").pack(side="left")
        ttk.Spinbox(frame, from_=0.2, to=5, increment=0.1, textvariable=self.timeout_var,
                    width=5).pack(side="left", padx=(4, 12))
        ttk.Label(frame, text="Parallel:").pack(side="left")
        ttk.Spinbox(frame, from_=1, to=256, increment=8, textvariable=self.workers_var,
                    width=5).pack(side="left", padx=(4, 12))
        self.start_button = ttk.Button(frame, text="Start Discovery", style="Run.TButton",
                                       command=self.start)
        self.start_button.pack(side="left", padx=4)
        self.stop_button = ttk.Button(frame, text="Stop", command=self.stop, state="disabled")
        self.stop_button.pack(side="left")

        bar = ttk.Frame(self, padding=(8, 0))
        bar.pack(fill="x")
        self.progress = ttk.Progressbar(bar, length=260, mode="determinate")
        self.progress.pack(side="left")
        self.progress_label = ttk.Label(bar, text="Enter the subnet(s) to sweep, e.g. "
                                                  "192.168.1.0/24, then press Start Discovery.")
        self.progress_label.pack(side="left", padx=10)
        ttk.Checkbutton(bar, text="Show only unexpected", variable=self.only_unexpected,
                        command=self.show).pack(side="right")

    def _build_table(self) -> None:
        frame = ttk.Frame(self, padding=8)
        frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(frame, columns=list(COLUMNS), show="headings",
                                 selectmode="extended")
        for col, (heading, width, anchor) in COLUMNS.items():
            self.tree.heading(col, text=heading)
            self.tree.column(col, width=width, anchor=anchor, stretch=(col == "message"))
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
        self.summary_labels = {}
        for name in (UNKNOWN, MOVED, MAC_CONFLICT, NOT_FOUND, EXPECTED):
            label = tk.Label(bar, text=f"{name}: 0", fg=SUMMARY_COLOURS[name],
                             font=("TkDefaultFont", 10, "bold"))
            label.pack(side="left", padx=(0, 12))
            self.summary_labels[name] = label
        ttk.Button(bar, text="Close", command=self.close).pack(side="right")
        self.export_button = ttk.Button(bar, text="Export...", command=self.export)
        self.export_button.pack(side="right", padx=4)
        self.add_button = ttk.Button(bar, text="Add Selected to Inventory",
                                     command=self.add_selected)
        self.add_button.pack(side="right", padx=4)

    # ------------------------------------------------------------------- view

    def _connections(self) -> list:
        try:
            return parse_inventory(self.app.data, "inventory")
        except InventoryError:
            return []

    def show(self) -> None:
        self.tree.delete(*self.tree.get_children())
        hide_expected = bool(self.only_unexpected.get())
        for number, f in enumerate(self.found):
            if hide_expected and f.category == EXPECTED:
                continue
            row = found_row(f)
            self.tree.insert("", "end", iid=str(number), tags=(f.category,),
                             values=[row[c] for c in COLUMNS])
        counts = summarize(self.found)
        for name, label in self.summary_labels.items():
            label.configure(text=f"{name}: {counts[name]}")

    # ------------------------------------------------------------------ sweep

    def running(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def start(self) -> None:
        if self.running():
            return
        try:
            networks = parse_subnets(self.subnets_var.get())
            timeout = float(self.timeout_var.get())
            workers = int(self.workers_var.get())
            if timeout <= 0 or workers < 1:
                raise ValueError("Timeout must be above 0 and Parallel at least 1")
        except (ValueError, tk.TclError) as exc:
            messagebox.showerror("Discover", f"Invalid setting: {exc}", parent=self)
            return
        if not shutil.which("ping"):
            messagebox.showerror("Ping not available",
                                 "The 'ping' command was not found on this computer.",
                                 parent=self)
            return
        count = sum(n.num_addresses for n in networks)
        if not DiscoverWindow.scan_confirmed:
            if not messagebox.askokcancel(
                    "Network sweep",
                    f"This sends one ping to each of up to {count} addresses in "
                    f"{', '.join(map(str, networks))}.\n\nMake sure scanning is allowed on "
                    "this network before continuing.", parent=self):
                return
            DiscoverWindow.scan_confirmed = True

        self.subnets = [str(n) for n in networks]
        connections = self._connections()
        self.stop_event = threading.Event()
        self.progress.configure(maximum=count, value=0)
        self.progress_label.configure(text="Sweeping...")

        def work():
            try:
                found = discover(
                    connections, networks, timeout_s=timeout, workers=workers,
                    vendors=self.vendors, stop_event=self.stop_event,
                    progress=lambda done, total: self.events.put(("progress", done, total)))
                self.events.put(("done", found))
            except Exception as exc:  # report anything unexpected in the GUI
                self.events.put(("error", str(exc)))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()
        self.set_running(True)
        self.after(100, self.poll)

    def poll(self) -> None:
        if not self.winfo_exists():
            return
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "progress":
                    _kind, done, total = event
                    self.progress.configure(maximum=total, value=done)
                    self.progress_label.configure(text=f"{done}/{total} addresses pinged")
                elif event[0] == "done":
                    self.found = event[1]
                    self.progress.configure(value=self.progress.cget("maximum"))
                    self.show()
                    counts = summarize(self.found)
                    stopped = " (stopped early)" if self.stop_event.is_set() else ""
                    self.progress_label.configure(
                        text=f"Finished{stopped}: {counts[UNKNOWN]} unknown, {counts[MOVED]} "
                             f"moved, {counts[MAC_CONFLICT]} MAC conflict(s).")
                else:
                    messagebox.showerror("Discover failed", event[1], parent=self)
                    self.progress_label.configure(text="Discovery failed")
        except queue.Empty:
            pass
        if self.running() or not self.events.empty():
            self.after(100, self.poll)
        else:
            self.set_running(False)

    def stop(self) -> None:
        self.stop_event.set()
        self.progress_label.configure(text="Stopping...")

    def set_running(self, running: bool) -> None:
        self.start_button.configure(state="disabled" if running else "normal")
        self.stop_button.configure(state="normal" if running else "disabled")
        for button in (self.add_button, self.export_button):
            button.configure(state="disabled" if running else "normal")

    # ---------------------------------------------------------------- actions

    def selected(self) -> list[Found]:
        return [self.found[int(iid)] for iid in self.tree.selection()]

    def add_selected(self) -> None:
        if self.running() or self.app.running():
            return
        chosen = self.selected()
        unknown = [f for f in chosen if f.category == UNKNOWN]
        if not unknown:
            messagebox.showinfo(
                "Add to inventory",
                "Select one or more UNKNOWN devices first (Ctrl+click or Shift+click to "
                "select several). Only devices that aren't in the inventory can be added.",
                parent=self)
            return
        existing = {str(e.get("ip")) for e in self.app.entries if isinstance(e, dict)}
        added = [f for f in unknown if f.ip not in existing]
        for f in added:
            self.app.entries.append(new_entry(f))
            f.category, f.message = EXPECTED, "Added to the inventory"
            f.index = len(self.app.entries)
        if added:
            self.app.changed(clear_all=False)
        self.show()
        skipped = len(chosen) - len(added)
        messagebox.showinfo(
            "Add to inventory",
            f"Added {len(added)} device(s) to the inventory"
            + (f" ({skipped} selected row(s) weren't unknown devices and were skipped)"
               if skipped else "")
            + ".\n\nIn the main window, double-click each new row to fill in its patch panel "
              "and switch port, then Save.", parent=self)

    def export(self) -> None:
        if not self.found:
            messagebox.showinfo("Export", "Run a discovery first.", parent=self)
            return
        path = filedialog.asksaveasfilename(
            parent=self, title="Export discovery results", defaultextension=".csv",
            initialfile="discovered_devices.csv",
            filetypes=[("CSV (Excel)", "*.csv"), ("JSON", "*.json")])
        if not path:
            return
        try:
            write_discovery(self.found, path, self.subnets)
        except OSError as exc:
            messagebox.showerror("Export failed", str(exc), parent=self)
            return
        self.progress_label.configure(text=f"Results written to {path}")

    def close(self) -> None:
        if self.running():
            self.stop_event.set()
            messagebox.showinfo("Busy", "Stopping the sweep - close the window again once "
                                        "it has finished.", parent=self)
            return
        self.app.discover_window = None
        self.destroy()
