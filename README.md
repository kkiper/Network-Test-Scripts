# Network Test Scripts

Tools for verifying a local network against its **expected physical interconnect**
(which device is patched into which patch-panel port and switch port).

Current capability (`interconnect_test.py`):

- Ping every device that should be connected.
- Discover each device's MAC address from this machine's ARP/neighbour table.
- Compare the discovered MAC against the expected MAC.
- Report each patch-panel port as PASS / FAIL / WARN / SKIP, on screen and as CSV or JSON.
- Capture a MAC "baseline" for devices whose MAC isn't recorded yet.

Planned: query the switches (SNMP / LLDP / CLI MAC address tables) to verify that each
device — and each *unused* patch-panel port — is actually on the expected switch port.

## Requirements

- Python 3.8+ (standard library only, nothing to install).
- The OS `ping` command. Works on Windows, Linux and macOS.
- Run it from a machine on the **same subnet/VLAN** as the devices: MAC addresses are
  only visible via ARP for hosts on the local layer-2 segment. Devices behind a router
  will ping fine but show `UNRESOLVED` MACs (reported as WARN).

## Describing the expected interconnect

Create a JSON file with a top-level object containing a `connections` list, with one
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
      "status": "unused"
    }
  ]
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
| `notes`        | Free text                                                       |

JSON has no comments, so use `notes` or any extra field you like (e.g. `"cable_id"`).
Extra fields, and extra top-level keys such as `description`, are ignored by the
tests and preserved in the baseline file. The file is validated before testing:
JSON syntax errors are reported with line/column, and bad IPs/MACs, unknown
statuses, unused ports with IPs, and duplicate IPs, MACs, panel ports or switch
ports are all reported together, identified as `connection #N (panel:port)`.

## Running

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
| SKIP | `unused` port, or no IP to test. |

## Tests

```sh
python -m unittest discover -s tests -t .
```

## Layout

```text
interconnect_test.py      entry point
netcheck/inventory.py     JSON loading and validation
netcheck/ping.py          cross-platform ping
netcheck/mac.py           MAC normalisation and ARP/neighbour-table lookup
netcheck/checker.py       runs the checks and grades each connection
netcheck/report.py        console table, CSV/JSON report, baseline writer
netcheck/cli.py           command-line options
```
