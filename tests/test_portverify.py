import io
import json
import os
import tempfile
import textwrap
import unittest
from unittest import mock

from netcheck import portcli
from netcheck.checker import FAIL, PASS, SKIP, WARN
from netcheck.cisco import (
    expand_port_range, normalize_hostname, normalize_interface, parse_cdp_neighbors_detail,
    parse_interfaces_status, parse_lldp_neighbors_detail, parse_tdr, short_interface,
)
from netcheck.inventory import Connection, InventoryError
from netcheck.portverify import (
    Assignment, TestSwitchSettings, plan_batches, preflight, verify_batch,
)

EXAMPLE = os.path.join(os.path.dirname(__file__), "..", "examples", "expected_interconnect.json")

STATUS_HEADER = (
    "Port         Name               Status       Vlan       Duplex  Speed Type\n")


def status_line(port, status="connected", vlan="routed", duplex="a-full", speed="a-1000",
                name=""):
    return (f"{port:<13}{name:<19}{status:<13}{vlan:<11}{duplex:>6} {speed:>6} "
            "10/100/1000BaseTX\n")


def cdp_entry(device, local, remote, platform="cisco ESS-3300"):
    return textwrap.dedent(f"""\
        -------------------------
        Device ID: {device}
        Entry address(es):
          IP address: 10.10.10.2
        Platform: {platform},  Capabilities: Switch IGMP
        Interface: {local},  Port ID (outgoing port): {remote}
        Holdtime : 172 sec

        Version :
        Cisco IOS Software [Cupertino], ESS3300 Software, Version 17.9.4

        advertisement version: 2
        Native VLAN: 1
        Duplex: full
        """)


LLDP_ENTRY = textwrap.dedent("""\
    ------------------------------------------------
    Local Intf: Gi1/0/2
    Chassis id: 7c95.f3e1.2b00
    Port id: Gi1/6
    Port Description: GigabitEthernet1/6
    System Name: SW-CORE-01.plant.local

    System Description:
    Cisco IOS Software [Cupertino], ESS3300 Software

    Time remaining: 101 seconds
    System Capabilities: B,R
    Enabled Capabilities: B
    Management Addresses:
        IP: 10.10.10.2

    Total entries displayed: 1
    """)

TDR_OUTPUT = textwrap.dedent("""\
    Interface Speed Local pair Pair length        Remote pair Pair status
    --------- ----- ---------- ------------------ ----------- --------------------
    Gi1/0/1   1000M Pair A     3    +/- 5  meters Pair A      Normal
                    Pair B     3    +/- 5  meters Pair B      Normal
                    Pair C     4    +/- 5  meters Pair C      Normal
                    Pair D     3    +/- 5  meters Pair D      Normal
    """)
TDR_FAULT = textwrap.dedent("""\
    Interface Speed Local pair Pair length        Remote pair Pair status
    --------- ----- ---------- ------------------ ----------- --------------------
    Gi1/0/3   auto  Pair A     12   +/- 5  meters N/A         Open
                    Pair B     35   +/- 5  meters N/A         Normal
                    Pair C     N/A                N/A         Not Supported
                    Pair D     35   +/- 5  meters N/A         Normal
    """)


