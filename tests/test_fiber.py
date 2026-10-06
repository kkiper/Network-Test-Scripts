"""Fiber (SFP+) run verification: 10G link, light levels, polarity, errors."""

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
    expand_port_range, parse_counters_errors, parse_interfaces_status, parse_transceiver_detail,
)
from netcheck.inventory import InventoryError, parse_inventory, parse_speed
from netcheck.portverify import (
    Assignment, TestSwitchSettings, assess_optics, plan_batches, preflight, verify_batch,
)
from tests.test_portverify import (
    STATUS_HEADER, Clock, FakeSession, cdp_entry, status_line, unused,
)

SECTION = """
           {title1:<25}High Alarm  High Warn  Low Warn   Low Alarm
           {title2:<25}Threshold   Threshold  Threshold  Threshold
Port       ({unit}){pad}({unit})     ({unit})    ({unit})    ({unit})
---------  -----------------------  ----------  ---------  ---------  ---------
"""


def dom_output(ports):
    """Build 'show interfaces transceiver detail' for {port: (rx_dbm, tx_dbm)}."""
    out = textwrap.dedent("""\
        ITU Channel not available (Wavelength not available),
        Transceiver is internally calibrated.
        mA: milliamperes, dBm: decibels (milliwatts), NA or N/A: not applicable.
        ++ : high alarm, +  : high warning, -  : low warning, -- : low alarm.
        A2D readouts (if they differ), are reported in parentheses.
        The threshold values are calibrated.
        """)
    sections = [
        ("", "Temperature", "Celsius", lambda rx, tx: "31.3", "75.0 70.0 0.0 -5.0"),
        ("", "Voltage", "Volts", lambda rx, tx: "3.29", "3.63 3.46 3.13 2.97"),
        ("", "Current", "mA", lambda rx, tx: "6.9", "10.5 10.0 2.5 2.0"),
        ("Optical", "Transmit Power", "dBm", lambda rx, tx: f"{tx}", "1.7 -1.3 -7.3 -11.3"),
        ("Optical", "Receive Power", "dBm", lambda rx, tx: f"{rx}", "2.0 -1.0 -9.9 -13.9"),
    ]
    for title1, title2, unit, value, limits in sections:
        out += SECTION.format(title1=title1, title2=title2, unit=unit,
                              pad=" " * max(1, 23 - len(unit)))
        for port, (rx, tx) in ports.items():
            flag = "  --" if value(rx, tx) == "-40.0" else ""
            out += f"{port:<11}{value(rx, tx):>6}{flag}      " + "  ".join(
                f"{v:>9}" for v in limits.split()) + "\n"
    return out


def errors_output(counts):
    out = "\nPort        Align-Err     FCS-Err    Xmit-Err     Rcv-Err  UnderSize  OutDiscards\n"
    for port, fcs in counts.items():
        out += f"{port:<12}{0:>9}{fcs:>12}{0:>12}{0:>12}{0:>11}{0:>13}\n"
    return out


def fiber_run(index, switch_port, **kw):
    return unused(index, switch_port, media="fiber", expected_speed=10000,
                  far_end=f"P{index}/S{index}", **kw)


def te_status(port="Te1/1/1", status="connected", speed="10G"):
    return status_line(port, status, duplex="full", speed=speed, media="SFP-10GBase-SR")


