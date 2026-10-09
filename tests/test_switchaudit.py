"""Switch Port Audit: read-only reading of the production switches' tables."""

import io
import json
import os
import tempfile
import unittest
from unittest import mock

from netcheck import auditcli
from netcheck.checker import FAIL, PASS, WARN
from netcheck.cisco import parse_cdp_neighbors_detail, parse_mac_address_table
from netcheck.inventory import Connection, InventoryError
from netcheck.checker import MAC_DISCOVERED, SKIP, check_all
from netcheck.ping import PingResult
from netcheck.switchaudit import (
    AUDIT_COMMANDS, audit, collect, combine, find_uplinks, logins_from_inventory, new_entry,
)
from tests.test_netif import patch_wired
from tests.test_portverify import STATUS_HEADER, cdp_entry, status_line


def mac_table(rows):
    """'show mac address-table' output for [(vlan, cisco-mac, type, port), ...]."""
    out = ("          Mac Address Table\n-------------------------------------------\n\n"
           "Vlan    Mac Address       Type        Ports\n"
           "----    -----------       --------    -----\n"
           " All    0100.0ccc.cccc    STATIC      CPU\n"
           " All    ffff.ffff.ffff    STATIC      CPU\n")
    for vlan, mac, kind, port in rows:
        out += f"{vlan:>4}    {mac}    {kind:<8}    {port}\n"
    return out + f"Total Mac Addresses for this criterion: {len(rows) + 2}\n"


CORE_STATUS = (STATUS_HEADER
               + status_line("Gi1/0/1", vlan="10")
               + status_line("Gi1/0/2", vlan="10")
               + status_line("Gi1/0/3", vlan="10")
               + status_line("Gi1/0/4", vlan="10", duplex="full", speed="10G",
                             media="10GBase-T")
               + status_line("Gi1/0/5", "notconnect", vlan="10", duplex="auto", speed="auto")
               + status_line("Gi1/0/6", vlan="10")
               + status_line("Gi1/0/7", vlan="10")
               + status_line("Gi1/0/8", "notconnect", vlan="10", duplex="auto", speed="auto")
               + status_line("Gi1/0/9", vlan="10")
               + status_line("Gi1/0/12", vlan="10")
               + status_line("Gi1/0/24", vlan="trunk"))
CORE_MACS = mac_table([
    (10, "001a.2b3c.4d01", "DYNAMIC", "Gi1/0/1"),   # firewall, where expected
    (10, "001a.2b3c.4d99", "DYNAMIC", "Gi1/0/2"),   # a different device on DB's port
    (10, "001a.2b3c.4d11", "DYNAMIC", "Gi1/0/3"),   # app server, no expected MAC yet
    (10, "001a.2b3c.4d66", "DYNAMIC", "Gi1/0/6"),   # something on an "unused" port
    (10, "001a.2b3c.4d10", "DYNAMIC", "Gi1/0/7"),   # the DB server, on the wrong port
    (10, "001a.2b3c.4d70", "DYNAMIC", "Gi1/0/9"),   # right device, too slow
    (10, "001a.2b3c.4d60", "DYNAMIC", "Gi1/0/24"),  # camera, seen through the uplink
])
CORE_CDP = cdp_entry("SW-EDGE-02", "GigabitEthernet1/0/24", "GigabitEthernet1/0/1",
                     platform="cisco IE-3300")

EDGE_STATUS = (STATUS_HEADER + status_line("Gi1/0/1", vlan="trunk")
               + status_line("Gi1/0/4", vlan="10"))
EDGE_MACS = mac_table([(10, "001a.2b3c.4d60", "DYNAMIC", "Gi1/0/4")])
EDGE_CDP = cdp_entry("SW-CORE-01", "GigabitEthernet1/0/1", "GigabitEthernet1/0/24")


class RecordingSession:
    """Answers the audit's show commands and records everything sent."""

    def __init__(self, responses):
        self.responses = responses
        self.sent = []

    def send(self, command):
        self.sent.append(command)
        return self.responses.get(command, "")

    def run(self, command):  # must never be used by the audit
        self.sent.append(command)
        return ""

    def close(self):
        pass


def core_session():
    return RecordingSession({"show interfaces status": CORE_STATUS,
                             "show mac address-table": CORE_MACS,
                             "show cdp neighbors detail": CORE_CDP,
                             "show lldp neighbors detail": "% LLDP is not enabled"})


