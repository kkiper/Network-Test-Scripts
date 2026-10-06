#!/usr/bin/env python3
"""Verify a local network against its expected physical interconnect.

Usage:  python interconnect_test.py examples/expected_interconnect.csv
"""

import sys

from netcheck.cli import main

if __name__ == "__main__":
    sys.exit(main())