class FiberParserTests(unittest.TestCase):
    def test_transceiver_detail(self):
        dom = parse_transceiver_detail(dom_output({"Te1/1/1": (-2.8, -2.3),
                                                   "Te1/1/2": (-40.0, -2.4)}))
        good = dom["TenGigabitEthernet1/1/1"]
        self.assertEqual(set(good), {"temperature", "voltage", "current", "tx_power", "rx_power"})
        rx = good["rx_power"]
        self.assertEqual((rx.value, rx.low_warn, rx.low_alarm), (-2.8, -9.9, -13.9))
        self.assertEqual(rx.level(), "ok")
        self.assertIsNone(dom["TenGigabitEthernet1/1/2"]["rx_power"].value)  # no light
        self.assertEqual(dom["TenGigabitEthernet1/1/2"]["temperature"].low_alarm, -5.0)

    def test_errors_and_media_type(self):
        self.assertEqual(parse_counters_errors(errors_output({"Te1/1/1": 4})),
                         {"TenGigabitEthernet1/1/1": 4})
        st = parse_interfaces_status(STATUS_HEADER + te_status()
                                     + status_line("Te1/1/2", "sfpAbsent", media="Not Present"))
        self.assertEqual(st["TenGigabitEthernet1/1/1"].media_type, "SFP-10GBase-SR")
        self.assertEqual(st["TenGigabitEthernet1/1/1"].speed_mbps, 10000)
        self.assertEqual(st["TenGigabitEthernet1/1/2"].status, "sfpAbsent")

    def test_assess_optics(self):
        def grade(rx):
            return assess_optics(parse_transceiver_detail(
                dom_output({"Te1/1/1": (rx, -2.3)}))["TenGigabitEthernet1/1/1"])
        level, summary, problems = grade(-2.8)
        self.assertEqual((level, summary, problems),
                         ("ok", "Rx -2.8 dBm, 7.1 dB margin; Tx -2.3 dBm", []))
        level, _summary, problems = grade(-10.5)
        self.assertEqual(level, "warn")
        self.assertEqual(problems, ["Rx power -10.5 dBm is below the low warning limit (-9.9 dBm)"])
        self.assertEqual(grade(-14.5)[0], "alarm")
        self.assertEqual(grade(-40.0)[2], ["no light received"])
        self.assertEqual(assess_optics(None)[0], "unknown")


class FiberInventoryTests(unittest.TestCase):
    def test_fields(self):
        conns = parse_inventory({"connections": [
            {"switch_port": "Te1/1", "status": "unused", "media": "Fiber",
             "expected_speed": "10G"},
            {"switch_port": "Gi1/1", "status": "unused"},
        ]})
        self.assertEqual((conns[0].media, conns[0].expected_speed), ("fiber", 10000))
        self.assertEqual((conns[1].media, conns[1].expected_speed), ("copper", None))
        with self.assertRaises(InventoryError) as ctx:
            parse_inventory({"connections": [{"media": "wifi", "expected_speed": "fast"}]})
        self.assertIn("media 'wifi'", str(ctx.exception))
        self.assertIn("expected_speed 'fast'", str(ctx.exception))
        self.assertEqual([parse_speed(v) for v in ("10G", "1000", "100M", "2.5G")],
                         [10000, 1000, 100, 2500])

    def test_settings_round_trip(self):
        s = TestSwitchSettings.from_inventory({"test_switch": {
            "ports": "Gi1/0/1-2", "fiber_ports": "Te1/1/1-2", "fiber_soak": 30}})
        self.assertEqual(len(s.fiber_ports), 2)
        self.assertEqual(s.to_dict()["fiber_ports"], ["Te1/1/1", "Te1/1/2"])
        self.assertEqual(TestSwitchSettings.from_inventory({"test_switch": s.to_dict()}), s)
        self.assertNotIn("fiber_ports", TestSwitchSettings(ports=s.ports).to_dict())


class FiberPlanTests(unittest.TestCase):
    def test_fiber_rides_along_with_copper(self):
        conns = [unused(1, "Gi1/1"), unused(2, "Gi1/2"), unused(3, "Gi1/3"),
                 fiber_run(4, "Te1/1"), fiber_run(5, "Te1/2")]
        batches, skipped = plan_batches(conns, expand_port_range("Gi1/0/1-2"),
                                        expand_port_range("Te1/1/1-2"))
        self.assertEqual([[(a.test_port_short, a.connection.index) for a in b] for b in batches],
                         [[("Gi1/0/1", 1), ("Gi1/0/2", 2), ("Te1/1/1", 4), ("Te1/1/2", 5)],
                          [("Gi1/0/1", 3)]])
        self.assertEqual(skipped, [])

    def test_fiber_without_fiber_ports(self):
        batches, skipped = plan_batches([unused(1, "Gi1/1"), fiber_run(2, "Te1/1")],
                                        expand_port_range("Gi1/0/1"))
        self.assertEqual(len(batches), 1)
        self.assertEqual((skipped[0].result, skipped[0].message),
                         (SKIP, "Fiber run - no fiber (SFP+) test ports configured"))


