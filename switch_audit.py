#!/usr/bin/env python3
"""Read-only audit of the production switches' ports against the inventory.

Usage:  python switch_audit.py my_network.json [--switch SW-CORE-01]
"""

import sys

from netcheck.auditcli import main

if __name__ == "__main__":
    sys.exit(main())
