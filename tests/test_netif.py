"""Wired-interface detection and binding traffic to it (Windows, Linux, macOS)."""

import io
import ipaddress
import json
import os
import tempfile
import textwrap
import unittest
from types import SimpleNamespace
from unittest import mock

from netcheck import discover as discovermod
from netcheck import mac as macmod
from netcheck import netif
from netcheck.netif import (
    AmbiguousInterface, Interface, InterfaceError, choose_interface, parse_hardware_ports,
    parse_ifconfig, parse_ip_addr, parse_linux, parse_windows,
)
from netcheck.ping import build_ping_command

# A wired interface the CLI/GUI tests can use, whatever the test machine has.
FAKE_IFACE = Interface("Ethernet", description="Test NIC", wired=True, up=True,
                       ipv4=[ipaddress.IPv4Interface("192.168.1.240/24")], index=7)


def patch_wired(candidates=(FAKE_IFACE,)):
    """Patch the wired-interface list seen by the tool."""
    return mock.patch("netcheck.netif.wired_candidates", return_value=list(candidates))


WINDOWS_JSON = json.dumps({
    "adapters": [
        {"Name": "Ethernet", "InterfaceDescription": "Intel(R) Ethernet I219-LM", "ifIndex": 7,
         "Status": "Up", "PhysicalMediaType": "802.3", "MacAddress": "00-1A-2B-3C-4D-5E"},
        {"Name": "Wi-Fi", "InterfaceDescription": "Intel(R) Wi-Fi 6 AX201", "ifIndex": 12,
         "Status": "Up", "PhysicalMediaType": "Native 802.11", "MacAddress": "00-1A-2B-3C-4D-5F"},
        {"Name": "Ethernet 2", "InterfaceDescription": "Realtek USB GbE Family Controller",
         "ifIndex": 21, "Status": "Disconnected", "PhysicalMediaType": "802.3",
         "MacAddress": "00-1A-2B-3C-4D-60"},
    ],
    "addresses": [
        {"InterfaceIndex": 7, "IPAddress": "192.168.1.240", "PrefixLength": 24},
        {"InterfaceIndex": 12, "IPAddress": "192.168.1.77", "PrefixLength": 24},
        {"InterfaceIndex": 21, "IPAddress": "169.254.10.20", "PrefixLength": 16},
        {"InterfaceIndex": 1, "IPAddress": "127.0.0.1", "PrefixLength": 8},
    ],
})

MAC_PORTS = textwrap.dedent("""\
    Hardware Port: Wi-Fi
    Device: en0
    Ethernet Address: 3c:22:fb:00:00:01

    Hardware Port: USB 10/100/1000 LAN
    Device: en7
    Ethernet Address: 00:e0:4c:00:00:02

    Hardware Port: Thunderbolt Bridge
    Device: bridge0
    Ethernet Address: N/A
    """)
MAC_IFCONFIG = textwrap.dedent("""\
    en7: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
    \tether 00:e0:4c:00:00:02
    \tinet 192.168.1.240 netmask 0xfffffe00 broadcast 192.168.1.255
    \tmedia: autoselect (1000baseT <full-duplex>)
    \tstatus: active
    """)


