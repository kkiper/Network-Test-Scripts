#!/usr/bin/env python3
"""Desktop GUI for the network interconnect test.

Usage:  python interconnect_gui.py [expected_interconnect.json]
On Windows, double-click interconnect_gui.pyw to start it without a console window.
"""

import sys

from netcheck.gui import main

if __name__ == "__main__":
    sys.exit(main())
