"""Device discovery: ARP table reading, subnet handling, sweep, classification, CLI."""

import io
import json
import os
import tempfile
import textwrap
import threading
import unittest
from unittest import mock

from netcheck import discovercli
from netcheck.discover import (
    EXPECTED, MAC_CONFLICT, MOVED, NOT_FOUND, UNKNOWN, classify, discover, hosts, is_netmask,
    load_oui, new_entry, off_subnet_warning, parse_subnets, suggest_subnets, sweep, vendor_of,
    write_discovery,
)
from netcheck.inventory import load_inventory
from netcheck.mac import is_locally_administered, parse_arp_table
from netcheck.ping import PingResult
from tests.test_netif import patch_wired

# A frozen copy of the example inventory, so the shipped example can change freely.
EXAMPLE = os.path.join(os.path.dirname(__file__), "fixtures", "sample_inventory.json")

# Example inventory: 192.168.1.1 Firewall-01 (00:1a:2b:3c:4d:01), .10 Server-DB-01 (..:10),
# .11 Server-APP-01 (no MAC), .50 Printer-2F (..:50), .101 Workstation-101 (..:65).
REPLIES = {"192.168.1.1", "192.168.1.10", "192.168.1.77", "192.168.1.5"}
ARP = {
    "192.168.1.1": "00:1a:2b:3c:4d:01",     # as expected
    "192.168.1.10": "00:1a:2b:3c:4d:99",    # someone else on Server-DB-01's IP
    "192.168.1.11": "00:1a:2b:3c:4d:11",    # ARP only (ping blocked), no MAC recorded
    "192.168.1.77": "00:1a:2b:3c:4d:50",    # Printer-2F moved from .50
    "192.168.1.200": "3a:11:22:33:44:55",   # unknown, randomised MAC, ping blocked
    "10.9.9.9": "00:00:00:00:00:09",        # outside the swept subnet
}


def fake_ping(ip, count, timeout_s):
    return PingResult(ip in REPLIES, 1 if ip in REPLIES else 0, 1, 0.4 if ip in REPLIES else None)


class ArpTableTests(unittest.TestCase):
    def test_linux(self):
        text = textwrap.dedent("""\
            192.168.1.1 dev eth0 lladdr 00:1a:2b:3c:4d:01 REACHABLE
            192.168.1.20 dev eth0  FAILED
            192.168.1.21 dev eth0 lladdr 3a:11:22:33:44:55 STALE
            192.168.1.255 dev eth0 lladdr ff:ff:ff:ff:ff:ff PERMANENT
            224.0.0.251 dev eth0 lladdr 01:00:5e:00:00:fb NOARP
            """)
        self.assertEqual(parse_arp_table(text, "linux"),
                         {"192.168.1.1": "00:1a:2b:3c:4d:01", "192.168.1.21": "3a:11:22:33:44:55"})
        proc = textwrap.dedent("""\
            IP address       HW type     Flags       HW address            Mask     Device
            192.168.1.1      0x1         0x2         00:1a:2b:3c:4d:01     *        eth0
            192.168.1.9      0x1         0x0         00:00:00:00:00:00     *        eth0
            """)
        self.assertEqual(parse_arp_table(proc, "linux"), {"192.168.1.1": "00:1a:2b:3c:4d:01"})

    def test_windows(self):
        text = textwrap.dedent("""
            Interface: 192.168.1.5 --- 0xb
              Internet Address      Physical Address      Type
              192.168.1.1           00-1a-2b-3c-4d-01     dynamic
              192.168.1.255         ff-ff-ff-ff-ff-ff     static
              224.0.0.22            01-00-5e-00-00-16     static
            """)
        self.assertEqual(parse_arp_table(text, "windows"), {"192.168.1.1": "00:1a:2b:3c:4d:01"})

    def test_macos(self):
        text = ("? (192.168.1.1) at 0:1a:2b:3c:4d:1 on en0 ifscope [ethernet]\n"
                "? (192.168.1.7) at (incomplete) on en0 ifscope [ethernet]\n")
        self.assertEqual(parse_arp_table(text, "darwin"), {"192.168.1.1": "00:1a:2b:3c:4d:01"})

    def test_locally_administered(self):
        self.assertTrue(is_locally_administered("3a:11:22:33:44:55"))
        self.assertFalse(is_locally_administered("00:1a:2b:3c:4d:01"))