def edge_session():
    return RecordingSession({"show interfaces status": EDGE_STATUS,
                             "show mac address-table": EDGE_MACS,
                             "show cdp neighbors detail": EDGE_CDP})


def row(index, port, switch="SW-CORE-01", **kw):
    return Connection(index=index, switch=switch, switch_port=port, **kw)


INVENTORY = [
    row(1, "Gi1/0/1", device="Firewall-01", ip="192.168.1.1", expected_mac="00:1a:2b:3c:4d:01"),
    row(2, "Gi1/0/2", device="Server-DB-01", ip="192.168.1.10", expected_mac="00:1a:2b:3c:4d:10"),
    row(3, "Gi1/0/3", device="Server-APP-01", ip="192.168.1.11"),
    row(4, "Gi1/0/4", device="XR5610 (no OS)", expected_speed=10000),
    row(5, "Gi1/0/5", status="unused"),
    row(6, "Gi1/0/6", status="unused"),
    row(7, "Gi1/0/8", device="Printer-2F", expected_mac="00:1a:2b:3c:4d:50"),
    row(8, "Gi1/0/9", device="Encoder", expected_mac="00:1a:2b:3c:4d:70", expected_speed=10000),
    row(9, "Gi1/0/4", switch="SW-EDGE-02", device="Camera-05", expected_mac="00:1a:2b:3c:4d:60"),
    row(10, "Fa0/1", switch="SW-ACCESS-01", device="Workstation", ip="192.168.1.101"),
]


class ParserTests(unittest.TestCase):
    def test_mac_table(self):
        table = parse_mac_address_table(CORE_MACS)
        self.assertNotIn("CPU", " ".join(table))
        self.assertEqual([e.mac for e in table["GigabitEthernet1/0/7"]], ["00:1a:2b:3c:4d:10"])
        self.assertEqual(table["GigabitEthernet1/0/1"][0].vlan, "10")

    def test_uplinks(self):
        neighbors = {n.local_interface: [n] for n in parse_cdp_neighbors_detail(CORE_CDP)}
        self.assertEqual(find_uplinks({}, neighbors),
                         {"GigabitEthernet1/0/24": "uplink to sw-edge-02"})
        busy = parse_mac_address_table(mac_table(
            [(10, f"001a.2b3c.{i:04x}", "DYNAMIC", "Gi1/0/20") for i in range(9)]))
        self.assertIn("9 MACs", find_uplinks(busy, {})["GigabitEthernet1/0/20"])


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.core, self.edge = core_session(), edge_session()
        states = {"SW-CORE-01": collect(self.core, "SW-CORE-01"),
                  "SW-EDGE-02": collect(self.edge, "SW-EDGE-02")}
        results, self.unlisted = audit(INVENTORY, states)
        self.by_row = {r.connection.index: r for r in results}

    def test_only_show_commands_are_sent(self):
        for session in (self.core, self.edge):
            self.assertEqual(session.sent, list(AUDIT_COMMANDS))

    def test_rows(self):
        got = {i: (r.result, r.message) for i, r in self.by_row.items()}
        self.assertEqual(got[1], (PASS, "Expected MAC 00:1a:2b:3c:4d:01 seen on this port"))
        self.assertEqual(got[2][0], FAIL)
        self.assertIn("Expected MAC 00:1a:2b:3c:4d:10 is on SW-CORE-01 Gi1/0/7, not here",
                      got[2][1])
        self.assertIn("this port has 00:1a:2b:3c:4d:99", got[2][1])
        self.assertEqual(got[3], (PASS, "Link up; MAC 00:1a:2b:3c:4d:11 discovered "
                                        "(no expected MAC to compare)"))
        self.assertEqual(self.by_row[3].discovered_mac, "00:1a:2b:3c:4d:11")
        self.assertEqual(got[4][0], WARN)
        self.assertIn("Link up at 10 Gb/s but no traffic seen", got[4][1])
        self.assertIn("may have no OS", got[4][1])
        self.assertEqual(got[5], (PASS, "Empty as expected (link down)"))
        self.assertEqual(got[6][0], FAIL)
        self.assertIn("Should be unused, but something is connected", got[6][1])
        self.assertEqual(got[7], (FAIL, "No link on Gi1/0/8 (down)"))
        self.assertEqual(got[8][0], FAIL)
        self.assertIn("link is 1 Gb/s, expected 10 Gb/s", got[8][1])
        # The camera is on SW-EDGE-02, found there - not on SW-CORE-01's uplink.
        self.assertEqual(got[9], (PASS, "Expected MAC 00:1a:2b:3c:4d:60 seen on this port"))
        self.assertNotIn(10, got)  # SW-ACCESS-01 wasn't audited
        pc = self.by_row[1].port_check
        self.assertEqual((pc.seen_switch, pc.seen_port, pc.link, pc.macs),
                         ("SW-CORE-01", "Gi1/0/1", "up 1 Gb/s", "00:1a:2b:3c:4d:01"))

    def test_unlisted(self):
        found = {(u.switch, u.port_short): u for u in self.unlisted}
        self.assertEqual(set(found), {("SW-CORE-01", "Gi1/0/7"), ("SW-CORE-01", "Gi1/0/12")})
        self.assertEqual(found[("SW-CORE-01", "Gi1/0/7")].macs, ["00:1a:2b:3c:4d:10"])
        self.assertEqual(found[("SW-CORE-01", "Gi1/0/12")].macs, [])  # link only, silent
        entry = new_entry(found[("SW-CORE-01", "Gi1/0/7")])
        self.assertEqual((entry["switch"], entry["switch_port"], entry["expected_mac"],
                          entry["status"]),
                         ("SW-CORE-01", "Gi1/0/7", "00:1a:2b:3c:4d:10", "connected"))

    def test_logins(self):
        data = {"switches": {"SW-CORE-01": {"host": "192.168.1.2", "username": "ro"}}}
        logins = logins_from_inventory(data, INVENTORY)
        self.assertEqual(list(logins), ["SW-ACCESS-01", "SW-CORE-01", "SW-EDGE-02"])
        self.assertEqual((logins["SW-CORE-01"].host, logins["SW-EDGE-02"].host),
                         ("192.168.1.2", ""))
        self.assertEqual(logins["SW-CORE-01"].to_dict(), {"host": "192.168.1.2", "username": "ro"})
        with self.assertRaises(InventoryError):
            logins_from_inventory({"switches": ["SW-CORE-01"]}, [])


