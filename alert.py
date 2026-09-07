# Copyright (c) 2025 Manuel Ochoa
# This file is part of CommStat.
# Licensed under the GNU General Public License v3.0.
"""
alert.py - Group Alert Dialog

Allows creating and transmitting group callsign alerts via JS8Call.
"""

import base64
import re
import sqlite3
import sys
import threading
import urllib.parse
import urllib.request
from typing import Optional, TYPE_CHECKING

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import QDateTime
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QComboBox,
    QMessageBox, QPlainTextEdit,
)

from constants import (
    DEFAULT_COLORS, COLOR_INPUT_BORDER,
    COLOR_DISABLED_BG, COLOR_DISABLED_TEXT,
    COLOR_BTN_BLUE, COLOR_BTN_CYAN, COLOR_BTN_RED,
)
from id_utils import generate_time_based_id
from little_gucci import create_verified_ssl_context, UpperCaseLineEdit
from ui_helpers import make_button, label_font, apply_standard_dialog_chrome, connect_single

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
DATABASE_FILE       = "traffic.db3"

_COMMSRVR = base64.b64decode("aHR0cHM6Ly9jb21tc3RhdC5hcHA=").decode()
_DATAFEED  = _COMMSRVR + "/datafeed-808585.php"

INTERNET_RIG = "INTERNET ONLY"

_PROG_BG  = DEFAULT_COLORS.get("program_background",   "#A52A2A")
_PROG_FG  = DEFAULT_COLORS.get("program_foreground",   "#FFFFFF")
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

_READONLY_STYLE = (
    f"QLineEdit {{ background-color:{COLOR_DISABLED_BG}; color:{COLOR_DISABLED_TEXT};"
    f" border:1px solid {COLOR_INPUT_BORDER}; border-radius:4px;"
    f" padding:2px 6px; font-family:'Kode Mono'; font-size:13px; }}"
)


# =============================================================================
# Dialog
# =============================================================================

