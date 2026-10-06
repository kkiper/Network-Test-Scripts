"""GUI window for verifying unused patch panel runs with the test switch."""

from __future__ import annotations

import csv
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Optional

from .checker import FAIL, PASS, SKIP, WARN, CheckResult
from .cisco import expand_port_range, short_interface
from .inventory import InventoryError, parse_inventory
from .portverify import Assignment, TestSwitchSettings, plan_batches, preflight, verify_batch
from .switch import SwitchError, connect

COLUMNS = {
    "test_port": ("Test Port", 75, "w"),
    "connect_to": ("Connect To", 170, "w"),
    "panel": ("Panel:Port", 85, "w"),
    "expected": ("Expected Switch Port", 170, "w"),
    "status": ("Result", 70, "center"),
    "link": ("Link", 110, "w"),
    "seen": ("Seen On", 150, "w"),
    "cable": ("Cable Test / Optics", 245, "w"),
    "errors": ("Errors", 70, "center"),
    "detail": ("Detail", 380, "w"),
}
ROW_STYLES = {
    PASS: {"background": "#d9f2d9"},
    FAIL: {"background": "#f8d0d0"},
    WARN: {"background": "#fbefc4"},
    SKIP: {"foreground": "#777777"},
    "busy": {"background": "#e3ecfa"},
}