class CombineTests(unittest.TestCase):
    """A ping test followed by the switch port check, merged per row."""

    def setUp(self):
        self.states = {"SW-CORE-01": collect(core_session(), "SW-CORE-01"),
                       "SW-EDGE-02": collect(edge_session(), "SW-EDGE-02")}
        self.rows = [
            row(1, "Gi1/0/1", device="Firewall-01", ip="192.168.1.1",
                expected_mac="00:1a:2b:3c:4d:01"),
            row(2, "Gi1/0/3", device="Server-APP-01", ip="192.168.1.11"),  # no MAC recorded
            row(3, "Gi1/0/7", device="Server-DB-01", ip="192.168.1.10"),   # no MAC recorded
            row(4, "Gi1/0/9", device="HMS 2 - CH 2"),                      # no IP, no MAC
            row(5, "Gi1/0/5", status="unused"),
            row(6, "Gi1/0/24", device="SW-EDGE-02 (daisy-chain uplink)", ip="192.168.1.3"),
            row(7, "Gi1/0/1", switch="SW-EDGE-02", device="SW-CORE-01 uplink", ip="192.168.1.2"),
            row(8, "Gi1/0/4", switch="SW-EDGE-02", device="Camera-05", ip="192.168.1.60"),
        ]
        arp = {"192.168.1.1": "00:1a:2b:3c:4d:01", "192.168.1.11": "00:1a:2b:3c:4d:11",
               "192.168.1.10": "00:1a:2b:3c:4d:99", "192.168.1.3": "00:1a:2b:3c:4d:e2",
               "192.168.1.2": "00:1a:2b:3c:4d:c1", "192.168.1.60": "00:1a:2b:3c:4d:60",
               "192.168.1.70": "00:1a:2b:3c:4d:70"}
        pings = check_all(self.rows, ping_fn=lambda ip, count, timeout_s: PingResult(True, 2, 2, 1.0),
                          mac_fn=arp.get, arp_fn=dict)
        results, self.unlisted = combine(pings, self.rows, self.states, arp)
        self.by_row = {r.connection.index: r for r in results}

    def test_ping_mac_is_checked_on_the_port(self):
        app = self.by_row[2]
        self.assertEqual((app.result, app.mac_check), (PASS, MAC_DISCOVERED))
        self.assertIn("Switch SW-CORE-01 Gi1/0/3: MAC 00:1a:2b:3c:4d:11 (answering at "
                      "192.168.1.11) seen on this port", app.message)
        # 192.168.1.10 answers with :99, which the switch has on Gi1/0/2, not Gi1/0/7.
        db = self.by_row[3]
        self.assertEqual(db.result, FAIL)
        self.assertIn("MAC 00:1a:2b:3c:4d:99 (answering at 192.168.1.10) is on SW-CORE-01 "
                      "Gi1/0/2, not here", db.message)
        self.assertEqual(self.by_row[1].result, PASS)
        self.assertTrue(self.by_row[1].message.startswith("Ping: Reachable, MAC matches. Switch"))

    def test_rows_without_ip_and_unused_rows_are_checked_by_the_switch(self):
        hms = self.by_row[4]
        self.assertEqual((hms.result, hms.ping, hms.discovered_mac),
                         (PASS, None, "00:1a:2b:3c:4d:70"))
        self.assertIn("it answers at 192.168.1.70 - add this IP to the inventory", hms.message)
        self.assertEqual(self.by_row[5].result, PASS)
        self.assertEqual(self.by_row[5].message, "Empty as expected (link down)")

    def test_uplinks_named_in_the_row_pass(self):
        self.assertEqual(self.by_row[6].result, PASS)
        self.assertIn("Uplink to sw-edge-02 as expected", self.by_row[6].message)
        self.assertIn("Uplink to sw-core-01 as expected", self.by_row[7].message)
        # The camera's MAC is learned on SW-CORE-01's uplink too, but found on SW-EDGE-02.
        self.assertIn("Switch SW-EDGE-02 Gi1/0/4: MAC 00:1a:2b:3c:4d:60 (answering at "
                      "192.168.1.60) seen on this port", self.by_row[8].message)

    def test_switch_not_read_keeps_ping_result(self):
        pings = check_all([self.rows[0], row(9, "Gi1/0/8")],
                          ping_fn=lambda ip, count, timeout_s: PingResult(True, 2, 2, 1.0),
                          mac_fn=lambda ip: "00:1a:2b:3c:4d:01", arp_fn=dict)
        results, _ = combine(pings, self.rows, {}, {})
        self.assertEqual([r.port_check for r in results], [None, None])
        self.assertEqual(results[1].result, SKIP)