class FakeSession:
    """Answers commands from a dict; values may be lists (one per call, last repeats)."""

    def __init__(self, responses, privileged=True):
        self.responses = responses
        self.privileged = privileged
        self.commands = []

    def _answer(self, command):
        self.commands.append(command)
        value = self.responses.get(command, "")
        if isinstance(value, list):
            return value.pop(0) if len(value) > 1 else value[0]
        return value

    send = run = _answer

    def enable(self, secret):
        self.privileged = True

    def close(self):
        pass


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class InterfaceNameTests(unittest.TestCase):
    def test_normalize(self):
        for name, expected in [
            ("Gi1/0/5", "GigabitEthernet1/0/5"), ("gi1/0/5", "GigabitEthernet1/0/5"),
            ("GigabitEthernet1/5", "GigabitEthernet1/5"), ("Te1/1/1", "TenGigabitEthernet1/1/1"),
            ("Tw1/0/1", "TwoGigabitEthernet1/0/1"), ("Fa0/1", "FastEthernet0/1"),
            ("Gig 1/0/2", "GigabitEthernet1/0/2"), ("Eth1/1", "Ethernet1/1"),
        ]:
            self.assertEqual(normalize_interface(name), expected, name)
        for bad in ("", None, "7c95.f3e1.2b00", "Vlan", "Xx1/0/1"):
            self.assertIsNone(normalize_interface(bad), bad)
        self.assertEqual(short_interface("TenGigabitEthernet1/1/1"), "Te1/1/1")
        self.assertEqual(short_interface("GigabitEthernet1/6"), "Gi1/6")

    def test_ranges(self):
        self.assertEqual([short_interface(p) for p in expand_port_range("Gi1/0/1-3, Gi1/0/10")],
                         ["Gi1/0/1", "Gi1/0/2", "Gi1/0/3", "Gi1/0/10"])
        self.assertEqual(len(expand_port_range("Gi1/0/1-24")), 24)
        for bad in ("Gi1/0/5-2", "nonsense"):
            with self.assertRaises(ValueError):
                expand_port_range(bad)

    def test_hostname(self):
        self.assertEqual(normalize_hostname("SW-CORE-01.plant.local"), "sw-core-01")
        self.assertEqual(normalize_hostname("N9K-1(FDO21120U8N)"), "n9k-1")


class ParserTests(unittest.TestCase):
    def test_cdp(self):
        text = (cdp_entry("SW-CORE-01.plant.local", "GigabitEthernet1/0/1", "GigabitEthernet1/5")
                + cdp_entry("SW-ACCESS-01", "GigabitEthernet1/0/3", "GigabitEthernet1/2")
                + "\nTotal cdp entries displayed : 2\n")
        neighbors = parse_cdp_neighbors_detail(text)
        self.assertEqual(len(neighbors), 2)
        n = neighbors[0]
        self.assertEqual((n.local_interface, n.device_id, n.remote_port, n.platform, n.ip),
                         ("GigabitEthernet1/0/1", "SW-CORE-01.plant.local", "GigabitEthernet1/5",
                          "cisco ESS-3300", "10.10.10.2"))
        self.assertEqual(parse_cdp_neighbors_detail("% CDP is not enabled\n"), [])

    def test_lldp(self):
        n = parse_lldp_neighbors_detail(LLDP_ENTRY)[0]
        self.assertEqual((n.local_interface, n.device_id, n.remote_port),
                         ("GigabitEthernet1/0/2", "SW-CORE-01.plant.local", "Gi1/6"))
        # Port id sent as a MAC: fall back to the port description.
        mac_port = LLDP_ENTRY.replace("Port id: Gi1/6", "Port id: 7c95.f3e1.2b06")
        self.assertEqual(parse_lldp_neighbors_detail(mac_port)[0].remote_port, "GigabitEthernet1/6")
        self.assertEqual(parse_lldp_neighbors_detail("% LLDP is not enabled\n"), [])

    def test_interfaces_status(self):
        text = (STATUS_HEADER + status_line("Gi1/0/1")
                + status_line("Gi1/0/2", "notconnect", duplex="auto", speed="auto",
                              name="spare disabled one")
                + status_line("Gi1/0/3", "disabled", vlan="1")
                + status_line("Gi1/0/4", speed="a-100")
                + status_line("Te1/1/1", "notconnect", vlan="1", duplex="full", speed="10G"))
        st = parse_interfaces_status(text)
        self.assertEqual(len(st), 5)
        g1 = st["GigabitEthernet1/0/1"]
        self.assertTrue(g1.link_up and g1.routed)
        self.assertEqual(g1.speed_mbps, 1000)
        g2 = st["GigabitEthernet1/0/2"]
        self.assertEqual((g2.status, g2.name, g2.speed_mbps), ("notconnect", "spare disabled one", None))
        self.assertEqual(st["GigabitEthernet1/0/3"].status, "disabled")
        self.assertFalse(st["GigabitEthernet1/0/3"].routed)
        self.assertEqual(st["GigabitEthernet1/0/4"].speed_mbps, 100)
        self.assertEqual(st["TenGigabitEthernet1/1/1"].speed_mbps, 10000)

    def test_tdr(self):
        ok = parse_tdr(TDR_OUTPUT)["GigabitEthernet1/0/1"]
        self.assertTrue(ok.ok)
        self.assertEqual(ok.summary(), "OK (4 m)")
        bad = parse_tdr(TDR_FAULT)["GigabitEthernet1/0/3"]
        self.assertFalse(bad.ok)
        self.assertEqual(bad.summary(), "pair A Open at 12 m; pair C Not Supported")