class FiberPreflightTests(unittest.TestCase):
    def test_module_checks(self):
        settings = TestSwitchSettings(ports=expand_port_range("Gi1/0/1"),
                                      fiber_ports=expand_port_range("Te1/1/1-3"))
        session = FakeSession({
            "show interfaces status": STATUS_HEADER + status_line("Gi1/0/1", "notconnect")
            + te_status("Te1/1/1", "notconnect")
            + status_line("Te1/1/2", "sfpAbsent", duplex="full", speed="10G", media="Not Present")
            + status_line("Te1/1/3", "err-disabled", duplex="full", speed="10G", media="unknown"),
        })
        errors, _ = preflight(session, settings)
        text = "\n".join(errors)
        self.assertIn("Te1/1/2 has no SFP+ module fitted", text)
        self.assertIn("Te1/1/3 is err-disabled", text)
        self.assertIn("service unsupported-transceiver", text)

    def test_dom_and_type_warnings(self):
        settings = TestSwitchSettings(fiber_ports=expand_port_range("Te1/1/1-2"))
        session = FakeSession({
            "show interfaces status": STATUS_HEADER + te_status("Te1/1/1", "notconnect")
            + status_line("Te1/1/2", "notconnect", duplex="full", speed="1000",
                          media="1000BaseSX SFP"),
            "show interfaces transceiver detail": dom_output({"Te1/1/1": (-40.0, -2.3)}),
        })
        errors, warnings = preflight(session, settings)
        self.assertEqual(errors, [])
        self.assertIn("reports its module as '1000BaseSX SFP', not a 10G SFP+", warnings[0])
        self.assertIn("No light-level (DOM) readings from the module in Te1/1/2", warnings[1])

    def test_overlapping_ports(self):
        settings = TestSwitchSettings(ports=expand_port_range("Te1/1/1"),
                                      fiber_ports=expand_port_range("Te1/1/1"))
        errors, _ = preflight(FakeSession({}), settings)
        self.assertIn("Te1/1/1 is listed as both a copper and a fiber test port.", errors)


