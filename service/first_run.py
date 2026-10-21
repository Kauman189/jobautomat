"""Bounded first real run: 1 query, 2 offers.

Patches QUERIES and LIMITE so the full 8-query sweep is not launched. Exercise
the whole loop: real search -> detail -> score -> generation -> Telegram.
"""

import os
import sys

_D = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _D)

import main as M  # noqa: E402

M.QUERIES = ["Product Designer remote"]
M.LIMITE_POR_QUERY = 2

if __name__ == "__main__":
    sys.exit(M.main())