def unused(index, switch_port, switch="SW-CORE-01", **kw):
    return Connection(index=index, patch_panel="PP-A", panel_port=str(index), switch=switch,
                      switch_port=switch_port, status="unused", **kw)


class PlanTests(unittest.TestCase):
    def test_batches(self):
        conns = [unused(i, f"Gi1/{i}") for i in range(1, 6)]
        conns.append(Connection(index=6, ip="10.0.0.1"))
        conns.append(unused(7, ""))
        ports = expand_port_range("Gi1/0/1-2")
        batches, skipped = plan_batches(conns, ports)
        self.assertEqual([[a.connection.index for a in b] for b in batches], [[1, 2], [3, 4], [5]])
        self.assertEqual([a.test_port_short for a in batches[2]], ["Gi1/0/1"])
        self.assertEqual([(r.connection.index, r.result) for r in skipped], [(7, SKIP)])

    def test_connect_point(self):
        self.assertEqual(unused(1, "Gi1/1").connect_point, "far end of PP-A port 1")
        self.assertEqual(unused(1, "Gi1/1", far_end="PP-Z:1").connect_point, "PP-Z:1")

    def test_settings_round_trip(self):
        data = {"test_switch": {"host": "10.0.0.2", "username": "admin",
                                "ports": "Gi1/0/1-3", "cable_test": True}}
        s = TestSwitchSettings.from_inventory(data)
        self.assertEqual(len(s.ports), 3)
        self.assertTrue(s.cable_test)
        self.assertEqual(s.to_dict()["ports"], ["Gi1/0/1", "Gi1/0/2", "Gi1/0/3"])
        self.assertEqual(TestSwitchSettings.from_inventory({"test_switch": s.to_dict()}), s)
        with self.assertRaises(InventoryError):
            TestSwitchSettings.from_inventory({"test_switch": {"ports": "bogus"}})


class PreflightTests(unittest.TestCase):
    def settings(self, **kw):
        return TestSwitchSettings(host="h", username="u",
                                  ports=expand_port_range("Gi1/0/1-4"), **kw)

    def test_ready(self):
        session = FakeSession({
            "show cdp neighbors": "Capability Codes: ...",
            "show interfaces status": STATUS_HEADER + "".join(
                status_line(f"Gi1/0/{i}", "notconnect") for i in range(1, 5)),
        })
        self.assertEqual(preflight(session, self.settings()), ([], []))

    def test_problems(self):
        session = FakeSession({
            "show cdp neighbors": "% CDP is not enabled",
            "show lldp neighbors": "% LLDP is not enabled",
            "show interfaces status": STATUS_HEADER + status_line("Gi1/0/1", vlan="1")
            + status_line("Gi1/0/2", "disabled") + status_line("Gi1/0/3", "err-disabled"),
        }, privileged=False)
        errors, _warnings = preflight(session, self.settings())
        text = "\n".join(errors)
        self.assertIn("Neither CDP nor LLDP", text)
        self.assertIn("Privileged (enable) access", text)
        self.assertIn("Gi1/0/1 is a switchport", text)
        self.assertIn("Gi1/0/2 is shut down", text)
        self.assertIn("Gi1/0/3 is err-disabled", text)
        self.assertIn("Gi1/0/4 doesn't exist", text)
        errors, _ = preflight(session, self.settings(allow_switchports=True, clear_tables=False))
        self.assertNotIn("switchport", "\n".join(errors))


