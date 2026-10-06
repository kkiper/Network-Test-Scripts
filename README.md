# Network Test Scripts

Tools for verifying a local network against its **expected physical interconnect**
(which device is patched into which patch-panel port and switch port).

Everything is available from the desktop GUI (`interconnect_gui.py`) or the command line.

**Connected devices** (`interconnect_test.py`):

- Ping every device that should be connected.
- Discover each device's MAC address from this machine's ARP/neighbour table.
- Compare the discovered MAC against the expected MAC.
- Capture a MAC "baseline" for devices whose MAC isn't recorded yet.

**Unused runs** (`port_verify.py`), using a spare Cisco switch as a test probe:

- Patch test-switch ports to the far ends of unused runs, a batch at a time.
- Read the production switch's CDP/LLDP advertisement to see exactly which switch port
  each run lands on, and compare it with the expected port. Production switches are
  never logged into or reconfigured.
- Flag runs with no link, a degraded link speed, or (optionally) a cable fault found by
  the switch's TDR cable test.
- **Fiber (SFP+) runs:** check each channel links at the expected speed (e.g. 10G), that the
  transceiver light levels are within the module's limits, and that no errors occur while
  the link is watched. If there's no link, report whether the strands are probably reversed.

Every patch-panel port is reported as PASS / FAIL / WARN / SKIP, on screen and as CSV or JSON.

Possible future addition: read-only access to the production switches, to check the switch
port of each *connected* device from the MAC address table, and to check ports that are shut down.

## Requirements

- Python 3.8+ (standard library only, nothing to install). The GUI uses Tkinter, which
  is included with the python.org installers for Windows and macOS; on Linux install
  it with your package manager (e.g. `sudo apt install python3-tk`).
- The OS `ping` command. Works on Windows, Linux and macOS.
- Run it from a machine on the **same subnet/VLAN** as the devices: MAC addresses are
  only visible via ARP for hosts on the local layer-2 segment. Devices behind a router
  will ping fine but show `UNRESOLVED` MACs (reported as WARN).
- Unused-run verification only: a Cisco IOS XE test switch (e.g. Catalyst C9200L-24T-4X-E; its 10G SFP+ uplinks are needed for fiber runs) and the
  Netmiko package: `pip install -r requirements.txt`.

## Desktop GUI

![Interconnect test GUI](docs/gui.png)

Start it with:

```sh
python interconnect_gui.py                      # then File > Open
python interconnect_gui.py my_network.json      # open a file straight away
```

On Windows you can double-click **`interconnect_gui.pyw`** to start it without a console window.

1. **Open** an expected interconnect file, or build one from scratch with **Add Connection**
   (one row per patch-panel port; the new row is pre-filled with the selected row's
   panel and switch). Double-click a row to **Edit** it. In the connection form, press
   **Help** (or **?** next to a field) for what each field means, an example, and how the
   tests use it. Mistakes such as invalid IPs or
   MACs and duplicate IPs, MACs or ports are caught as you enter them, and any invalid
   rows in an opened file are highlighted.
2. Pick the ping settings and optionally a single **Switch** or **Patch panel**, then press
   **Ping Test** (F5). Rows turn green/red/yellow as results come in; **Stop** cancels the
   rest of the run.
3. Select a row to see the full details below the table. Click a column heading to sort.
4. **Accept Discovered MACs** records the MACs of devices that didn't have one yet
   (mismatched MACs are never overwritten), then **Save** (Ctrl+S).
5. **Verify Unused Ports...** opens the test switch window (see below).
6. **Export Report** saves all results, ping and port verification, as CSV (opens in Excel) or JSON.

## Verifying unused runs with a test switch

An unused run has no device to ping, so a spare Cisco switch stands in for one:

![How the test switch connects to the production switch](docs/test_switch_setup.svg)

(A PNG copy for printing: [`docs/test_switch_setup.png`](docs/test_switch_setup.png).)

