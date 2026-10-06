#!/usr/bin/env python3
"""Verify unused patch panel runs with a test switch (CDP/LLDP).

Usage:  python port_verify.py examples/expected_interconnect.json --host 192.168.100.2 \
            --username admin --ports Gi1/0/1-23
"""

import sys

from netcheck.portcli import main

if __name__ == "__main__":
    sys.exit(main())
