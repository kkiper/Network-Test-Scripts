"""Tkinter desktop GUI: edit the expected interconnect and run the checks."""

from __future__ import annotations

import copy
import os
import queue
import re
import shutil
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Optional

from . import __version__
from .checker import FAIL, PASS, SKIP, WARN, CheckResult, check_all
from .inventory import (
    KNOWN_FIELDS, STATUS_UNUSED, VALID_STATUSES, InventoryError, parse_inventory,
    read_json, validate_entry,
)
from .report import fill_discovered_macs, result_row, save_json, write_report

try:
    from .gui_portverify import PortVerifyWindow
except ImportError:  # pragma: no cover - only if the module is missing
    PortVerifyWindow = None

APP_TITLE = "Network Interconnect Test"
ALL = "(all)"
INVALID = "INVALID"
PENDING = "..."

# (heading, width, anchor) keyed by tree column id.
COLUMNS = {
    "result": ("Result", 65, "center"),
    "patch_panel": ("Panel", 65, "w"),
    "panel_port": ("Panel Port", 85, "center"),
    "switch": ("Switch", 115, "w"),
    "switch_port": ("Switch Port", 90, "w"),
    "device": ("Device", 130, "w"),
    "ip": ("IP Address", 110, "w"),
    "expected_mac": ("Expected MAC", 140, "w"),
    "discovered_mac": ("Discovered MAC", 140, "w"),
    "mac_check": ("MAC Check", 100, "center"),
    "ping_replies": ("Ping", 50, "center"),
    "avg_rtt_ms": ("RTT ms", 60, "e"),
    "message": ("Detail", 420, "w"),
}
RESULT_COLUMNS = ("result", "discovered_mac", "mac_check", "ping_replies", "avg_rtt_ms", "message")

ROW_STYLES = {
    PASS: {"background": "#d9f2d9"},
    FAIL: {"background": "#f8d0d0"},
    WARN: {"background": "#fbefc4"},
    SKIP: {"foreground": "#777777"},
    INVALID: {"background": "#f8d0d0", "foreground": "#8b0000"},
}
SUMMARY_COLOURS = {PASS: "#1e7b1e", FAIL: "#b00020", WARN: "#a06800", SKIP: "#666666"}

FIELD_LABELS = {
    "patch_panel": "Patch panel",
    "panel_port": "Panel port",
    "switch": "Switch",
    "switch_port": "Switch port",
    "device": "Device name",
    "ip": "IP address",
    "expected_mac": "Expected MAC",
    "status": "Status",
    "far_end": "Test point",
    "notes": "Notes",
}


def _display(value) -> str:
    return "" if value is None else str(value)


def _entry_label(entry: dict, index: int) -> str:
    panel = f"{_display(entry.get('patch_panel'))}:{_display(entry.get('panel_port'))}"
    return _display(entry.get("device")) or (panel if panel != ":" else f"connection #{index}")


