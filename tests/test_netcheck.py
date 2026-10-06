import io
import json
import os
import tempfile
import textwrap
import unittest
from unittest import mock

from netcheck import mac as macmod
from netcheck.checker import (
    FAIL, MAC_DISCOVERED, MAC_MATCH, MAC_MISMATCH, MAC_UNRESOLVED, PASS, SKIP, WARN,
    check_all,
)
from netcheck.cli import main
from netcheck.inventory import Connection, InventoryError, load_inventory
from netcheck.ping import PingResult, build_ping_command, parse_ping_output

EXAMPLE = os.path.join(os.path.dirname(__file__), "..", "examples", "expected_interconnect.json")


def write_tmp(data):
    """Write ``data`` (JSON-serialisable, or raw text) to a temporary .json file."""
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as fh:
        fh.write(data if isinstance(data, str) else json.dumps(data))
    return path


class NormalizeMacTests(unittest.TestCase):
    def test_formats(self):
        expected = "00:1a:2b:3c:4d:5e"
        for value in ("00:1A:2B:3C:4D:5E", "00-1a-2b-3c-4d-5e", "001a.2b3c.4d5e",
                      "001A2B3C4D5E", "0:1a:2b:3c:4d:5e", " 00:1a:2b:3c:4d:5e "):
            self.assertEqual(macmod.normalize_mac(value), expected, value)

    def test_invalid(self):
        for value in (None, "", "zz:1a:2b:3c:4d:5e", "00:1a:2b:3c:4d", "hello"):
            self.assertIsNone(macmod.normalize_mac(value), value)


class MacLookupTests(unittest.TestCase):
    def test_linux_ip_neigh(self):
        out = "192.168.1.10 dev eth0 lladdr 00:1a:2b:3c:4d:10 REACHABLE\n"
        with mock.patch.object(macmod, "_run", return_value=out):
            self.assertEqual(macmod.lookup_mac("192.168.1.10", "Linux"), "00:1a:2b:3c:4d:10")

    def test_linux_incomplete_entry_falls_back(self):
        with mock.patch.object(macmod, "_run", return_value="192.168.1.10 dev eth0 FAILED\n"), \
                mock.patch("builtins.open", side_effect=OSError):
            self.assertIsNone(macmod.lookup_mac("192.168.1.10", "Linux"))

    def test_windows_arp(self):
        out = textwrap.dedent("""
            Interface: 192.168.1.5 --- 0xb
              Internet Address      Physical Address      Type
              192.168.1.1           00-1a-2b-3c-4d-01     dynamic
              192.168.1.10          00-1a-2b-3c-4d-10     dynamic
        """)
        with mock.patch.object(macmod, "_run", return_value=out):
            self.assertEqual(macmod.lookup_mac("192.168.1.1", "Windows"), "00:1a:2b:3c:4d:01")
            self.assertEqual(macmod.lookup_mac("192.168.1.10", "Windows"), "00:1a:2b:3c:4d:10")

    def test_ip_must_match_exactly(self):
        out = "  192.168.1.100         00-1a-2b-3c-4d-64     dynamic\n"
        with mock.patch.object(macmod, "_run", return_value=out):
            self.assertIsNone(macmod.lookup_mac("192.168.1.10", "Windows"))

    def test_macos_arp(self):
        out = "? (192.168.1.10) at 0:1a:2b:3c:4d:10 on en0 ifscope [ethernet]\n"
        with mock.patch.object(macmod, "_run", return_value=out):
            self.assertEqual(macmod.lookup_mac("192.168.1.10", "Darwin"), "00:1a:2b:3c:4d:10")

    def test_macos_incomplete(self):
        out = "? (192.168.1.10) at (incomplete) on en0 ifscope [ethernet]\n"
        with mock.patch.object(macmod, "_run", return_value=out):
            self.assertIsNone(macmod.lookup_mac("192.168.1.10", "Darwin"))


