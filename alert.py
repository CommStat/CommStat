# Copyright (c) 2025 Manuel Ochoa
# This file is part of CommStat.
# Licensed under the GNU General Public License v3.0.
"""
alert.py - Group Alert Dialog

Allows creating and transmitting group callsign alerts via JS8Call.
"""

import re
import sqlite3
import sys
from typing import Optional, TYPE_CHECKING

from PyQt5 import QtWidgets
from PyQt5.QtCore import QDateTime
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout,
    QLabel, QComboBox,
    QPlainTextEdit,
)

from constants import (
    SPEED_OPTIONS, INTERNET_RIG,
    COMMSRVR_URL,
    DEFAULT_COLORS,
    COLOR_BTN_BLUE, COLOR_BTN_CYAN, COLOR_BTN_RED,
)
from db_utils import db_connect
from id_utils import generate_time_based_id
from transmit_base import RigDialogMixin
from ui_helpers import (show_error, get_internet_user_settings, make_button, label_font, apply_standard_dialog_chrome, connect_single,
                        make_title_strip, make_combobox, make_input)
from commsrvr_client import submit_to_commsrvr

if TYPE_CHECKING:
    from js8_tcp_client import TCPConnectionPool
    from connector_manager import ConnectorManager


# =============================================================================
# Constants
# =============================================================================

MIN_CALLSIGN_LENGTH = 4
MAX_CALLSIGN_LENGTH = 8
MAX_TITLE_LENGTH    = 20
MAX_MESSAGE_LENGTH  = 195
NEWLINE_PLACEHOLDER = "||"

_COMMSRVR = COMMSRVR_URL


_PANEL_BG = DEFAULT_COLORS.get("module_background",    "#DDDDDD")
_PANEL_FG = DEFAULT_COLORS.get("module_foreground",    "#000000")

_COL_CANCEL = "#555555"
_COL_COUNTER = "#444444"  # muted but legible counter text (COLOR_DISABLED_TEXT is too light here)

_WIN_W = 640
_WIN_H = 440

CALLSIGN_PATTERN = re.compile(r'[A-Z0-9]{1,3}[0-9][A-Z]{1,3}')

COLOR_OPTIONS = [
    ("Yellow", 1, "#e8e800", "#000000"),
    ("Orange", 2, "#ff8c00", "#ffffff"),
    ("Red",    3, "#dc3545", "#ffffff"),
    ("Black",  4, "#000000", "#ffffff"),
]

# =============================================================================
# Dialog
# =============================================================================