class ParserTests(unittest.TestCase):
    def test_windows(self):
        by_name = {i.name: i for i in parse_windows(WINDOWS_JSON)}
        eth = by_name["Ethernet"]
        self.assertEqual((eth.wired, eth.up, eth.address, eth.index), (True, True, "192.168.1.240", 7))
        self.assertFalse(by_name["Wi-Fi"].wired)
        usb = by_name["Ethernet 2"]
        self.assertTrue(usb.wired)
        self.assertFalse(usb.up)
        self.assertEqual(usb.ipv4, [])  # link-local 169.254.x.x doesn't count
        self.assertEqual(parse_windows(""), [])
        # PowerShell gives a single object (not a list) when there's only one adapter.
        single = json.loads(WINDOWS_JSON)
        single["adapters"] = single["adapters"][0]
        self.assertEqual([i.name for i in parse_windows(json.dumps(single))], ["Ethernet"])

    def test_linux(self):
        root = tempfile.mkdtemp()

        def make(name, type_="1", state="up", device=True, wireless=False):
            base = os.path.join(root, name)
            os.makedirs(base)
            for fname, value in (("type", type_), ("operstate", state), ("address", "aa:bb")):
                with open(os.path.join(base, fname), "w") as fh:
                    fh.write(value + "\n")
            if device:
                os.makedirs(os.path.join(base, "device"))
            if wireless:
                os.makedirs(os.path.join(base, "wireless"))

        make("enp0s31f6")                              # built-in Ethernet
        make("wlp2s0", wireless=True)                  # Wi-Fi
        make("docker0", device=False)                  # virtual bridge
        make("lo", type_="772", state="unknown", device=False)
        make("enx00e04c000002", state="down")          # USB Ethernet, unplugged
        ip_addr = textwrap.dedent("""\
            1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever
            2: enp0s31f6    inet 192.168.1.240/23 brd 192.168.1.255 scope global enp0s31f6
            3: wlp2s0    inet 192.168.1.77/24 brd 192.168.1.255 scope global dynamic wlp2s0
            4: docker0    inet 172.17.0.1/16 brd 172.17.255.255 scope global docker0
            """)
        self.assertEqual(parse_ip_addr(ip_addr)["enp0s31f6"],
                         [ipaddress.IPv4Interface("192.168.1.240/23")])
        by_name = {i.name: i for i in parse_linux(root, ip_addr)}
        self.assertEqual({n for n, i in by_name.items() if i.wired},
                         {"enp0s31f6", "enx00e04c000002"})
        self.assertTrue(by_name["enp0s31f6"].up)
        self.assertFalse(by_name["enx00e04c000002"].up)
        self.assertFalse(by_name["wlp2s0"].wired)
        self.assertFalse(by_name["docker0"].wired)

    def test_macos(self):
        self.assertEqual(parse_hardware_ports(MAC_PORTS),
                         [("Wi-Fi", "en0"), ("USB 10/100/1000 LAN", "en7"),
                          ("Thunderbolt Bridge", "bridge0")])
        up, addresses = parse_ifconfig(MAC_IFCONFIG)
        self.assertTrue(up)
        self.assertEqual(addresses, [ipaddress.IPv4Interface("192.168.1.240/23")])
        with mock.patch.object(netif, "_run",
                               side_effect=lambda cmd, **kw: MAC_PORTS if cmd[0] == "networksetup"
                               else (MAC_IFCONFIG if cmd[1] == "en7" else "status: active")):
            wired = netif.wired_candidates("Darwin")
        self.assertEqual([(i.name, i.description) for i in wired], [("en7", "USB 10/100/1000 LAN")])


class ChooseTests(unittest.TestCase):
    def setUp(self):
        self.second = Interface("Ethernet 2", description="Realtek USB GbE", wired=True, up=True,
                                ipv4=[ipaddress.IPv4Interface("10.0.5.20/24")], index=21)

    def test_one_is_chosen_automatically(self):
        self.assertIs(choose_interface(candidates=[FAKE_IFACE]), FAKE_IFACE)

    def test_none(self):
        with self.assertRaises(InterfaceError) as ctx:
            choose_interface(candidates=[])
        self.assertIn("No wired Ethernet connection", str(ctx.exception))
        self.assertIn("Wi-Fi is never used", str(ctx.exception))

    def test_several_need_a_choice(self):
        with self.assertRaises(AmbiguousInterface) as ctx:
            choose_interface(candidates=[FAKE_IFACE, self.second])
        self.assertEqual(ctx.exception.candidates, [FAKE_IFACE, self.second])
        for wanted in ("ethernet 2", "Realtek USB GbE", "10.0.5.20", "21"):
            self.assertIs(choose_interface(wanted, [FAKE_IFACE, self.second]), self.second, wanted)
        with self.assertRaises(InterfaceError):
            choose_interface("wlan0", [FAKE_IFACE, self.second])
        self.assertIsNone(choose_interface("any", []))  # escape hatch: let the OS choose