class AlertDialog(QDialog):
    """Group Alert dialog — create and transmit callsign alerts via JS8Call."""

    _commsrvr_result = QtCore.pyqtSignal(str)

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
        self.selected_group: str = ""
        self.alert_id: str       = ""
        self._pending_message: str  = ""
        self._pending_callsign: str = ""

        self._commsrvr_result.connect(self._on_commsrvr_result)

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
            f"QLineEdit {{ background-color:white; color:#333333; border:1px solid #cccccc;"
            f" border-radius:4px; padding:2px 6px; font-family:'Kode Mono'; font-size:13px; }}"
            f"QLineEdit:focus {{ border:1px solid #007bff; }}"
            f"QPlainTextEdit {{ background-color:white; color:#333333; border:1px solid #cccccc;"
            f" border-radius:4px; padding:2px 6px; font-family:'Kode Mono'; font-size:13px; }}"
            f"QPlainTextEdit:focus {{ border:1px solid #007bff; }}"
            f"QComboBox {{ background-color:white; color:#333333; border:1px solid #cccccc;"
            f" border-radius:4px; padding:2px 4px; font-family:'Kode Mono'; font-size:13px;"
            f" combobox-popup:0; }}"
            f"QComboBox:disabled {{ background-color:{COLOR_DISABLED_BG};"
            f" color:{COLOR_DISABLED_TEXT}; }}"
            f"QComboBox QAbstractItemView {{ background-color:white; color:#333333;"
            f" selection-background-color:#cce5ff; selection-color:#000000; }}"
            f"QComboBox QAbstractItemView::item {{ min-height:22px; padding:0 6px; }}"
        )

        body = QVBoxLayout(self)
        body.setContentsMargins(15, 15, 15, 15)
        body.setSpacing(10)

        # ── Title ─────────────────────────────────────────────────────────────
        title_lbl = QLabel("Group Alert / Callsign Alert")
        title_lbl.setAlignment(QtCore.Qt.AlignCenter)
        title_lbl.setFont(QtGui.QFont("Roboto Slab", -1, QtGui.QFont.Black))
        title_lbl.setFixedHeight(36)
        title_lbl.setStyleSheet(
            f"QLabel {{ background-color:{_PROG_BG}; color:{_PROG_FG};"
            f" font-family:'Roboto Slab'; font-size:16px; font-weight:900;"
            f" padding-top:9px; padding-bottom:9px; }}"
        )
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

        self.rig_combo = QComboBox()
        self.rig_combo.setMaxVisibleItems(30)
        self.rig_combo.setItemDelegate(QtWidgets.QStyledItemDelegate(self.rig_combo))
        settings_row.addLayout(_labeled_col("Rig:", self.rig_combo))

        self.mode_combo = QComboBox()
        self.mode_combo.setMaxVisibleItems(30)
        self.mode_combo.setItemDelegate(QtWidgets.QStyledItemDelegate(self.mode_combo))
        self.mode_combo.addItem("Slow",   4)
        self.mode_combo.addItem("Normal", 0)
        self.mode_combo.addItem("Fast",   1)
        self.mode_combo.addItem("Turbo",  2)
        self.mode_combo.addItem("Ultra",  8)
        settings_row.addLayout(_labeled_col("Mode:", self.mode_combo))

        self.freq_field = QLineEdit()
        self.freq_field.setReadOnly(True)
        self.freq_field.setFixedWidth(90)
        settings_row.addLayout(_labeled_col("Freq:", self.freq_field))

        self.delivery_combo = QComboBox()
        self.delivery_combo.setMaxVisibleItems(30)
        self.delivery_combo.setItemDelegate(QtWidgets.QStyledItemDelegate(self.delivery_combo))
        self.delivery_combo.addItem("Maximum Reach")
        self.delivery_combo.addItem("Limited Reach")
        settings_row.addLayout(_labeled_col("Delivery:", self.delivery_combo))

        settings_row.addStretch()
        body.addLayout(settings_row)

        # ── Target ────────────────────────────────────────────────────────────
        target_lbl = QLabel("Group or Callsign:")
        target_lbl.setFont(label_font())
        body.addWidget(target_lbl)

        target_row = QHBoxLayout()
        target_row.setSpacing(8)

        self.to_combo = QComboBox()
        self.to_combo.setMinimumWidth(200)
        self.to_combo.setMaxVisibleItems(30)
        self.to_combo.setEditable(True)
        self.to_combo.setInsertPolicy(QComboBox.NoInsert)
        self.to_combo.setCompleter(None)
        # setLineEdit() must come after setEditable(True) — it replaces the
        # combo's editor, so signals must be wired to the new editor afterward.
        self.to_combo.setLineEdit(UpperCaseLineEdit(self.to_combo))
        self.to_combo.setItemDelegate(QtWidgets.QStyledItemDelegate(self.to_combo))
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

        self.title_field = QLineEdit()
        self.title_field.setMaxLength(MAX_TITLE_LENGTH)
        self.title_field.setPlaceholderText("20 characters max")
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
        self.message_field.setPlaceholderText("195 characters max")
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
        self.cancel_button.clicked.connect(self.close)
        btn_row.addWidget(self.cancel_button)

        body.addLayout(btn_row)

    # =========================================================================
    # Config / DB
    # =========================================================================

    def _load_config(self) -> None:
        self.selected_group = self._get_active_group_from_db()
        all_groups = self._get_all_groups_from_db()
        if len(all_groups) == 1:
            self.to_combo.addItem(all_groups[0])
        else:
            self.to_combo.addItem("")
            for group in all_groups:
                self.to_combo.addItem(group)

    def _load_rigs(self) -> None:
        self.rig_combo.blockSignals(True)
        self.rig_combo.clear()

        enabled_connectors = self.connector_manager.get_all_connectors(enabled_only=True) if self.connector_manager else []
        connected_rigs     = self.tcp_pool.get_connected_rig_names() if self.tcp_pool else []
        available          = [c for c in enabled_connectors if c['rig_name'] in connected_rigs]

        internet_available = bool(self.parent() and getattr(self.parent(), '_internet_available', False))

        if not available:
            if internet_available:
                self.rig_combo.addItem(INTERNET_RIG)
        else:
            self.rig_combo.addItem("")
            for c in available:
                self.rig_combo.addItem(c['rig_name'])
            if internet_available:
                self.rig_combo.addItem(INTERNET_RIG)

        self.rig_combo.blockSignals(False)

        current = self.rig_combo.currentText()
        if current:
            self._on_rig_changed(current)

    def closeEvent(self, event) -> None:
        if self.tcp_pool:
            for rig_name in self.tcp_pool.get_all_rig_names():
                client = self.tcp_pool.get_client(rig_name)
                if client:
                    for sig, slot in [
                        (client.callsign_received, self._on_callsign_received),
                        (client.frequency_received, self._on_frequency_for_transmit),
                    ]:
                        try:
                            sig.disconnect(slot)
                        except (TypeError, RuntimeError):
                            pass
        super().closeEvent(event)

    def _on_rig_changed(self, rig_name: str) -> None:
        if not rig_name or "(disconnected)" in rig_name:
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
            self.callsign = self._get_internet_callsign()
            self.freq_field.setText("")
            self.mode_combo.setEnabled(False)
            return

        self.mode_combo.setEnabled(True)

        if not self.tcp_pool:
            return

        for cn in self.tcp_pool.get_all_rig_names():
            c = self.tcp_pool.get_client(cn)
            if c:
                try:
                    c.callsign_received.disconnect(self._on_callsign_received)
                except (TypeError, RuntimeError):
                    pass

        client = self.tcp_pool.get_client(rig_name)
        if client and client.is_connected():
            speed_name = (client.speed_name or "").upper()
            mode_map = {"SLOW": 0, "NORMAL": 1, "FAST": 2, "TURBO": 3, "ULTRA": 4}
            idx = mode_map.get(speed_name, 1)
            self.mode_combo.blockSignals(True)
            self.mode_combo.setCurrentIndex(idx)
            self.mode_combo.blockSignals(False)

            frequency = client.frequency
            self.freq_field.setText(f"{frequency:.3f}" if frequency else "")

            client.callsign_received.connect(self._on_callsign_received)
            client.get_callsign()
        else:
            self.freq_field.setText("")

    def _on_callsign_received(self, rig_name: str, callsign: str) -> None:
        if self.rig_combo.currentText() == rig_name:
            self.callsign = callsign

    def _get_internet_callsign(self) -> str:
        try:
            with sqlite3.connect(DATABASE_FILE, timeout=10) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT callsign FROM controls WHERE id = 1")
                row = cursor.fetchone()
                return (row[0] or "").strip().upper() if row else ""
        except sqlite3.Error:
            return ""

    def _get_internet_user_settings(self) -> tuple:
        """Return (callsign, gridsquare, state) from User Settings."""
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

    def _enforce_message_limit(self) -> None:
        text = self.message_field.toPlainText()
        if len(text) > MAX_MESSAGE_LENGTH:
            cursor = self.message_field.textCursor()
            pos = min(cursor.position(), MAX_MESSAGE_LENGTH)
            self.message_field.blockSignals(True)
            self.message_field.setPlainText(text[:MAX_MESSAGE_LENGTH])
            cursor.setPosition(pos)
            self.message_field.setTextCursor(cursor)
            self.message_field.blockSignals(False)
            text = text[:MAX_MESSAGE_LENGTH]
        self._update_message_count_label(len(text))

    def _update_message_count_label(self, count: Optional[int] = None) -> None:
        """Refresh the 'N of MAX' counter next to the Message label."""
        if not hasattr(self, 'message_count_label'):
            return
        if count is None:
            count = len(self.message_field.toPlainText())
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

    def _show_error(self, message: str) -> None:
        msg = QMessageBox(self)
        msg.setWindowTitle("CommStat Error")
        msg.setText(message)
        msg.setIcon(QMessageBox.Critical)
        msg.setWindowFlag(QtCore.Qt.WindowStaysOnTopHint)
        msg.exec_()

    def _show_info(self, message: str) -> None:
        msg = QMessageBox(self)
        msg.setWindowTitle("CommStat TX")
        msg.setText(message)
        msg.setIcon(QMessageBox.Information)
        msg.setWindowFlag(QtCore.Qt.WindowStaysOnTopHint)
        msg.exec_()

    def _validate_input(self, validate_callsign: bool = True) -> Optional[tuple]:
        rig_name = self.rig_combo.currentText()
        if not rig_name:
            self._show_error("Please select a Rig")
            self.rig_combo.setFocus()
            return None

        if not self._get_target():
            self._show_error("Please select a Group or enter a Target Callsign")
            self.to_combo.setFocus()
            return None

        color_value = self.color_combo.currentData()

        title = re.sub(r"[^ -~]+", " ", self.title_field.text()).strip()
        if len(title) < 1:
            self._show_error("Title is required")
            self.title_field.setFocus()
            return None

        message = re.sub(r"[^ -~]+", " ", self.message_field.toPlainText()).strip()
        if len(message) < 1:
            self._show_error("Message is required")
            self.message_field.setFocus()
            return None

        if validate_callsign:
            call = self.callsign.upper()
            if len(call) < MIN_CALLSIGN_LENGTH:
                self._show_error("Callsign too short (minimum 4 characters)")
                return None
            if len(call) > MAX_CALLSIGN_LENGTH:
                self._show_error("Callsign too long (maximum 8 characters)")
                return None
            if not CALLSIGN_PATTERN.match(call):
                self._show_error("Does not meet callsign structure!")
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

    def _submit_to_commsrvr_async(self, frequency: int, callsign: str, alert_data: str, now: str) -> None:
        def submit_thread():
            import netguard
            if not netguard.guard("Alert internet submission"):
                self._commsrvr_result.emit("ERR::Off-Grid Mode is enabled — switch back to ONLINE to send.")
                return
            try:
                data_string = f"{now}\t{frequency}\t0\t30\t{alert_data}"
                post_data = urllib.parse.urlencode({
                    'cs': callsign, 'data': data_string
                }).encode('utf-8')
                req = urllib.request.Request(_DATAFEED, data=post_data, method='POST')
                with urllib.request.urlopen(req, timeout=5, context=create_verified_ssl_context()) as response:
                    result = response.read().decode('utf-8').strip()
                if result.isdigit():
                    print(f"[Commsrvr] Alert submitted successfully (global_id={result})")
                else:
                    print(f"[Commsrvr] Alert submission failed — server returned: {result}")
                self._commsrvr_result.emit(result)
            except Exception as e:
                reason = getattr(e, 'reason', e)
                if isinstance(reason, TimeoutError):
                    err = "ERR::Server timeout — the server did not respond in time."
                else:
                    err = f"ERR::Connection error — {e}"
                print(f"[Commsrvr] Alert submission failed — {err[5:]}")
                self._commsrvr_result.emit(err)

        threading.Thread(target=submit_thread, daemon=True).start()

    def _on_commsrvr_result(self, result: str) -> None:
        if result.startswith("ERR::"):
            from qrz_lookup import InternetDeliveryFailureDialog
            parent = self if self.isVisible() else (self.parent() or self)
            InternetDeliveryFailureDialog(result[5:], parent=parent).exec_()
        elif result.isdigit():
            if self.isVisible():
                self._save_to_database(
                    self._pending_callsign, self._pending_color,
                    self._pending_title, self._pending_alert_message, frequency=0,
                )
                self.close()
                if self.on_alert_saved:
                    self.on_alert_saved()

    def _save_to_database(self, callsign: str, color: int, title: str, message: str,
                          frequency: int = 0, db: int = 30) -> None:
        now          = QDateTime.currentDateTime()
        datetime_str = now.toUTC().toString("yyyy-MM-dd HH:mm:ss")
        date_only    = now.toUTC().toString("yyyy-MM-dd")
        target       = self._get_target()
        source       = 3 if self.rig_combo.currentText() == INTERNET_RIG else 1

        with sqlite3.connect(DATABASE_FILE, timeout=10) as conn:
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
                self._submit_to_commsrvr_async(frequency, callsign, alert_data, datetime_str)

    def _save_only(self) -> None:
        if self.rig_combo.currentText() == INTERNET_RIG:
            callsign, grid, state = self._get_internet_user_settings()
            if not callsign or not grid or not state:
                self._show_error(
                    "Cannot transmit — User Settings are not fully configured.\n\n"
                    "Please set your callsign, grid square, and state at:\n"
                    "Menu → Config → User Settings"
                )
                return
            self.callsign = callsign
        elif not self.callsign:
            self._show_error(
                "Callsign not yet received from the rig.\n\n"
                "Please wait a moment and try again."
            )
            return

        result = self._validate_input(validate_callsign=True)
        if result is None:
            return
        callsign, color, title, message = result
        self._save_to_database(callsign, color, title, message)
        self.close()
        if self.on_alert_saved:
            self.on_alert_saved()

    def _transmit(self) -> None:
        result = self._validate_input(validate_callsign=False)
        if result is None:
            return

        rig_name = self.rig_combo.currentText()
        callsign, color, title, message = result

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
            self._pending_callsign = callsign
            self._pending_message  = self._build_message(callsign, color, title, message)
            self._pending_color         = color
            self._pending_title         = title
            self._pending_alert_message = message
            now = QDateTime.currentDateTimeUtc().toString("yyyy-MM-dd HH:mm:ss")
            self._submit_to_commsrvr_async(0, callsign, self._pending_message, now)
            return

        if "(disconnected)" in rig_name:
            self._show_error("Cannot transmit: rig is disconnected")
            return

        if not self.tcp_pool:
            self._show_error("Cannot transmit: TCP pool not available")
            return

        client = self.tcp_pool.get_client(rig_name)
        if not client or not client.is_connected():
            self._show_error("Cannot transmit: not connected to rig")
            return

        if not callsign:
            self._show_error(
                "Callsign not yet received from the rig.\n\n"
                "Please wait a moment and try again."
            )
            return

        self._pending_message       = self._build_message(callsign, color, title, message)
        self._pending_callsign      = callsign
        self._pending_color         = color
        self._pending_title         = title
        self._pending_alert_message = message

        try:
            client.call_selected_received.disconnect(self._on_call_selected_for_transmit)
        except TypeError:
            pass
        client.call_selected_received.connect(self._on_call_selected_for_transmit)
        client.get_call_selected()

    def _on_call_selected_for_transmit(self, rig_name: str, selected_call: str) -> None:
        if self.rig_combo.currentText() != rig_name:
            return

        client = self.tcp_pool.get_client(rig_name)
        if client:
            try:
                client.call_selected_received.disconnect(self._on_call_selected_for_transmit)
            except TypeError:
                pass

        if selected_call:
            QMessageBox.critical(
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
            self._save_to_database(
                self._pending_callsign,
                self._pending_color,
                self._pending_title,
                self._pending_alert_message,
                frequency,
            )
            self.close()
            if self.on_alert_saved:
                self.on_alert_saved()
        except Exception as e:
            self._show_error(f"Failed to transmit alert: {e}")


if __name__ == "__main__":
    from connector_manager import ConnectorManager
    from js8_tcp_client import TCPConnectionPool

    app = QtWidgets.QApplication(sys.argv)
    connector_manager = ConnectorManager()
    connector_manager.init_connectors_table()
    tcp_pool = TCPConnectionPool(connector_manager)
    tcp_pool.connect_all()

    dlg = AlertDialog(tcp_pool, connector_manager)
    dlg.exec_()
    sys.exit(app.exec_())
