# Copyright (c) 2025 Manuel Ochoa
# This file is part of CommStat.
# Licensed under the GNU General Public License v3.0.
# AI Assistance: Claude (Anthropic), ChatGPT (OpenAI)

"""
Group Event Dialog for CommStat
Allows creating and transmitting AMRRON Events via JS8Call.

An Event is a StatRep record distinguished by all 12 condition columns set to
STATUS_EVENT ("6") and scope set to the literal text "EVENT" — see statrep.py.
"""

import re
import sqlite3
import threading
import urllib.parse
import urllib.request
from typing import Dict, TYPE_CHECKING

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import QDateTime, Qt
from PyQt5.QtWidgets import QMessageBox, QDialog

from constants import (
    DEFAULT_COLORS, COLOR_INPUT_TEXT, COLOR_INPUT_BORDER,
    COLOR_DISABLED_BG, COLOR_DISABLED_TEXT,
    COLOR_BTN_GREEN, COLOR_BTN_BLUE, COLOR_BTN_CYAN, COLOR_BTN_HELP, COLOR_BTN_RED,
    RIG_FREQ_DELAY_MS,
)
from id_utils import generate_time_based_id
from little_gucci import create_verified_ssl_context
from statrep import STATUS_EVENT, _COMMSRVR, _DATAFEED, INTERNET_RIG, make_uppercase
from ui_helpers import (
    make_button, label_font, mono_font, apply_standard_dialog_chrome,
    connect_single, show_help_dialog,
)

if TYPE_CHECKING:
    from js8_tcp_client import TCPConnectionPool
    from connector_manager import ConnectorManager


# ── Help content ──────────────────────────────────────────────────────────────
# Lives beside the feature it documents. Chrome comes from ui_helpers.

_HELP_HTML = """
<div style="font-family: Roboto; font-size: 13px; color: #333333;">

<p>An Event is an ongoing, developing, or planned situation that has the
potential to escalate into civil unrest, large-scale protests, rioting,
violence, anarchy, or even a full-blown revolution.</p>

<p>Events are location-based and must be associated with a Maidenhead grid
square so they can be displayed geographically within CommStat. Examples
might include a planned demonstration, a growing protest, a major political
gathering, a developing security situation, or any other activity that may
become significant enough to monitor.</p>

<p>When creating an Event, the user has the option to pin it to the map.
Pinning is not automatic. A pinned Event remains visible on CommStat maps
until users choose to remove it. This is useful for situations that may
continue developing over several hours, days, or even weeks and need to
remain easy to find and monitor.</p>

<p>In other words, pinning an Event tells CommStat, &ldquo;Keep this on the
map&mdash;we&rsquo;re still watching it.&rdquo; Events that are not pinned
can still be reported without becoming a persistent map item.</p>

<p>Think of an Event as something happening&mdash;or expected to
happen&mdash;at a particular location that may deserve continued
attention.</p>

<p>If the situation progresses to a Zombie Apocalypse, Alien Invasion, World
War, collapse of civilization, or another minor inconvenience of similar
magnitude, create a Status Report instead.</p>

</div>
"""


# =============================================================================
# Constants
# =============================================================================

DATABASE_FILE = "traffic.db3"

WINDOW_WIDTH = 700
WINDOW_HEIGHT = 494
WINDOW_HEIGHT_FORWARD = 514  # Room for the "Forward Mode" banner above the buttons
MESSAGE_MAX_RADIO = 500
MESSAGE_MAX_INTERNET = 500
NEWLINE_PLACEHOLDER = "||"

_PROG_BG    = DEFAULT_COLORS.get("program_background",  "#000000")
_PROG_FG    = DEFAULT_COLORS.get("program_foreground",  "#FFFFFF")
_DATA_BG    = DEFAULT_COLORS.get("data_background",     "#F8F6F4")
_PANEL_BG   = DEFAULT_COLORS.get("module_background",   "#DDDDDD")
_PANEL_FG   = DEFAULT_COLORS.get("module_foreground",   "#000000")
_COL_CANCEL = "#555555"
_COL_PINK   = COLOR_BTN_HELP
_COL_COUNTER = "#444444"  # muted but legible counter text (COLOR_DISABLED_TEXT is too light here)