class CliTests(unittest.TestCase):
    def test_end_to_end(self):
        data = {
            "switches": {"SW-CORE-01": {"host": "192.168.1.2", "username": "netcheck-ro"},
                         "SW-EDGE-02": {"host": "192.168.1.3", "username": "netcheck-ro"}},
            "connections": [
                {"switch": "SW-CORE-01", "switch_port": "Gi1/0/1", "device": "Firewall-01",
                 "ip": "192.168.1.1", "expected_mac": "00:1a:2b:3c:4d:01", "status": "connected"},
                {"switch": "SW-CORE-01", "switch_port": "Gi1/0/5", "status": "unused"},
                {"switch": "SW-EDGE-02", "switch_port": "Gi1/0/4", "device": "Camera-05",
                 "expected_mac": "00:1a:2b:3c:4d:60", "status": "connected"},
            ]}
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
        self.addCleanup(os.remove, path)
        sessions = {"192.168.1.2": core_session(), "192.168.1.3": edge_session()}
        calls = []

        def fake_connect(host, username, password, device_type, ssh_port, iface):
            calls.append((host, username, iface.name if iface else None))
            return sessions[host]

        out = io.StringIO()
        with mock.patch.object(auditcli, "connect_to", fake_connect), \
                mock.patch.dict(os.environ, {auditcli.PASSWORD_ENV: "pw"}), \
                mock.patch("sys.stdout", out), mock.patch("sys.stderr", io.StringIO()), \
                patch_wired():
            code = auditcli.main([path, "--no-colour"])
        text = out.getvalue()
        self.assertEqual(code, 1)  # unlisted ports exist on SW-CORE-01
        self.assertEqual(calls, [("192.168.1.2", "netcheck-ro", "Ethernet"),
                                 ("192.168.1.3", "netcheck-ro", "Ethernet")])
        self.assertIn("Summary: 3 connections - 3 pass, 0 fail, 0 warn, 0 skipped", text)
        self.assertIn("Expected MAC 00:1a:2b:3c:4d:60 seen on this port", text)
        self.assertRegex(text, r"UNLISTED\s+SW-CORE-01 Gi1/0/12\s+link up 1 Gb/s\s+no MAC seen")
        self.assertIn("A switch only learns MAC addresses from traffic", text)
        for session in sessions.values():
            self.assertTrue(all(c.startswith("show ") for c in session.sent))


if __name__ == "__main__":
    unittest.main()
