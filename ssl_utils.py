# Copyright (c) 2025, 2026 Manuel Ochoa
# This file is part of CommStat.
# Licensed under the GNU General Public License v3.0.
"""
ssl_utils.py - SSL contexts for CommStat's outbound HTTPS requests.

Lives in its own module so feature dialogs can use it without importing
little_gucci (the whole main program).
"""

import ssl


def create_verified_ssl_context():
    """Create a cert-VERIFYING SSL context for every outbound HTTPS request (heartbeat, datafeed, news feeds).

    The heartbeat reply can drive
    _handle_db_update (runs server-supplied SQL) and _handle_program_update
    (downloads + installs a zip), so verification must stay ON to prevent MITM.

    We prefer certifi's bundle when available so a stale OS/Python trust store
    isn't a silent failure point (commstat.app uses a Let's Encrypt cert whose
    ISRG root may be missing on un-updated machines).
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()
