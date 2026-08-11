"""Compatibility shim -- vdisplay moved to refract.core.vdisplay (Phase 0).

Kept so the standalone scripts (xrdesk.py, virtual-monitors.py) keep working
untouched until their Phase 4 port. New code imports refract.core.vdisplay.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from refract.core.vdisplay import (          # noqa: E402,F401
    SC_NAME, SC_PATH, RD_NAME, RD_PATH, VIRTUAL_PRODUCT, VirtualDisplays,
)
