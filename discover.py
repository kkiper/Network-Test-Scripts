#!/usr/bin/env python3
"""Find devices on the local network that aren't in the expected interconnect.

Usage:  python discover.py my_network.json [--subnet 192.168.1.0/24]
"""

import sys

from netcheck.discovercli import main

if __name__ == "__main__":
    sys.exit(main())