class PortVerifyWindow(tk.Toplevel):
    """Walks the user through cabling and verifying unused runs batch by batch."""

    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.title("Verify Unused Ports - Test Switch")
        self.geometry("1250x640")
        self.minsize(900, 450)
        self.session = None
        self.worker: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.events: "queue.Queue" = queue.Queue()
        self.batches: list[list[Assignment]] = []
        self.unverifiable: list[CheckResult] = []
        self.batch_no = 0

        try:
            self.settings = TestSwitchSettings.from_inventory(app.data)
        except InventoryError:
            self.settings = TestSwitchSettings()

        self._build_settings()
        self._build_batch_bar()
        self._build_table()
        self._build_actions()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.replan()

    # ------------------------------------------------------------------ layout

    def _build_settings(self) -> None:
        frame = ttk.LabelFrame(self, text="Test switch (Catalyst, test ports configured as "
                                          "routed ports - see docs/test_switch_c9200.cfg)",
                               padding=8)
        frame.pack(fill="x", padx=8, pady=8)
        s = self.settings
        self.host_var = tk.StringVar(value=s.host)
        self.user_var = tk.StringVar(value=s.username)
        self.password_var = tk.StringVar()
        self.secret_var = tk.StringVar()
        self.ports_var = tk.StringVar(value=", ".join(short_interface(p) for p in s.ports)
                                      or "Gi1/0/1-22")
        self.fiber_var = tk.StringVar(value=", ".join(short_interface(p) for p in s.fiber_ports))
        self.soak_var = tk.IntVar(value=int(s.fiber_soak))
        self.timeout_var = tk.IntVar(value=int(s.cdp_timeout))
        self.tdr_var = tk.BooleanVar(value=s.cable_test)
        self.clear_var = tk.BooleanVar(value=s.clear_tables)

        def field(row, col, label, var, width, show=None):
            ttk.Label(frame, text=label).grid(row=row, column=col, sticky="w", padx=(0, 4), pady=3)
            entry = ttk.Entry(frame, textvariable=var, width=width, show=show)
            entry.grid(row=row, column=col + 1, sticky="w", padx=(0, 14), pady=3)
            return entry

        field(0, 0, "Management IP:", self.host_var, 18)
        field(0, 2, "Username:", self.user_var, 14)
        field(0, 4, "Password:", self.password_var, 14, show="•")
        field(0, 6, "Enable secret:", self.secret_var, 14, show="•")
        for row, label, var in ((1, "Copper test ports:", self.ports_var),
                                (2, "Fiber (SFP+) ports:", self.fiber_var)):
            entry = field(row, 0, label, var, 18)
            entry.bind("<FocusOut>", lambda _e: self.replan())
            entry.bind("<Return>", lambda _e: self.replan())
        ttk.Label(frame, text="Fiber error check (s):").grid(row=2, column=2, sticky="w")
        ttk.Spinbox(frame, from_=0, to=3600, increment=30, textvariable=self.soak_var,
                    width=6).grid(row=2, column=3, sticky="w")
        ttk.Label(frame, text="e.g. Te1/1/1-2 - leave blank if there are no fiber runs",
                  foreground="#555555").grid(row=2, column=4, columnspan=4, sticky="w")
        ttk.Label(frame, text="CDP wait (s):").grid(row=1, column=2, sticky="w")
        ttk.Spinbox(frame, from_=20, to=600, increment=10, textvariable=self.timeout_var,
                    width=6).grid(row=1, column=3, sticky="w")
        ttk.Checkbutton(frame, text="Cable test (TDR)", variable=self.tdr_var).grid(
            row=1, column=4, columnspan=2, sticky="w")
        ttk.Checkbutton(frame, text="Clear CDP/LLDP tables per batch",
                        variable=self.clear_var).grid(row=1, column=6, columnspan=2, sticky="w")
        self.connect_button = ttk.Button(frame, text="Connect", command=self.connect)
        self.connect_button.grid(row=0, column=8, rowspan=3, padx=(6, 0), sticky="ns")
        # The fiber test only sees one direction: the test switch can read its own
        # SFP+ modules, but not the production switch's (no login there).
        self.fiber_note = ttk.Label(frame, foreground="#8a5a1c", justify="left",
                                    wraplength=1000, text=(
            "Note: the fiber test can only measure what arrives FROM the ESS 3300 (its transmit "
            "light, as received by the test switch, and receive errors). The other direction, "
            "test switch to ESS 3300, is confirmed only by the link coming up at the expected "
            "speed: the ESS 3300's own receive level, errors and module readings can't be read "
            "without logging into it."))
        self.fiber_note.grid(row=3, column=0, columnspan=8, sticky="w", pady=(4, 0))
        self.conn_status = ttk.Label(frame, text="Not connected", foreground="#555555")
        self.conn_status.grid(row=4, column=0, columnspan=9, sticky="w", pady=(4, 0))

    def _build_batch_bar(self) -> None:
        bar = ttk.Frame(self, padding=(8, 0))
        bar.pack(fill="x")
        self.prev_button = ttk.Button(bar, text="◀ Previous", command=lambda: self.goto(-1))
        self.prev_button.pack(side="left")
        self.batch_label = ttk.Label(bar, text="", font=("TkDefaultFont", 11, "bold"))
        self.batch_label.pack(side="left", padx=10)
        self.next_button = ttk.Button(bar, text="Next ▶", command=lambda: self.goto(1))
        self.next_button.pack(side="left")
        ttk.Button(bar, text="Export Cabling Plan...", command=self.export_plan).pack(side="right")
        self.instructions = ttk.Label(self, text="", padding=(8, 6), foreground="#1f4e8c",
                                      wraplength=1200, justify="left")
        self.instructions.pack(fill="x")

    def _build_table(self) -> None:
        frame = ttk.Frame(self, padding=(8, 0))
        frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(frame, columns=list(COLUMNS), show="headings",
                                 selectmode="browse")
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
        bar = ttk.Frame(self, padding=8)
        bar.pack(fill="x")
        self.verify_button = ttk.Button(bar, text="Verify Batch", style="Run.TButton",
                                        command=self.verify, state="disabled")
        self.verify_button.pack(side="left")
        self.stop_button = ttk.Button(bar, text="Stop", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=4)
        self.progress_label = ttk.Label(bar, text="")
        self.progress_label.pack(side="left", padx=10)
        ttk.Button(bar, text="Close", command=self.close).pack(side="right")

    # -------------------------------------------------------------- planning

    def read_settings(self, quiet: bool = False) -> Optional[TestSwitchSettings]:
        try:
            ports = expand_port_range(self.ports_var.get())
            fiber_ports = expand_port_range(self.fiber_var.get())
            timeout = int(self.timeout_var.get())
            soak = int(self.soak_var.get())
            if not ports and not fiber_ports:
                raise ValueError("enter the test switch ports to use, e.g. Gi1/0/1-22 "
                                 "(and Te1/1/1-2 for fiber runs)")
            if timeout < 10:
                raise ValueError("CDP wait must be at least 10 seconds")
            if soak < 0:
                raise ValueError("the fiber error check can't be negative")
        except (ValueError, tk.TclError) as exc:
            if not quiet:
                messagebox.showerror("Test ports", f"Invalid setting: {exc}", parent=self)
            return None
        settings = TestSwitchSettings(**{**vars(self.settings),
                                         "ports": [], "fiber_ports": []})
        settings.host = self.host_var.get().strip()
        settings.username = self.user_var.get().strip()
        settings.ports = ports
        settings.fiber_ports = fiber_ports
        settings.fiber_soak = soak
        settings.cdp_timeout = timeout
        settings.cable_test = bool(self.tdr_var.get())
        settings.clear_tables = bool(self.clear_var.get())
        return settings

    def replan(self) -> None:
        if self.running():
            return
        settings = self.read_settings(quiet=True)
        try:
            connections = parse_inventory(self.app.data, "inventory")
        except InventoryError:
            connections = []
            self.instructions.configure(
                text="The inventory has problems - fix them in the main window first.")
        connections = self.app.filtered(connections)
        if settings is None:
            self.batches, self.unverifiable = [], []
            self.instructions.configure(text="Enter valid test ports, e.g. Gi1/0/1-22.")
        else:
            self.batches, self.unverifiable = plan_batches(connections, settings.ports,
                                                           settings.fiber_ports)
            self.app.add_results(self.unverifiable)
        self.batch_no = min(self.batch_no, max(len(self.batches) - 1, 0))
        self.show_batch()

    def goto(self, step: int) -> None:
        if not self.running():
            self.batch_no = max(0, min(len(self.batches) - 1, self.batch_no + step))
            self.show_batch()

    def show_batch(self) -> None:
        self.tree.delete(*self.tree.get_children())
        total = len(self.batches)
        runs = sum(len(b) for b in self.batches)
        if not total:
            self.batch_label.configure(text="Nothing to verify")
            if not str(self.instructions.cget("text")).startswith(("The inventory", "Enter")):
                self.instructions.configure(
                    text="There are no unused runs with a switch port in the inventory"
                         + self.app.filter_note() + ".")
            self.update_buttons()
            return
        batch = self.batches[self.batch_no]
        self.batch_label.configure(text=f"Batch {self.batch_no + 1} of {total}")
        self.instructions.configure(text=(
            f"{runs} unused run(s){self.app.filter_note()} in {total} batch(es). "
            f"Patch each test port below to the run shown in 'Connect To', then press "
            f"Verify Batch. Disconnect the cables before moving to the next batch."))
        for a in batch:
            self.tree.insert("", "end", iid=a.test_port, values=self.row(a),
                             tags=self.tags(a))
        self.update_buttons()

    def row(self, a: Assignment, interim: str = "") -> list[str]:
        c = a.connection
        res = self.app.results.get(c.index)
        pc = res.port_check if res else None
        return [
            a.test_port_short,
            c.connect_point,
            f"{c.patch_panel}:{c.panel_port}",
            f"{c.switch} {c.switch_port}".strip(),
            res.result if res and pc else "",
            pc.link if pc else "",
            f"{pc.seen_switch} {pc.seen_port}".strip() if pc else "",
            (pc.optics or pc.cable_test) if pc else "",
            pc.errors if pc else "",
            interim or (res.message if res and pc else ""),
        ]

    def tags(self, a: Assignment) -> tuple:
        res = self.app.results.get(a.connection.index)
        return (res.result,) if res and res.port_check else ()

    def export_plan(self) -> None:
        if not self.batches:
            messagebox.showinfo("Cabling plan", "Nothing to verify.", parent=self)
            return
        path = filedialog.asksaveasfilename(
            parent=self, title="Export cabling plan", defaultextension=".csv",
            initialfile="test_switch_cabling_plan.csv", filetypes=[("CSV (Excel)", "*.csv")])
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(["batch", "test_port", "connect_to", "patch_panel", "panel_port",
                                 "expected_switch", "expected_port"])
                for number, batch in enumerate(self.batches, start=1):
                    for a in batch:
                        c = a.connection
                        writer.writerow([number, a.test_port_short, c.connect_point,
                                         c.patch_panel, c.panel_port, c.switch, c.switch_port])
        except OSError as exc:
            messagebox.showerror("Export failed", str(exc), parent=self)

    # ------------------------------------------------------------ switch I/O

    def running(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def update_buttons(self) -> None:
        busy = self.running()
        self.connect_button.configure(state="disabled" if busy else "normal")
        self.verify_button.configure(
            state="normal" if self.session and self.batches and not busy else "disabled")
        self.stop_button.configure(state="normal" if busy else "disabled")
        self.prev_button.configure(state="normal" if self.batch_no > 0 and not busy else "disabled")
        self.next_button.configure(
            state="normal" if self.batch_no < len(self.batches) - 1 and not busy else "disabled")

    def start(self, target) -> None:
        self.stop_event = threading.Event()
        self.worker = threading.Thread(target=target, daemon=True)
        self.worker.start()
        self.update_buttons()
        self.after(100, self.poll)

    def connect(self) -> None:
        settings = self.read_settings()
        if settings is None or self.running():
            return
        if not settings.host or not settings.username:
            messagebox.showerror("Test switch", "Enter the management IP and username.",
                                 parent=self)
            return
        self.save_settings(settings)
        self.replan()
        password, secret = self.password_var.get(), self.secret_var.get()
        old, self.session = self.session, None
        self.conn_status.configure(text=f"Connecting to {settings.host}...", foreground="#555555")

        def work():
            if old is not None:
                old.close()
            try:
                session = connect(settings, password, secret)
                errors, warnings = preflight(session, settings)
                self.events.put(("connected", session, errors, warnings,
                                 getattr(session, "hostname", "")))
            except SwitchError as exc:
                self.events.put(("connect_failed", str(exc)))

        self.start(work)

    def save_settings(self, settings: TestSwitchSettings) -> None:
        """Remember the switch settings in the inventory (never the password)."""
        self.settings = settings
        new = settings.to_dict()
        if self.app.data.get("test_switch") != new:
            self.app.data["test_switch"] = new
            self.app.changed(clear_all=False)

    def verify(self) -> None:
        settings = self.read_settings()
        if settings is None or self.running() or not self.session or not self.batches:
            return
        self.save_settings(settings)
        batch = self.batches[self.batch_no]
        for a in batch:
            self.tree.item(a.test_port, values=self.row(a, "Starting..."), tags=("busy",))
        session = self.session

        def work():
            try:
                results = verify_batch(
                    session, batch, settings, stop_event=self.stop_event,
                    progress=lambda states, left: self.events.put(("progress", states, left)))
                self.events.put(("verified", results))
            except SwitchError as exc:
                self.events.put(("verify_failed", str(exc)))
            except Exception as exc:  # report anything unexpected in the GUI
                self.events.put(("verify_failed", f"Unexpected error: {exc}"))

        self.progress_label.configure(text="Verifying...")
        self.start(work)

    def stop(self) -> None:
        self.stop_event.set()
        self.progress_label.configure(text="Stopping...")

    def poll(self) -> None:
        if not self.winfo_exists():
            return
        try:
            while True:
                self.handle(self.events.get_nowait())
        except queue.Empty:
            pass
        if self.running() or not self.events.empty():
            self.after(100, self.poll)
        else:
            self.update_buttons()

    def handle(self, event) -> None:
        kind = event[0]
        if kind == "progress":
            _kind, states, left = event
            batch = {a.test_port: a for a in self.batches[self.batch_no]}
            for port, state in states.items():
                if port in batch and self.tree.exists(port):
                    self.tree.item(port, values=self.row(batch[port], state), tags=("busy",))
            heard = sum(s.startswith("Heard") for s in states.values())
            self.progress_label.configure(
                text=f"{heard}/{len(states)} heard - waiting up to {left:.0f} s more")
        elif kind == "verified":
            results = event[1]
            self.app.add_results(results)
            self.show_batch()
            fails = sum(r.result == FAIL for r in results)
            warns = sum(r.result == WARN for r in results)
            more = " Disconnect the cables and press Next." \
                if self.batch_no < len(self.batches) - 1 else " That was the last batch."
            self.progress_label.configure(
                text=f"Batch done: {len(results) - fails - warns} verified, {fails} failed, "
                     f"{warns} warning(s).{more}")
        elif kind == "verify_failed":
            self.show_batch()
            self.progress_label.configure(text="Verification failed")
            messagebox.showerror("Verification failed", event[1], parent=self)
        elif kind == "connected":
            _kind, session, errors, warnings, prompt = event
            name = prompt or self.settings.host
            if errors:
                session.close()
                self.conn_status.configure(text=f"Connected to {name}, but it isn't ready.",
                                           foreground="#b00020")
                messagebox.showerror(
                    "Test switch not ready",
                    "Fix these on the test switch, then press Connect again:\n\n- "
                    + "\n- ".join(errors), parent=self)
                return
            self.session = session
            text = f"Connected to {name} ({self.settings.host}) - ready."
            if warnings:
                text += "  Note: " + " ".join(warnings)
            self.conn_status.configure(text=text, foreground="#1e7b1e")
        elif kind == "connect_failed":
            self.conn_status.configure(text="Not connected", foreground="#b00020")
            messagebox.showerror("Could not connect", event[1], parent=self)

    def close(self) -> None:
        if self.running():
            messagebox.showinfo("Busy", "Stop the verification (or wait for it) before closing.",
                                parent=self)
            return
        if self.session is not None:
            threading.Thread(target=self.session.close, daemon=True).start()
            self.session = None
        self.app.port_window = None
        self.destroy()
