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

Create a CSV (Excel can save one) with one row per patch-panel port. See
[`examples/expected_interconnect.csv`](examples/expected_interconnect.csv).

| Column         | Required | Description                                                     |
|----------------|----------|-----------------------------------------------------------------|
| `patch_panel`  |          | Patch panel name, e.g. `PP-A`                                   |
| `panel_port`   |          | Port on the patch panel                                         |
| `switch`       |          | Switch the panel port is cabled to                              |
| `switch_port`  |          | Switch port, e.g. `Gi1/0/1`                                     |
| `device`       |          | Name of the device that should be on the far end                |
| `ip`           |          | Device IP address (blank = can't be pinged, row is skipped)     |
| `expected_mac` |          | Expected MAC; any common format (`aa:bb:..`, `AA-BB-..`, `aabb.ccdd.eeff`) |
| `status`       | yes      | `connected` or `unused`                                         |
| `notes`        |          | Free text                                                       |

Lines starting with `#` are comments. Extra columns are allowed and carried through
to the baseline file. The file is validated before testing; bad IPs/MACs, unknown
statuses, unused ports with IPs, and duplicate IPs, MACs, panel ports or switch ports
are all reported with their line numbers.

## Running

```sh
python interconnect_test.py examples/expected_interconnect.csv
```

Useful options:

```text
-c, --count N            ping packets per device (default 2)
-t, --timeout SECONDS    wait per ping reply (default 1.0)
-w, --workers N          devices tested in parallel (default 16)
-o, --output FILE        write a report; .json for JSON, anything else is CSV
--write-baseline FILE    write a copy of the inventory with blank expected_mac
                         values filled in from the discovered MACs
--switch NAME            only test rows on this switch (repeatable)
--patch-panel NAME       only test rows on this patch panel (repeatable)
-q, --quiet              no per-device progress
--no-colour              plain output
```

Exit codes: `0` no failures, `1` at least one FAIL, `2` invalid input / ping not available.

### Recording MACs for the first time

If you don't know the MACs yet, leave `expected_mac` blank, then:

```sh
python interconnect_test.py my_network.csv --write-baseline my_network.baseline.csv
```

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
netcheck/inventory.py     CSV loading and validation
netcheck/ping.py          cross-platform ping
netcheck/mac.py           MAC normalisation and ARP/neighbour-table lookup
netcheck/checker.py       runs the checks and grades each connection
netcheck/report.py        console table, CSV/JSON report, baseline writer
netcheck/cli.py           command-line options
```
