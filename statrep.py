# Copyright (c) 2025 Manuel Ochoa
# This file is part of CommStat.
# Licensed under the GNU General Public License v3.0.
# AI Assistance: Claude (Anthropic), ChatGPT (OpenAI)

"""
StatRep Dialog for CommStat
Allows creating and transmitting AMRRON Status Reports via JS8Call.
"""

import base64
import re
import subprocess
import sqlite3
import sys
import urllib.request
import urllib.parse
import threading
from configparser import ConfigParser
from typing import Optional, Dict, List, TYPE_CHECKING
from dataclasses import dataclass

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import QDateTime, Qt
from PyQt5.QtWidgets import QMessageBox, QDialog, QComboBox

from constants import (
    DEFAULT_COLORS, COLOR_INPUT_TEXT, COLOR_INPUT_BORDER,
    COLOR_DISABLED_BG, COLOR_DISABLED_TEXT,
    COLOR_BTN_GREEN, COLOR_BTN_BLUE, COLOR_BTN_CYAN, COLOR_BTN_HELP, COLOR_BTN_RED,
    RIG_FETCH_DELAY_MS, RIG_FREQ_DELAY_MS,
    SCOPE_OPTIONS, scope_code_for_text, scope_db_text_for_code,
)
from id_utils import generate_time_based_id
from little_gucci import create_verified_ssl_context, UpperCaseLineEdit
from ui_helpers import (make_button, label_font, mono_font, apply_standard_dialog_chrome,
                        connect_single, show_help_dialog)

if TYPE_CHECKING:
    from js8_tcp_client import TCPConnectionPool
    from connector_manager import ConnectorManager


# =============================================================================
# Constants
# =============================================================================

DATABASE_FILE = "traffic.db3"

# Commsrvr server (base64 encoded)
_COMMSRVR = base64.b64decode("aHR0cHM6Ly9jb21tc3RhdC5hcHA=").decode()
_DATAFEED = _COMMSRVR + "/datafeed-808585.php"

# Status codes
STATUS_GREEN = "1"
STATUS_YELLOW = "2"
STATUS_RED = "3"
STATUS_UNKNOWN = "4"
STATUS_EVENT = "6"

# Status display names and their codes
STATUS_OPTIONS = [
    ("", ""),           # Empty/unselected
    ("Green", STATUS_GREEN),
    ("Yellow", STATUS_YELLOW),
    ("Red", STATUS_RED),
    ("Unknown", STATUS_UNKNOWN),
]

# Status categories in display order (label, internal_name)
# Note: internal_name is used as dictionary key in the form, not the DB column name
STATUS_CATEGORIES = [
    ("Map Pin", "status"),
    ("Power", "power"),
    ("Water", "water"),
    ("Medical", "medical"),
    ("Comms", "comms"),
    ("Travel", "travel"),
    ("Internet", "internet"),
    ("Fuel", "fuel"),
    ("Food", "food"),
    ("Crime", "crime"),
    ("Civil", "civil"),
    ("Political", "political"),
]

# Colors for status indicators
STATUS_COLORS = {
    "Green": "#28a745",
    "Yellow": "#ffc107",
    "Red": "#dc3545",
    "Unknown": "#6c757d",
}

# ── Help content ──────────────────────────────────────────────────────────────
# Lives beside the feature it documents: change a Mode or a status color here
# and the help text is in the same file. Chrome comes from ui_helpers.

_HELP_HTML = f"""
<div style="font-family: Roboto; font-size: 13px; color: #333333;">

<h3 style="color:#555555;">Mode</h3>
<table cellspacing="2" cellpadding="2">
<tr><td><b>Slow</b></td><td>&nbsp;&nbsp;8 WPM</td></tr>
<tr><td><b>Normal</b></td><td>&nbsp;&nbsp;16 WPM</td></tr>
<tr><td><b>Fast</b></td><td>&nbsp;&nbsp;24 WPM</td></tr>
<tr><td><b>Turbo</b></td><td>&nbsp;&nbsp;40 WPM</td></tr>
<tr><td><b>Ultra</b></td><td>&nbsp;&nbsp;60 WPM&nbsp;&nbsp;<b>(Use only for JS8Call 3.0.1 or greater)</b></td></tr>
</table>

<h3 style="color:#555555;">Delivery</h3>
<ul>
<li><b>Maximum Reach</b> &mdash; RF + Internet</li>
<li><b>Limited Reach</b> &mdash; RF Only</li>
</ul>

<h3 style="color:#555555;">Color Selection</h3>
<table cellspacing="0" cellpadding="8" width="100%">
<tr>
  <td bgcolor="{STATUS_COLORS['Green']}" align="center" width="33%">
      <b style="color:#FFFFFF;">Green</b><br><span style="color:#FFFFFF;">Normal</span></td>
  <td bgcolor="{STATUS_COLORS['Yellow']}" align="center" width="33%">
      <b style="color:#000000;">Yellow</b><br><span style="color:#000000;">Limited</span></td>
  <td bgcolor="{STATUS_COLORS['Red']}" align="center">
      <b style="color:#FFFFFF;">Red</b><br><span style="color:#FFFFFF;">Collapsed/None</span></td>
</tr>
</table>

</div>
"""


WINDOW_WIDTH = 700
WINDOW_HEIGHT = 510
WINDOW_HEIGHT_EXPANDED = 650
WINDOW_HEIGHT_FORWARD = WINDOW_HEIGHT_EXPANDED - 180  # Shorter: no editable status grid to fit
INTERNET_RIG = "INTERNET ONLY"
REMARKS_MAX_RADIO = 500
REMARKS_MAX_INTERNET = 500
NEWLINE_PLACEHOLDER = "||"

_PROG_BG    = DEFAULT_COLORS.get("program_background",  "#000000")
_PROG_FG    = DEFAULT_COLORS.get("program_foreground",  "#FFFFFF")
_DATA_BG    = DEFAULT_COLORS.get("data_background",     "#F8F6F4")
_PANEL_BG   = DEFAULT_COLORS.get("module_background",   "#DDDDDD")
_PANEL_FG   = DEFAULT_COLORS.get("module_foreground",   "#000000")
_COL_CANCEL = "#555555"
_COL_GRAY   = "#6c757d"
_COL_PURPLE = "#6f42c1"
_COL_PINK   = COLOR_BTN_HELP
_COL_COUNTER = "#444444"  # muted but legible counter text (COLOR_DISABLED_TEXT is too light here)


# =============================================================================
# Utility Functions
# =============================================================================

def make_uppercase(field):
    """Force uppercase input on a QLineEdit."""
    def to_upper(text):
        if text != text.upper():
            pos = field.cursorPosition()
            field.setText(text.upper())
            field.setCursorPosition(pos)
    field.textEdited.connect(to_upper)


def get_state_from_connector(connector_manager, rig_name: str) -> str:
    """Get the state abbreviation from connector table for a specific rig.

    Args:
        connector_manager: ConnectorManager instance for database access.
        rig_name: Name of the rig to look up.

    Returns:
        State abbreviation from connector, or empty string if not found.
    """
    if not connector_manager or not rig_name:
        return ""
    try:
        connector = connector_manager.get_connector_by_name(rig_name)
        if connector and connector.get("state"):
            return connector["state"].strip().upper()
    except Exception:
        pass
    return ""


# =============================================================================
# StatRep Dialog
# =============================================================================