class PingTests(unittest.TestCase):
    def test_commands(self):
        self.assertEqual(build_ping_command("10.0.0.1", 3, 1.5, "Windows"),
                         ["ping", "-n", "3", "-w", "1500", "10.0.0.1"])
        self.assertEqual(build_ping_command("10.0.0.1", 3, 1.5, "Linux"),
                         ["ping", "-c", "3", "-W", "2", "10.0.0.1"])
        self.assertEqual(build_ping_command("10.0.0.1", 3, 1.5, "Darwin"),
                         ["ping", "-c", "3", "-W", "1500", "10.0.0.1"])

    def test_linux_output(self):
        out = textwrap.dedent("""
            PING 10.0.0.1 (10.0.0.1) 56(84) bytes of data.
            64 bytes from 10.0.0.1: icmp_seq=1 ttl=64 time=0.412 ms
            64 bytes from 10.0.0.1: icmp_seq=2 ttl=64 time=0.588 ms
            2 packets transmitted, 2 received, 0% packet loss, time 1001ms
        """)
        res = parse_ping_output(out, 2, "Linux")
        self.assertTrue(res.reachable)
        self.assertEqual(res.replies, 2)
        self.assertEqual(res.avg_rtt_ms, 0.5)

    def test_linux_no_reply(self):
        out = "2 packets transmitted, 0 received, 100% packet loss, time 1001ms\n"
        self.assertFalse(parse_ping_output(out, 2, "Linux").reachable)

    def test_windows_output(self):
        out = textwrap.dedent("""
            Reply from 10.0.0.1: bytes=32 time<1ms TTL=64
            Reply from 10.0.0.1: bytes=32 time=2ms TTL=64
        """)
        res = parse_ping_output(out, 2, "Windows")
        self.assertTrue(res.reachable)
        self.assertEqual(res.avg_rtt_ms, 1.5)

    def test_windows_router_unreachable_is_not_a_reply(self):
        out = "Reply from 10.0.0.254: Destination host unreachable.\n" * 2
        self.assertFalse(parse_ping_output(out, 2, "Windows").reachable)


class InventoryTests(unittest.TestCase):
    def test_example_loads(self):
        conns = load_inventory(EXAMPLE)
        self.assertEqual(len(conns), 10)
        self.assertEqual(conns[3].expected_mac, "00:1a:2b:3c:4d:50")
        self.assertEqual(sum(c.status == "unused" for c in conns), 4)
        self.assertEqual([c.media for c in conns[-2:]], ["fiber", "fiber"])

    def test_reports_all_errors(self):
        def entry(port, ip, mac="", status="connected"):
            return {"patch_panel": "PP", "panel_port": port, "switch": "SW",
                    "switch_port": f"Gi{port}", "device": f"d{port}", "ip": ip,
                    "expected_mac": mac, "status": status}
        path = write_tmp({"connections": [
            entry(1, "10.0.0.1", mac="bad-mac"),
            entry(2, "10.0.0.999"),
            entry(3, "10.0.0.3"),
            entry(4, "10.0.0.4", status="unused"),
            entry(5, "10.0.0.5", status="maybe"),
            dict(entry(3, "10.0.0.3"), switch_port="Gi6"),
            "not an object",
            dict(entry(8, "10.0.0.8"), device=["a", "list"]),
        ]})
        self.addCleanup(os.remove, path)
        with self.assertRaises(InventoryError) as ctx:
            load_inventory(path)
        msg = str(ctx.exception)
        self.assertIn("connection #1 (PP:1): 'bad-mac' is not a valid MAC", msg)
        self.assertIn("connection #2 (PP:2): '10.0.0.999'", msg)
        self.assertIn("connection #4 (PP:4): unused connection", msg)
        self.assertIn("connection #5 (PP:5): status 'maybe'", msg)
        self.assertIn("connection #6: duplicate IP address '10.0.0.3' (also on connection #3)", msg)
        self.assertIn("connection #6: duplicate patch panel port 'PP:3' (also on connection #3)", msg)
        self.assertIn("connection #7: must be an object", msg)
        self.assertIn('connection #8 (PP:8): "device" must be a string or number', msg)

    def test_numeric_ports_and_missing_fields(self):
        path = write_tmp({"connections": [{"panel_port": 7, "ip": "10.0.0.7"}]})
        self.addCleanup(os.remove, path)
        conn = load_inventory(path)[0]
        self.assertEqual(conn.panel_port, "7")
        self.assertEqual(conn.status, "connected")
        self.assertIsNone(conn.expected_mac)

    def test_missing_file_explains_what_to_do(self):
        with self.assertRaises(InventoryError) as ctx:
            load_inventory("does_not_exist.json")
        msg = str(ctx.exception)
        self.assertIn("Inventory file not found", msg)
        self.assertIn("expected_interconnect.json", msg)

    def test_structure_errors(self):
        for data, expected in [
            ('{"connections": [', "invalid JSON at line 1"),
            ([], 'top level must be an object with a "connections" list'),
            ({"devices": []}, 'top level must be an object with a "connections" list'),
            ({"connections": {}}, '"connections" must be a list'),
            ({"connections": []}, "no connections defined"),
        ]:
            path = write_tmp(data)
            self.addCleanup(os.remove, path)
            with self.assertRaises(InventoryError) as ctx:
                load_inventory(path)
            self.assertIn(expected, str(ctx.exception))


def fake_ping(results):
    def _ping(ip, count, timeout_s):
        return results.get(ip, PingResult(False, 0, count, None))
    return _ping


