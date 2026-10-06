import gc
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

# A frozen copy of the example inventory, so the shipped example can change freely.
EXAMPLE = os.path.join(os.path.dirname(__file__), "fixtures", "sample_inventory.json")
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
        # Cleanups run last-first: destroy the window, drop every reference to the
        # Tk objects, then collect them here on the main thread. Otherwise a later
        # test's worker thread may trigger the collection, and Tk aborts when its
        # interpreter is freed outside the main thread.
        self.addCleanup(self._release_tk)
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

    def _release_tk(self):
        for name in ("app", "root"):
            self.__dict__.pop(name, None)
        gc.collect()

    def wait_for_run(self):
        deadline = time.time() + 10
        while (self.app.running() or not self.app.events.empty()) and time.time() < deadline:
            self.root.update()
            time.sleep(0.02)
        self.root.update()

    def test_run_accept_and_save(self):
        self.assertEqual(len(self.app.tree.get_children()), 10)
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
        self.assertEqual(self.app.tree.set("11", "result"), gui.INVALID)
        self.app.run_test()
        self.assertFalse(self.app.running())
        gui.messagebox.showerror.assert_called_once()

    def test_dialog_rejects_duplicates(self):
        problems = self.app._dialog_problems(2, dict(self.app.entries[1], ip="192.168.1.1"))
        self.assertEqual(problems, ["duplicate IP address '192.168.1.1' (also on connection #1)"])
        self.assertEqual(self.app._dialog_problems(2, self.app.entries[1]), [])


    def test_connection_dialog_help(self):
        dialog = gui.ConnectionDialog(self.root, "Add connection", {"status": "connected"},
                                      lambda entry: [])
        self.addCleanup(dialog.destroy)
        self.assertEqual(set(gui.FIELD_HELP), set(gui.KNOWN_FIELDS))
        self.assertFalse(dialog.help_visible)

        dialog.toggle_help()
        self.assertTrue(dialog.help_visible)
        self.assertTrue(dialog.help_text().startswith("Patch panel"))

        # The open panel follows the field being edited.
        dialog._focus_help("expected_mac")
        lines = dialog.help_text().splitlines()
        self.assertEqual(lines[0], "Expected MAC")
        self.assertIn("001a.2b3c.4d5e", lines)  # each example on its own line

        dialog.toggle_help()
        self.assertFalse(dialog.help_visible)
        dialog.show_help("far_end")  # the ? button next to a field
        self.assertTrue(dialog.help_visible)
        self.assertTrue(dialog.help_text().startswith("Test point"))


class FakeTestSwitch:
    """Test switch where test port 1 lands correctly and port 2 on the wrong port."""

    privileged = True
    hostname = "NETTEST"

    def run(self, command):
        return ""

    def send(self, command):
        from tests.test_fiber import dom_output, te_status
        from tests.test_portverify import STATUS_HEADER, cdp_entry, status_line
        if command == "show interfaces status":
            # Fiber channel 1 is good; channel 2 has its strands reversed (no light, no link).
            return (STATUS_HEADER + status_line("Gi1/0/1") + status_line("Gi1/0/2")
                    + te_status("Te1/1/1") + te_status("Te1/1/2", "notconnect"))
        if command == "show cdp neighbors detail":
            return (cdp_entry("SW-CORE-01", "Gi1/0/1", "Gi1/0/5")
                    + cdp_entry("SW-CORE-01", "Gi1/0/2", "Gi1/0/16")
                    + cdp_entry("SW-CORE-01", "Te1/1/1", "TenGigabitEthernet1/1"))
        if command == "show interfaces transceiver detail":
            return dom_output({"Te1/1/1": (-2.8, -2.3), "Te1/1/2": (-40.0, -2.4)})
        return ""

    def close(self):
        pass


@unittest.skipIf(tk is None, "tkinter not available")
class PortVerifyGuiTests(GuiTests):
    def wait(self, window):
        deadline = time.time() + 10
        while (window.running() or not window.events.empty()) and time.time() < deadline:
            self.root.update()
            time.sleep(0.02)
        self.root.update()

    def test_verify_unused_ports(self):
        from netcheck import gui_portverify
        with mock.patch.object(gui_portverify, "connect", return_value=FakeTestSwitch()), \
                mock.patch.object(gui_portverify.messagebox, "showerror") as error, \
                mock.patch("netcheck.portverify.POLL_INTERVAL_S", 0.05), \
                mock.patch("netcheck.portverify.LINK_GRACE_S", 0.2):
            self.app.open_port_verify()
            window = self.app.port_window
            window.host_var.set("10.0.0.2")
            window.user_var.set("admin")
            window.ports_var.set("Gi1/0/1-2")
            self.assertEqual(window.fiber_var.get(), "Te1/1/1, Te1/1/2")  # from the inventory
            window.soak_var.set(0)
            window.replan()
            # Copper PP-A:5/6 and fiber channels 1/2 are verified in the same batch.
            self.assertEqual([[(a.connection.patch_panel, a.connection.panel_port) for a in b]
                              for b in window.batches],
                             [[("PP-A", "5"), ("PP-A", "6"), ("FIBER", "1"), ("FIBER", "2")]])
            window.connect()
            self.wait(window)
            error.assert_not_called()
            self.assertIn("Connected to NETTEST", window.conn_status.cget("text"))
            self.assertEqual(self.app.data["test_switch"]["ports"], ["Gi1/0/1", "Gi1/0/2"])
            self.assertEqual(self.app.data["test_switch"]["fiber_soak"], 0)
            self.assertNotIn("password", json.dumps(self.app.data["test_switch"]))

            window.verify()
            self.wait(window)
            self.assertEqual(window.tree.set("GigabitEthernet1/0/1", "status"), "PASS")
            self.assertEqual(window.tree.set("GigabitEthernet1/0/2", "status"), "FAIL")
            fiber1 = "TenGigabitEthernet1/1/1"
            self.assertEqual(window.tree.set(fiber1, "status"), "PASS")
            self.assertEqual(window.tree.set(fiber1, "link"), "up 10 Gb/s")
            self.assertEqual(window.tree.set(fiber1, "cable"),
                             "Rx -2.8 dBm, 7.1 dB margin; Tx -2.3 dBm")
            self.assertEqual(window.tree.set("TenGigabitEthernet1/1/2", "status"), "FAIL")
            self.assertIn("swap the P and S strands",
                          window.tree.set("TenGigabitEthernet1/1/2", "detail"))
            # Results show in the main table too.
            self.assertEqual(self.app.tree.set("5", "result"), "PASS")
            self.assertEqual(self.app.tree.set("6", "result"), "FAIL")
            window.close()

        # A later ping test keeps the port verification results.
        self.app.run_test()
        self.wait_for_run()
        self.assertEqual(self.app.tree.set("6", "result"), "FAIL")
        self.assertIn("Gi1/0/16", self.app.tree.set("6", "message"))
        self.assertEqual({r.connection.index for r in self.app.last_run}, set(range(1, 11)))

    # Don't re-run the inherited ping tests in this class.
    test_run_accept_and_save = test_filter_by_switch = None
    test_invalid_entry_blocks_run = test_dialog_rejects_duplicates = None