When the link comes up, the production switch (e.g. an ESS 3300) advertises itself over
CDP, which Cisco switches send by default. The test switch then reports
"SW-CORE-01, port Gi1/0/5", and the tool compares that with the inventory.

**1. Set up the test switch once** with [`docs/test_switch_c9200.cfg`](docs/test_switch_c9200.cfg).
The important part is that the test ports are **routed ports** (`no switchport`). Routed
ports never bridge production ports together, so many runs to the same switch can be
connected at once without making a loop. They also send no BPDUs, so BPDU guard on the
production ports isn't triggered. The tool refuses to use test ports that aren't routed.

**2. In the GUI**, click **Verify Unused Ports...**:

![Verify unused ports](docs/port_verify.png)

1. Enter the test switch's management IP, username and password (the password is never
   saved), and the test ports to use, e.g. `Gi1/0/1-23`. Press **Connect**. The tool checks
   that CDP is on and that the test ports are routed and enabled.
2. The unused runs are split into batches, one test port per run. **Export Cabling Plan**
   gives a printable list of the batches.
3. Patch the test ports as listed in **Connect To**, then press **Verify Batch**. It waits
   (up to the CDP wait time, 90 s by default) until every port has heard its neighbour or
   has clearly no link.
4. Disconnect the cables, press **Next**, and repeat. The results also appear in the main
   window and in the exported report.

**Or from the command line**, which prompts for each batch:

```sh
pip install -r requirements.txt
# my_network.json = your inventory file (see "Describing the expected interconnect")
python port_verify.py my_network.json --host 192.168.100.2 --username admin --ports Gi1/0/1-23
```

Options: `--fiber-ports Te1/1/1-2` and `--fiber-soak SECONDS` for fiber runs, `--cable-test` (TDR), `--timeout SECONDS`, `--switch NAME` / `--patch-panel NAME`
filters, `-o report.csv`. The password can also come from `$NETCHECK_SWITCH_PASSWORD`, and an
enable secret from `$NETCHECK_ENABLE_SECRET`. Settings can be stored in the inventory instead
(see `test_switch` below).

| Result | Meaning |
|--------|---------|
| PASS | Neighbour heard on the expected production switch and port. |
| FAIL | Heard on a **different** switch or port (the message says which), or no link at all: check the test cable, the run, or whether the production port is shut down. |
| WARN | Right port but the link negotiated below 1 Gb/s or the cable test found a fault; or link up but no CDP/LLDP heard (CDP may be disabled on that production switch). |
| SKIP | Unused row with no `switch_port` to compare against, or a fiber run when no fiber test ports are set. |

### Fiber (SFP+) channels

Fiber runs, such as the multimode channels to the production switch's SFP+ ports, are tested the
same way, using the test switch's **SFP+ ports**. On a C9200L-24T-4X-E these are the 10G
uplinks Te1/1/1-4, fitted with 10GBASE-SR modules of the same type as the production end.

1. In the inventory, give each channel `"media": "fiber"` and `"expected_speed": "10G"` (in
   the GUI, the Media and Expected speed fields). `far_end` can name the fiber end labels,
   e.g. `"Fiber end P1/S1"`.
2. Set the **Fiber (SFP+) ports** (`Te1/1/1-2`) in the Verify Unused Ports window, or use
   `--fiber-ports Te1/1/1-2` or `"fiber_ports"` in `test_switch`. Fiber channels are verified
   in the same batch as copper runs.
3. For each channel the tool checks:
   - it lands on the expected production SFP+ port (CDP);
   - the link is up at the expected speed: anything slower is a FAIL;
   - **light levels** (DOM) from `show interfaces transceiver detail`: received power from the
     production switch, transmit power, temperature, voltage and laser current, against the
     module's own warning and alarm limits. The receive margin is reported, e.g.
     `Rx -2.8 dBm, 7.1 dB margin`. Below the warning limit is a WARN; below the alarm limit
     is a FAIL;
   - **errors:** the link is watched for 60 s (the *Fiber error check* setting, or
     `--fiber-soak`), and any receive (CRC/FCS) errors are a WARN.