class CheckerTests(unittest.TestCase):
    def run_one(self, conn, reachable, mac):
        ping_res = PingResult(reachable, 2 if reachable else 0, 2, 1.0 if reachable else None)
        return check_all([conn], ping_fn=lambda ip, count, timeout_s: ping_res,
                         mac_fn=lambda ip: mac)[0]

    def conn(self, **kw):
        base = dict(index=1, device="dev", ip="10.0.0.1", expected_mac="00:00:00:00:00:01")
        base.update(kw)
        return Connection(**base)

    def test_grading(self):
        cases = [
            # (reachable, discovered, expected_mac, result, mac_check)
            (True, "00:00:00:00:00:01", "00:00:00:00:00:01", PASS, MAC_MATCH),
            (True, "00:00:00:00:00:02", "00:00:00:00:00:01", FAIL, MAC_MISMATCH),
            (False, "00:00:00:00:00:02", "00:00:00:00:00:01", FAIL, MAC_MISMATCH),
            (True, "00:00:00:00:00:02", None, PASS, MAC_DISCOVERED),
            (True, None, "00:00:00:00:00:01", WARN, MAC_UNRESOLVED),
            (False, "00:00:00:00:00:01", "00:00:00:00:00:01", WARN, MAC_MATCH),
            (False, None, "00:00:00:00:00:01", FAIL, MAC_UNRESOLVED),
        ]
        for reachable, found, expected, result, mac_check in cases:
            res = self.run_one(self.conn(expected_mac=expected), reachable, found)
            self.assertEqual((res.result, res.mac_check), (result, mac_check),
                             (reachable, found, expected))

    def test_unused_and_no_ip_are_skipped_without_pinging(self):
        def boom(*a, **k):
            raise AssertionError("should not ping")
        res = check_all([self.conn(status="unused", ip=""), self.conn(ip="")],
                        ping_fn=boom, mac_fn=boom)
        self.assertEqual([r.result for r in res], [SKIP, SKIP])


class StopTests(unittest.TestCase):
    def test_stop_event_cancels_pending_checks(self):
        import threading
        stop = threading.Event()
        conns = [Connection(index=i, ip=f"10.0.0.{i}") for i in range(1, 6)]

        def ping(ip, count, timeout_s):
            stop.set()  # request a stop as soon as the first ping runs
            return PingResult(True, 1, 1, 1.0)

        res = check_all(conns, workers=1, ping_fn=ping, mac_fn=lambda ip: None, stop_event=stop)
        self.assertEqual(res[0].result, WARN)
        self.assertEqual([r.message for r in res[1:]], ["Cancelled"] * 4)


class EndToEndTests(unittest.TestCase):
    def test_cli_with_example(self):
        pings = {
            "192.168.1.1": PingResult(True, 2, 2, 0.5),
            "192.168.1.10": PingResult(True, 2, 2, 0.5),
            "192.168.1.11": PingResult(True, 2, 2, 0.5),
            "192.168.1.50": PingResult(True, 2, 2, 0.5),
        }
        macs = {
            "192.168.1.1": "00:1a:2b:3c:4d:01",
            "192.168.1.10": "00:1a:2b:3c:4d:99",  # wrong device
            "192.168.1.11": "00:1a:2b:3c:4d:11",  # discovered
            "192.168.1.50": "00:1a:2b:3c:4d:50",
        }
        tmpdir = tempfile.mkdtemp()
        report = os.path.join(tmpdir, "report.csv")
        baseline = os.path.join(tmpdir, "baseline.json")
        out = io.StringIO()
        with mock.patch("netcheck.checker.ping", fake_ping(pings)), \
                mock.patch("netcheck.cli.shutil.which", return_value="/bin/ping"), \
                mock.patch("netcheck.cli.check_all") as patched, \
                mock.patch("sys.stdout", out), mock.patch("sys.stderr", io.StringIO()):
            patched.side_effect = lambda conns, **kw: check_all(
                conns, ping_fn=fake_ping(pings), mac_fn=macs.get,
                **{k: v for k, v in kw.items() if k != "progress"})
            code = main([EXAMPLE, "-o", report, "--write-baseline", baseline, "--no-colour"])

        self.assertEqual(code, 1)  # the MAC mismatch and unreachable workstation fail
        text = out.getvalue()
        self.assertIn("3 pass, 2 fail, 0 warn, 5 skipped", text)
        self.assertIn("MAC mismatch", text)

        with open(report) as fh:
            self.assertEqual(len(fh.readlines()), 11)
        conns = load_inventory(baseline)
        self.assertEqual(conns[2].expected_mac, "00:1a:2b:3c:4d:11")
        # Everything else in the original file is preserved untouched.
        with open(EXAMPLE) as fh:
            original = json.load(fh)
        with open(baseline) as fh:
            updated = json.load(fh)
        original["connections"][2]["expected_mac"] = "00:1a:2b:3c:4d:11"
        self.assertEqual(updated, original)


if __name__ == "__main__":
    unittest.main()
