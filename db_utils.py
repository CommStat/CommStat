# Copyright (c) 2026 Manuel Ochoa
# This file is part of CommStat.
# Licensed under the GNU General Public License v3.0.
"""
db_utils.py - One way to open traffic.db3 for a single operation.

    with db_connect() as conn:
        conn.execute(...)

`with sqlite3.connect(...)` alone only commits or rolls back: it leaves the
connection open until the garbage collector gets to it (a ResourceWarning on
Python 3.13+, and a file-descriptor leak if it ever does not). db_connect()
commits on success, rolls back on error, and always closes.
"""

import sqlite3
from contextlib import contextmanager

from constants import DATABASE_FILE


@contextmanager
def db_connect(path=DATABASE_FILE, timeout: float = 10):
    """Open the database for one operation: commit on success, roll back on error, always close."""
    conn = sqlite3.connect(path, timeout=timeout)
    try:
        with conn:
            yield conn
    finally:
        conn.close()