4. **No link?** If **no light is received**, the fiber is almost always reversed: swap the P and
   S strands at one end. If light is received but there's no link, the strand in the other
   direction is broken or dirty, or the modules don't match.

The DOM readings are a health check, typically accurate to about ±2-3 dB. They don't replace
certifying the fiber with an optical loss test set. The preflight warns if a module gives no
DOM readings, or doesn't report as a 10G SFP+. If a third-party module is rejected (the port
goes err-disabled), see the note at the end of `docs/test_switch_c9200.cfg`.

Notes:

- **Hostnames:** set each row's `switch` to the production switch's **hostname**. The domain
  name is ignored, so `SW-CORE-01` matches `SW-CORE-01.plant.local`.
- **Stale entries:** between batches the tool clears the test switch's CDP/LLDP tables
  (`clear cdp table`), so an old entry can't give a false result. This needs privilege 15
  or an enable secret.
- **Cable test:** the TDR cable test briefly drops the link and adds about 10 s per batch.
  It runs after the CDP check.
- **Production switches:** they will log a new CDP neighbour when a test port connects.
  That's harmless, and nothing on them is changed.

## Describing the expected interconnect

`my_network.json` in the commands in this README stands for **your own** inventory file.
To make one, either:

- copy [`examples/expected_interconnect.json`](examples/expected_interconnect.json) to
  `my_network.json` and replace the example rows with your patch panels, switches and devices; or
- start the GUI (`python interconnect_gui.py`), use **Add Connection** for each patch-panel
  port, then **File > Save As**.

To try the tools first, use the example file itself, e.g.
`python port_verify.py examples/expected_interconnect.json ...`. Its switch names (`SW-CORE-01`
etc.) are made up, though, so port verification against your real switches will report them
as mismatches.

It is a JSON file with a top-level object containing a `connections` list, with one
entry per patch-panel port. See
[`examples/expected_interconnect.json`](examples/expected_interconnect.json).

```json
{
  "description": "Building 1, comms room",
  "connections": [
    {
      "patch_panel": "PP-A",
      "panel_port": 1,
      "switch": "SW-CORE-01",
      "switch_port": "Gi1/0/1",
      "device": "Firewall-01",
      "ip": "192.168.1.1",
      "expected_mac": "00:1a:2b:3c:4d:01",
      "status": "connected",
      "notes": "Default gateway"
    },
    {
      "patch_panel": "PP-A",
      "panel_port": 5,
      "switch": "SW-CORE-01",
      "switch_port": "Gi1/0/5",
      "status": "unused",
      "far_end": "PP-Z:5"
    }
  ],
  "test_switch": {
    "host": "192.168.100.2",
    "username": "admin",
    "ports": "Gi1/0/1-23"
  }
}
```

Connection fields (all optional; omit or use `null` for "not set"):