# All 12 statrep condition columns, all forced to STATUS_EVENT for an Event.
_CONDITION_COLUMNS = [
    "map", "power", "water", "med", "telecom", "travel",
    "internet", "fuel", "food", "crime", "civil", "political",
]


# =============================================================================
# Group Event Dialog
# =============================================================================

class GroupEventDialog(QDialog):
    """Compose and transmit a Group Event (an Event-flavored StatRep record)."""

    _commsrvr_error = QtCore.pyqtSignal(str)

    def __init__(
        self,
        tcp_pool: "TCPConnectionPool",
        connector_manager: "ConnectorManager",
        parent=None,
        module_background: str = _DATA_BG,
        data_background: str = _DATA_BG,
    ):
        super().__init__(parent)
        self.tcp_pool = tcp_pool
        self.connector_manager = connector_manager
        self.module_background = module_background
        self.data_background = data_background

        apply_standard_dialog_chrome(self, "Event", WINDOW_WIDTH, WINDOW_HEIGHT)

        self._commsrvr_error.connect(self._on_commsrvr_error)

        self.callsign = ""
        self.grid = ""
        self.selected_group = ""
        self.event_id = ""
        self._pending_frequency = 0
        self._forwarder_callsign = ""       # Forwarder's callsign in forward mode
        self._forward_original_message = "" # Original message before "Forwarded By:" is appended

        self._load_config()
        self._setup_ui()
        self._load_rigs()

    def _load_config(self) -> None:
        self.selected_group = self._get_active_group_from_db()

    def _get_active_group_from_db(self) -> str:
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

    def _get_all_groups_from_db(self) -> list:
        try:
            with sqlite3.connect(DATABASE_FILE, timeout=10) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT name FROM groups ORDER BY name")
                return [row[0] for row in cursor.fetchall()]
        except sqlite3.Error as e:
            print(f"Error reading groups from database: {e}")
        return []

    def _is_internet_only(self) -> bool:
        return hasattr(self, 'rig_combo') and self.rig_combo.currentText() == INTERNET_RIG

    def _message_max_len(self) -> int:
        """Max message length for the current rig selection."""
        return MESSAGE_MAX_INTERNET if self._is_internet_only() else MESSAGE_MAX_RADIO

    def _enforce_message_limit(self) -> None:
        """Hard-cap the message at the character limit (measured post-cleaning,
        same as _validate/_build_message) and refresh the counter."""
        max_len = self._message_max_len()
        raw = self.message_edit.toPlainText()
        cleaned = self._clean_message(raw)
        if len(cleaned) > max_len:
            cursor = self.message_edit.textCursor()
            pos = cursor.position()
            while raw and len(self._clean_message(raw)) > max_len:
                raw = raw[:-1]
            cleaned = self._clean_message(raw)
            self.message_edit.blockSignals(True)
            self.message_edit.setPlainText(raw)
            self.message_edit.blockSignals(False)
            cursor = self.message_edit.textCursor()
            cursor.setPosition(min(pos, len(raw)))
            self.message_edit.setTextCursor(cursor)
        self._update_message_count_label(len(cleaned), max_len)

    def _update_message_count_label(self, count: int = None, max_len: int = None) -> None:
        """Refresh the 'N of MAX' counter next to the Message label."""
        if not hasattr(self, 'message_count_label'):
            return
        if max_len is None:
            max_len = self._message_max_len()
        if count is None:
            count = len(self._clean_message(self.message_edit.toPlainText()))
        self.message_count_label.setText(f"{count} of {max_len}")
        color = COLOR_BTN_RED if count >= max_len else _COL_COUNTER
        self.message_count_label.setStyleSheet(f"color: {color};")

    def _is_commsrvr_enabled(self) -> bool:
        """True when the global Off-Grid/Online switch is Online."""
        import netguard
        return netguard.is_network_enabled()

    def _submit_to_commsrvr_async(self, frequency: int, on_complete=None) -> None:
        """Start background thread to submit the event to commsrvr server."""
        if not self._is_commsrvr_enabled():
            if on_complete:
                on_complete(0)
            return

        callsign = self.callsign
        message = self._pending_message
        now = QDateTime.currentDateTimeUtc().toString("yyyy-MM-dd HH:mm:ss")

        def submit_thread():
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
                    print(f"[Commsrvr] Event submitted successfully (global_id={global_id})")
                else:
                    error_msg = result[5:] if result.startswith("ERR::") else (result or "Unknown server error")
                    print(f"[Commsrvr] Event submission failed — server returned: {result}")

            except Exception as e:
                reason = getattr(e, 'reason', e)
                if isinstance(reason, TimeoutError):
                    error_msg = "Server timeout — the server did not respond in time."
                else:
                    error_msg = f"Connection error — {e}"
                print(f"[Commsrvr] Event submission failed — {error_msg}")
            finally:
                if on_complete:
                    on_complete(global_id)
                if error_msg:
                    self._commsrvr_error.emit(error_msg)

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
            if internet_available:
                self.rig_combo.addItem(INTERNET_RIG)
        else:
            self.rig_combo.addItem("")
            for c in available_connectors:
                self.rig_combo.addItem(c['rig_name'])
            if internet_available:
                self.rig_combo.addItem(INTERNET_RIG)

        self.rig_combo.blockSignals(False)

        current_text = self.rig_combo.currentText()
        if current_text:
            self._on_rig_changed(current_text)

    def _on_rig_changed(self, rig_name: str) -> None:
        """Handle rig selection change - fetch callsign from JS8Call."""
        if not rig_name or "(disconnected)" in rig_name:
            if not getattr(self, '_forward_origin', None):
                self.callsign = ""
                self.grid = ""
                if hasattr(self, 'from_field'):
                    self.from_field.setText("")
                if hasattr(self, 'grid_field'):
                    self.grid_field.setText("")
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

        self._update_message_count_label()

        if rig_name == INTERNET_RIG:
            # Grid is intentionally left as whatever the operator has typed —
            # unlike StatRep, Group Event never auto-fills it from User Settings.
            callsign, _, _ = self._get_internet_user_settings()
            if getattr(self, '_forward_origin', None):
                self._forwarder_callsign = callsign
                self._update_forward_message_field(callsign)
            else:
                self.callsign = callsign
                if hasattr(self, 'from_field'):
                    self.from_field.setText(callsign)
            if hasattr(self, 'freq_field'):
                self.freq_field.setText("")
            if hasattr(self, 'mode_combo'):
                self.mode_combo.setEnabled(False)
                self.mode_combo.setCurrentIndex(-1)
            return

        if hasattr(self, 'mode_combo'):
            self.mode_combo.setEnabled(True)
            if self.mode_combo.currentIndex() == -1:
                self.mode_combo.setCurrentIndex(0)

        if not self.tcp_pool:
            print("[GroupEvent] No TCP pool available")
            return

        for client_name in self.tcp_pool.get_all_rig_names():
            client = self.tcp_pool.get_client(client_name)
            if client:
                try:
                    client.callsign_received.disconnect(self._on_callsign_received)
                except TypeError:
                    pass
                try:
                    client.frequency_received.disconnect(self._on_frequency_received)
                except TypeError:
                    pass

        client = self.tcp_pool.get_client(rig_name)
        if client and client.is_connected():
            client.callsign_received.connect(self._on_callsign_received)
            client.frequency_received.connect(self._on_frequency_received)

            if hasattr(self, 'mode_combo'):
                speed_name = (client.speed_name or "").upper()
                mode_map = {"SLOW": 0, "NORMAL": 1, "FAST": 2, "TURBO": 3, "ULTRA": 4}
                idx = mode_map.get(speed_name, 1)
                self.mode_combo.blockSignals(True)
                self.mode_combo.setCurrentIndex(idx)
                self.mode_combo.blockSignals(False)

            if hasattr(self, 'freq_field'):
                frequency = client.frequency
                if frequency:
                    self.freq_field.setText(f"{frequency:.3f}")
                else:
                    self.freq_field.setText("")

            print(f"[GroupEvent] Requesting callsign and frequency from {rig_name}")
            client.get_callsign()
            QtCore.QTimer.singleShot(RIG_FREQ_DELAY_MS, client.get_frequency)
        else:
            print(f"[GroupEvent] Client not available or not connected for {rig_name}")
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
        rig_name = self.rig_combo.currentText()
        if not rig_name or rig_name == INTERNET_RIG or "(disconnected)" in rig_name:
            return
        if not self.tcp_pool:
            return
        client = self.tcp_pool.get_client(rig_name)
        if client and client.is_connected():
            speed_value = self.mode_combo.currentData()
            client.send_message("MODE.SET_SPEED", "", {"SPEED": speed_value})
            print(f"[GroupEvent] Set mode to {self.mode_combo.currentText()} (speed={speed_value})")

    def _on_delivery_changed(self, delivery: str) -> None:
        pass

    def _on_callsign_received(self, rig_name: str, callsign: str) -> None:
        if self.rig_combo.currentText() == rig_name:
            if getattr(self, '_forward_origin', None):
                self._forwarder_callsign = callsign
                self._update_forward_message_field(callsign)
            else:
                self.callsign = callsign
                if hasattr(self, 'from_field'):
                    self.from_field.setText(callsign)

    def _on_frequency_received(self, rig_name: str, dial_freq: int) -> None:
        if self.rig_combo.currentText() == rig_name:
            frequency_mhz = dial_freq / 1000000
            print(f"[GroupEvent] Frequency received from {rig_name}: {frequency_mhz:.3f} MHz")
            if hasattr(self, 'freq_field'):
                self.freq_field.setText(f"{frequency_mhz:.3f}")

    def _on_from_field_changed(self, text: str) -> None:
        self.callsign = text.upper()

    def _on_grid_field_changed(self, text: str) -> None:
        raw = text.strip()
        formatted = raw.upper()
        self.grid = formatted
        if text != formatted:
            pos = self.grid_field.cursorPosition()
            self.grid_field.blockSignals(True)
            self.grid_field.setText(formatted)
            self.grid_field.blockSignals(False)
            self.grid_field.setCursorPosition(pos)

    def _generate_event_id(self) -> None:
        """Generate a time-based Event ID from current UTC time."""
        if not self.event_id:
            self.event_id = generate_time_based_id()

    def prefill(self, data: dict) -> None:
        """Pre-populate fields from a previously received Event, for forwarding."""
        if data.get("grid"):
            self.grid_field.setText(data["grid"])

        message = (data.get("comments") or "").replace("||", "\n")
        self._forward_original_message = message
        self.message_edit.setPlainText(message)

        if data.get("sr_id"):
            self.event_id = data["sr_id"]

        if hasattr(self, 'pin_combo'):
            self.pin_combo.setCurrentIndex(0 if data.get("pinned") else 1)

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
        # update the message now since _on_rig_changed fired before prefill set
        # _forward_origin.
        if hasattr(self, 'rig_combo'):
            current_rig = self.rig_combo.currentText()
            if current_rig == INTERNET_RIG:
                callsign, _, _ = self._get_internet_user_settings()
                if callsign:
                    self._forwarder_callsign = callsign
                    self._update_forward_message_field(callsign)
            elif self._forwarder_callsign:
                self._update_forward_message_field(self._forwarder_callsign)

    def _lock_for_forward_mode(self) -> None:
        """Lock Event structure fields when forwarding.

        Forwarding preserves the original event verbatim. The user may only
        change Rig, Mode, Delivery, and Group (target) — Grid, Pin to Map,
        and the message body are read-only.
        """
        if hasattr(self, 'grid_field'):
            self.grid_field.setReadOnly(True)
        if hasattr(self, 'pin_combo'):
            self.pin_combo.setEnabled(False)
        if hasattr(self, 'message_edit'):
            self.message_edit.setReadOnly(True)
        if hasattr(self, 'btn_gf'):
            self.btn_gf.setEnabled(False)

    def _update_forward_message_field(self, callsign: str) -> None:
        """Update the message field to show 'original_message - Forwarded By: {callsign}'."""
        if not getattr(self, '_forward_origin', None) or not callsign:
            return
        base = getattr(self, '_forward_original_message', "")
        suffix = f" - Forwarded By: {callsign}"
        full = (base + suffix) if base else suffix.lstrip()
        if hasattr(self, 'message_edit'):
            self.message_edit.setPlainText(full)

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

        title = QtWidgets.QLabel("Group Event")
        title.setAlignment(Qt.AlignCenter)
        title.setFont(QtGui.QFont("Roboto Slab", -1, QtGui.QFont.Black))
        title.setFixedHeight(36)
        title.setStyleSheet(
            f"QLabel {{ background-color:{_PROG_BG}; color:{_PROG_FG};"
            f" font-family:'Roboto Slab'; font-size:16px; font-weight:900;"
            f" padding-top:9px; padding-bottom:9px; }}"
        )
        layout.addWidget(title)

        subtitle = QtWidgets.QLabel(
            "An Event is an ongoing, developing, or planned situation that could "
            "escalate into\ncivil unrest, protests, rioting, violence, anarchy, or revolution."
        )
        subtitle.setAlignment(Qt.AlignCenter)
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet(
            f"QLabel {{ color: {_PANEL_FG}; background-color: transparent;"
            f" font-family: 'Roboto'; font-size: 15px;"
            f" padding: 2px 10px 4px 10px; }}"
        )
        layout.addWidget(subtitle)

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

        # ── Header row: From | To | Grid | Pin to Map ───────────────────
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
        all_groups = self._get_all_groups_from_db()
        if len(all_groups) == 1:
            self.to_combo.addItem(all_groups[0])
        else:
            self.to_combo.addItem("")
            for group in all_groups:
                self.to_combo.addItem(group)
        _apply_combo_popup_style(self.to_combo)
        _add_header_cell(1, "Group:", self.to_combo)

        self.grid_field = QtWidgets.QLineEdit(self.grid)
        self.grid_field.setMaxLength(6)
        self.grid_field.setFont(mono_font())
        self.grid_field.textChanged.connect(self._on_grid_field_changed)
        _add_header_cell(2, "Grid:", self.grid_field)

        self.pin_combo = QtWidgets.QComboBox()
        self.pin_combo.setFont(mono_font())
        self.pin_combo.addItem("Yes")
        self.pin_combo.addItem("No")
        self.pin_combo.setCurrentIndex(0)  # Default to "Yes"
        _apply_combo_popup_style(self.pin_combo)
        _add_header_cell(3, "Persistent Map Pin:", self.pin_combo)

        layout.addLayout(header_grid)

        # Message body
        message_row = QtWidgets.QHBoxLayout()
        message_label = QtWidgets.QLabel("Message:")
        message_label.setFont(label_font())
        message_row.addWidget(message_label)
        message_row.addStretch()
        self.message_count_label = QtWidgets.QLabel()
        self.message_count_label.setFont(mono_font())
        message_row.addWidget(self.message_count_label)
        layout.addLayout(message_row)

        self.message_edit = QtWidgets.QPlainTextEdit()
        self.message_edit.setFont(mono_font())
        self.message_edit.setFixedHeight(160)
        self.message_edit.setPlaceholderText(
            f"Max {MESSAGE_MAX_RADIO} characters, multiple lines allowed"
        )
        self.message_edit.setStyleSheet(
            f"background-color: white; color: {COLOR_INPUT_TEXT};"
            f" border: 1px solid {COLOR_INPUT_BORDER}; border-radius: 4px; padding: 2px 4px;"
        )
        self.message_edit.textChanged.connect(self._enforce_message_limit)
        layout.addWidget(self.message_edit)
        self._update_message_count_label()

        layout.addStretch()

        self._forward_mode_label = QtWidgets.QLabel("Forward Mode - RF + Internet")
        self._forward_mode_label.setAlignment(Qt.AlignCenter)
        self._forward_mode_label.setFont(label_font())
        self._forward_mode_label.setStyleSheet(
            "background-color: #FFFF00; color: #000000; border-radius: 4px; padding: 4px;"
        )
        self._forward_mode_label.setMinimumHeight(28)
        self._forward_mode_label.hide()
        layout.addWidget(self._forward_mode_label)

        # ── Buttons: Help | Grid Finder | Save Only | Transmit | Cancel ─
        # Same 5-column stretch grid as Status Report so button width matches.
        btn_grid = QtWidgets.QGridLayout()
        btn_grid.setSpacing(8)
        for col in range(5):
            btn_grid.setColumnStretch(col, 1)

        self.help_btn = make_button("Help", _COL_PINK)
        self.help_btn.clicked.connect(self._on_help_clicked)
        btn_grid.addWidget(self.help_btn, 0, 0)

        self.btn_gf = make_button("Grid Finder", COLOR_BTN_GREEN)
        self.btn_gf.clicked.connect(self._on_grid_finder)
        btn_grid.addWidget(self.btn_gf, 0, 1)

        self.btn_save = make_button("Save Only", COLOR_BTN_CYAN)
        self.btn_save.clicked.connect(self._on_save_only)
        btn_grid.addWidget(self.btn_save, 0, 2)

        btn_tx = make_button("Transmit", COLOR_BTN_BLUE)
        connect_single(btn_tx, self._on_transmit)
        btn_grid.addWidget(btn_tx, 0, 3)

        btn_cancel = make_button("Cancel", _COL_CANCEL)
        btn_cancel.clicked.connect(self.close)
        btn_grid.addWidget(btn_cancel, 0, 4)

        layout.addLayout(btn_grid)

    def _on_help_clicked(self) -> None:
        """Show a styled help dialog explaining what an Event is and how pinning works."""
        show_help_dialog(self, "Group Event Help", _HELP_HTML, width=470, height=430)

    def _on_commsrvr_error(self, message: str) -> None:
        from qrz_lookup import InternetDeliveryFailureDialog
        parent = self if self.isVisible() else (self.parent() or self)
        InternetDeliveryFailureDialog(message, parent=parent).exec_()

    def _show_error(self, message: str) -> None:
        msg = QMessageBox(self)
        msg.setWindowTitle("CommStat Error")
        msg.setText(message)
        msg.setIcon(QMessageBox.Critical)
        msg.setWindowFlag(Qt.WindowStaysOnTopHint)
        msg.exec_()

    def _show_info(self, message: str) -> None:
        msg = QMessageBox(self)
        msg.setWindowTitle("CommStat")
        msg.setText(message)
        msg.setIcon(QMessageBox.Information)
        msg.setWindowFlag(Qt.WindowStaysOnTopHint)
        msg.exec_()

    def _validate(self) -> bool:
        """Validate all form fields. Returns True if valid."""
        rig_name = self.rig_combo.currentText()
        if not rig_name or rig_name == "":
            self._show_error("Please select a Rig")
            self.rig_combo.setFocus()
            return False

        group_name = self.to_combo.currentText()
        if not group_name or group_name == "":
            self._show_error("Please select a Group")
            self.to_combo.setFocus()
            return False

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

        # Measure the cleaned message (newlines expanded to the 2-char "||"
        # placeholder) — that's what actually gets transmitted/stored, and it
        # can run longer than the raw textbox contents.
        message = self._clean_message(self.message_edit.toPlainText().strip())
        max_len = self._message_max_len()
        if len(message) > max_len:
            self._show_error(f"Message too long (max {max_len} characters)")
            return False

        return True

    def _clean_message(self, text: str) -> str:
        """Replace newlines with the storage/transmission placeholder and strip
        characters outside the allowed transmit charset (matches StatRep remarks)."""
        cleaned = text.replace('\r\n', NEWLINE_PLACEHOLDER).replace('\n', NEWLINE_PLACEHOLDER).replace('\r', NEWLINE_PLACEHOLDER)
        return re.sub(r"[^A-Za-z0-9*\-\s|.?!'/:()#@+=&]+", " ", cleaned)

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
        self.grid_field.setText(grid)  # triggers _on_grid_field_changed via textChanged
        if hasattr(self, '_grid_finder'):
            self._grid_finder.close()

    def _build_message(self) -> str:
        """Build the Group Event message string for transmission.

        Format: CALLSIGN: @GROUP ,GRID,6,ID,PIN,MESSAGE,{##}
        Forwarded: ORIGIN_CALL: @GROUP ,GRID,6,ID,PIN,MESSAGE,{F#}
        Scope slot is hardcoded to "6" (Event); the normal 12-digit status
        string is replaced by a single Pin-to-Map digit ("1"/"0").
        """
        message = self._clean_message(self.message_edit.toPlainText().strip())
        pin_flag = "1" if self.pin_combo.currentText() == "Yes" else "0"
        group = f"@{self.to_combo.currentText()}"
        if getattr(self, "_forward_origin", None):
            marker = "{F#}"
            return f"{self._forward_origin.upper()}: {group} ,{self.grid},6,{self.event_id},{pin_flag},{message},{marker}"
        marker = "{#3}" if self.rig_combo.currentText() == INTERNET_RIG else "{##}"
        return f"{self.callsign.upper()}: {group} ,{self.grid},6,{self.event_id},{pin_flag},{message},{marker}"

    def _capture_save_data(self, frequency: int) -> dict:
        """Capture all widget state needed for DB insert on the main thread."""
        message = self._clean_message(self.message_edit.toPlainText().strip())
        now = QDateTime.currentDateTimeUtc()
        data = {
            'frequency': frequency,
            'source': 3 if self.rig_combo.currentText() == INTERNET_RIG else 1,
            'event_id': self.event_id,
            'callsign': self.callsign.upper(),
            'target': '@' + self.to_combo.currentText().upper(),
            'grid': self.grid.upper(),
            'date': now.toString("yyyy-MM-dd HH:mm:ss"),
            'date_only': now.toString("yyyy-MM-dd"),
            'comments': message,
            'pinned': 1 if self.pin_combo.currentText() == "Yes" else 0,
        }
        for col in _CONDITION_COLUMNS:
            data[col] = STATUS_EVENT
        return data

    def _save_to_database(self, frequency: int = 0, global_id: int = 0) -> None:
        """Save the Event to the statrep table."""
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
                        fuel, food, crime, civil, political, comments, pinned
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    global_id,
                    d['date'],
                    d['date_only'],
                    d['frequency'],
                    30,  # db (SNR): set to 30 for manual entries
                    d['source'],
                    d['event_id'],
                    d['callsign'],
                    d['target'],
                    d['grid'],
                    "EVENT",
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
                    d['pinned'],
                ))
                conn.commit()
        except sqlite3.Error as e:
            print(f"Database error saving Group Event: {e}")
            raise

    def _refresh_parent_data(self) -> None:
        """Refresh the parent window's StatRep table and map."""
        parent = self.parent()
        if parent:
            if hasattr(parent, '_load_statrep_data'):
                parent._load_statrep_data()
            if hasattr(parent, '_load_map'):
                parent._load_map()

    def _refresh_and_close(self) -> None:
        """Refresh parent data and close the dialog (main-thread safe)."""
        self._refresh_parent_data()
        self.accept()

    def _on_save_only(self) -> None:
        """Validate and save without transmitting."""
        self._generate_event_id()
        if not self._validate():
            return

        try:
            self._save_to_database()
            message = self._build_message()

            now = QDateTime.currentDateTimeUtc().toString("yyyy-MM-dd HH:mm:ss")
            print(f"\n{'='*60}")
            print(f"GROUP EVENT SAVED - {now} UTC")
            print(f"{'='*60}")
            print(f"  ID:       {self.event_id}")
            print(f"  To:       {self.to_combo.currentText()}")
            print(f"  From:     {self.callsign}")
            print(f"  Grid:     {self.grid}")
            print(f"  Pin:      {self.pin_combo.currentText()}")
            print(f"  Message:  {message}")
            print(f"{'='*60}\n")

            self._show_info(f"Group Event saved:\n{message}")
            self._refresh_parent_data()
            self.accept()
        except Exception as e:
            self._show_error(f"Failed to save Group Event: {e}")

    def _on_transmit(self) -> None:
        """Validate, check for selected call, get frequency, transmit, and save."""
        self._generate_event_id()
        if not self._validate():
            return

        rig_name = self.rig_combo.currentText()

        if rig_name == INTERNET_RIG:
            # Only callsign is actually used below — Group Event transmits the
            # Grid the operator entered in this dialog, not User Settings' grid.
            callsign, _, _ = self._get_internet_user_settings()
            if not callsign:
                self._show_error(
                    "Cannot transmit — User Settings are not fully configured.\n\n"
                    "Please set your callsign at:\n"
                    "Menu → Config → User Settings"
                )
                return
            if not self._is_commsrvr_enabled():
                self._show_error(
                    "Cannot transmit — Off-Grid Mode is enabled.\n\n"
                    "Switch to Online mode to transmit via the Internet."
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
            print(f"GROUP EVENT TRANSMITTED (Internet) - {now} UTC")
            print(f"{'='*60}")
            print(f"  ID:       {self.event_id}")
            print(f"  To:       {self.to_combo.currentText()}")
            print(f"  From:     {self.callsign}")
            print(f"  Grid:     {self.grid}")
            print(f"  Pin:      {self.pin_combo.currentText()}")
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

        self._pending_message = self._build_message()

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

        if selected_call:
            QtWidgets.QMessageBox.critical(
                self, "ERROR",
                f"JS8Call has {selected_call} selected.\n\n"
                "Go to JS8Call and click the \"Deselect\" button.\n\n"
                "The Deselect button is above the waterfall."
            )
            return

        if client:
            try:
                client.frequency_received.disconnect(self._on_frequency_for_transmit)
            except TypeError:
                pass
            client.frequency_received.connect(self._on_frequency_for_transmit)
            client.get_frequency()

    def _on_frequency_for_transmit(self, rig_name: str, frequency: int) -> None:
        """Handle frequency received - now transmit and save."""
        if self.rig_combo.currentText() != rig_name:
            return

        client = self.tcp_pool.get_client(rig_name)
        if client:
            try:
                client.frequency_received.disconnect(self._on_frequency_for_transmit)
            except TypeError:
                pass

        try:
            client.send_tx_message(self._pending_message)

            deferred_close = False
            if not getattr(self, '_forward_origin', None):
                self._pending_save_data = self._capture_save_data(frequency)
                if self.delivery_combo.currentText() == "Limited Reach":
                    self._save_to_database(frequency, 0)
                else:
                    deferred_close = True
                    def _on_radio_commsrvr_complete(global_id: int) -> None:
                        self._save_to_database(frequency, global_id)
                        QtCore.QTimer.singleShot(0, self._refresh_and_close)
                    self._submit_to_commsrvr_async(frequency, on_complete=_on_radio_commsrvr_complete)
            elif self.delivery_combo.currentText() != "Limited Reach":
                # Forwarding path — still submit to commsrvr, no DB write
                self._submit_to_commsrvr_async(frequency)

            now = QDateTime.currentDateTimeUtc().toString("yyyy-MM-dd HH:mm:ss")
            freq_mhz = frequency / 1000000.0 if frequency else 0
            print(f"\n{'='*60}")
            print(f"GROUP EVENT TRANSMITTED - {now} UTC")
            print(f"{'='*60}")
            print(f"  ID:       {self.event_id}")
            print(f"  To:       {self.to_combo.currentText()}")
            print(f"  From:     {self.callsign}")
            print(f"  Grid:     {self.grid}")
            print(f"  Pin:      {self.pin_combo.currentText()}")
            print(f"  Freq:     {freq_mhz:.6f} MHz")
            print(f"  Message:  {self._pending_message}")
            print(f"{'='*60}\n")

            if not deferred_close:
                self._refresh_parent_data()
                self.accept()
        except Exception as e:
            self._show_error(f"Failed to transmit Group Event: {e}")


# =============================================================================
# Standalone Entry Point
# =============================================================================

if __name__ == "__main__":
    import sys
    from connector_manager import ConnectorManager
    from js8_tcp_client import TCPConnectionPool

    app = QtWidgets.QApplication(sys.argv)

    connector_manager = ConnectorManager()
    connector_manager.init_connectors_table()
    tcp_pool = TCPConnectionPool(connector_manager)
    tcp_pool.connect_all()

    dialog = GroupEventDialog(tcp_pool, connector_manager)
    dialog.show()
    sys.exit(app.exec_())