@unittest.skipIf(tk is None, "tkinter not available")
class DiscoverGuiTests(GuiTests):
    def test_discover_and_add(self):
        from netcheck import gui_discover
        from tests.test_discover import ARP, fake_ping
        real = gui_discover.discover

        def fake_discover(connections, networks, **kw):
            return real(connections, networks, ping_fn=fake_ping, arp_fn=lambda: ARP,
                        local_ips={"192.168.1.5"}, **kw)

        with mock.patch.object(gui_discover, "discover", fake_discover), \
                mock.patch.object(gui_discover.shutil, "which", return_value="/bin/ping"), \
                mock.patch.object(gui_discover, "local_addresses", return_value={"192.168.1.5"}), \
                mock.patch.object(gui_discover.messagebox, "askokcancel",
                                  return_value=True) as confirm:
            gui_discover.DiscoverWindow.scan_confirmed = False
            self.app.open_discover()
            window = self.app.discover_window
            self.assertEqual(window.subnets_var.get(), "192.168.1.0/24")  # from the inventory
            window.start()
            deadline = time.time() + 10
            while (window.running() or not window.events.empty()) and time.time() < deadline:
                self.root.update()
                time.sleep(0.02)
            self.root.update()
            confirm.assert_called_once()

            shown = {window.tree.set(i, "ip"): window.tree.set(i, "category")
                     for i in window.tree.get_children()}
            self.assertEqual(shown["192.168.1.200"], "UNKNOWN")
            self.assertEqual(shown["192.168.1.77"], "MOVED")
            self.assertEqual(shown["192.168.1.10"], "MAC CONFLICT")
            self.assertNotIn("192.168.1.1", shown)  # expected rows hidden by default
            self.assertEqual(window.summary_labels["EXPECTED"].cget("text"), "EXPECTED: 2")
            window.only_unexpected.set(False)
            window.show()
            self.assertEqual(len(window.tree.get_children()), 7)

            # Add the unknown device (and a non-unknown one, which is skipped).
            pick = [i for i in window.tree.get_children()
                    if window.tree.set(i, "ip") in ("192.168.1.200", "192.168.1.77")]
            window.tree.selection_set(pick)
            window.add_selected()
            self.assertTrue(self.app.dirty)
            self.assertEqual(len(self.app.entries), 11)
            self.assertEqual(self.app.entries[-1]["ip"], "192.168.1.200")
            self.assertEqual(self.app.entries[-1]["expected_mac"], "3a:11:22:33:44:55")
            self.assertIn("1 selected row(s) weren't unknown", gui.messagebox.showinfo.call_args[0][1])
            window.close()
            self.assertIsNone(self.app.discover_window)

    def test_discover_rejects_mask_and_warns_off_subnet(self):
        from netcheck import gui_discover
        with mock.patch.object(gui_discover.shutil, "which", return_value="/bin/ping"), \
                mock.patch.object(gui_discover, "local_addresses", return_value=set()), \
                mock.patch("netcheck.discover.primary_address", return_value="10.20.30.40"), \
                mock.patch.object(gui_discover.messagebox, "askokcancel",
                                  return_value=False) as ask:
            self.app.open_discover()
            window = self.app.discover_window
            window.subnets_var.set("255.255.255.0")
            window.start()
            self.assertIn("is a subnet mask, not a subnet",
                          gui.messagebox.showerror.call_args[0][1])
            window.subnets_var.set("192.168.1.0 255.255.255.0")
            window.start()  # this computer isn't on 192.168.1.0/24: warn, user cancels
            self.assertEqual(ask.call_args[0][0], "Different subnet")
            self.assertIn("so you may want 10.20.30.0/24", ask.call_args[0][1])
            self.assertFalse(window.running())
            window.close()

    # Don't re-run the inherited ping tests in this class.
    test_run_accept_and_save = test_filter_by_switch = None
    test_invalid_entry_blocks_run = test_dialog_rejects_duplicates = None
    test_connection_dialog_help = None


if __name__ == "__main__":
    unittest.main()