class BindingTests(unittest.TestCase):
    def test_ping_command(self):
        self.assertEqual(build_ping_command("10.0.0.1", 1, 1, "Windows", FAKE_IFACE),
                         ["ping", "-n", "1", "-w", "1000", "-S", "192.168.1.240", "10.0.0.1"])
        self.assertEqual(build_ping_command("10.0.0.1", 1, 1, "Linux", FAKE_IFACE),
                         ["ping", "-c", "1", "-W", "1", "-I", "Ethernet", "10.0.0.1"])
        self.assertEqual(build_ping_command("10.0.0.1", 1, 1, "Darwin", FAKE_IFACE),
                         ["ping", "-c", "1", "-W", "1000", "-b", "Ethernet", "10.0.0.1"])
        self.assertNotIn("-S", build_ping_command("10.0.0.1", 1, 1, "Windows"))

    def test_arp_commands(self):
        calls = []

        def fake_run(cmd, timeout=5.0):
            calls.append(cmd)
            return "192.168.1.1 dev Ethernet lladdr 00:1a:2b:3c:4d:01 REACHABLE\n"

        with mock.patch.object(macmod, "_run", fake_run):
            macmod.lookup_mac("192.168.1.1", "Windows", iface=FAKE_IFACE)
            macmod.read_arp_table("Windows", iface=FAKE_IFACE)
            macmod.lookup_mac("192.168.1.1", "Linux", iface=FAKE_IFACE)
            table = macmod.read_arp_table("Linux", iface=FAKE_IFACE)
            macmod.read_arp_table("Darwin", iface=FAKE_IFACE)
        self.assertEqual(calls, [
            ["arp", "-a", "192.168.1.1", "-N", "192.168.1.240"],
            ["arp", "-a", "-N", "192.168.1.240"],
            ["ip", "neigh", "show", "192.168.1.1", "dev", "Ethernet"],
            ["ip", "-4", "neigh", "show", "dev", "Ethernet"],
            ["arp", "-an", "-i", "Ethernet"],
        ])
        self.assertEqual(table, {"192.168.1.1": "00:1a:2b:3c:4d:01"})

    def test_discover_uses_interface(self):
        iface = Interface("eth1", wired=True, up=True,
                          ipv4=[ipaddress.IPv4Interface("10.1.2.3/23")])
        self.assertEqual(discovermod.primary_address(iface), "10.1.2.3/23")
        self.assertEqual(discovermod.suggest_subnets([], fallback="10.1.2.3/23"), ["10.1.2.0/23"])
        nets = discovermod.parse_subnets("10.1.2.0/23")
        self.assertEqual(discovermod.local_addresses(nets, iface), {"10.1.2.3"})
        self.assertEqual(discovermod.local_addresses(discovermod.parse_subnets("10.9.0.0/24"),
                                                     iface), set())
        warning = discovermod.off_subnet_warning(discovermod.parse_subnets("10.9.0.0/24"),
                                                 set(), iface)
        self.assertIn("Its wired interface (eth1) is 10.1.2.3, so you may want 10.1.2.0/23",
                      warning)

    def test_check_all_and_discover_bind_their_pings(self):
        from netcheck import checker
        calls = []
        with mock.patch("netcheck.ping.subprocess.run",
                        side_effect=lambda cmd, **kw: calls.append(cmd) or SimpleNamespace(
                            stdout="", stderr="")), \
                mock.patch("netcheck.mac._run", return_value=""), \
                mock.patch("netcheck.ping.platform.system", return_value="Windows"):
            discovermod.discover([], discovermod.parse_subnets("10.0.0.0/30"), local_ips=set(),
                                 iface=FAKE_IFACE, workers=1)
            checker.check_all([checker.Connection(index=1, ip="10.0.0.1")], count=1,
                              iface=FAKE_IFACE)
        self.assertEqual(len(calls), 3)  # two sweep pings + one ping test
        for cmd in calls:
            self.assertEqual(cmd[cmd.index("-S") + 1], "192.168.1.240", cmd)


class CliTests(unittest.TestCase):
    def test_list_and_choose(self):
        from netcheck import discovercli
        out, err = io.StringIO(), io.StringIO()
        with patch_wired(), mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            self.assertEqual(discovercli.main(["--list-interfaces"]), 0)
        self.assertIn("Ethernet - 192.168.1.240/24 (Test NIC)", out.getvalue())

        second = Interface("Ethernet 2", wired=True, up=True,
                           ipv4=[ipaddress.IPv4Interface("10.0.5.20/24")])
        err = io.StringIO()
        with patch_wired([FAKE_IFACE, second]), mock.patch("sys.stderr", err), \
                mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(discovercli.main(["--subnet", "192.168.1.0/30"]), 2)
        self.assertIn("More than one wired Ethernet interface", err.getvalue())
        self.assertIn("Use --interface NAME", err.getvalue())

        err = io.StringIO()
        with patch_wired([]), mock.patch("sys.stderr", err), mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(discovercli.main(["--subnet", "192.168.1.0/30"]), 2)
        self.assertIn("No wired Ethernet connection", err.getvalue())


if __name__ == "__main__":
    unittest.main()