class SubnetTests(unittest.TestCase):
    def test_parse(self):
        nets = parse_subnets("192.168.1.0/24, 10.0.5.0/30 10.0.9.7")
        self.assertEqual([str(n) for n in nets], ["192.168.1.0/24", "10.0.5.0/30", "10.0.9.7/32"])
        self.assertEqual(len(hosts(nets)), 254 + 2 + 1)
        self.assertEqual(str(parse_subnets("192.168.1.77/24")[0]), "192.168.1.0/24")

    def test_masks(self):
        with self.assertRaises(ValueError) as ctx:
            parse_subnets("255.255.255.0")
        self.assertIn("'255.255.255.0' is a subnet mask, not a subnet", str(ctx.exception))
        self.assertIn("192.168.1.0/24", str(ctx.exception))
        for text in ("192.168.1.0 255.255.255.0", "192.168.1.20 255.255.255.0",
                     "192.168.1.0/255.255.255.0", "192.168.1.20/24"):
            self.assertEqual([str(n) for n in parse_subnets(text)], ["192.168.1.0/24"], text)
        self.assertEqual([str(n) for n in parse_subnets("10.0.0.8 255.255.255.248, 10.1.0.0/30")],
                         ["10.0.0.8/29", "10.1.0.0/30"])
        self.assertTrue(is_netmask("255.255.255.192"))
        self.assertFalse(is_netmask("255.0.255.0"))
        self.assertFalse(is_netmask("192.168.1.1"))

    def test_off_subnet_warning(self):
        nets = parse_subnets("192.168.1.0/24")
        self.assertIsNone(off_subnet_warning(nets, {"192.168.1.5"}))
        with mock.patch("netcheck.discover.primary_address", return_value="10.20.30.40"):
            warning = off_subnet_warning(nets, set())
        self.assertIn("doesn't have an address in 192.168.1.0/24", warning)
        self.assertIn("This computer is 10.20.30.40, so you may want 10.20.30.0/24", warning)
        self.assertEqual(suggest_subnets([], fallback="10.20.30.40"), ["10.20.30.0/24"])

    def test_rejects(self):
        for text, message in [("", "at least one subnet"), ("fe80::/64", "only IPv4"),
                              ("10.0.0.0/16", "65534 addresses; the limit is 1024"),
                              ("banana", "'banana' is not a subnet"), ("0.0.0.0", "isn't a network")]:
            with self.assertRaises(ValueError) as ctx:
                parse_subnets(text)
            self.assertIn(message, str(ctx.exception))
        self.assertEqual(len(parse_subnets("10.0.0.0/22", max_hosts=1022)), 1)

    def test_suggest(self):
        self.assertEqual(suggest_subnets(load_inventory(EXAMPLE)), ["192.168.1.0/24"])


class SweepTests(unittest.TestCase):
    def test_sweep_and_progress(self):
        progress = []
        replies = sweep(parse_subnets("192.168.1.0/28"), fake_ping, workers=4,
                        progress=lambda done, total: progress.append((done, total)))
        self.assertEqual(len(replies), 14)
        self.assertEqual({ip for ip, ok in replies.items() if ok}, {"192.168.1.1", "192.168.1.10",
                                                                   "192.168.1.5"})
        self.assertEqual(progress[-1], (14, 14))

    def test_stop(self):
        stop = threading.Event()
        pinged = []

        def ping_then_stop(ip, count, timeout_s):
            pinged.append(ip)
            stop.set()
            return PingResult(True, 1, 1, 1.0)

        replies = sweep(parse_subnets("192.168.1.0/28"), ping_then_stop, workers=1,
                        stop_event=stop)
        self.assertEqual(len(pinged), 1)
        self.assertEqual(sum(replies.values()), 1)