class VerifyBatchTests(unittest.TestCase):
    def run_batch(self, conns, responses, **settings_kw):
        ports = expand_port_range(f"Gi1/0/1-{len(conns)}")
        batch = [Assignment(p, c) for p, c in zip(ports, conns)]
        settings = TestSwitchSettings(ports=ports, cdp_timeout=60, **settings_kw)
        session = FakeSession(responses)
        clock = Clock()
        states = []
        results = verify_batch(session, batch, settings,
                               progress=lambda s, left: states.append((dict(s), left)),
                               sleep=clock.sleep, clock=clock)
        return results, session, clock, states

    def test_mixed_batch(self):
        conns = [
            unused(1, "Gi1/5"),                         # correct (CDP)
            unused(2, "Gi1/6"),                         # correct (LLDP only)
            unused(3, "Gi1/7"),                         # patched to the wrong port
            unused(4, "Gi1/8"),                         # no link
            unused(5, "Gi1/9"),                         # link but silent neighbour
            unused(6, "Gi1/10"),                        # correct, but only 100 Mb/s
            unused(7, "Gi1/11", switch="SW-OTHER"),     # right port, wrong switch
        ]
        status = (STATUS_HEADER + status_line("Gi1/0/1") + status_line("Gi1/0/2")
                  + status_line("Gi1/0/3") + status_line("Gi1/0/4", "notconnect")
                  + status_line("Gi1/0/5") + status_line("Gi1/0/6", speed="a-100")
                  + status_line("Gi1/0/7"))
        cdp_late = (cdp_entry("SW-CORE-01.plant.local", "GigabitEthernet1/0/1", "GigabitEthernet1/5")
                    + cdp_entry("SW-CORE-01.plant.local", "GigabitEthernet1/0/3", "GigabitEthernet1/2")
                    + cdp_entry("SW-CORE-01", "GigabitEthernet1/0/6", "GigabitEthernet1/10")
                    + cdp_entry("SW-CORE-01", "GigabitEthernet1/0/7", "GigabitEthernet1/11"))
        results, session, clock, states = self.run_batch(conns, {
            "show interfaces status": status,
            # Nothing heard on the first poll, then the advertisements arrive.
            "show cdp neighbors detail": ["", cdp_late],
            "show lldp neighbors detail": LLDP_ENTRY,
        })
        got = [(r.result, r.message) for r in results]
        self.assertEqual(got[0], (PASS, "Verified via CDP: patched to sw-core-01 Gi1/5"))
        self.assertEqual(got[1], (PASS, "Verified via LLDP: patched to sw-core-01 Gi1/6"))
        self.assertEqual(got[2], (FAIL, "Patched to sw-core-01 Gi1/2, expected SW-CORE-01 Gi1/7"))
        self.assertEqual(got[3][0], FAIL)
        self.assertIn("No link - check the test cable at far end of PP-A port 4", got[3][1])
        self.assertEqual(got[4][0], WARN)
        self.assertIn("no CDP/LLDP heard within 60s", got[4][1])
        self.assertEqual(got[5][0], WARN)
        self.assertIn("link only 100 Mb/s", got[5][1])
        self.assertEqual(got[6], (FAIL, "Patched to sw-core-01 Gi1/11, expected SW-OTHER Gi1/11"))

        pc = results[0].port_check
        self.assertEqual((pc.test_port, pc.link, pc.seen_switch, pc.seen_port, pc.protocol),
                         ("Gi1/0/1", "up 1000 Mb/s", "sw-core-01", "Gi1/5", "CDP"))
        self.assertEqual(session.commands[:2], ["clear cdp table", "clear lldp table"])
        # The silent port keeps us waiting until the timeout.
        self.assertGreaterEqual(clock.now, 60)
        self.assertEqual(states[0][0]["GigabitEthernet1/0/1"], "Link up - waiting for CDP/LLDP...")
        self.assertEqual(states[-1][0]["GigabitEthernet1/0/1"], "Heard sw-core-01 Gi1/5")

    def test_finishes_early_when_everything_is_heard_or_dead(self):
        status = STATUS_HEADER + status_line("Gi1/0/1") + status_line("Gi1/0/2", "notconnect")
        results, _session, clock, _ = self.run_batch(
            [unused(1, "Gi1/5"), unused(2, "Gi1/6")],
            {"show interfaces status": status,
             "show cdp neighbors detail": cdp_entry("SW-CORE-01", "Gi1/0/1", "Gi1/5")})
        self.assertEqual([r.result for r in results], [PASS, FAIL])
        self.assertLess(clock.now, 60)  # stopped after the link grace period

    def test_cable_test(self):
        status = STATUS_HEADER + status_line("Gi1/0/1") + status_line("Gi1/0/2", "notconnect")
        results, session, _clock, _ = self.run_batch(
            [unused(1, "Gi1/5"), unused(2, "Gi1/6")],
            {"show interfaces status": status,
             "show cdp neighbors detail": cdp_entry("SW-CORE-01", "Gi1/0/1", "Gi1/5"),
             "show cable-diagnostics tdr interface Gi1/0/1": TDR_OUTPUT,
             "show cable-diagnostics tdr interface Gi1/0/2":
                 TDR_FAULT.replace("Gi1/0/3", "Gi1/0/2")},
            cable_test=True)
        self.assertIn("test cable-diagnostics tdr interface Gi1/0/1", session.commands)
        self.assertEqual(results[0].result, PASS)
        self.assertEqual(results[0].port_check.cable_test, "OK (4 m)")
        self.assertEqual(results[1].result, FAIL)
        self.assertIn("cable test: pair A Open at 12 m", results[1].message)

    def test_no_clear(self):
        _r, session, _c, _s = self.run_batch(
            [unused(1, "Gi1/5")],
            {"show interfaces status": STATUS_HEADER + status_line("Gi1/0/1"),
             "show cdp neighbors detail": cdp_entry("SW-CORE-01", "Gi1/0/1", "Gi1/5")},
            clear_tables=False)
        self.assertNotIn("clear cdp table", session.commands)