class AlertDialog(RigDialogMixin, QDialog):
    """Group Alert dialog — create and transmit callsign alerts via JS8Call."""

    ALLOW_INTERNET_RIG = True


    def __init__(
        self,
        tcp_pool: "TCPConnectionPool" = None,
        connector_manager: "ConnectorManager" = None,
        on_alert_saved: callable = None,
        parent=None,
    ):
        super().__init__(parent)
        self.tcp_pool            = tcp_pool
        self.connector_manager   = connector_manager
        self.on_alert_saved      = on_alert_saved
        self.callsign: str       = ""
        self.grid: str           = ""
        self.alert_id: str       = ""
        self._pending_message: str  = ""
        self._pending_callsign: str = ""


        apply_standard_dialog_chrome(self, "Alerts", _WIN_W, _WIN_H)

        self._setup_ui()
        self._generate_alert_id()
        self._load_config()
        self._load_rigs()

        self.rig_combo.currentTextChanged.connect(self._on_rig_changed)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)

    # =========================================================================
    # UI Construction
    # =========================================================================

    def _setup_ui(self) -> None:
        self.setStyleSheet(
            f"QDialog {{ background-color:{_PANEL_BG}; }}"
            f"QLabel {{ font-family:Roboto; font-size:13px; color:{_PANEL_FG}; }}"
            f"QPlainTextEdit {{ background-color:white; color:#333333; border:1px solid #cccccc;"
            f" border-radius:4px; padding:2px 6px; font-family:'Kode Mono'; font-size:13px; }}"
            f"QPlainTextEdit:focus {{ border:1px solid #007bff; }}"
        )

        body = QVBoxLayout(self)
        body.setContentsMargins(15, 15, 15, 15)
        body.setSpacing(10)

        # ── Title ─────────────────────────────────────────────────────────────
        title_lbl = make_title_strip("Group Alert / Callsign Alert")
        body.addWidget(title_lbl)

        # ── Settings row ──────────────────────────────────────────────────────
        def _labeled_col(lbl_text, widget):
            col = QVBoxLayout()
            col.setSpacing(2)
            lbl = QLabel(lbl_text)
            lbl.setFont(label_font())
            col.addWidget(lbl)
            col.addWidget(widget)
            return col

        settings_row = QHBoxLayout()
        settings_row.setSpacing(12)

        self.rig_combo = make_combobox([], list_popup=True)
        settings_row.addLayout(_labeled_col("Rig:", self.rig_combo))

        self.mode_combo = make_combobox(
            SPEED_OPTIONS,
            list_popup=True,
        )
        settings_row.addLayout(_labeled_col("Mode:", self.mode_combo))

        self.freq_field = make_input(read_only=True)
        self.freq_field.setFixedWidth(90)
        settings_row.addLayout(_labeled_col("Freq:", self.freq_field))

        self.delivery_combo = make_combobox(
            [("Maximum Reach", None), ("Limited Reach", None)], list_popup=True
        )
        settings_row.addLayout(_labeled_col("Delivery:", self.delivery_combo))

        settings_row.addStretch()
        body.addLayout(settings_row)

        # ── Target ────────────────────────────────────────────────────────────
        target_lbl = QLabel("Group or Callsign:")
        target_lbl.setFont(label_font())
        body.addWidget(target_lbl)

        target_row = QHBoxLayout()
        target_row.setSpacing(8)

        self.to_combo = make_combobox([], list_popup=True, editable=True)
        self.to_combo.setMinimumWidth(200)
        target_row.addWidget(self.to_combo)
        target_row.addStretch()
        body.addLayout(target_row)

        # ── Color combo (functional, not displayed) ───────────────────────────
        self.color_combo = QComboBox()
        for name, value, _bg, _fg in COLOR_OPTIONS:
            self.color_combo.addItem(name, value)
        self.color_combo.setCurrentIndex(0)

        # ── Title field ───────────────────────────────────────────────────────
        title_input_lbl = QLabel("Title:")
        title_input_lbl.setFont(label_font())
        body.addWidget(title_input_lbl)

        self.title_field = make_input("20 characters max", max_len=MAX_TITLE_LENGTH)
        body.addWidget(self.title_field)

        # ── Message field ─────────────────────────────────────────────────────
        message_row = QHBoxLayout()
        message_lbl = QLabel("Message:")
        message_lbl.setFont(label_font())
        message_row.addWidget(message_lbl)
        message_row.addStretch()
        self.message_count_label = QLabel()
        self.message_count_label.setStyleSheet(
            "QLabel { font-family:'Kode Mono'; font-size:13px; }"
        )
        message_row.addWidget(self.message_count_label)
        body.addLayout(message_row)

        self.message_field = QPlainTextEdit()
        self.message_field.setPlaceholderText(f"{MAX_MESSAGE_LENGTH} characters max, multiple lines allowed")
        self.message_field.setFixedHeight(86)
        self.message_field.textChanged.connect(self._enforce_message_limit)
        body.addWidget(self.message_field)
        self._update_message_count_label()

        body.addStretch()

        # ── Buttons ───────────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch()

        self.save_button = make_button("Save Only", COLOR_BTN_CYAN, min_w=100)
        self.save_button.clicked.connect(self._save_only)
        btn_row.addWidget(self.save_button)

        self.transmit_button = make_button("Transmit", COLOR_BTN_BLUE, min_w=100)
        connect_single(self.transmit_button, self._transmit)
        btn_row.addWidget(self.transmit_button)

        self.cancel_button = make_button("Cancel", _COL_CANCEL, min_w=100)
        self.cancel_button.clicked.connect(self.reject)
        btn_row.addWidget(self.cancel_button)

        body.addLayout(btn_row)

    # =========================================================================
    # Config / DB
    # =========================================================================

    def _load_config(self) -> None:
        all_groups = self._get_all_groups_from_db()
        if len(all_groups) == 1:
            self.to_combo.addItem(all_groups[0])
        else:
            self.to_combo.addItem("")
            for group in all_groups:
                self.to_combo.addItem(group)

    def _on_rig_changed(self, rig_name: str) -> None:
        if not rig_name:
            self.callsign = ""
            self.freq_field.setText("")
            return

        is_internet = (rig_name == INTERNET_RIG)
        self.delivery_combo.blockSignals(True)
        self.delivery_combo.clear()
        self.delivery_combo.addItem("Maximum Reach")
        if not is_internet:
            self.delivery_combo.addItem("Limited Reach")
        self.delivery_combo.blockSignals(False)

        if rig_name == INTERNET_RIG:
            self.callsign = get_internet_user_settings()[0]
            self.freq_field.setText("")
            self.mode_combo.setEnabled(False)
            return

        self.mode_combo.setEnabled(True)

        if not self.tcp_pool:
            return

        self._disconnect_rig_signals("rig")
        client = self.tcp_pool.get_client(rig_name)
        if client and client.is_connected():
            self._sync_mode_combo(client)

            self._show_frequency(client)

            self._connect_rig_signal(client, "callsign_received", self._on_callsign_received)
            client.get_callsign()
        else:
            self.freq_field.setText("")

    def _on_callsign_received(self, rig_name: str, callsign: str) -> None:
        if self.rig_combo.currentText() == rig_name:
            self.callsign = callsign

    def _get_all_groups_from_db(self) -> list:
        try:
            with db_connect() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT name FROM groups ORDER BY name")
                return [row[0] for row in cursor.fetchall()]
        except sqlite3.Error as e:
            print(f"Error reading groups from database: {e}")
        return []

    @staticmethod
    def _clean_message(text: str) -> str:
        """Encode newlines as "||" (decoded back to newlines in the alert display),
        then replace any remaining non-printable/non-ASCII runs with a space."""
        text = text.replace('\r\n', NEWLINE_PLACEHOLDER).replace('\n', NEWLINE_PLACEHOLDER).replace('\r', NEWLINE_PLACEHOLDER)
        return re.sub(r"[^ -~]+", " ", text)

    def _enforce_message_limit(self) -> None:
        """Hard-cap the message at the character limit (measured post-cleaning,
        with newlines expanded to "||", same as what is transmitted/stored)
        and refresh the counter."""
        raw = self.message_field.toPlainText()
        cleaned = self._clean_message(raw)
        if len(cleaned) > MAX_MESSAGE_LENGTH:
            cursor = self.message_field.textCursor()
            pos = cursor.position()
            # Pre-trim to the limit so a large paste doesn't loop char-by-char
            # over thousands of characters
            raw = raw[:MAX_MESSAGE_LENGTH]
            while raw and len(self._clean_message(raw)) > MAX_MESSAGE_LENGTH:
                raw = raw[:-1]
            cleaned = self._clean_message(raw)
            self.message_field.blockSignals(True)
            self.message_field.setPlainText(raw)
            self.message_field.blockSignals(False)
            cursor = self.message_field.textCursor()
            cursor.setPosition(min(pos, len(raw)))
            self.message_field.setTextCursor(cursor)
        self._update_message_count_label(len(cleaned))

    def _update_message_count_label(self, count: Optional[int] = None) -> None:
        """Refresh the 'N of MAX' counter next to the Message label."""
        if not hasattr(self, 'message_count_label'):
            return
        if count is None:
            count = len(self._clean_message(self.message_field.toPlainText()))
        self.message_count_label.setText(f"{count} of {MAX_MESSAGE_LENGTH}")
        color = COLOR_BTN_RED if count >= MAX_MESSAGE_LENGTH else _COL_COUNTER
        self.message_count_label.setStyleSheet(
            f"QLabel {{ font-family:'Kode Mono'; font-size:13px; color:{color}; }}"
        )

    def _get_target(self) -> str:
        """Build the transmit/DB target from the Group-or-Callsign field.

        A saved group is prefixed with '@' (matches the '@GROUP' convention
        parsed app-wide); an unrecognized entry is treated as a manually
        typed callsign and sent bare, same as StatRep's '_get_group_target'."""
        text = self.to_combo.currentText().strip()
        if not text:
            return ""
        known_groups = {g.strip().upper() for g in self._get_all_groups_from_db()}
        if text.upper() in known_groups:
            return f"@{text.upper()}"
        return text.upper()

    def _validate_input(self, validate_callsign: bool = True) -> Optional[tuple]:
        rig_name = self.rig_combo.currentText()
        if not rig_name:
            show_error(self, "Please select a Rig")
            self.rig_combo.setFocus()
            return None

        if not self._get_target():
            show_error(self, "Please select a Group or enter a Target Callsign")
            self.to_combo.setFocus()
            return None

        color_value = self.color_combo.currentData()

        title = re.sub(r"[^ -~]+", " ", self.title_field.text()).strip()
        if len(title) < 1:
            show_error(self, "Title is required")
            self.title_field.setFocus()
            return None

        message = self._clean_message(self.message_field.toPlainText().strip()).strip()
        if len(message) < 1:
            show_error(self, "Message is required")
            self.message_field.setFocus()
            return None

        if validate_callsign:
            call = self.callsign.upper()
            if len(call) < MIN_CALLSIGN_LENGTH:
                show_error(self, "Callsign too short (minimum 4 characters)")
                return None
            if len(call) > MAX_CALLSIGN_LENGTH:
                show_error(self, "Callsign too long (maximum 8 characters)")
                return None
            if not CALLSIGN_PATTERN.match(call):
                show_error(self, "Does not meet callsign structure!")
                return None
        else:
            call = self.callsign

        return (call, color_value, title, message)

    def _generate_alert_id(self) -> None:
        self.alert_id = generate_time_based_id()

    def _build_message(self, callsign: str, color: int, title: str, message: str) -> str:
        target = self._get_target()
        marker = "{%%3}" if self.rig_combo.currentText() == INTERNET_RIG else "{%%}"
        return f"{callsign}: {target} ,{self.alert_id},{color},{title},{message},{marker}"

    def _on_internet_accepted(self) -> None:
        """Internet-only send accepted by the server: save the alert and close ."""
        self._save_to_database(
            self._pending_callsign, self._pending_color,
            self._pending_title, self._pending_alert_message, frequency=0,
        )
        self.accept()
        if self.on_alert_saved:
            self.on_alert_saved()

    def _save_to_database(self, callsign: str, color: int, title: str, message: str,
                          frequency: int = 0, db: int = 30) -> None:
        now          = QDateTime.currentDateTime()
        datetime_str = now.toUTC().toString("yyyy-MM-dd HH:mm:ss")
        date_only    = now.toUTC().toString("yyyy-MM-dd")
        target       = self._get_target()
        source       = 3 if self.rig_combo.currentText() == INTERNET_RIG else 1

        with db_connect() as conn:
            conn.execute(
                "INSERT INTO alerts "
                "(datetime, date, freq, db, source, alert_id, from_callsign, target, color, title, message) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (datetime_str, date_only, frequency, db, source, self.alert_id,
                 callsign, target, color, title, message)
            )
            conn.commit()

        if frequency > 0:
            if self.delivery_combo.currentText() != "Limited Reach":
                alert_data = f"{callsign}: {target} ,{self.alert_id},{color},{title},{message},{{%%}}"
                submit_to_commsrvr(self, frequency, callsign, alert_data, datetime_str)

    def _save_only(self) -> None:
        if self.rig_combo.currentText() == INTERNET_RIG:
            callsign, grid, state = get_internet_user_settings()
            if not callsign or not grid or not state:
                show_error(self, 
                    "Cannot transmit — User Settings are not fully configured.\n\n"
                    "Please set your callsign, grid square, and state at:\n"
                    "Settings → User Settings"
                )
                return
            self.callsign = callsign
        elif not self.callsign:
            show_error(self, 
                "Callsign not yet received from the rig.\n\n"
                "Please wait a moment and try again."
            )
            return

        result = self._validate_input(validate_callsign=True)
        if result is None:
            return
        callsign, color, title, message = result
        self._save_to_database(callsign, color, title, message)
        self.accept()
        if self.on_alert_saved:
            self.on_alert_saved()

    def _transmit(self) -> None:
        result = self._validate_input(validate_callsign=False)
        if result is None:
            return

        rig_name = self.rig_combo.currentText()
        callsign, color, title, message = result

        if rig_name == INTERNET_RIG:
            callsign, grid, state = get_internet_user_settings()
            if not callsign or not grid or not state:
                show_error(self, 
                    "Cannot transmit — User Settings are not fully configured.\n\n"
                    "Please set your callsign, grid square, and state at:\n"
                    "Settings → User Settings"
                )
                return
            import netguard
            if not netguard.is_network_enabled():
                show_error(self, 
                    "Cannot transmit — Off-Grid Mode is enabled.\n\n"
                    "Switch to Online mode to transmit via the Internet."
                )
                return
            self.callsign = callsign
            self._pending_callsign = callsign
            self._pending_message  = self._build_message(callsign, color, title, message)
            self._pending_color         = color
            self._pending_title         = title
            self._pending_alert_message = message
            now = QDateTime.currentDateTimeUtc().toString("yyyy-MM-dd HH:mm:ss")
            # Save only when the server accepts it (numeric reply); the alerts
            # table has no global_id column.
            submit_to_commsrvr(self, 
                0, callsign, self._pending_message, now,
                on_complete=lambda global_id: self._on_internet_accepted() if global_id else None,
            )
            return

        client = self._connected_client(rig_name)
        if client is None:
            return

        if not callsign:
            show_error(self, 
                "Callsign not yet received from the rig.\n\n"
                "Please wait a moment and try again."
            )
            return

        self._pending_message       = self._build_message(callsign, color, title, message)
        self._pending_callsign      = callsign
        self._pending_color         = color
        self._pending_title         = title
        self._pending_alert_message = message

        self._begin_rf_transmit(client)

    def _transmit_with_frequency(self, client, frequency: int) -> None:
        """Send over the rig and save; runs once the rig has reported its frequency."""
        try:
            client.send_tx_message(self._pending_message)
            self._save_to_database(
                self._pending_callsign,
                self._pending_color,
                self._pending_title,
                self._pending_alert_message,
                frequency,
            )
            self.accept()
            if self.on_alert_saved:
                self.on_alert_saved()
        except Exception as e:
            show_error(self, f"Failed to transmit alert: {e}")


if __name__ == "__main__":
    from connector_manager import ConnectorManager
    from js8_tcp_client import TCPConnectionPool

    app = QtWidgets.QApplication(sys.argv)
    connector_manager = ConnectorManager()
    tcp_pool = TCPConnectionPool(connector_manager)
    tcp_pool.connect_all()

    dlg = AlertDialog(tcp_pool, connector_manager)
    dlg.exec_()
    sys.exit(app.exec_())