class StatRepDialog(QDialog):
    """Modern StatRep form for creating and transmitting status reports."""

    _commsrvr_error = QtCore.pyqtSignal(str)

    def __init__(
        self,
        tcp_pool: "TCPConnectionPool",
        connector_manager: "ConnectorManager",
        parent=None,
        module_background: str = _DATA_BG,
        data_background: str = _DATA_BG
    ):
        super().__init__(parent)
        self.tcp_pool = tcp_pool
        self.connector_manager = connector_manager
        self.module_background = module_background
        self.data_background = data_background

        apply_standard_dialog_chrome(self, "Status Report", WINDOW_WIDTH, WINDOW_HEIGHT_EXPANDED)

        self._commsrvr_error.connect(self._on_commsrvr_error)

        # Configuration
        self.callsign = ""
        self.grid = ""
        self._grid_user_edited = False  # blocks late JS8Call grid_received from clobbering a manual edit
        self.selected_group = ""
        self.statrep_id = ""
        self._pending_frequency = 0  # For storing frequency during transmit
        self._forwarder_callsign = ""       # Forwarder's callsign in forward mode
        self._forward_original_remarks = "" # Original remarks before "Forwarded By:" is appended

        # Status combo boxes
        self.status_combos: Dict[str, QComboBox] = {}

        # Map Pin auto-flip state: tracks whether the user has manually picked
        # a Map Pin value (in which case we stop auto-flipping it from the
        # worst of the other 11 categories) and a guard flag used during
        # prefill so loading a forwarded report doesn't clobber its Map Pin.
        self._map_pin_overridden = False
        self._suppress_auto_map_pin = False

        # Load config
        self._load_config()

        # Build UI
        self._setup_ui()

        # Load rigs and select default
        self._load_rigs()

    def _load_config(self) -> None:
        """Load configuration from database."""
        # Get active group from database
        self.selected_group = self._get_active_group_from_db()
        # Callsign and grid will be loaded from JS8Call when rig is selected

    def _get_active_group_from_db(self) -> str:
        """Get the active group from the database."""
        try:
            with sqlite3.connect(DATABASE_FILE, timeout=10) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT name FROM groups ORDER BY name LIMIT 1")
                result = cursor.fetchone()
                if result:
                    return result[0]
        except sqlite3.Error as e:
            print(f"Error reading active group from database: {e}")
        return ""

    def _get_default_remarks(self) -> str:
        """Get default remarks with state from the selected rig's connector.

        Returns the state from the connector table, or empty if not set.
        """
        # Get the currently selected rig
        if hasattr(self, 'rig_combo'):
            rig_name = self.rig_combo.currentText()
            if rig_name and "(disconnected)" not in rig_name:
                state = get_state_from_connector(self.connector_manager, rig_name)
                if state:
                    return state
        return ""

    def _is_internet_only(self) -> bool:
        """Check if the current rig selection is Internet Only."""
        return hasattr(self, 'rig_combo') and self.rig_combo.currentText() == INTERNET_RIG

    def _get_remarks_text(self) -> str:
        """Get remarks text from the expanded widget."""
        if hasattr(self, 'remarks_expanded'):
            return self.remarks_expanded.toPlainText().strip()
        return self.remarks_field.text().strip()

    def _set_remarks_text(self, text: str) -> None:
        """Set remarks text on the expanded widget."""
        if hasattr(self, 'remarks_expanded'):
            self.remarks_expanded.setPlainText(text)
        else:
            self.remarks_field.setText(text)

    def _swap_remarks_widget(self, internet_only: bool) -> None:
        """No-op: expanded remarks widget is always shown."""

    def _remarks_max_len(self) -> int:
        """Max remarks length for the current rig selection."""
        return REMARKS_MAX_INTERNET if self._is_internet_only() else REMARKS_MAX_RADIO

    def _on_remarks_text_changed(self) -> None:
        """Hard-cap remarks at the character limit and refresh the counter."""
        max_len = self._remarks_max_len()
        text = self.remarks_expanded.toPlainText()
        if len(text) > max_len:
            cursor = self.remarks_expanded.textCursor()
            pos = cursor.position()
            text = text[:max_len]
            self.remarks_expanded.blockSignals(True)
            self.remarks_expanded.setPlainText(text)
            self.remarks_expanded.blockSignals(False)
            cursor = self.remarks_expanded.textCursor()
            cursor.setPosition(min(pos, len(text)))
            self.remarks_expanded.setTextCursor(cursor)
        self._update_remarks_count_label(len(text), max_len)

    def _update_remarks_count_label(self, count: Optional[int] = None, max_len: Optional[int] = None) -> None:
        """Refresh the 'N of MAX' counter next to the Remarks label."""
        if not hasattr(self, 'remarks_count_label'):
            return
        if max_len is None:
            max_len = self._remarks_max_len()
        if count is None:
            count = len(self.remarks_expanded.toPlainText())
        self.remarks_count_label.setText(f"{count} of {max_len}")
        color = COLOR_BTN_RED if count >= max_len else _COL_COUNTER
        self.remarks_count_label.setStyleSheet(f"color: {color};")

    def _get_all_groups_from_db(self) -> list:
        """Get all groups from the database."""
        try:
            with sqlite3.connect(DATABASE_FILE, timeout=10) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT name FROM groups ORDER BY name")
                return [row[0] for row in cursor.fetchall()]
        except sqlite3.Error as e:
            print(f"Error reading groups from database: {e}")
        return []

    def _get_group_target(self) -> str:
        """Build the transmit/DB target from the Group-or-Callsign field.

        A saved group is prefixed with '@' (matches the '@GROUP' convention
        parsed app-wide); an unrecognized entry is treated as a manually
        typed callsign and sent bare, same as Alert's '_get_target'."""
        text = self.to_combo.currentText().strip()
        if not text:
            return ""
        known_groups = {g.strip().upper() for g in self._get_all_groups_from_db()}
        if text.upper() in known_groups:
            return f"@{text}"
        return text.upper()

    def _is_commsrvr_enabled(self) -> bool:
        """Check if commsrvr submission is enabled.

        Returns:
            True if enabled — i.e. Online mode (the global Off-Grid/Online
            switch in the main header). False whenever Off-Grid Mode is on.
        """
        import netguard
        return netguard.is_network_enabled()

    def _submit_to_commsrvr_async(self, frequency: int, on_complete=None) -> None:
        """Start background thread to submit statrep to commsrvr server.

        Args:
            frequency: Transmission frequency in Hz.
            on_complete: Optional callable(global_id: int) invoked after the
                request completes (success or failure).  global_id is 0 on
                failure or when the server returns a non-numeric response.
        """
        if not self._is_commsrvr_enabled():
            if on_complete:
                on_complete(0)
            return

        # Capture current state for the thread
        callsign = self.callsign
        message = self._pending_message
        now = QDateTime.currentDateTimeUtc().toString("yyyy-MM-dd HH:mm:ss")

        def submit_thread():
            """Background thread that performs the HTTP POST."""
            global_id = 0
            error_msg = ""
            try:
                data_string = f"{now}\t{frequency}\t0\t30\t{message}"
                post_data = urllib.parse.urlencode({
                    'cs': callsign,
                    'data': data_string
                }).encode('utf-8')

                req = urllib.request.Request(_DATAFEED, data=post_data, method='POST')
                with urllib.request.urlopen(req, timeout=5, context=create_verified_ssl_context()) as response:
                    result = response.read().decode('utf-8').strip()

                if result.isdigit():
                    global_id = int(result)
                    print(f"[Commsrvr] Statrep submitted successfully (global_id={global_id})")
                else:
                    error_msg = result[5:] if result.startswith("ERR::") else (result or "Unknown server error")
                    print(f"[Commsrvr] Statrep submission failed — server returned: {result}")

            except Exception as e:
                reason = getattr(e, 'reason', e)
                if isinstance(reason, TimeoutError):
                    error_msg = "Server timeout — the server did not respond in time."
                else:
                    error_msg = f"Connection error — {e}"
                print(f"[Commsrvr] Statrep submission failed — {error_msg}")
            finally:
                if on_complete:
                    on_complete(global_id)
                if error_msg:
                    self._commsrvr_error.emit(error_msg)

        # Start daemon thread (won't block app shutdown)
        thread = threading.Thread(target=submit_thread, daemon=True)
        thread.start()

    def _load_rigs(self) -> None:
        """Load enabled connectors into the rig dropdown, plus Internet option."""
        self.rig_combo.blockSignals(True)
        self.rig_combo.clear()

        enabled_connectors = self.connector_manager.get_all_connectors(enabled_only=True) if self.connector_manager else []
        connected_rigs = self.tcp_pool.get_connected_rig_names() if self.tcp_pool else []
        available_connectors = [c for c in enabled_connectors if c['rig_name'] in connected_rigs]
        available_count = len(available_connectors)

        internet_available = bool(self.parent() and getattr(self.parent(), '_internet_available', False))

        if available_count == 0:
            # No available connectors — Internet only if online
            if internet_available:
                self.rig_combo.addItem(INTERNET_RIG)
        else:
            # Connectors available — require explicit selection; Internet at bottom if online
            self.rig_combo.addItem("")  # empty first
            for c in available_connectors:
                self.rig_combo.addItem(c['rig_name'])
            if internet_available:
                self.rig_combo.addItem(INTERNET_RIG)

        self.rig_combo.blockSignals(False)

        current_text = self.rig_combo.currentText()
        if current_text:
            self._on_rig_changed(current_text)

    def _on_rig_changed(self, rig_name: str) -> None:
        """Handle rig selection change - fetch callsign and grid from JS8Call."""
        # Re-arm auto-population for the newly selected rig's fetch.
        self._grid_user_edited = False
        if not rig_name or "(disconnected)" in rig_name:
            if not getattr(self, '_forward_origin', None):
                self.callsign = ""
                self.grid = ""
                if hasattr(self, 'from_field'):
                    self.from_field.setText("")
            if hasattr(self, 'grid_field'):
                self._grid_auto_populating = True
                self.grid_field.setText("")
                self._grid_auto_populating = False
            if hasattr(self, 'freq_field'):
                self.freq_field.setText("")
            return

        is_internet = (rig_name == INTERNET_RIG)
        if hasattr(self, 'delivery_combo'):
            self.delivery_combo.blockSignals(True)
            self.delivery_combo.clear()
            self.delivery_combo.addItem("Maximum Reach")
            if not is_internet:
                self.delivery_combo.addItem("Limited Reach")
            self.delivery_combo.blockSignals(False)

        # Swap remarks widget based on rig type
        self._swap_remarks_widget(is_internet)
        self._update_remarks_count_label()

        if rig_name == INTERNET_RIG:
            callsign, grid, state = self._get_internet_user_settings()
            if getattr(self, '_forward_origin', None):
                self._forwarder_callsign = callsign
                self._update_forward_remarks_field(callsign)
            else:
                self.grid = grid
                self.callsign = callsign
                if hasattr(self, 'from_field'):
                    self.from_field.setText(callsign)
                if hasattr(self, 'grid_field'):
                    self._grid_auto_populating = True
                    self.grid_field.setText(grid)
                    self._grid_auto_populating = False
            if hasattr(self, 'freq_field'):
                self.freq_field.setText("")
            if hasattr(self, 'mode_combo'):
                self.mode_combo.setEnabled(False)
                self.mode_combo.setCurrentIndex(-1)
            if state and not getattr(self, '_forward_origin', None):
                self._set_remarks_text(state)
            return

        # Re-enable mode combo for real rig
        if hasattr(self, 'mode_combo'):
            self.mode_combo.setEnabled(True)
            if self.mode_combo.currentIndex() == -1:
                self.mode_combo.setCurrentIndex(0)

        # Update remarks with state from connector (skip if forwarding - preserve forwarded remarks)
        state = get_state_from_connector(self.connector_manager, rig_name)
        if state and not getattr(self, '_forward_origin', None):
            self._set_remarks_text(state)

        if not self.tcp_pool:
            print("[StatRep] No TCP pool available")
            return

        # Disconnect signals from ALL clients to avoid duplicates
        for client_name in self.tcp_pool.get_all_rig_names():
            client = self.tcp_pool.get_client(client_name)
            if client:
                try:
                    client.callsign_received.disconnect(self._on_callsign_received)
                except TypeError:
                    pass
                try:
                    client.grid_received.disconnect(self._on_grid_received)
                except TypeError:
                    pass
                try:
                    client.frequency_received.disconnect(self._on_frequency_received)
                except TypeError:
                    pass

        client = self.tcp_pool.get_client(rig_name)
        if client and client.is_connected():
            # Connect signals for this client
            client.callsign_received.connect(self._on_callsign_received)
            client.grid_received.connect(self._on_grid_received)
            client.frequency_received.connect(self._on_frequency_received)

            # Populate mode dropdown with current mode preselected
            if hasattr(self, 'mode_combo'):
                speed_name = (client.speed_name or "").upper()
                mode_map = {"SLOW": 0, "NORMAL": 1, "FAST": 2, "TURBO": 3, "ULTRA": 4}
                idx = mode_map.get(speed_name, 1)  # Default to Normal
                self.mode_combo.blockSignals(True)
                self.mode_combo.setCurrentIndex(idx)
                self.mode_combo.blockSignals(False)

            # Populate frequency field
            if hasattr(self, 'freq_field'):
                frequency = client.frequency
                if frequency:
                    self.freq_field.setText(f"{frequency:.3f}")
                else:
                    self.freq_field.setText("")

            # Request callsign, grid, and frequency from JS8Call
            # Small delay between requests to avoid race condition
            print(f"[StatRep] Requesting callsign, grid, and frequency from {rig_name}")
            client.get_callsign()
            QtCore.QTimer.singleShot(RIG_FETCH_DELAY_MS, client.get_grid)
            QtCore.QTimer.singleShot(RIG_FREQ_DELAY_MS, client.get_frequency)
        else:
            print(f"[StatRep] Client not available or not connected for {rig_name}")
            if hasattr(self, 'freq_field'):
                self.freq_field.setText("")

    def _get_internet_user_settings(self) -> tuple:
        """Get callsign, grid, and state from User Settings for internet-only transmission."""
        try:
            with sqlite3.connect(DATABASE_FILE, timeout=10) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT callsign, gridsquare, state FROM controls WHERE id = 1")
                row = cursor.fetchone()
                if row:
                    return (
                        (row[0] or "").strip().upper(),
                        (row[1] or "").strip(),
                        (row[2] or "").strip().upper(),
                    )
        except sqlite3.Error:
            pass
        return ("", "", "")

    def _on_mode_changed(self, index: int) -> None:
        """Handle mode dropdown change - send MODE.SET_SPEED to JS8Call."""
        rig_name = self.rig_combo.currentText()
        if not rig_name or rig_name == INTERNET_RIG or "(disconnected)" in rig_name:
            return

        if not self.tcp_pool:
            return

        client = self.tcp_pool.get_client(rig_name)
        if client and client.is_connected():
            speed_value = self.mode_combo.currentData()
            client.send_message("MODE.SET_SPEED", "", {"SPEED": speed_value})
            print(f"[StatRep] Set mode to {self.mode_combo.currentText()} (speed={speed_value})")

    def _on_delivery_changed(self, delivery: str) -> None:
        """Handle delivery dropdown change."""
        pass

    def _on_callsign_received(self, rig_name: str, callsign: str) -> None:
        """Handle callsign received from JS8Call."""
        # Only update if this is the currently selected rig
        if self.rig_combo.currentText() == rig_name:
            if getattr(self, '_forward_origin', None):
                self._forwarder_callsign = callsign
                self._update_forward_remarks_field(callsign)
            else:
                self.callsign = callsign
                if hasattr(self, 'from_field'):
                    self.from_field.setText(callsign)

    def _on_grid_received(self, rig_name: str, grid: str) -> None:
        """Handle grid received from JS8Call."""
        print(f"[StatRep] Grid received from {rig_name}: {grid}")
        # Only update if this is the currently selected rig and not forwarding
        if self.rig_combo.currentText() == rig_name and not getattr(self, '_forward_origin', None):
            # Don't clobber a grid the user already typed — this response can
            # arrive well after the request was sent (rig/JS8Call round trip),
            # potentially after the user has edited the field and tabbed away.
            if self._grid_user_edited:
                print(f"[StatRep] Ignoring grid from {rig_name} — user already edited the field")
                return
            self.grid = grid
            if hasattr(self, 'grid_field'):
                self._grid_auto_populating = True
                self.grid_field.setText(grid)
                self._grid_auto_populating = False
            # Only auto-populate remarks if the user hasn't typed anything yet
            if hasattr(self, 'remarks_field') and not self._get_remarks_text():
                self._set_remarks_text(self._get_default_remarks())

    def _on_frequency_received(self, rig_name: str, dial_freq: int) -> None:
        """Handle frequency received from JS8Call."""
        # Only update if this is the currently selected rig
        if self.rig_combo.currentText() == rig_name:
            frequency_mhz = dial_freq / 1000000
            print(f"[StatRep] Frequency received from {rig_name}: {frequency_mhz:.3f} MHz")
            if hasattr(self, 'freq_field'):
                self.freq_field.setText(f"{frequency_mhz:.3f}")

    def _on_from_field_changed(self, text: str) -> None:
        """Handle user editing the From (callsign) field."""
        self.callsign = text.upper()

    def _on_grid_field_changed(self, text: str) -> None:
        """Handle user editing the Grid field."""
        if not getattr(self, '_grid_auto_populating', False):
            self._grid_user_edited = True
        raw = text.strip()
        formatted = raw.upper()
        self.grid = formatted
        if text != formatted:
            pos = self.grid_field.cursorPosition()
            self.grid_field.blockSignals(True)
            self.grid_field.setText(formatted)
            self.grid_field.blockSignals(False)
            self.grid_field.setCursorPosition(pos)

    def _generate_statrep_id(self) -> None:
        """Generate a time-based StatRep ID from current UTC time."""
        if not self.statrep_id:
            self.statrep_id = generate_time_based_id()

    def _setup_ui(self) -> None:
        """Build the user interface."""
        self.setStyleSheet(f"""
            QDialog {{ background-color: {_PANEL_BG}; }}
            QLabel {{ color: {_PANEL_FG}; background-color: transparent; font-size: 13px; }}
            QLineEdit {{
                background-color: white; color: {COLOR_INPUT_TEXT};
                border: 1px solid {COLOR_INPUT_BORDER}; border-radius: 4px; padding: 2px 4px;
                font-family: 'Kode Mono'; font-size: 13px;
            }}
            QComboBox {{
                background-color: white; color: {COLOR_INPUT_TEXT};
                border: 1px solid {COLOR_INPUT_BORDER}; border-radius: 4px; padding: 2px 4px;
                font-family: 'Kode Mono'; font-size: 13px;
                combobox-popup: 0;
            }}
            QComboBox:disabled {{
                background-color: {COLOR_DISABLED_BG}; color: {COLOR_DISABLED_TEXT};
                border: 1px solid {COLOR_INPUT_BORDER};
            }}
            QComboBox QAbstractItemView {{
                background-color: white; color: {COLOR_INPUT_TEXT};
                selection-background-color: #cce5ff; selection-color: #000000;
            }}
            QComboBox QAbstractItemView::item {{
                min-height: 22px; padding: 0 6px;
            }}
        """)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(15, 15, 15, 15)

        # Title
        title = QtWidgets.QLabel("Status Report")
        title.setAlignment(Qt.AlignCenter)
        title.setFont(QtGui.QFont("Roboto Slab", -1, QtGui.QFont.Black))
        title.setFixedHeight(36)
        title.setStyleSheet(
            f"QLabel {{ background-color:{_PROG_BG}; color:{_PROG_FG};"
            f" font-family:'Roboto Slab'; font-size:16px; font-weight:900;"
            f" padding-top:9px; padding-bottom:9px; }}"
        )
        layout.addWidget(title)

        # ── Settings row: Rig | Mode | Freq | Delivery ──────────────────
        def _labeled_col(lbl_text, ctrl):
            col = QtWidgets.QVBoxLayout()
            col.setSpacing(2)
            lbl = QtWidgets.QLabel(lbl_text)
            lbl.setFont(label_font())
            col.addWidget(lbl)
            col.addWidget(ctrl)
            return col

        def _apply_combo_popup_style(combo):
            """Force a styled delegate so QAbstractItemView::item rules apply on macOS."""
            combo.setItemDelegate(QtWidgets.QStyledItemDelegate(combo))

        rig_row = QtWidgets.QHBoxLayout()
        rig_row.setSpacing(8)

        self.rig_combo = QtWidgets.QComboBox()
        self.rig_combo.setFont(mono_font())
        self.rig_combo.setMinimumWidth(180)
        self.rig_combo.setMaxVisibleItems(30)
        _apply_combo_popup_style(self.rig_combo)
        self.rig_combo.currentTextChanged.connect(self._on_rig_changed)
        rig_row.addLayout(_labeled_col("Rig:", self.rig_combo))

        self.mode_combo = QtWidgets.QComboBox()
        self.mode_combo.setFont(mono_font())
        self.mode_combo.addItem("Slow",   4)
        self.mode_combo.addItem("Normal", 0)
        self.mode_combo.addItem("Fast",   1)
        self.mode_combo.addItem("Turbo",  2)
        self.mode_combo.addItem("Ultra",  8)
        _apply_combo_popup_style(self.mode_combo)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        rig_row.addLayout(_labeled_col("Mode:", self.mode_combo))

        self.freq_field = QtWidgets.QLineEdit()
        self.freq_field.setFont(mono_font())
        self.freq_field.setMaximumWidth(100)
        self.freq_field.setReadOnly(True)
        self.freq_field.setStyleSheet(
            f"background-color: white; color: {COLOR_INPUT_TEXT};"
            f" border: 1px solid {COLOR_INPUT_BORDER}; border-radius: 4px; padding: 2px 4px;"
        )
        rig_row.addLayout(_labeled_col("Freq:", self.freq_field))

        self.delivery_combo = QtWidgets.QComboBox()
        self.delivery_combo.setFont(mono_font())
        self.delivery_combo.setMaxVisibleItems(30)
        self.delivery_combo.addItem("Maximum Reach")
        self.delivery_combo.addItem("Limited Reach")
        _apply_combo_popup_style(self.delivery_combo)
        self.delivery_combo.currentTextChanged.connect(self._on_delivery_changed)
        rig_row.addLayout(_labeled_col("Delivery:", self.delivery_combo))

        rig_row.addStretch()
        layout.addLayout(rig_row)

        # ── Header row: From | To | Grid | Scope ────────────────────────
        header_grid = QtWidgets.QGridLayout()
        header_grid.setSpacing(8)
        for col in range(4):
            header_grid.setColumnStretch(col, 1)

        def _add_header_cell(col, label_text, widget):
            lbl = QtWidgets.QLabel(label_text)
            lbl.setFont(label_font())
            header_grid.addWidget(lbl, 0, col)
            header_grid.addWidget(widget, 1, col)

        self.from_field = QtWidgets.QLineEdit(self.callsign)
        self.from_field.setFont(mono_font())
        self.from_field.textChanged.connect(self._on_from_field_changed)
        make_uppercase(self.from_field)
        _add_header_cell(0, "From:", self.from_field)

        self.to_combo = QtWidgets.QComboBox()
        self.to_combo.setFont(mono_font())
        self.to_combo.setMaxVisibleItems(30)
        self.to_combo.setEditable(True)
        self.to_combo.setInsertPolicy(QComboBox.NoInsert)
        self.to_combo.setCompleter(None)
        all_groups = self._get_all_groups_from_db()
        if len(all_groups) == 1:
            self.to_combo.addItem(all_groups[0])
        else:
            self.to_combo.addItem("")
            for group in all_groups:
                self.to_combo.addItem(group)
        # setLineEdit() must come after setEditable(True)/addItem() — it
        # replaces the combo's editor, so signals must be wired afterward.
        self.to_combo.setLineEdit(UpperCaseLineEdit(self.to_combo))
        _apply_combo_popup_style(self.to_combo)
        _add_header_cell(1, "Group or Callsign:", self.to_combo)

        self.grid_field = QtWidgets.QLineEdit(self.grid)
        self.grid_field.setMaxLength(6)
        self.grid_field.setFont(mono_font())
        self.grid_field.textChanged.connect(self._on_grid_field_changed)
        _add_header_cell(2, "Grid:", self.grid_field)

        self.scope_combo = QtWidgets.QComboBox()
        self.scope_combo.setFont(mono_font())
        for display, code in SCOPE_OPTIONS:
            self.scope_combo.addItem(display, code)
        _apply_combo_popup_style(self.scope_combo)
        _add_header_cell(3, "Scope:", self.scope_combo)

        layout.addLayout(header_grid)

        # ── Status grid (4 columns x 3 rows) ────────────────────────────
        status_grid = QtWidgets.QGridLayout()
        status_grid.setSpacing(8)
        for col in range(4):
            status_grid.setColumnStretch(col, 1)

        for i, (label, name) in enumerate(STATUS_CATEGORIES):
            label_row = (i // 4) * 2
            combo_row = label_row + 1
            col = i % 4

            cell_label = QtWidgets.QLabel(f"{label}:")
            cell_label.setFont(label_font())
            cell_label.setAlignment(Qt.AlignCenter)
            status_grid.addWidget(cell_label, label_row, col)

            combo = self._create_status_combo()
            self.status_combos[name] = combo
            if name == "status":
                # `activated` fires only on real user interaction, not on
                # programmatic setCurrentIndex — exactly what we need to
                # detect a manual override of the auto-flipping Map Pin.
                combo.activated.connect(self._on_map_pin_activated)
            else:
                combo.currentTextChanged.connect(
                    lambda _t, n=name: self._on_status_combo_changed(n)
                )
            status_grid.addWidget(combo, combo_row, col)

        self.status_grid_widget = QtWidgets.QWidget()
        self.status_grid_widget.setLayout(status_grid)
        layout.addWidget(self.status_grid_widget)

        # Remarks
        remarks_row = QtWidgets.QHBoxLayout()
        remarks_label = QtWidgets.QLabel("Remarks:")
        remarks_label.setFont(label_font())
        remarks_row.addWidget(remarks_label)
        remarks_row.addStretch()
        self.remarks_count_label = QtWidgets.QLabel()
        self.remarks_count_label.setFont(mono_font())
        remarks_row.addWidget(self.remarks_count_label)
        layout.addLayout(remarks_row)

        self.remarks_field = QtWidgets.QLineEdit()
        self.remarks_field.setFont(mono_font())
        self.remarks_field.setMaxLength(REMARKS_MAX_RADIO)
        self.remarks_field.hide()
        layout.addWidget(self.remarks_field)

        self.remarks_expanded = QtWidgets.QPlainTextEdit()
        self.remarks_expanded.setFont(mono_font())
        self.remarks_expanded.setFixedHeight(160)
        self.remarks_expanded.setPlaceholderText(
            f"Optional - max {REMARKS_MAX_INTERNET} characters, multiple lines allowed"
        )
        self.remarks_expanded.setStyleSheet(
            f"background-color: white; color: {COLOR_INPUT_TEXT};"
            f" border: 1px solid {COLOR_INPUT_BORDER}; border-radius: 4px; padding: 2px 4px;"
        )
        self.remarks_expanded.textChanged.connect(self._on_remarks_text_changed)
        _, _, initial_state = self._get_internet_user_settings()
        self.remarks_expanded.setPlainText(initial_state)
        layout.addWidget(self.remarks_expanded)
        self._update_remarks_count_label()

        layout.addStretch()

        # ── Buttons: two rows in a 5-column grid ────────────────────────
        btn_grid = QtWidgets.QGridLayout()
        btn_grid.setSpacing(8)
        for col in range(5):
            btn_grid.setColumnStretch(col, 1)

        # Row 0: Forward Mode indicator (cols 0-1), Help (col 2), Grid Finder (col 3), Brevity (col 4)
        self._forward_mode_label = QtWidgets.QLabel("Forward Mode - RF + Internet")
        self._forward_mode_label.setAlignment(QtCore.Qt.AlignCenter)
        self._forward_mode_label.setFont(label_font())
        self._forward_mode_label.setStyleSheet(
            "background-color: #FFFF00; color: #000000; border-radius: 4px; padding: 4px;"
        )
        self._forward_mode_label.setMinimumHeight(28)
        self._forward_mode_label.hide()
        btn_grid.addWidget(self._forward_mode_label, 0, 0, 1, 2)

        self.help_btn = make_button("Help", _COL_PINK)
        self.help_btn.clicked.connect(self._on_help_clicked)
        btn_grid.addWidget(self.help_btn, 0, 2)

        self.btn_gf = make_button("Grid Finder", COLOR_BTN_GREEN)
        self.btn_gf.clicked.connect(self._on_grid_finder)
        btn_grid.addWidget(self.btn_gf, 0, 3)

        self.btn_brev = make_button("Brevity", _COL_PURPLE)
        self.btn_brev.clicked.connect(self._on_brevity)
        btn_grid.addWidget(self.btn_brev, 0, 4)

        # Row 1: All Green | All Gray | Save Only | Transmit | Cancel
        self.btn_ag = make_button("All Green", COLOR_BTN_GREEN)
        self.btn_ag.clicked.connect(self._on_all_green)
        btn_grid.addWidget(self.btn_ag, 1, 0)

        self.btn_gray = make_button("All Gray", _COL_GRAY)
        self.btn_gray.clicked.connect(self._on_all_gray)
        btn_grid.addWidget(self.btn_gray, 1, 1)

        self.btn_save = make_button("Save Only", COLOR_BTN_CYAN)
        self.btn_save.clicked.connect(self._on_save_only)
        btn_grid.addWidget(self.btn_save, 1, 2)

        btn_tx = make_button("Transmit", COLOR_BTN_BLUE)
        connect_single(btn_tx, self._on_transmit)
        btn_grid.addWidget(btn_tx, 1, 3)

        btn_cancel = make_button("Cancel", _COL_CANCEL)
        btn_cancel.clicked.connect(self.close)
        btn_grid.addWidget(btn_cancel, 1, 4)

        layout.addLayout(btn_grid)

    def _create_status_combo(self) -> QComboBox:
        """Create a status dropdown with color-coded options."""
        combo = QtWidgets.QComboBox()
        combo.setFont(mono_font())
        combo.setMinimumWidth(130)
        combo.setMinimumHeight(28)
        combo.setItemDelegate(QtWidgets.QStyledItemDelegate(combo))

        for display, code in STATUS_OPTIONS:
            combo.addItem(display, code)

        combo.currentTextChanged.connect(
            lambda text, c=combo: self._update_combo_color(c, text)
        )

        return combo

    def _update_combo_color(self, combo: QComboBox, text: str) -> None:
        """Update combo box background color based on selection."""
        color = STATUS_COLORS.get(text, "#ffffff")
        if text in ("Green", "Yellow", "Red", "Unknown"):
            text_color = "#000" if text == "Yellow" else "#fff"
            combo.setStyleSheet(
                f"background-color: {color}; color: {text_color}; font-weight: bold;"
            )
        else:
            combo.setStyleSheet("")


    def _on_help_clicked(self, _link: str = "") -> None:
        """Show a styled help dialog explaining Mode, Delivery, and Color selection."""
        show_help_dialog(self, "Status Report Help", _HELP_HTML, width=470, height=430)

    def _on_commsrvr_error(self, message: str) -> None:
        from qrz_lookup import InternetDeliveryFailureDialog
        parent = self if self.isVisible() else (self.parent() or self)
        InternetDeliveryFailureDialog(message, parent=parent).exec_()

    def _show_error(self, message: str) -> None:
        """Display an error message box."""
        msg = QMessageBox(self)
        msg.setWindowTitle("CommStat Error")
        msg.setText(message)
        msg.setIcon(QMessageBox.Critical)
        msg.setWindowFlag(Qt.WindowStaysOnTopHint)
        msg.exec_()

    def _show_info(self, message: str) -> None:
        """Display an info message box."""
        msg = QMessageBox(self)
        msg.setWindowTitle("CommStat")
        msg.setText(message)
        msg.setIcon(QMessageBox.Information)
        msg.setWindowFlag(Qt.WindowStaysOnTopHint)
        msg.exec_()

    def _validate(self) -> bool:
        """Validate all form fields. Returns True if valid."""
        # Check rig is selected
        rig_name = self.rig_combo.currentText()
        if not rig_name or rig_name == "":
            self._show_error("Please select a Rig")
            self.rig_combo.setFocus()
            return False

        # Check group/callsign is entered
        group_name = self.to_combo.currentText()
        if not group_name or group_name == "":
            self._show_error("Please select a Group or enter a Callsign")
            self.to_combo.setFocus()
            return False

        # Check all status fields are selected
        for label, name in STATUS_CATEGORIES:
            combo = self.status_combos[name]
            if not combo.currentData():
                self._show_error(f"Please select a status for '{label}'")
                combo.setFocus()
                return False

        # Check grid format
        grid = self.grid.strip()
        if not grid or len(grid) not in (4, 6):
            self._show_error("Please enter a valid grid square (4 or 6 characters).")
            self.grid_field.setFocus()
            return False
        grid_upper = grid.upper()
        if not (grid_upper[0] in 'ABCDEFGHIJKLMNOPQR' and
                grid_upper[1] in 'ABCDEFGHIJKLMNOPQR' and
                grid_upper[2].isdigit() and grid_upper[3].isdigit()):
            self._show_error("Please enter a valid Maidenhead grid square (e.g., EM83 or EM83cv).")
            self.grid_field.setFocus()
            return False

        # Check remarks length
        remarks = self._get_remarks_text()
        max_len = self._remarks_max_len()
        if len(remarks) > max_len:
            self._show_error(f"Remarks too long (max {max_len} characters)")
            return False

        return True

    def _get_status_values(self) -> Dict[str, str]:
        """Collect all status values as codes."""
        values = {}
        for _, name in STATUS_CATEGORIES:
            values[name] = self.status_combos[name].currentData() or ""
        return values

    def prefill(self, data: dict) -> None:
        """Pre-populate fields from a previously received statrep for forwarding."""
        _MAP = [
            ("map",       "status"),
            ("power",     "power"),
            ("water",     "water"),
            ("med",       "medical"),
            ("telecom",   "comms"),
            ("travel",    "travel"),
            ("internet",  "internet"),
            ("fuel",      "fuel"),
            ("food",      "food"),
            ("crime",     "crime"),
            ("civil",     "civil"),
            ("political", "political"),
        ]
        # Suppress Map Pin auto-flip while loading so the forwarded report's
        # original Map Pin value is preserved verbatim.
        self._suppress_auto_map_pin = True
        try:
            for db_key, combo_key in _MAP:
                code = data.get(db_key, "")
                if code:
                    combo = self.status_combos[combo_key]
                    idx = combo.findData(code)
                    if idx >= 0:
                        combo.setCurrentIndex(idx)
        finally:
            self._suppress_auto_map_pin = False

        if data.get("grid"):
            self._grid_auto_populating = True
            self.grid_field.setText(data["grid"])
            self._grid_auto_populating = False

        scope_text = (data.get("scope") or "").strip()
        if scope_text and hasattr(self, 'scope_combo'):
            code = scope_code_for_text(scope_text)
            idx = self.scope_combo.findData(code) if code else -1
            if idx < 0 and code:
                # Retired scope code (e.g. "5"/"Other Location") isn't a normal
                # combo choice; add it so a forwarded report keeps its original
                # scope instead of silently defaulting to index 0.
                idx = self.scope_combo.count()
                self.scope_combo.addItem(scope_text, code)
            if idx >= 0:
                self.scope_combo.setCurrentIndex(idx)

        comments = (data.get("comments") or "").replace("||", "\n")
        self._forward_original_remarks = comments
        self.remarks_field.setText(comments[:self.remarks_field.maxLength()])
        self.remarks_expanded.setPlainText(comments)

        if data.get("sr_id"):
            self.statrep_id = data["sr_id"]

        if data.get("origin_callsign"):
            self._forward_origin = data["origin_callsign"]
            if hasattr(self, 'from_field'):
                self.from_field.setText(self._forward_origin)
                self.from_field.setReadOnly(True)
            if hasattr(self, '_forward_mode_label'):
                self._forward_mode_label.show()
            if hasattr(self, 'btn_save'):
                self.btn_save.setEnabled(False)
            self.setFixedSize(WINDOW_WIDTH, WINDOW_HEIGHT_FORWARD)
            self._lock_for_forward_mode()

        # If a rig is already selected (e.g. Internet Only pre-selected at open),
        # update remarks now since _on_rig_changed fired before prefill set _forward_origin.
        if hasattr(self, 'rig_combo'):
            current_rig = self.rig_combo.currentText()
            if current_rig == INTERNET_RIG:
                callsign, _, _ = self._get_internet_user_settings()
                if callsign:
                    self._forwarder_callsign = callsign
                    self._update_forward_remarks_field(callsign)
            elif self._forwarder_callsign:
                self._update_forward_remarks_field(self._forwarder_callsign)

    def _lock_for_forward_mode(self) -> None:
        """Lock all StatRep structure fields when forwarding.

        Forwarding preserves the original report verbatim. The user may only
        change Rig, Mode, Delivery, and To (target). Everything else — From,
        Grid, Scope, all 12 statuses, and remarks — is read-only. The 12
        status dropdowns are hidden outright (their values are still read
        and transmitted from the background combos) so the dialog reads as a
        report rather than an editable form, matching the Group Event
        forward view. The dialog is also resized shorter (WINDOW_HEIGHT_FORWARD)
        since there's no 12-dropdown grid left to fit.
        """
        if hasattr(self, 'grid_field'):
            self.grid_field.setReadOnly(True)
        if hasattr(self, 'scope_combo'):
            self.scope_combo.setEnabled(False)
        if hasattr(self, 'status_grid_widget'):
            self.status_grid_widget.hide()
        if hasattr(self, 'remarks_field'):
            self.remarks_field.setReadOnly(True)
        if hasattr(self, 'remarks_expanded'):
            self.remarks_expanded.setReadOnly(True)
        for attr in ('btn_ag', 'btn_gray', 'btn_brev', 'btn_gf'):
            btn = getattr(self, attr, None)
            if btn:
                btn.setEnabled(False)

    def _update_forward_remarks_field(self, callsign: str) -> None:
        """Update the remarks fields to show 'original_remarks Forwarded By: {callsign}'."""
        if not getattr(self, '_forward_origin', None) or not callsign:
            return
        base = getattr(self, '_forward_original_remarks', "")
        suffix = f" - Forwarded By: {callsign}"
        full = (base + suffix) if base else suffix.lstrip()
        if hasattr(self, 'remarks_field'):
            self.remarks_field.setText(full[:self.remarks_field.maxLength()])
        if hasattr(self, 'remarks_expanded'):
            self.remarks_expanded.setPlainText(full)

    def _set_all_status(self, status_name: str) -> None:
        """Set all status dropdowns to the specified status."""
        for _, name in STATUS_CATEGORIES:
            combo = self.status_combos[name]
            index = combo.findText(status_name)
            if index >= 0:
                combo.setCurrentIndex(index)

    def _on_map_pin_activated(self, _index: int) -> None:
        """User manually picked a Map Pin value; stop auto-flipping it."""
        self._map_pin_overridden = True

    def _on_status_combo_changed(self, _name: str) -> None:
        """A non-Map-Pin status changed; auto-flip Map Pin to the worst color."""
        if self._suppress_auto_map_pin:
            return
        if getattr(self, '_forward_origin', None):
            return
        if self._map_pin_overridden:
            return
        self._update_map_pin_from_worst()

    # Worst-to-best ordering used to pick Map Pin's auto-flip target; Unknown
    # ranks above Green since it means the category hasn't been confirmed OK.
    _STATUS_SEVERITY = {"Red": 3, "Yellow": 2, "Unknown": 1, "Green": 0}

    def _update_map_pin_from_worst(self) -> None:
        """Set Map Pin to the worst status among the other 11 categories, so
        it always tracks them (including down to Green/Unknown when nothing
        is Red or Yellow anymore, e.g. after All Green/All Gray or manually
        clearing the last flagged category)."""
        worst_text = "Green"
        worst_rank = -1
        for _label, name in STATUS_CATEGORIES:
            if name == "status":
                continue
            text = self.status_combos[name].currentText()
            rank = self._STATUS_SEVERITY.get(text, -1)
            if rank > worst_rank:
                worst_rank = rank
                worst_text = text

        map_pin = self.status_combos["status"]
        if map_pin.currentText() == worst_text:
            return
        idx = map_pin.findText(worst_text)
        if idx >= 0:
            map_pin.setCurrentIndex(idx)

    def _on_brevity(self) -> None:
        """Launch Brevity over StatRep; Copy Code inserts into remarks field."""
        from brevity import BrevityApp
        self._brevity_window = BrevityApp(self.module_background, "#333333", parent=self)
        self._brevity_window.code_selected.connect(self._on_brevity_code_selected)
        self._brevity_window.show()
        self._brevity_window.raise_()
        self._brevity_window.activateWindow()

    def _on_brevity_code_selected(self, code: str) -> None:
        """Insert selected brevity code into the remarks field and close Brevity."""
        padded = f" {code} "
        if self.remarks_field.isVisible():
            current = self.remarks_field.text()
            self.remarks_field.setText(current + padded)
            self.remarks_field.setCursorPosition(len(self.remarks_field.text()))
        else:
            cursor = self.remarks_expanded.textCursor()
            cursor.movePosition(QtGui.QTextCursor.End)
            self.remarks_expanded.setTextCursor(cursor)
            self.remarks_expanded.insertPlainText(padded)
        if hasattr(self, '_brevity_window'):
            self._brevity_window.close()

    def _on_grid_finder(self) -> None:
        """Launch Grid Finder and wire selected grid back to the grid field."""
        from gridfinder import GridFinderApp
        self._grid_finder = GridFinderApp(
            self.module_background, "#333333", self.data_background, "#000000", parent=self
        )
        self._grid_finder.setWindowModality(QtCore.Qt.WindowModal)
        self._grid_finder.grid_selected.connect(self._on_grid_finder_selected)
        self._grid_finder.show()

    def _on_grid_finder_selected(self, grid: str) -> None:
        """Receive grid from Grid Finder, populate the grid field, and close the finder."""
        self.grid_field.setText(grid)
        self._on_grid_field_changed(grid)
        if hasattr(self, '_grid_finder'):
            self._grid_finder.close()

    def _on_all_green(self) -> None:
        """Set all statuses to Green."""
        self._map_pin_overridden = False
        self._set_all_status("Green")

    def _on_all_gray(self) -> None:
        """Set all statuses to Unknown (Gray)."""
        self._map_pin_overridden = False
        self._set_all_status("Unknown")

    def _build_message(self) -> str:
        """Build the StatRep message string for transmission."""
        values = self._get_status_values()
        scope_code = self.scope_combo.currentData()
        raw_remarks = self._get_remarks_text()
        remarks = raw_remarks

        # Replace newlines with || for storage/transmission
        remarks = remarks.replace('\r\n', NEWLINE_PLACEHOLDER).replace('\n', NEWLINE_PLACEHOLDER).replace('\r', NEWLINE_PLACEHOLDER)

        # Clean remarks - only alphanumeric, spaces, hyphens, asterisks, and pipe chars
        remarks = re.sub(r"[^A-Za-z0-9*\-\s|.?!'/:()#@+=&]+", " ", remarks)

        # Build status string (all 12 values concatenated)
        status_str = "".join([
            values["status"],
            values["power"],
            values["water"],
            values["medical"],
            values["comms"],
            values["travel"],
            values["internet"],
            values["fuel"],
            values["food"],
            values["crime"],
            values["civil"],
            values["political"],
        ])

        # Compress all-green status to "+" to save bandwidth
        if status_str == "111111111111":
            status_str = "+"

        # Format: CALLSIGN: @GROUP ,GRID,SCOPE,ID,STATUSES,REMARKS,{&%}
        # (or CALLSIGN: TARGETCALL ,... when addressing a specific callsign)
        group = self._get_group_target()
        if getattr(self, "_forward_origin", None):
            marker = "{F%}"
            message = f"{self._forward_origin.upper()}: {group} ,{self.grid},{scope_code},{self.statrep_id},{status_str},{remarks},{marker}"
        else:
            marker = "{&%3}" if self.rig_combo.currentText() == INTERNET_RIG else "{&%}"
            message = f"{self.callsign.upper()}: {group} ,{self.grid},{scope_code},{self.statrep_id},{status_str},{remarks},{marker}"

        return message

    def _capture_save_data(self, frequency: int) -> dict:
        """Capture all widget state needed for DB insert on the main thread.

        Call this before launching any background thread so Qt widgets are only
        accessed from the main thread.

        Args:
            frequency: The frequency in Hz at the time of transmission.

        Returns:
            Dict of pre-captured values ready for _save_to_database().
        """
        values = self._get_status_values()
        remarks = self._get_remarks_text()
        remarks = remarks.replace('\r\n', NEWLINE_PLACEHOLDER).replace('\n', NEWLINE_PLACEHOLDER).replace('\r', NEWLINE_PLACEHOLDER)
        remarks = re.sub(r"[^A-Za-z0-9*\-\s|.?!'/:()#@+=&]+", " ", remarks)

        now = QDateTime.currentDateTimeUtc()
        return {
            'frequency': frequency,
            'source': 3 if self.rig_combo.currentText() == INTERNET_RIG else 1,
            'statrep_id': self.statrep_id,
            'callsign': self.callsign.upper(),
            'target': self._get_group_target(),
            'grid': self.grid.upper(),
            'scope_text': scope_db_text_for_code(self.scope_combo.currentData()),
            'date': now.toString("yyyy-MM-dd HH:mm:ss"),
            'date_only': now.toString("yyyy-MM-dd"),
            'map': values["status"],
            'power': values["power"],
            'water': values["water"],
            'med': values["medical"],
            'telecom': values["comms"],
            'travel': values["travel"],
            'internet': values["internet"],
            'fuel': values["fuel"],
            'food': values["food"],
            'crime': values["crime"],
            'civil': values["civil"],
            'political': values["political"],
            'comments': remarks + NEWLINE_PLACEHOLDER,
        }

    def _save_to_database(self, frequency: int = 0, global_id: int = 0) -> None:
        """Save StatRep to database.

        Uses pre-captured data from self._pending_save_data if available,
        otherwise reads widget state directly (safe only on the main thread).

        Args:
            frequency: The frequency in Hz at the time of transmission.
            global_id: The global ID returned by the commsrvr server (0 if unknown).
        """
        if hasattr(self, '_pending_save_data') and self._pending_save_data:
            d = self._pending_save_data
        else:
            d = self._capture_save_data(frequency)

        try:
            with sqlite3.connect(DATABASE_FILE, timeout=10) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO statrep(
                        global_id, datetime, date, freq, db, source, sr_id, from_callsign, target, grid, scope,
                        map, power, water, med, telecom, travel, internet,
                        fuel, food, crime, civil, political, comments
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    global_id,
                    d['date'],
                    d['date_only'],
                    d['frequency'],
                    30,  # db (SNR): set to 30 for manual entries
                    d['source'],
                    d['statrep_id'],
                    d['callsign'],
                    d['target'],
                    d['grid'],
                    d['scope_text'],
                    d['map'],
                    d['power'],
                    d['water'],
                    d['med'],
                    d['telecom'],
                    d['travel'],
                    d['internet'],
                    d['fuel'],
                    d['food'],
                    d['crime'],
                    d['civil'],
                    d['political'],
                    d['comments'],
                ))
                conn.commit()
        except sqlite3.Error as e:
            print(f"Database error saving StatRep: {e}")
            raise

    def _refresh_parent_data(self) -> None:
        """Refresh the parent window's StatRep table, map, and messages."""
        parent = self.parent()
        if parent:
            if hasattr(parent, '_load_statrep_data'):
                parent._load_statrep_data()
            if hasattr(parent, '_load_map'):
                parent._load_map()
            if hasattr(parent, '_load_message_data'):
                parent._load_message_data()

    def _refresh_and_close(self) -> None:
        """Refresh parent data and close the dialog (main-thread safe)."""
        self._refresh_parent_data()
        self.accept()

    def _on_save_only(self) -> None:
        """Validate and save without transmitting."""
        self._generate_statrep_id()
        if not self._validate():
            return

        try:
            self._save_to_database()
            message = self._build_message()

            # Print to terminal
            now = QDateTime.currentDateTimeUtc().toString("yyyy-MM-dd HH:mm:ss")
            print(f"\n{'='*60}")
            print(f"STATREP SAVED - {now} UTC")
            print(f"{'='*60}")
            print(f"  ID:       {self.statrep_id}")
            print(f"  To:       {self.to_combo.currentText()}")
            print(f"  From:     {self.callsign}")
            print(f"  Grid:     {self.grid}")
            print(f"  Scope:    {self.scope_combo.currentText()}")
            print(f"  Message:  {message}")
            print(f"{'='*60}\n")

            self._show_info(f"StatRep saved:\n{message}")
            self._refresh_parent_data()
            self.accept()
        except Exception as e:
            self._show_error(f"Failed to save StatRep: {e}")

    def _on_transmit(self) -> None:
        """Validate, check for selected call, get frequency, transmit, and save."""
        self._generate_statrep_id()
        if not self._validate():
            return

        rig_name = self.rig_combo.currentText()

        if rig_name == INTERNET_RIG:
            callsign, grid, state = self._get_internet_user_settings()
            if not callsign or not grid or not state:
                self._show_error(
                    "Cannot transmit — User Settings are not fully configured.\n\n"
                    "Please set your callsign, grid square, and state at:\n"
                    "Menu → Config → User Settings"
                )
                return
            self.callsign = callsign
            self._pending_message = self._build_message()
            if not getattr(self, '_forward_origin', None):
                self._pending_save_data = self._capture_save_data(0)

                def _on_internet_commsrvr_complete(global_id: int) -> None:
                    if global_id:
                        self._save_to_database(0, global_id)
                        QtCore.QTimer.singleShot(0, self._refresh_and_close)

                self._submit_to_commsrvr_async(0, on_complete=_on_internet_commsrvr_complete)
            else:
                self._submit_to_commsrvr_async(0)
            now = QDateTime.currentDateTimeUtc().toString("yyyy-MM-dd HH:mm:ss")
            print(f"\n{'='*60}")
            print(f"STATREP TRANSMITTED (Internet) - {now} UTC")
            print(f"{'='*60}")
            print(f"  ID:       {self.statrep_id}")
            print(f"  To:       {self.to_combo.currentText()}")
            print(f"  From:     {self.callsign}")
            print(f"  Grid:     {self.grid}")
            print(f"  Scope:    {self.scope_combo.currentText()}")
            print(f"  Message:  {self._pending_message}")
            print(f"{'='*60}\n")
            if getattr(self, '_forward_origin', None):
                self._refresh_parent_data()
                self.accept()
            return

        if "(disconnected)" in rig_name:
            self._show_error("Cannot transmit: rig is disconnected")
            return

        client = self.tcp_pool.get_client(rig_name)
        if not client or not client.is_connected():
            self._show_error("Cannot transmit: not connected to rig")
            return

        # Store the message to transmit
        self._pending_message = self._build_message()

        # First check if a call is selected in JS8Call
        try:
            client.call_selected_received.disconnect(self._on_call_selected_for_transmit)
        except TypeError:
            pass
        client.call_selected_received.connect(self._on_call_selected_for_transmit)
        client.get_call_selected()

    def _on_call_selected_for_transmit(self, rig_name: str, selected_call: str) -> None:
        """Handle call selected response - check if clear to transmit."""
        if self.rig_combo.currentText() != rig_name:
            return

        client = self.tcp_pool.get_client(rig_name)
        if client:
            try:
                client.call_selected_received.disconnect(self._on_call_selected_for_transmit)
            except TypeError:
                pass

        # If a call is selected, show error and abort
        if selected_call:
            QtWidgets.QMessageBox.critical(
                self, "ERROR",
                f"JS8Call has {selected_call} selected.\n\n"
                "Go to JS8Call and click the \"Deselect\" button.\n\n"
                "The Deselect button is above the waterfall."
            )
            return

        # No call selected - proceed with getting frequency and transmitting
        if client:
            try:
                client.frequency_received.disconnect(self._on_frequency_for_transmit)
            except TypeError:
                pass
            client.frequency_received.connect(self._on_frequency_for_transmit)
            client.get_frequency()

    def _on_frequency_for_transmit(self, rig_name: str, frequency: int) -> None:
        """Handle frequency received - now transmit and save."""
        # Only process if this is the currently selected rig
        if self.rig_combo.currentText() != rig_name:
            return

        # Disconnect signal to prevent multiple calls
        client = self.tcp_pool.get_client(rig_name)
        if client:
            try:
                client.frequency_received.disconnect(self._on_frequency_for_transmit)
            except TypeError:
                pass

        try:
            # Transmit via TCP
            client.send_tx_message(self._pending_message)

            # Save to database (skip if forwarding — record already exists)
            deferred_close = False
            if not getattr(self, '_forward_origin', None):
                self._pending_save_data = self._capture_save_data(frequency)
                if self.delivery_combo.currentText() == "Limited Reach":
                    # No commsrvr submission — save immediately with no global_id
                    self._save_to_database(frequency, 0)
                else:
                    # Delay DB write until commsrvr returns the assigned global_id
                    deferred_close = True
                    def _on_radio_commsrvr_complete(global_id: int) -> None:
                        self._save_to_database(frequency, global_id)
                        QtCore.QTimer.singleShot(0, self._refresh_and_close)
                    self._submit_to_commsrvr_async(frequency, on_complete=_on_radio_commsrvr_complete)
            elif self.delivery_combo.currentText() != "Limited Reach":
                # Forwarding path — still submit to commsrvr, no DB write
                self._submit_to_commsrvr_async(frequency)

            # Print to terminal
            now = QDateTime.currentDateTimeUtc().toString("yyyy-MM-dd HH:mm:ss")
            freq_mhz = frequency / 1000000.0 if frequency else 0
            print(f"\n{'='*60}")
            print(f"STATREP TRANSMITTED - {now} UTC")
            print(f"{'='*60}")
            print(f"  ID:       {self.statrep_id}")
            print(f"  To:       {self.to_combo.currentText()}")
            print(f"  From:     {self.callsign}")
            print(f"  Grid:     {self.grid}")
            print(f"  Scope:    {self.scope_combo.currentText()}")
            print(f"  Freq:     {freq_mhz:.6f} MHz")
            print(f"  Message:  {self._pending_message}")
            print(f"{'='*60}\n")

            if not deferred_close:
                self._refresh_parent_data()
                self.accept()
        except Exception as e:
            self._show_error(f"Failed to transmit StatRep: {e}")


# =============================================================================
# Standalone Entry Point
# =============================================================================

if __name__ == "__main__":
    import sys
    from connector_manager import ConnectorManager
    from js8_tcp_client import TCPConnectionPool

    app = QtWidgets.QApplication(sys.argv)

    # Initialize dependencies
    connector_manager = ConnectorManager()
    connector_manager.init_connectors_table()
    tcp_pool = TCPConnectionPool(connector_manager)
    tcp_pool.connect_all()

    dialog = StatRepDialog(tcp_pool, connector_manager)
    dialog.show()
    sys.exit(app.exec_())