class CliTests(unittest.TestCase):
    def test_end_to_end(self):
        with open(EXAMPLE) as fh:
            data = json.load(fh)
        data["test_switch"] = {"host": "10.0.0.2", "username": "admin", "ports": "Gi1/0/1"}
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
        self.addCleanup(os.remove, path)
        report = path + ".report.json"
        self.addCleanup(lambda: os.path.exists(report) and os.remove(report))

        session = FakeSession({
            "show cdp neighbors": "",
            "show interfaces status": STATUS_HEADER + status_line("Gi1/0/1"),
            # Batch 1 (PP-A:5) is right; batch 2 (PP-A:6) lands on the wrong port.
            "show cdp neighbors detail": [
                cdp_entry("SW-CORE-01", "Gi1/0/1", "Gi1/0/5"),
                cdp_entry("SW-CORE-01", "Gi1/0/1", "Gi1/0/9")],
        })
        out = io.StringIO()
        with mock.patch.object(portcli, "connect", return_value=session), \
                mock.patch.dict(os.environ, {portcli.PASSWORD_ENV: "pw"}), \
                mock.patch("builtins.input", return_value=""), \
                mock.patch("netcheck.portverify.time.sleep"), \
                mock.patch("sys.stdout", out), mock.patch("sys.stderr", io.StringIO()):
            code = portcli.main([path, "-o", report, "--no-colour"])
        self.assertEqual(code, 1)
        text = out.getvalue()
        self.assertIn("2 unused run(s) to verify in 2 batch(es)", text)
        self.assertRegex(text, r"Gi1/0/1\s+->\s+PP-Z:5\s+\(SW-CORE-01 Gi1/0/5\)")
        self.assertIn("Patched to sw-core-01 Gi1/0/9, expected SW-CORE-01 Gi1/0/6", text)
        with open(report) as fh:
            rows = json.load(fh)["results"]
        self.assertEqual([(r["panel_port"], r["result"], r["seen_port"]) for r in rows],
                         [("5", PASS, "Gi1/0/5"), ("6", FAIL, "Gi1/0/9")])


if __name__ == "__main__":
    unittest.main()
