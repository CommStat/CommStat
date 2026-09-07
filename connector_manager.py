# Copyright (c) 2025 Manuel Ochoa
# This file is part of CommStat.
# Licensed under the GNU General Public License v3.0.
"""
connector_manager.py - JS8Call TCP Connector Database Manager

Manages database operations for JS8Call TCP connectors.
Supports unlimited connectors with one designated as default.
"""

import logging
import sqlite3
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Constants
DATABASE_FILE = "traffic.db3"
DEFAULT_TCP_PORT = 2442
DEFAULT_SERVER = "127.0.0.1"

_CONNECTOR_COLS = (
    "id, rig_name, tcp_port, server, state, comment, "
    "date_added, is_default, enabled, auto_connect, rf_ack"
)

# Why: under fd exhaustion (EMFILE) every DB call here fails identically, and
# without throttling the log can hit thousands of lines/sec — CPU-burning noise
# that obscures the real failure. Suppress duplicate messages; surface a
# heartbeat every 100 repeats so the issue is still visible.
_last_error_msg: Optional[str] = None
_repeat_count: int = 0


def _log_error_throttled(prefix: str, exc: Exception) -> None:
    global _last_error_msg, _repeat_count
    text = f"{prefix}: {exc}"
    if text == _last_error_msg:
        _repeat_count += 1
        if _repeat_count % 100 == 0:
            logger.error("%s (repeated %d times)", text, _repeat_count)
        return
    if _repeat_count > 0:
        logger.error("(previous error repeated %d more time(s))", _repeat_count)
    logger.error(text)
    _last_error_msg = text
    _repeat_count = 0