| Field          | Description                                                     |
|----------------|-----------------------------------------------------------------|
| `patch_panel`  | Patch panel name, e.g. `"PP-A"`                                 |
| `panel_port`   | Port on the patch panel (string or number)                      |
| `switch`       | Switch the panel port is cabled to                              |
| `switch_port`  | Switch port, e.g. `"Gi1/0/1"`                                   |
| `device`       | Name of the device that should be on the far end                |
| `ip`           | Device IP address (omitted = can't be pinged, entry is skipped) |
| `expected_mac` | Expected MAC; any common format (`aa:bb:..`, `AA-BB-..`, `aabb.ccdd.eeff`) |
| `status`       | `"connected"` (default) or `"unused"`                           |
| `media`        | `"copper"` (default) or `"fiber"`: fiber runs use the test switch's SFP+ ports |
| `expected_speed` | Speed the run must link at when tested, e.g. `"10G"`; slower is a FAIL |
| `far_end`      | Where to plug the test switch in to reach this run, e.g. `"PP-Z:5"` |
| `notes`        | Free text                                                       |

The optional `test_switch` section stores the test switch settings: `host`, `username`,
`ports` (a range string or a list), `fiber_ports` (SFP+ test ports for fiber runs),
`fiber_soak` (seconds to watch fiber links for errors, default 60), `cdp_timeout` (seconds, default 90), `cable_test`
(default false), `clear_tables` (default true), `allow_switchports` (default false),
`device_type` (Netmiko type, default `cisco_xe`) and `ssh_port` (default 22). The GUI
fills it in for you. Passwords are never stored.

JSON has no comments, so use `notes` or any extra field you like (e.g. `"cable_id"`).
Extra fields, and extra top-level keys such as `description`, are ignored by the
tests and preserved in the baseline file. The file is validated before testing:
JSON syntax errors are reported with line/column, and bad IPs/MACs, unknown
statuses, unused ports with IPs, and duplicate IPs, MACs, panel ports or switch
ports are all reported together, identified as `connection #N (panel:port)`.

## Command line

```sh
python interconnect_test.py examples/expected_interconnect.json
```

Useful options:

```text
-c, --count N            ping packets per device (default 2)
-t, --timeout SECONDS    wait per ping reply (default 1.0)
-w, --workers N          devices tested in parallel (default 16)
-o, --output FILE        write a report; .json for JSON, anything else is CSV
--write-baseline FILE    write a copy of the inventory JSON with missing
                         expected_mac values filled in from the discovered MACs
--switch NAME            only test rows on this switch (repeatable)
--patch-panel NAME       only test rows on this patch panel (repeatable)
-q, --quiet              no per-device progress
--no-colour              plain output
```

Exit codes: `0` no failures, `1` at least one FAIL, `2` invalid input / ping not available.

### Recording MACs for the first time

If you don't know the MACs yet, leave `expected_mac` out, then:

```sh
python interconnect_test.py my_network.json --write-baseline my_network.baseline.json
```

Only missing MACs are filled in; a MAC that doesn't match is reported as a FAIL and
never overwritten.

Check the baseline file is right, then use it as your expected interconnect from then on.

## Understanding the results

| Result | Meaning |
|--------|---------|
| PASS | Device replied to ping and its MAC matches (or was discovered, when no MAC was expected). |
| FAIL | Device didn't reply and no MAC was seen, **or** a *different* MAC answered for that IP (wrong device / IP conflict). |
| WARN | Device replied but its MAC couldn't be resolved (other subnet, or it's this machine's own IP); or it didn't reply to ping but did answer ARP (ICMP probably firewalled). |
| SKIP | `unused` port, or no IP to test. Unused runs are checked with the test switch instead (see above). |

## Tests

```sh
python -m unittest discover -s tests -t .
```

The GUI tests are skipped automatically when Tkinter or a display isn't available.

## Layout

```text
interconnect_gui.py(w)    desktop GUI entry point (.pyw = no console on Windows)
interconnect_test.py      command-line ping/MAC test
port_verify.py            command-line unused-run verification with the test switch
docs/test_switch_c9200.cfg  configuration for the Catalyst C9200L test switch
netcheck/inventory.py     JSON loading and validation
netcheck/ping.py          cross-platform ping
netcheck/mac.py           MAC normalisation and ARP/neighbour-table lookup
netcheck/checker.py       runs the checks and grades each connection
netcheck/report.py        console table, CSV/JSON report, baseline writer
netcheck/cli.py           command-line options
netcheck/gui.py           Tkinter desktop GUI
netcheck/gui_portverify.py  GUI window for unused-run verification
netcheck/portverify.py    batch planning, test-switch checks and grading of unused runs
netcheck/cisco.py         parsers for Cisco CDP/LLDP/interface/TDR output
netcheck/switch.py        SSH session to the test switch (Netmiko)
netcheck/portcli.py       command-line options for port_verify.py
```