class ConnectionDialog(tk.Toplevel):
    """Modal dialog for adding or editing one connection entry."""

    def __init__(self, parent: tk.Misc, title: str, entry: dict, validate):
        super().__init__(parent)
        self.title(title)
        self.transient(parent)
        self.resizable(False, False)
        self.result: Optional[dict] = None
        self._original = entry
        self._validate = validate
        self._vars: dict[str, tk.StringVar] = {}

        body = ttk.Frame(self, padding=12)
        body.grid(sticky="nsew")
        for row, field in enumerate(KNOWN_FIELDS):
            ttk.Label(body, text=FIELD_LABELS[field] + ":").grid(
                row=row, column=0, sticky="w", pady=3, padx=(0, 8))
            var = tk.StringVar(value=_display(entry.get(field)))
            if field == "status":
                var.set(var.get() or VALID_STATUSES[0])
                widget = ttk.Combobox(body, textvariable=var, values=VALID_STATUSES,
                                      state="readonly", width=34)
                widget.bind("<<ComboboxSelected>>", lambda _e: self._update_state())
            else:
                widget = ttk.Entry(body, textvariable=var, width=37)
            widget.grid(row=row, column=1, sticky="we", pady=3)
            self._vars[field] = var
            if row == 0:
                widget.focus_set()

        hint = ("MAC may be in any format (aa:bb:cc:dd:ee:ff, AA-BB-..., aabb.ccdd.eeff).\n"
                "Leave IP/MAC blank if unknown. Unused ports have no device or IP.\n"
                "Test point: where the test switch plugs in to reach this run (e.g. PP-Z:5).")
        ttk.Label(body, text=hint, foreground="#555555").grid(
            row=len(KNOWN_FIELDS), column=0, columnspan=2, sticky="w", pady=(8, 0))
        self._error = ttk.Label(body, text="", foreground="#b00020", wraplength=380)
        self._error.grid(row=len(KNOWN_FIELDS) + 1, column=0, columnspan=2, sticky="w")

        buttons = ttk.Frame(body)
        buttons.grid(row=len(KNOWN_FIELDS) + 2, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(buttons, text="OK", command=self._ok, default="active").pack(side="left", padx=4)
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="left")
        self.bind("<Return>", lambda _e: self._ok())
        self.bind("<Escape>", lambda _e: self.destroy())

        self._ip_entry = body.grid_slaves(row=KNOWN_FIELDS.index("ip"), column=1)[0]
        self._device_entry = body.grid_slaves(row=KNOWN_FIELDS.index("device"), column=1)[0]
        self._update_state()

        self.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        self.grab_set()

    def _update_state(self) -> None:
        unused = self._vars["status"].get() == STATUS_UNUSED
        for widget in (self._ip_entry, self._device_entry):
            widget.configure(state="disabled" if unused else "normal")

    def _build(self) -> dict:
        # Start from the original so unknown/extra fields are preserved.
        entry = copy.deepcopy(self._original)
        unused = self._vars["status"].get() == STATUS_UNUSED
        for field, var in self._vars.items():
            value = var.get().strip()
            if unused and field in ("ip", "device"):
                value = ""
            if not value:
                entry.pop(field, None)
            elif field == "panel_port" and value.isdigit():
                entry[field] = int(value)
            else:
                entry[field] = value
        # Keep the standard fields first, in their usual order.
        ordered = {f: entry[f] for f in KNOWN_FIELDS if f in entry}
        ordered.update({k: v for k, v in entry.items() if k not in ordered})
        return ordered

    def _ok(self) -> None:
        entry = self._build()
        problems = self._validate(entry)
        if problems:
            self._error.configure(text="\n".join(problems))
            return
        self.result = entry
        self.destroy()