class FiberVerifyTests(unittest.TestCase):
    def run_fiber(self, status, rx=-2.8, cdp=True, errors=(0, 0), speed="10G",
                  dom=True, soak=60, conn=None):
        conn = conn or fiber_run(1, "Te1/1")
        batch = [Assignment(expand_port_range("Te1/1/1")[0], conn)]
        settings = TestSwitchSettings(fiber_ports=[batch[0].test_port], cdp_timeout=60,
                                      fiber_soak=soak)
        session = FakeSession({
            "show interfaces status": STATUS_HEADER + te_status(status=status, speed=speed),
            "show cdp neighbors detail":
                cdp_entry("SW-CORE-01", "Te1/1/1", "TenGigabitEthernet1/1") if cdp else "",
            "show interfaces counters errors": [errors_output({"Te1/1/1": n}) for n in errors],
            "show interfaces transceiver detail":
                dom_output({"Te1/1/1": (rx, -2.3)}) if dom else "",
        })
        clock = Clock()
        result = verify_batch(session, batch, settings, sleep=clock.sleep, clock=clock)[0]
        return result, clock, session

    def test_pass(self):
        res, clock, _ = self.run_fiber("connected")
        self.assertEqual(res.result, PASS)
        self.assertEqual(res.message,
                         "Verified via CDP: patched to sw-core-01 Te1/1 at 10 Gb/s; light levels "
                         "OK (Rx -2.8 dBm, 7.1 dB margin; Tx -2.3 dBm)")
        pc = res.port_check
        self.assertEqual((pc.link, pc.optics, pc.errors),
                         ("up 10 Gb/s", "Rx -2.8 dBm, 7.1 dB margin; Tx -2.3 dBm", "0 in 60s"))
        self.assertGreaterEqual(clock.now, 60)  # the link was watched for the soak period

    def test_reversed_fiber(self):
        res, clock, _ = self.run_fiber("notconnect", rx=-40.0, cdp=False)
        self.assertEqual(res.result, FAIL)
        self.assertIn("no light received", res.message)
        self.assertIn("swap the P and S strands", res.message)
        self.assertLess(clock.now, 60)  # gave up after the link grace period

    def test_light_but_no_link(self):
        res, _, _ = self.run_fiber("notconnect", rx=-3.1, cdp=False)
        self.assertEqual(res.result, FAIL)
        self.assertIn("Light received (Rx -3.1 dBm) but no link", res.message)

    def test_wrong_speed(self):
        res, _, _ = self.run_fiber("connected", speed="1000")
        self.assertEqual(res.result, FAIL)
        self.assertIn("link is 1 Gb/s, expected 10 Gb/s", res.message)

    def test_low_light(self):
        res, _, _ = self.run_fiber("connected", rx=-10.5)
        self.assertEqual(res.result, WARN)
        self.assertIn("Rx power -10.5 dBm is below the low warning limit", res.message)
        res, _, _ = self.run_fiber("connected", rx=-14.5)
        self.assertEqual(res.result, FAIL)

    def test_errors_during_soak(self):
        res, _, _ = self.run_fiber("connected", errors=(10, 10, 15))
        self.assertEqual(res.result, WARN)
        self.assertIn("5 receive error(s) during the 60s check", res.message)
        self.assertEqual(res.port_check.errors, "5 in 60s")

    def test_no_dom(self):
        res, _, _ = self.run_fiber("connected", dom=False)
        self.assertEqual(res.result, WARN)
        self.assertIn("light levels not available from the module", res.message)

    def test_wrong_port(self):
        res, _, _ = self.run_fiber("connected", conn=fiber_run(1, "Te1/2"))
        self.assertEqual(res.result, FAIL)
        self.assertIn("Patched to sw-core-01 Te1/1, expected SW-CORE-01 Te1/2", res.message)

    def test_cable_test_skips_fiber(self):
        conns = [unused(1, "Gi1/5"), fiber_run(2, "Te1/1")]
        batch = [Assignment(expand_port_range("Gi1/0/1")[0], conns[0]),
                 Assignment(expand_port_range("Te1/1/1")[0], conns[1])]
        settings = TestSwitchSettings(ports=[batch[0].test_port],
                                      fiber_ports=[batch[1].test_port], cable_test=True,
                                      fiber_soak=10)
        session = FakeSession({
            "show interfaces status": STATUS_HEADER + status_line("Gi1/0/1") + te_status(),
            "show cdp neighbors detail": cdp_entry("SW-CORE-01", "Gi1/0/1", "Gi1/5")
            + cdp_entry("SW-CORE-01", "Te1/1/1", "Te1/1"),
            "show interfaces transceiver detail": dom_output({"Te1/1/1": (-2.8, -2.3)}),
        })
        clock = Clock()
        results = verify_batch(session, batch, settings, sleep=clock.sleep, clock=clock)
        self.assertIn("test cable-diagnostics tdr interface Gi1/0/1", session.commands)
        self.assertNotIn("test cable-diagnostics tdr interface Te1/1/1", session.commands)
        self.assertEqual([r.result for r in results], [PASS, PASS])


class FiberCliTests(unittest.TestCase):
    def test_fiber_only_run(self):
        data = {"connections": [
            {"patch_panel": "FIBER", "panel_port": "1", "switch": "SW-CORE-01",
             "switch_port": "Te1/1", "status": "unused", "media": "fiber",
             "expected_speed": "10G", "far_end": "P1/S1"}]}
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
        self.addCleanup(os.remove, path)
        session = FakeSession({
            "show cdp neighbors": "",
            "show interfaces status": STATUS_HEADER + te_status(),
            "show cdp neighbors detail": cdp_entry("SW-CORE-01", "Te1/1/1", "Te1/1"),
            "show interfaces transceiver detail": dom_output({"Te1/1/1": (-2.8, -2.3)}),
        })
        out = io.StringIO()
        with mock.patch.object(portcli, "connect", return_value=session), \
                mock.patch.dict(os.environ, {portcli.PASSWORD_ENV: "pw"}), \
                mock.patch("builtins.input", return_value=""), \
                mock.patch("netcheck.portverify.POLL_INTERVAL_S", 0), \
                mock.patch("netcheck.portverify.DEFAULT_FIBER_SOAK_S", 0), \
                mock.patch("sys.stdout", out), mock.patch("sys.stderr", io.StringIO()):
            code = portcli.main([path, "--host", "h", "--username", "u",
                                 "--fiber-ports", "Te1/1/1", "--fiber-soak", "0", "--no-colour"])
        text = out.getvalue()
        self.assertEqual(code, 0, text)
        self.assertRegex(text, r"Te1/1/1\s+->\s+P1/S1\s+\(SW-CORE-01 Te1/1\)")
        self.assertIn("at 10 Gb/s; light levels OK", text)


if __name__ == "__main__":
    unittest.main()