class ConnectorManager:
    """Manages JS8Call TCP connector configuration in database."""

    def __init__(self, db_path: str = DATABASE_FILE):
        """
        Initialize ConnectorManager.

        Args:
            db_path: Path to SQLite database file.
        """
        self.db_path = db_path

    def init_connectors_table(self) -> None:
        """Create js8_connectors table if it doesn't exist."""
        try:
            with sqlite3.connect(self.db_path, timeout=10) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS js8_connectors (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        rig_name TEXT UNIQUE NOT NULL,
                        tcp_port INTEGER NOT NULL DEFAULT 2442,
                        state TEXT,
                        comment TEXT,
                        date_added TEXT NOT NULL,
                        is_default INTEGER DEFAULT 0,
                        enabled INTEGER DEFAULT 1,
                        rf_ack INTEGER DEFAULT 1
                    )
                """)
                conn.commit()
        except sqlite3.Error as e:
            _log_error_throttled("Error initializing js8_connectors table", e)

    def get_all_connectors(self, enabled_only: bool = False) -> List[Dict]:
        """
        Get all configured connectors.

        Args:
            enabled_only: If True, only return enabled connectors.

        Returns:
            List of connector dictionaries with keys:
            id, rig_name, tcp_port, server, state, comment, date_added, is_default, enabled
        """
        try:
            with sqlite3.connect(self.db_path, timeout=10) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                if enabled_only:
                    cursor.execute(
                        f"SELECT {_CONNECTOR_COLS} FROM js8_connectors "
                        "WHERE enabled = 1 ORDER BY is_default DESC, rig_name ASC"
                    )
                else:
                    cursor.execute(
                        f"SELECT {_CONNECTOR_COLS} FROM js8_connectors "
                        "ORDER BY is_default DESC, rig_name ASC"
                    )
                rows = cursor.fetchall()
                return [dict(row) for row in rows]
        except sqlite3.Error as e:
            _log_error_throttled("Error getting connectors", e)
            return []

    def get_connector_by_id(self, connector_id: int) -> Optional[Dict]:
        """
        Get a connector by its ID.

        Args:
            connector_id: The connector's database ID.

        Returns:
            Connector dictionary or None if not found.
        """
        try:
            with sqlite3.connect(self.db_path, timeout=10) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(
                    f"SELECT {_CONNECTOR_COLS} FROM js8_connectors WHERE id = ?",
                    (connector_id,)
                )
                row = cursor.fetchone()
                return dict(row) if row else None
        except sqlite3.Error as e:
            _log_error_throttled("Error getting connector by ID", e)
            return None

    def get_connector_by_name(self, rig_name: str) -> Optional[Dict]:
        """
        Get a connector by its rig name.

        Args:
            rig_name: The rig name to look up.

        Returns:
            Connector dictionary or None if not found.
        """
        try:
            with sqlite3.connect(self.db_path, timeout=10) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(
                    f"SELECT {_CONNECTOR_COLS} FROM js8_connectors WHERE rig_name = ?",
                    (rig_name,)
                )
                row = cursor.fetchone()
                return dict(row) if row else None
        except sqlite3.Error as e:
            _log_error_throttled("Error getting connector by name", e)
            return None

    def get_default_connector(self) -> Optional[Dict]:
        """
        Get the default connector.

        Returns:
            Default connector dictionary or None if no default set.
        """
        try:
            with sqlite3.connect(self.db_path, timeout=10) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(
                    f"SELECT {_CONNECTOR_COLS} FROM js8_connectors WHERE is_default = 1"
                )
                row = cursor.fetchone()
                return dict(row) if row else None
        except sqlite3.Error as e:
            _log_error_throttled("Error getting default connector", e)
            return None

    def add_connector(
        self,
        rig_name: str,
        tcp_port: int = DEFAULT_TCP_PORT,
        state: str = "",
        comment: str = "",
        set_as_default: bool = False,
        server: str = DEFAULT_SERVER,
        auto_connect: bool = True,
        rf_ack: bool = True,
    ) -> bool:
        """
        Add a new connector.

        Args:
            rig_name: Name for the rig (must be unique).
            tcp_port: TCP port for JS8Call (default 2442).
            state: 2-letter state code (e.g., TX).
            comment: Optional description.
            set_as_default: If True, set this as the default connector.
            server: IP address or hostname of the JS8Call computer (default 127.0.0.1).
            auto_connect: If True (default), CommStat reconnects this row at startup.
                False marks it as manual-only (e.g. the TCP test tool).
            rf_ack: If True (default), CommStat auto-transmits an RF RR-ack
                for STATREPs received live on this connector.

        Returns:
            True if successful, False otherwise.
        """
        # Validate rig_name
        rig_name = rig_name.strip()
        if not rig_name:
            logger.warning("Cannot add connector: rig name is required")
            return False

        # Clean state (uppercase, max 2 chars)
        state = state.strip().upper()[:2] if state else ""

        try:
            with sqlite3.connect(self.db_path, timeout=10) as conn:
                cursor = conn.cursor()

                # Enforce unique server + port combination
                cursor.execute(
                    "SELECT COUNT(*) FROM js8_connectors WHERE server = ? AND tcp_port = ?",
                    (server, tcp_port)
                )
                if cursor.fetchone()[0] > 0:
                    logger.warning("Cannot add connector: %s:%s already in use", server, tcp_port)
                    return False

                # If setting as default, clear existing default first
                if set_as_default:
                    cursor.execute("UPDATE js8_connectors SET is_default = 0")

                # If this is the first connector, make it default
                cursor.execute("SELECT COUNT(*) FROM js8_connectors")
                count = cursor.fetchone()[0]
                is_default = 1 if (set_as_default or count == 0) else 0

                date_added = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

                server = server.strip() if server else DEFAULT_SERVER

                cursor.execute("""
                    INSERT INTO js8_connectors
                    (rig_name, tcp_port, state, comment, date_added, is_default, server, auto_connect, rf_ack)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (rig_name, tcp_port, state, comment, date_added, is_default, server,
                      1 if auto_connect else 0, 1 if rf_ack else 0))

                conn.commit()
                logger.info("Added connector: %s on %s:%s", rig_name, server, tcp_port)
                return True

        except sqlite3.IntegrityError:
            logger.warning("Cannot add connector: rig name '%s' already exists", rig_name)
            return False
        except sqlite3.Error as e:
            _log_error_throttled("Error adding connector", e)
            return False

    def update_connector(
        self,
        connector_id: int,
        rig_name: str,
        tcp_port: int,
        state: str = "",
        comment: str = "",
        server: str = DEFAULT_SERVER,
        auto_connect: Optional[bool] = None,
        rf_ack: Optional[bool] = None,
    ) -> bool:
        """
        Update an existing connector.

        Args:
            connector_id: The connector's database ID.
            rig_name: New rig name.
            tcp_port: New TCP port.
            state: 2-letter state code (e.g., TX).
            comment: New comment.
            server: IP address or hostname of the JS8Call computer.
            auto_connect: If provided, update the row's auto_connect flag.
                None (default) leaves it unchanged.
            rf_ack: If provided, update the row's rf_ack flag.
                None (default) leaves it unchanged.

        Returns:
            True if successful, False otherwise.
        """
        rig_name = rig_name.strip()
        if not rig_name:
            logger.warning("Cannot update connector: rig name is required")
            return False

        # Clean state (uppercase, max 2 chars)
        state = state.strip().upper()[:2] if state else ""
        server = server.strip() if server else DEFAULT_SERVER

        try:
            with sqlite3.connect(self.db_path, timeout=10) as conn:
                cursor = conn.cursor()

                # Enforce unique server + port combination (excluding this connector)
                cursor.execute(
                    "SELECT COUNT(*) FROM js8_connectors WHERE server = ? AND tcp_port = ? AND id != ?",
                    (server, tcp_port, connector_id)
                )
                if cursor.fetchone()[0] > 0:
                    logger.warning("Cannot update connector: %s:%s already in use", server, tcp_port)
                    return False

                set_parts = ["rig_name = ?", "tcp_port = ?", "state = ?", "comment = ?", "server = ?"]
                params = [rig_name, tcp_port, state, comment, server]
                if auto_connect is not None:
                    set_parts.append("auto_connect = ?")
                    params.append(1 if auto_connect else 0)
                if rf_ack is not None:
                    set_parts.append("rf_ack = ?")
                    params.append(1 if rf_ack else 0)
                params.append(connector_id)

                cursor.execute(f"""
                    UPDATE js8_connectors
                    SET {", ".join(set_parts)}
                    WHERE id = ?
                """, params)
                conn.commit()

                if cursor.rowcount > 0:
                    logger.info("Updated connector ID %s", connector_id)
                    return True
                else:
                    logger.warning("Connector ID %s not found", connector_id)
                    return False

        except sqlite3.IntegrityError:
            logger.warning("Cannot update: rig name '%s' already exists", rig_name)
            return False
        except sqlite3.Error as e:
            _log_error_throttled("Error updating connector", e)
            return False

    def remove_connector(self, connector_id: int) -> bool:
        """
        Remove a connector by ID.

        Cannot remove the default connector or the last connector.

        Args:
            connector_id: The connector's database ID.

        Returns:
            True if successful, False otherwise.
        """
        try:
            with sqlite3.connect(self.db_path, timeout=10) as conn:
                cursor = conn.cursor()

                # Check if this is the default connector
                cursor.execute(
                    "SELECT is_default FROM js8_connectors WHERE id = ?",
                    (connector_id,)
                )
                row = cursor.fetchone()
                if not row:
                    logger.warning("Connector ID %s not found", connector_id)
                    return False

                if row[0] == 1:
                    logger.warning("Cannot remove the default connector")
                    return False

                # Check if this is the last connector
                cursor.execute("SELECT COUNT(*) FROM js8_connectors")
                count = cursor.fetchone()[0]
                if count <= 1:
                    logger.warning("Cannot remove the last connector")
                    return False

                # Remove the connector
                cursor.execute(
                    "DELETE FROM js8_connectors WHERE id = ?",
                    (connector_id,)
                )
                conn.commit()

                logger.info("Removed connector ID %s", connector_id)
                return True

        except sqlite3.Error as e:
            _log_error_throttled("Error removing connector", e)
            return False

    def set_default(self, connector_id: int) -> bool:
        """
        Set a connector as the default.

        Args:
            connector_id: The connector's database ID.

        Returns:
            True if successful, False otherwise.
        """
        try:
            with sqlite3.connect(self.db_path, timeout=10) as conn:
                cursor = conn.cursor()

                # Clear existing default
                cursor.execute("UPDATE js8_connectors SET is_default = 0")

                # Set new default
                cursor.execute(
                    "UPDATE js8_connectors SET is_default = 1 WHERE id = ?",
                    (connector_id,)
                )
                conn.commit()

                if cursor.rowcount > 0:
                    logger.info("Set connector ID %s as default", connector_id)
                    return True
                else:
                    logger.warning("Connector ID %s not found", connector_id)
                    return False

        except sqlite3.Error as e:
            _log_error_throttled("Error setting default connector", e)
            return False

    def get_connector_count(self) -> int:
        """
        Get the total number of configured connectors.

        Returns:
            Number of connectors.
        """
        try:
            with sqlite3.connect(self.db_path, timeout=10) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM js8_connectors")
                return cursor.fetchone()[0]
        except sqlite3.Error as e:
            _log_error_throttled("Error getting connector count", e)
            return 0

    def has_connectors(self) -> bool:
        """
        Check if any connectors are configured.

        Returns:
            True if at least one connector exists.
        """
        return self.get_connector_count() > 0

    def set_enabled(self, connector_id: int, enabled: bool) -> bool:
        """
        Enable or disable a connector.

        Args:
            connector_id: The connector's database ID.
            enabled: True to enable, False to disable.

        Returns:
            True if successful, False otherwise.
        """
        try:
            with sqlite3.connect(self.db_path, timeout=10) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE js8_connectors SET enabled = ? WHERE id = ?",
                    (1 if enabled else 0, connector_id)
                )
                conn.commit()

                if cursor.rowcount > 0:
                    status = "enabled" if enabled else "disabled"
                    logger.info("Connector ID %s %s", connector_id, status)
                    return True
                else:
                    logger.warning("Connector ID %s not found", connector_id)
                    return False

        except sqlite3.Error as e:
            _log_error_throttled("Error setting connector enabled state", e)
            return False

    def is_enabled(self, connector_id: int) -> bool:
        """
        Check if a connector is enabled.

        Args:
            connector_id: The connector's database ID.

        Returns:
            True if enabled, False if disabled or not found.
        """
        connector = self.get_connector_by_id(connector_id)
        if connector:
            return connector.get("enabled", 1) == 1
        return False

    def set_auto_connect(self, connector_id: int, auto_connect: bool) -> bool:
        """
        Set whether a connector auto-connects at CommStat startup.

        Args:
            connector_id: The connector's database ID.
            auto_connect: True to auto-connect at startup, False to leave it
                quiescent until the user clicks Reconnect.

        Returns:
            True if successful, False otherwise.
        """
        try:
            with sqlite3.connect(self.db_path, timeout=10) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE js8_connectors SET auto_connect = ? WHERE id = ?",
                    (1 if auto_connect else 0, connector_id)
                )
                conn.commit()

                if cursor.rowcount > 0:
                    status = "auto-connect" if auto_connect else "manual-only"
                    logger.info("Connector ID %s set to %s", connector_id, status)
                    return True
                else:
                    logger.warning("Connector ID %s not found", connector_id)
                    return False

        except sqlite3.Error as e:
            _log_error_throttled("Error setting connector auto_connect state", e)
            return False