class InterconnectApp:
    def __init__(self, root: tk.Tk, path: Optional[str] = None):
        self.root = root
        self.path: Optional[str] = None
        self.data: dict = {"connections": []}
        self.dirty = False
        self.results: dict[int, CheckResult] = {}
        self.last_run: list[CheckResult] = []
        self.worker: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.events: "queue.Queue" = queue.Queue()
        self.sort_state: tuple[str, bool] = ("", False)
        self.port_window = None

        root.title(APP_TITLE)
        root.geometry("1280x720")
        root.minsize(900, 500)
        style = ttk.Style(root)
        if sys.platform.startswith("linux") and "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Run.TButton", font=("TkDefaultFont", 10, "bold"))

        self._build_menu()
        self._build_toolbar()
        self._build_options()
        self._build_table()
        self._build_details()
        self._build_statusbar()

        root.protocol("WM_DELETE_WINDOW", self.quit)
        root.bind("<Control-o>", lambda _e: self.open_file())
        root.bind("<Control-s>", lambda _e: self.save())
        root.bind("<Control-n>", lambda _e: self.new_file())
        root.bind("<F5>", lambda _e: self.run_test())

        if path:
            self.load(path)
        self.refresh()

    # ------------------------------------------------------------------ layout

    def _build_menu(self) -> None:
        menubar = tk.Menu(self.root)
        file_menu = tk.Menu(menubar, tearoff=False)
        file_menu.add_command(label="New", accelerator="Ctrl+N", command=self.new_file)
        file_menu.add_command(label="Open...", accelerator="Ctrl+O", command=self.open_file)
        file_menu.add_command(label="Save", accelerator="Ctrl+S", command=self.save)
        file_menu.add_command(label="Save As...", command=self.save_as)
        file_menu.add_separator()
        file_menu.add_command(label="Export Report...", command=self.export_report)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.quit)
        menubar.add_cascade(label="File", menu=file_menu)

        tools_menu = tk.Menu(menubar, tearoff=False)
        tools_menu.add_command(label="Run Ping/MAC Test", accelerator="F5", command=self.run_test)
        tools_menu.add_command(label="Verify Unused Ports with Test Switch...",
                               command=self.open_port_verify)
        menubar.add_cascade(label="Tools", menu=tools_menu)

        help_menu = tk.Menu(menubar, tearoff=False)
        help_menu.add_command(label="How to use", command=self.show_help)
        help_menu.add_command(label="About", command=lambda: messagebox.showinfo(
            "About", f"{APP_TITLE}\nVersion {__version__}", parent=self.root))
        menubar.add_cascade(label="Help", menu=help_menu)
        self.root.config(menu=menubar)

    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self.root, padding=(8, 8, 8, 0))
        bar.pack(fill="x")
        ttk.Button(bar, text="Open...", command=self.open_file).pack(side="left")
        ttk.Button(bar, text="Save", command=self.save).pack(side="left", padx=(4, 12))
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=4)
        self.edit_buttons = [
            ttk.Button(bar, text="Add Connection", command=self.add_connection),
            ttk.Button(bar, text="Edit", command=self.edit_connection),
            ttk.Button(bar, text="Delete", command=self.delete_connection),
            ttk.Button(bar, text="Accept Discovered MACs", command=self.accept_macs),
        ]
        for button in self.edit_buttons:
            button.pack(side="left", padx=(4, 0))
        ttk.Button(bar, text="Verify Unused Ports...", command=self.open_port_verify).pack(
            side="right")

    def _build_options(self) -> None:
        frame = ttk.LabelFrame(self.root, text="Test", padding=8)
        frame.pack(fill="x", padx=8, pady=8)

        # Packed first so they keep their space when the window is narrow.
        self.export_button = ttk.Button(frame, text="Export Report...", command=self.export_report)
        self.export_button.pack(side="right")
        self.stop_button = ttk.Button(frame, text="Stop", command=self.stop_test, state="disabled")
        self.stop_button.pack(side="right", padx=4)
        self.run_button = ttk.Button(frame, text="Ping Test (F5)", style="Run.TButton",
                                     command=self.run_test)
        self.run_button.pack(side="right", padx=4)

        self.count_var = tk.IntVar(value=2)
        self.timeout_var = tk.DoubleVar(value=1.0)
        self.workers_var = tk.IntVar(value=16)
        self.switch_var = tk.StringVar(value=ALL)
        self.panel_var = tk.StringVar(value=ALL)

        def spin(label, var, lo, hi, inc, width=5):
            ttk.Label(frame, text=label).pack(side="left", padx=(0, 4))
            ttk.Spinbox(frame, from_=lo, to=hi, increment=inc, textvariable=var,
                        width=width).pack(side="left", padx=(0, 10))

        spin("Pings per device:", self.count_var, 1, 20, 1)
        spin("Timeout (s):", self.timeout_var, 0.2, 10, 0.5)
        spin("Parallel:", self.workers_var, 1, 128, 1)

        ttk.Label(frame, text="Switch:").pack(side="left", padx=(0, 4))
        self.switch_combo = ttk.Combobox(frame, textvariable=self.switch_var,
                                         state="readonly", width=14)
        self.switch_combo.pack(side="left", padx=(0, 10))
        ttk.Label(frame, text="Patch panel:").pack(side="left", padx=(0, 4))
        self.panel_combo = ttk.Combobox(frame, textvariable=self.panel_var,
                                        state="readonly", width=10)
        self.panel_combo.pack(side="left", padx=(0, 10))


    def _build_table(self) -> None:
        frame = ttk.LabelFrame(self.root, text="Expected interconnect", padding=4)
        frame.pack(fill="both", expand=True, padx=8)
        self.table_frame = frame
        self.tree = ttk.Treeview(frame, columns=list(COLUMNS), show="headings",
                                 selectmode="browse")
        for col, (heading, width, anchor) in COLUMNS.items():
            self.tree.heading(col, text=heading, command=lambda c=col: self.sort_by(c))
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

        self.tree.bind("<<TreeviewSelect>>", lambda _e: self.show_details())
        self.tree.bind("<Double-1>", lambda _e: self.edit_connection())
        self.tree.bind("<Delete>", lambda _e: self.delete_connection())

    def _build_details(self) -> None:
        frame = ttk.LabelFrame(self.root, text="Selected connection", padding=6)
        frame.pack(fill="x", padx=8, pady=(6, 0))
        self.details = tk.Text(frame, height=4, wrap="word", relief="flat",
                               font="TkDefaultFont", background=self.root.cget("background"))
        self.details.pack(fill="x")
        self.details.configure(state="disabled")

    def _build_statusbar(self) -> None:
        bar = ttk.Frame(self.root, padding=8)
        bar.pack(fill="x")
        self.summary_labels = {}
        for name in (PASS, FAIL, WARN, SKIP):
            label = tk.Label(bar, text=f"{name}: 0", fg=SUMMARY_COLOURS[name],
                             font=("TkDefaultFont", 10, "bold"))
            label.pack(side="left", padx=(0, 14))
            self.summary_labels[name] = label
        self.progress = ttk.Progressbar(bar, length=220, mode="determinate")
        self.progress.pack(side="right")
        self.status = ttk.Label(bar, text="")
        self.status.pack(side="right", padx=10)

    # ------------------------------------------------------------- inventory

    @property
    def entries(self) -> list:
        return self.data["connections"]

    def set_status(self, text: str) -> None:
        self.status.configure(text=text)

    def confirm_discard(self) -> bool:
        if not self.dirty:
            return True
        answer = messagebox.askyesnocancel(
            "Unsaved changes", "Save changes to the expected interconnect first?",
            parent=self.root)
        if answer is None:
            return False
        return self.save() if answer else True

    def new_file(self) -> None:
        if self.running() or not self.confirm_discard():
            return
        self.path, self.data, self.dirty = None, {"connections": []}, False
        self.clear_results()
        self.refresh()
        self.set_status("New inventory - use Add Connection to describe each patch panel port")

    def open_file(self) -> None:
        if self.running() or not self.confirm_discard():
            return
        path = filedialog.askopenfilename(
            parent=self.root, title="Open expected interconnect",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")])
        if path:
            self.load(path)
            self.refresh()

    def load(self, path: str) -> None:
        try:
            data = read_json(path)
        except InventoryError as exc:
            messagebox.showerror("Cannot open file", str(exc), parent=self.root)
            return
        if not isinstance(data, dict) or not isinstance(data.get("connections"), list):
            messagebox.showerror(
                "Cannot open file",
                f'{path}\n\nThe file must contain an object with a "connections" list.',
                parent=self.root)
            return
        self.path, self.data, self.dirty = path, data, False
        self.clear_results()
        self.refresh()
        problems = self.problems()
        if problems:
            messagebox.showwarning(
                "Inventory has problems",
                "The file was opened, but these problems must be fixed before testing "
                "(double-click a row to edit it):\n\n" + "\n".join(problems[:20]),
                parent=self.root)
        self.set_status(f"Loaded {len(self.entries)} connection(s)")

    def save(self) -> bool:
        if not self.path:
            return self.save_as()
        try:
            save_json(self.data, self.path)
        except OSError as exc:
            messagebox.showerror("Save failed", str(exc), parent=self.root)
            return False
        self.dirty = False
        self.refresh()
        self.set_status(f"Saved {os.path.basename(self.path)}")
        return True

    def save_as(self) -> bool:
        path = filedialog.asksaveasfilename(
            parent=self.root, title="Save expected interconnect", defaultextension=".json",
            initialfile=os.path.basename(self.path) if self.path else "expected_interconnect.json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")])
        if not path:
            return False
        self.path = path
        return self.save()

    def problems(self) -> list[str]:
        if not self.entries:
            return []
        try:
            parse_inventory(self.data, "inventory")
        except InventoryError as exc:
            return [line.strip() for line in str(exc).splitlines()[1:]]
        return []

    def problems_for(self, index: int, data: Optional[dict] = None) -> list[str]:
        """Problems (including duplicates) that mention connection #index."""
        saved = self.data
        if data is not None:
            self.data = data
        try:
            pattern = re.compile(rf"connection #{index}(?!\d)")
            return [p for p in self.problems() if pattern.search(p)]
        finally:
            self.data = saved

    def selected_index(self) -> Optional[int]:
        selection = self.tree.selection()
        return int(selection[0]) if selection else None

    def add_connection(self) -> None:
        if self.running():
            return
        template = {"status": "connected"}
        index = self.selected_index()
        if index:
            # Pre-fill from the selected row to make entering a panel quick.
            current = self.entries[index - 1]
            template.update({k: current[k] for k in ("patch_panel", "switch") if k in current})
        new_index = len(self.entries) + 1
        dialog = ConnectionDialog(self.root, "Add connection", template,
                                  lambda e: self._dialog_problems(new_index, e, adding=True))
        self.root.wait_window(dialog)
        if dialog.result is not None:
            self.entries.append(dialog.result)
            self.changed(clear_all=False)
            self.tree.selection_set(str(new_index))
            self.tree.see(str(new_index))

    def edit_connection(self) -> None:
        index = self.selected_index()
        if self.running() or index is None:
            return
        dialog = ConnectionDialog(self.root, f"Edit connection #{index}", self.entries[index - 1],
                                  lambda e: self._dialog_problems(index, e))
        self.root.wait_window(dialog)
        if dialog.result is not None and dialog.result != self.entries[index - 1]:
            self.entries[index - 1] = dialog.result
            self.results.pop(index, None)
            self.changed(clear_all=False)
            self.tree.selection_set(str(index))

    def _dialog_problems(self, index: int, entry: dict, adding: bool = False) -> list[str]:
        problems = validate_entry(entry, index)
        if problems:
            return [p.split(": ", 1)[-1] for p in problems]
        candidate = copy.deepcopy(self.data)
        if adding:
            candidate["connections"].append(entry)
        else:
            candidate["connections"][index - 1] = entry
        dupes = [p for p in self.problems_for(index, candidate) if "duplicate" in p]
        return [re.sub(r"^connection #\d+[^:]*: ", "", p) for p in dupes]

    def delete_connection(self) -> None:
        index = self.selected_index()
        if self.running() or index is None:
            return
        entry = self.entries[index - 1]
        if not messagebox.askyesno(
                "Delete connection", f"Delete {_entry_label(entry, index)}?", parent=self.root):
            return
        del self.entries[index - 1]
        self.changed(clear_all=True)

    def accept_macs(self) -> None:
        if self.running():
            return
        filled = fill_discovered_macs(self.data, list(self.results.values()))
        if not filled:
            messagebox.showinfo(
                "Accept discovered MACs",
                "There are no newly discovered MACs to accept.\n\nRun a test first; MACs are "
                "only filled in for connections that don't have an expected MAC yet.",
                parent=self.root)
            return
        self.changed(clear_all=False)
        self.set_status(f"Filled in {filled} expected MAC(s) - review and Save")

    def changed(self, clear_all: bool) -> None:
        self.dirty = True
        if clear_all:
            self.clear_results()
        self.refresh()
        if self.port_window is not None and not self.port_window.running():
            self.port_window.replan()

    # ------------------------------------------------------- port verification

    def open_port_verify(self) -> None:
        if self.port_window is not None:
            self.port_window.lift()
            return
        if self.running():
            return
        if self.problems():
            messagebox.showerror("Fix the inventory first",
                                 "\n".join(self.problems()[:20]), parent=self.root)
            return
        self.port_window = PortVerifyWindow(self)

    def filtered(self, connections: list) -> list:
        """Apply the Switch / Patch panel filters chosen in the main window."""
        if self.switch_var.get() != ALL:
            connections = [c for c in connections if c.switch == self.switch_var.get()]
        if self.panel_var.get() != ALL:
            connections = [c for c in connections if c.patch_panel == self.panel_var.get()]
        return connections

    def filter_note(self) -> str:
        parts = [f"switch {v.get()}" for v in (self.switch_var,) if v.get() != ALL]
        parts += [f"patch panel {v.get()}" for v in (self.panel_var,) if v.get() != ALL]
        return f" (only {', '.join(parts)})" if parts else ""

    def add_results(self, results: list) -> None:
        """Merge results from elsewhere (port verification) into the table."""
        indexes = {r.connection.index for r in results}
        self.last_run = [r for r in self.last_run if r.connection.index not in indexes]
        self.last_run.extend(results)
        self.last_run.sort(key=lambda r: r.connection.index)
        for res in results:
            self.results[res.connection.index] = res
            self.update_row(res)
        self.update_summary()
        self.show_details()

    # ------------------------------------------------------------------- view

    def clear_results(self) -> None:
        self.results = {}
        self.last_run = []

    def refresh(self) -> None:
        name = os.path.basename(self.path) if self.path else "Untitled"
        self.root.title(f"{'*' if self.dirty else ''}{name} - {APP_TITLE}")
        self.table_frame.configure(
            text=f"Expected interconnect: {self.path or 'not saved yet'}"
                 f"  ({len(self.entries)} connections)")

        selection = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        for index, entry in enumerate(self.entries, start=1):
            self.tree.insert("", "end", iid=str(index), values=self.row_values(index, entry),
                             tags=self.row_tags(index, entry))
        if selection and self.tree.exists(selection[0]):
            self.tree.selection_set(selection[0])

        def values(field):
            return sorted({_display(e.get(field)).strip() for e in self.entries
                           if isinstance(e, dict) and _display(e.get(field)).strip()})

        switches, panels = values("switch"), values("patch_panel")
        self.switch_combo.configure(values=[ALL] + switches)
        self.panel_combo.configure(values=[ALL] + panels)
        if self.switch_var.get() not in [ALL] + switches:
            self.switch_var.set(ALL)
        if self.panel_var.get() not in [ALL] + panels:
            self.panel_var.set(ALL)
        self.update_summary()
        self.show_details()

    def row_values(self, index: int, entry) -> list[str]:
        if not isinstance(entry, dict):
            return [INVALID] + [""] * (len(COLUMNS) - 2) + ["Entry must be a JSON object"]
        res = self.results.get(index)
        row = result_row(res) if res else {}
        values = []
        for col in COLUMNS:
            if col in RESULT_COLUMNS:
                values.append(_display(row.get(col, "")))
            else:
                values.append(_display(entry.get(col)))
        problems = validate_entry(entry, index)
        if problems and not res:
            values[0] = INVALID
            values[-1] = "; ".join(p.split(": ", 1)[-1] for p in problems)
        return values

    def row_tags(self, index: int, entry) -> tuple:
        res = self.results.get(index)
        if res:
            return (res.result,)
        if not isinstance(entry, dict) or validate_entry(entry, index):
            return (INVALID,)
        return ()

    def update_row(self, res: CheckResult) -> None:
        iid = str(res.connection.index)
        if self.tree.exists(iid):
            self.tree.item(iid, values=self.row_values(res.connection.index,
                                                        self.entries[res.connection.index - 1]),
                           tags=(res.result,))

    def update_summary(self) -> None:
        counts = {name: 0 for name in self.summary_labels}
        for res in self.results.values():
            counts[res.result] = counts.get(res.result, 0) + 1
        for name, label in self.summary_labels.items():
            label.configure(text=f"{name}: {counts[name]}")

    def show_details(self) -> None:
        index = self.selected_index()
        lines = []
        if index is not None and index <= len(self.entries):
            entry = self.entries[index - 1]
            if isinstance(entry, dict):
                fields = [f"{FIELD_LABELS.get(k, k)}: {_display(v)}"
                          for k, v in entry.items() if v not in (None, "")]
                lines.append(f"#{index}   " + "   |   ".join(fields))
            res = self.results.get(index)
            if res and res.port_check:
                pc = res.port_check
                lines.append(f"{res.result}: {res.message}")
                lines.append(f"Test port: {pc.test_port}   |   Link: {pc.link or '-'}   |   "
                             f"Seen on: {(pc.seen_switch + ' ' + pc.seen_port).strip() or '-'}"
                             + (f" ({pc.protocol})" if pc.protocol else "")
                             + (f"   |   Cable test: {pc.cable_test}" if pc.cable_test else ""))
            elif res:
                ping = (f"{res.ping.replies}/{res.ping.sent} replies"
                        + (f", avg {res.ping.avg_rtt_ms} ms" if res.ping.avg_rtt_ms else "")
                        if res.ping else "not pinged")
                lines.append(f"{res.result}: {res.message}")
                lines.append(f"Ping: {ping}   |   Discovered MAC: {res.discovered_mac or '-'}"
                             f"   |   MAC check: {res.mac_check}")
            else:
                lines.extend(self.problems_for(index) or ["Not tested yet."])
        else:
            lines.append("Select a row to see its details. Double-click a row to edit it.")
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("1.0", "\n".join(lines))
        self.details.configure(state="disabled")

    def sort_by(self, col: str) -> None:
        last_col, descending = self.sort_state
        descending = not descending if col == last_col else False
        self.sort_state = (col, descending)

        def key(iid):
            value = self.tree.set(iid, col)
            # Natural sort so "Gi1/0/10" follows "Gi1/0/9".
            return [(0, int(t), "") if t.isdigit() else (1, 0, t.lower())
                    for t in re.split(r"(\d+)", value)]

        for pos, iid in enumerate(sorted(self.tree.get_children(), key=key, reverse=descending)):
            self.tree.move(iid, "", pos)
        for c, (heading, _w, _a) in COLUMNS.items():
            arrow = (" ▼" if descending else " ▲") if c == col else ""
            self.tree.heading(c, text=heading + arrow)

    def show_help(self) -> None:
        messagebox.showinfo("How to use", (
            "1. Open an expected interconnect file (File > Open), or build one with "
            "Add Connection: one row per patch panel port.\n\n"
            "2. Mark ports with nothing patched in as 'unused'. Give each connected "
            "device its IP address, and its MAC if known.\n\n"
            "3. Press Ping Test (F5). Each device is pinged and its MAC read from this "
            "computer's ARP table, then compared with the expected MAC.\n\n"
            "   PASS  reachable and MAC matches (or newly discovered)\n"
            "   FAIL  unreachable, or a different MAC answered\n"
            "   WARN  reachable but MAC unknown, or ARP-only reply\n"
            "   SKIP  unused port or no IP address\n\n"
            "4. Use 'Accept Discovered MACs' to record MACs you didn't have yet, then Save.\n\n"
            "5. Use 'Verify Unused Ports' to check unused runs with a test switch: patch "
            "its ports to the runs it lists, and it reads which production switch port "
            "each run lands on (CDP/LLDP).\n\n"
            "Run from a computer on the same subnet/VLAN as the devices - MACs are only "
            "visible for devices on the local network segment."), parent=self.root)

    # ------------------------------------------------------------------- test

    def running(self) -> bool:
        if self.port_window is not None and self.port_window.running():
            return True
        return self.worker is not None and self.worker.is_alive()

    def run_test(self) -> None:
        if self.running():
            return
        if not self.entries:
            messagebox.showinfo("Nothing to test", "Add or open some connections first.",
                                parent=self.root)
            return
        try:
            connections = parse_inventory(self.data, "inventory")
        except InventoryError as exc:
            messagebox.showerror(
                "Fix the inventory first",
                "\n".join(str(exc).splitlines()[1:21]), parent=self.root)
            return
        try:
            count = int(self.count_var.get())
            timeout = float(self.timeout_var.get())
            workers = int(self.workers_var.get())
            if count < 1 or timeout <= 0 or workers < 1:
                raise ValueError
        except (tk.TclError, ValueError):
            messagebox.showerror(
                "Invalid settings",
                "Pings and Parallel must be whole numbers of 1 or more, "
                "and Timeout must be greater than 0.", parent=self.root)
            return

        # Keep port verification results; pinging can't add anything to those rows.
        verified = {i: r for i, r in self.results.items() if r.port_check is not None}
        connections = [c for c in self.filtered(connections) if c.index not in verified]
        if not connections:
            messagebox.showinfo("Nothing to test", "No connections match the selected filters.",
                                parent=self.root)
            return
        if any(c.ip and c.status != STATUS_UNUSED for c in connections) \
                and not shutil.which("ping"):
            messagebox.showerror("Ping not available",
                                 "The 'ping' command was not found on this computer.",
                                 parent=self.root)
            return

        self.clear_results()
        self.results = dict(verified)
        self.last_run = list(verified.values())
        self.refresh()
        for conn in connections:
            self.tree.set(str(conn.index), "result", PENDING)
        self.progress.configure(maximum=len(connections), value=0)
        self.set_status(f"Testing {len(connections)} connection(s)...")
        self.set_running(True)
        self.stop_event = threading.Event()

        def work():
            try:
                results = check_all(connections, count=count, timeout_s=timeout,
                                    workers=workers, progress=self.events.put,
                                    stop_event=self.stop_event)
                self.events.put(("done", results))
            except Exception as exc:  # report anything unexpected in the GUI
                self.events.put(("error", exc))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()
        self.root.after(100, self.poll)

    def poll(self) -> None:
        finished = None
        try:
            while True:
                item = self.events.get_nowait()
                if isinstance(item, CheckResult):
                    self.results[item.connection.index] = item
                    self.update_row(item)
                    self.progress.step(1)
                else:
                    finished = item
        except queue.Empty:
            pass
        self.update_summary()
        if finished is None:
            self.root.after(100, self.poll)
            return

        self.set_running(False)
        kind, payload = finished
        if kind == "error":
            messagebox.showerror("Test failed", f"Unexpected error: {payload}", parent=self.root)
            self.set_status("Test failed")
            return
        self.last_run = sorted(self.last_run + payload, key=lambda r: r.connection.index)
        self.show_details()
        fails = sum(r.result == FAIL for r in payload)
        stopped = " (stopped)" if self.stop_event.is_set() else ""
        self.set_status(f"Finished{stopped}: {len(payload)} tested, {fails} failed")

    def stop_test(self) -> None:
        if self.running():
            self.stop_event.set()
            self.set_status("Stopping - waiting for pings in progress...")

    def set_running(self, running: bool) -> None:
        state = "disabled" if running else "normal"
        self.run_button.configure(state=state)
        self.export_button.configure(state=state)
        for button in self.edit_buttons:
            button.configure(state=state)
        self.stop_button.configure(state="normal" if running else "disabled")
        self.root.configure(cursor="watch" if running else "")

    def export_report(self) -> None:
        if self.running():
            return
        if not self.last_run:
            messagebox.showinfo("Export report", "Run a test first.", parent=self.root)
            return
        path = filedialog.asksaveasfilename(
            parent=self.root, title="Export test report", defaultextension=".csv",
            initialfile="interconnect_report.csv",
            filetypes=[("CSV (Excel)", "*.csv"), ("JSON", "*.json")])
        if not path:
            return
        # Use the latest result per connection (edits may have cleared some).
        results = [self.results[r.connection.index] for r in self.last_run
                   if r.connection.index in self.results]
        try:
            write_report(results, path, self.path or "unsaved inventory")
        except OSError as exc:
            messagebox.showerror("Export failed", str(exc), parent=self.root)
            return
        self.set_status(f"Report written to {path}")

    def quit(self) -> None:
        if self.running():
            if not messagebox.askyesno("Test running", "A test is running. Quit anyway?",
                                       parent=self.root):
                return
            self.stop_event.set()
        if self.confirm_discard():
            self.root.destroy()


def main(argv: Optional[list[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    root = tk.Tk()
    InterconnectApp(root, argv[0] if argv else None)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
