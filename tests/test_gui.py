import json
import os
import tempfile
import time
import unittest
from unittest import mock

try:
    import tkinter as tk
    from netcheck import gui
except ImportError:  # Python built without Tk
    tk = None

from netcheck import checker
from netcheck.ping import PingResult

EXAMPLE = os.path.join(os.path.dirname(__file__), "..", "examples", "expected_interconnect.json")
MACS = {"192.168.1.1": "00:1a:2b:3c:4d:01", "192.168.1.10": "00:1a:2b:3c:4d:99",
        "192.168.1.11": "00:1a:2b:3c:4d:11", "192.168.1.50": "00:1a:2b:3c:4d:50"}


def fake_check_all(conns, **kw):
    def ping(ip, count, timeout_s):
        return PingResult(ip in MACS, count if ip in MACS else 0, count, 0.5)
    return checker.check_all(conns, ping_fn=ping, mac_fn=MACS.get, **kw)


@unittest.skipIf(tk is None, "tkinter not available")
class GuiTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"no display: {exc}")
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        patches = [
            mock.patch.object(gui, "check_all", fake_check_all),
            mock.patch.object(gui.shutil, "which", return_value="/bin/ping"),
            mock.patch.object(gui.messagebox, "showwarning"),
            mock.patch.object(gui.messagebox, "showerror"),
            mock.patch.object(gui.messagebox, "showinfo"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.app = gui.InterconnectApp(self.root, EXAMPLE)

    def wait_for_run(self):
        deadline = time.time() + 10
        while (self.app.running() or not self.app.last_run) and time.time() < deadline:
            self.root.update()
            time.sleep(0.02)
        self.root.update()

    def test_run_accept_and_save(self):
        self.assertEqual(len(self.app.tree.get_children()), 8)
        self.app.run_test()
        self.wait_for_run()
        self.assertEqual(self.app.tree.set("1", "result"), "PASS")
        self.assertEqual(self.app.tree.set("2", "mac_check"), "MISMATCH")
        self.assertEqual(self.app.summary_labels["FAIL"].cget("text"), "FAIL: 2")

        self.app.accept_macs()
        self.assertTrue(self.app.dirty)
        # Only the missing MAC is filled; the mismatched one is left alone.
        self.assertEqual(self.app.entries[2]["expected_mac"], "00:1a:2b:3c:4d:11")
        self.assertEqual(self.app.entries[1]["expected_mac"], "00:1a:2b:3c:4d:10")

        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.addCleanup(os.remove, path)
        self.app.path = path
        self.assertTrue(self.app.save())
        with open(path) as fh:
            self.assertEqual(json.load(fh)["connections"][2]["expected_mac"], "00:1a:2b:3c:4d:11")

    def test_filter_by_switch(self):
        self.app.switch_var.set("SW-ACCESS-01")
        self.app.run_test()
        self.wait_for_run()
        self.assertEqual(len(self.app.last_run), 2)
        self.assertEqual(self.app.tree.set("1", "result"), "")

    def test_invalid_entry_blocks_run(self):
        self.app.entries.append({"ip": "not-an-ip"})
        self.app.refresh()
        self.assertEqual(self.app.tree.set("9", "result"), gui.INVALID)
        self.app.run_test()
        self.assertFalse(self.app.running())
        gui.messagebox.showerror.assert_called_once()

    def test_dialog_rejects_duplicates(self):
        problems = self.app._dialog_problems(2, dict(self.app.entries[1], ip="192.168.1.1"))
        self.assertEqual(problems, ["duplicate IP address '192.168.1.1' (also on connection #1)"])
        self.assertEqual(self.app._dialog_problems(2, self.app.entries[1]), [])


if __name__ == "__main__":
    unittest.main()
