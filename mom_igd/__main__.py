"""``python -m mom_igd`` entry point.

Kept trivial on purpose: all parsing and dispatch lives in :mod:`mom_igd.cli`,
which imports heavy dependencies lazily so that ``doctor`` stays cheap.
"""

from __future__ import annotations

import sys

from mom_igd.cli import main

if __name__ == "__main__":
    sys.exit(main())