class ClassifyTests(unittest.TestCase):
    def setUp(self):
        self.found = discover(load_inventory(EXAMPLE), parse_subnets("192.168.1.0/24"),
                              ping_fn=fake_ping, arp_fn=lambda: ARP, local_ips={"192.168.1.5"},
                              workers=32)
        self.by_ip = {f.ip: f for f in self.found}

    def test_categories(self):
        got = {ip: f.category for ip, f in self.by_ip.items()}
        self.assertEqual(got, {
            "192.168.1.1": EXPECTED,
            "192.168.1.10": MAC_CONFLICT,
            "192.168.1.11": EXPECTED,
            "192.168.1.77": MOVED,
            "192.168.1.200": UNKNOWN,
            "192.168.1.50": NOT_FOUND,
            "192.168.1.101": NOT_FOUND,
        })
        # Ordered by importance: unknown first, expected last.
        self.assertEqual(self.found[0].ip, "192.168.1.200")
        self.assertEqual(self.found[-1].category, EXPECTED)

    def test_details(self):
        unknown = self.by_ip["192.168.1.200"]
        self.assertEqual((unknown.mac, unknown.reply, unknown.vendor),
                         ("3a:11:22:33:44:55", "ARP only", "random/private MAC"))
        self.assertIn("ping is blocked", unknown.message)
        moved = self.by_ip["192.168.1.77"]
        self.assertEqual(moved.inventory, "PP-A:4 Printer-2F")
        self.assertIn("the inventory has it at 192.168.1.50", moved.message)
        conflict = self.by_ip["192.168.1.10"]
        self.assertIn("Expected 00:1a:2b:3c:4d:10 here, but 00:1a:2b:3c:4d:99 answered",
                      conflict.message)
        self.assertIn("MAC not recorded yet", self.by_ip["192.168.1.11"].message)
        # This computer's own address and ARP entries outside the subnet are left out.
        self.assertNotIn("192.168.1.5", self.by_ip)
        self.assertNotIn("10.9.9.9", self.by_ip)

    def test_ping_without_arp(self):
        nets = parse_subnets("10.1.1.0/30")
        found = classify([], nets, {"10.1.1.1": True, "10.1.1.2": False}, {})
        self.assertEqual([(f.ip, f.category) for f in found], [("10.1.1.1", UNKNOWN)])
        self.assertIn("MAC unknown", found[0].message)

    def test_vendor_and_new_entry(self):
        fd, path = tempfile.mkstemp(suffix=".csv")
        with os.fdopen(fd, "w") as fh:
            fh.write("Registry,Assignment,Organization Name,Organization Address\n"
                     "MA-L,001A2B,Example Controls Ltd,Somewhere\n")
        self.addCleanup(os.remove, path)
        vendors = load_oui(path)
        self.assertEqual(vendor_of("00:1a:2b:3c:4d:01", vendors), "Example Controls Ltd")
        self.assertEqual(load_oui("/nonexistent/oui.csv"), {})
        entry = new_entry(self.by_ip["192.168.1.200"])
        self.assertEqual((entry["ip"], entry["expected_mac"], entry["status"]),
                         ("192.168.1.200", "3a:11:22:33:44:55", "connected"))
        self.assertIn("Found by Discover", entry["notes"])

    def test_report(self):
        for ext in (".csv", ".json"):
            fd, path = tempfile.mkstemp(suffix=ext)
            os.close(fd)
            self.addCleanup(os.remove, path)
            write_discovery(self.found, path, ["192.168.1.0/24"])
            with open(path) as fh:
                text = fh.read()
            self.assertIn("192.168.1.200", text)
        self.assertEqual(json.loads(text)["summary"][UNKNOWN], 1)


class DiscoverCliTests(unittest.TestCase):
    def run_cli(self, args):
        out, err = io.StringIO(), io.StringIO()
        real = discovercli.discover
        with mock.patch.object(discovercli, "discover",
                               lambda c, n, **kw: real(c, n, ping_fn=fake_ping,
                                                       arp_fn=lambda: ARP,
                                                       local_ips={"192.168.1.5"}, **kw)), \
                mock.patch.object(discovercli.shutil, "which", return_value="/bin/ping"), \
                mock.patch.object(discovercli, "local_addresses", return_value={"192.168.1.5"}), \
                mock.patch("sys.stdout", out), mock.patch("sys.stderr", err), patch_wired():
            code = discovercli.main(args)
        return code, out.getvalue(), err.getvalue()

    def test_with_inventory(self):
        tmp = tempfile.mkdtemp()
        report, added = os.path.join(tmp, "found.csv"), os.path.join(tmp, "added.json")
        code, out, err = self.run_cli([EXAMPLE, "-o", report, "--add-unknown", added,
                                       "--no-colour"])
        self.assertEqual(code, 1)
        self.assertIn("Sweeping 192.168.1.0/24", err)
        self.assertRegex(out, r"UNKNOWN\s+192\.168\.1\.200\s+3a:11:22:33:44:55")
        self.assertNotIn("Firewall-01", out)  # expected devices hidden without --all
        self.assertIn("Summary: 1 unknown, 1 moved, 1 mac conflict, 2 not found, 2 expected", out)
        new = load_inventory(added)
        self.assertEqual(len(new), 11)
        self.assertEqual((new[-1].ip, new[-1].expected_mac), ("192.168.1.200",
                                                              "3a:11:22:33:44:55"))
        with open(report) as fh:
            self.assertEqual(len(fh.readlines()), 8)

    def test_without_inventory(self):
        code, out, _ = self.run_cli(["--subnet", "192.168.1.0/24", "--all", "--no-colour"])
        self.assertEqual(code, 1)
        self.assertIn("5 unknown", out)
        with mock.patch.object(discovercli, "primary_address", return_value=None):
            code, _out, err = self.run_cli([])
        self.assertEqual(code, 2)
        self.assertIn("give the subnet(s) to sweep", err)
        code, _out, err = self.run_cli(["--subnet", "255.255.255.0"])
        self.assertEqual(code, 2)
        self.assertIn("is a subnet mask, not a subnet", err)


if __name__ == "__main__":
    unittest.main()
