#!/usr/bin/env python3
"""Verify unused patch panel runs with a test switch (CDP/LLDP).

Usage:  python port_verify.py examples/expected_interconnect.json --host 192.168.1.250 \
            --username admin --ports Gi1/0/1-22
"""

import sys

from netcheck.portcli import main

if __name__ == "__main__":
    sys.exit(main())
