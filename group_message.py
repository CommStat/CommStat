# Copyright (c) 2025 Manuel Ochoa
# This file is part of CommStat.
# Licensed under the GNU General Public License v3.0.
# AI Assistance: Claude (Anthropic), ChatGPT (OpenAI)

"""
Group Message Dialog for CommStat
Allows creating and transmitting group messages via JS8Call.
"""

import base64
import re
import sqlite3
import threading
import urllib.parse
import urllib.request
from typing import Optional, TYPE_CHECKING

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import Qt, QDateTime
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QComboBox, QPlainTextEdit,
    QMessageBox,
)

from constants import (
    DEFAULT_COLORS, COLOR_INPUT_TEXT, COLOR_INPUT_BORDER,
    COLOR_DISABLED_BG, COLOR_DISABLED_TEXT,
    COLOR_BTN_CYAN, COLOR_BTN_BLUE, COLOR_BTN_RED,
    RIG_FETCH_DELAY_MS,
)
from id_utils import generate_time_based_id
from little_gucci import create_verified_ssl_context
from ui_helpers import make_button, apply_standard_dialog_chrome, connect_single

if TYPE_CHECKING:
    from js8_tcp_client import TCPConnectionPool
    from connector_manager import ConnectorManager


# =============================================================================
# Constants
# =============================================================================

MIN_MESSAGE_LENGTH   = 4
MAX_MESSAGE_LENGTH   = 1500
MAX_MESSAGE_LENGTH_INTERNET = 1500
DATABASE_FILE = "traffic.db3"

_COMMSRVR = base64.b64decode("aHR0cHM6Ly9jb21tc3RhdC5hcHA=").decode()
_DATAFEED  = _COMMSRVR + "/datafeed-808585.php"

INTERNET_RIG = "INTERNET ONLY"

_PROG_BG  = DEFAULT_COLORS.get("program_background",   "#A52A2A")
_PROG_FG  = DEFAULT_COLORS.get("program_foreground",   "#FFFFFF")
_PANEL_BG = DEFAULT_COLORS.get("module_background",    "#DDDDDD")
_PANEL_FG = DEFAULT_COLORS.get("module_foreground",    "#000000")
_DATA_BG  = DEFAULT_COLORS.get("data_background",      "#F8F6F4")
_DATA_FG  = DEFAULT_COLORS.get("data_foreground",      "#000000")

_COL_CANCEL = "#555555"
_COL_COUNTER = "#444444"  # muted but legible counter text (COLOR_DISABLED_TEXT is too light here)

_WIN_W          = 640
_WIN_H_RF       = 420
_WIN_H_INTERNET = 420


# =============================================================================
# Helpers
# =============================================================================

def _labeled_col(lbl_text: str, ctrl: QtWidgets.QWidget) -> QHBoxLayout:
    col = QVBoxLayout()
    col.setSpacing(2)
    lbl = QLabel(lbl_text)
    lbl.setStyleSheet("QLabel { font-family:Roboto; font-size:13px; font-weight:bold; }")
    col.addWidget(lbl)
    col.addWidget(ctrl)
    return col


# =============================================================================
# Dialog
# =============================================================================

class GroupMessageDialog(QDialog):
    """Group Message dialog — compose and transmit a group message."""

    _commsrvr_result = QtCore.pyqtSignal(str)

    def __init__(
        self,
        tcp_pool: "TCPConnectionPool" = None,
        connector_manager: "ConnectorManager" = None,
        refresh_callback=None,
        parent=None,
    ):
        super().__init__(parent)
        self.tcp_pool            = tcp_pool
        self.connector_manager   = connector_manager
        self.refresh_callback    = refresh_callback
        self.callsign: str       = ""
        self.selected_group: str = ""
        self.msg_id: str         = ""
        self._pending_message: str   = ""
        self._pending_save_data: Optional[dict] = None
        self._message_is_expanded: bool = False

        self._commsrvr_result.connect(self._on_commsrvr_result)

        apply_standard_dialog_chrome(self, "Group Message", _WIN_W, _WIN_H_RF)

        self.setStyleSheet(
            f"QDialog {{ background-color:{_PANEL_BG}; }}"
            f"QLabel {{ color:{_PANEL_FG}; font-family:Roboto; font-size:13px; }}"
            f"QLineEdit {{ background-color:white; color:{COLOR_INPUT_TEXT};"
            f" border:1px solid {COLOR_INPUT_BORDER}; border-radius:4px; padding:2px 4px;"
            f" font-family:'Kode Mono'; font-size:13px; }}"
            f"QComboBox {{ background-color:white; color:{COLOR_INPUT_TEXT};"
            f" border:1px solid {COLOR_INPUT_BORDER}; border-radius:4px; padding:2px 4px;"
            f" font-family:'Kode Mono'; font-size:13px; combobox-popup:0; }}"
            f"QComboBox:disabled {{ background-color:{COLOR_DISABLED_BG}; color:{COLOR_DISABLED_TEXT}; }}"
            f"QComboBox QAbstractItemView {{ background-color:white; color:{COLOR_INPUT_TEXT};"
            f" selection-background-color:#cce5ff; selection-color:#000000; }}"
            f"QComboBox QAbstractItemView::item {{ min-height:22px; padding:0 6px; }}"
            f"QPlainTextEdit {{ background-color:white; color:{COLOR_INPUT_TEXT};"
            f" border:1px solid {COLOR_INPUT_BORDER}; border-radius:4px; padding:4px;"
            f" font-family:'Kode Mono'; font-size:13px; }}"
        )

        self._setup_ui()
        self._generate_msg_id()
        self._load_config()
        self._load_rigs()

    # -------------------------------------------------------------------------
    # UI construction
    # -------------------------------------------------------------------------

    def _setup_ui(self) -> None:
        body = QVBoxLayout(self)
        body.setContentsMargins(15, 15, 15, 15)
        body.setSpacing(10)

        # Title
        title_lbl = QLabel("Group Message")
        title_lbl.setAlignment(Qt.AlignCenter)
        title_lbl.setFont(QtGui.QFont("Roboto Slab", -1, QtGui.QFont.Black))
        title_lbl.setFixedHeight(36)
        title_lbl.setStyleSheet(
            f"QLabel {{ background-color:{_PROG_BG}; color:{_PROG_FG};"
            f" font-family:'Roboto Slab'; font-size:16px; font-weight:900;"
            f" padding-top:9px; padding-bottom:9px; }}"
        )
        body.addWidget(title_lbl)

        # Settings row: Rig | Mode | Freq | Delivery
        settings_row = QHBoxLayout()
        settings_row.setSpacing(8)

        self.rig_combo = QComboBox()
        self.rig_combo.setFixedWidth(150)
        self.rig_combo.setMaxVisibleItems(30)
        self.rig_combo.setItemDelegate(QtWidgets.QStyledItemDelegate(self.rig_combo))
        settings_row.addLayout(_labeled_col("Rig:", self.rig_combo))

        self.mode_combo = QComboBox()
        self.mode_combo.setFixedWidth(100)
        self.mode_combo.setMaxVisibleItems(30)
        self.mode_combo.setItemDelegate(QtWidgets.QStyledItemDelegate(self.mode_combo))
        self.mode_combo.addItem("Slow",   4)
        self.mode_combo.addItem("Normal", 0)
        self.mode_combo.addItem("Fast",   1)
        self.mode_combo.addItem("Turbo",  2)
        self.mode_combo.addItem("Ultra",  8)
        settings_row.addLayout(_labeled_col("Mode:", self.mode_combo))

        self.freq_field = QLineEdit()
        self.freq_field.setFixedWidth(90)
        self.freq_field.setReadOnly(True)
        self.freq_field.setStyleSheet(
            "QLineEdit { background-color:white; color:#333333;"
            " border:1px solid #cccccc; border-radius:4px; padding:2px 4px;"
            " font-family:'Kode Mono'; font-size:13px; }"
        )
        settings_row.addLayout(_labeled_col("Freq:", self.freq_field))

        self.delivery_combo = QComboBox()
        self.delivery_combo.setFixedWidth(160)
        self.delivery_combo.setMaxVisibleItems(30)
        self.delivery_combo.setItemDelegate(QtWidgets.QStyledItemDelegate(self.delivery_combo))
        self.delivery_combo.addItem("Maximum Reach")
        self.delivery_combo.addItem("Limited Reach")
        settings_row.addLayout(_labeled_col("Delivery:", self.delivery_combo))

        settings_row.addStretch()
        body.addLayout(settings_row)

        # Group row
        group_row = QHBoxLayout()
        group_row.setSpacing(8)
        self.group_combo = QComboBox()
        self.group_combo.setFixedWidth(180)
        self.group_combo.setMaxVisibleItems(30)
        self.group_combo.setItemDelegate(QtWidgets.QStyledItemDelegate(self.group_combo))
        group_row.addLayout(_labeled_col("Group:", self.group_combo))
        group_row.addStretch()
        body.addLayout(group_row)

        # Message label + inputs
        msg_row = QHBoxLayout()
        msg_lbl = QLabel("Message:")
        msg_lbl.setStyleSheet(
            "QLabel { font-family:Roboto; font-size:13px; font-weight:bold; }"
        )
        msg_row.addWidget(msg_lbl)
        msg_row.addStretch()
        self.message_count_label = QLabel()
        self.message_count_label.setStyleSheet(
            "QLabel { font-family:'Kode Mono'; font-size:13px; }"
        )
        msg_row.addWidget(self.message_count_label)
        body.addLayout(msg_row)

        self.message_expanded = QPlainTextEdit()
        self.message_expanded.setMinimumHeight(160)
        self.message_expanded.setPlaceholderText("1500 characters max")
        self.message_expanded.textChanged.connect(self._enforce_message_limit)
        body.addWidget(self.message_expanded)
        self._update_message_count_label()

        body.addStretch()

        # Button row
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch()

        self.pushButton_3 = make_button("Save Only", COLOR_BTN_CYAN)
        self.pushButton_3.clicked.connect(self._save_only)
        btn_row.addWidget(self.pushButton_3)

        self.pushButton = make_button("Transmit", COLOR_BTN_BLUE)
        connect_single(self.pushButton, self._transmit)
        btn_row.addWidget(self.pushButton)

        self.pushButton_2 = make_button("Cancel", _COL_CANCEL)
        self.pushButton_2.clicked.connect(self.reject)
        btn_row.addWidget(self.pushButton_2)

        body.addLayout(btn_row)

        # Signals
        self.rig_combo.currentTextChanged.connect(self._on_rig_changed)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)

    # -------------------------------------------------------------------------
    # Data / config loading
    # -------------------------------------------------------------------------

    def _load_config(self) -> None:
        self.selected_group = self._get_active_group_from_db()
        all_groups = self._get_all_groups_from_db()
        if len(all_groups) == 1:
            self.group_combo.addItem(all_groups[0])
        else:
            self.group_combo.addItem("")
            for group in all_groups:
                self.group_combo.addItem(group)

    def _load_rigs(self) -> None:
        self.rig_combo.blockSignals(True)
        self.rig_combo.clear()

        enabled = self.connector_manager.get_all_connectors(enabled_only=True) if self.connector_manager else []
        connected = self.tcp_pool.get_connected_rig_names() if self.tcp_pool else []
        available = [c for c in enabled if c['rig_name'] in connected]

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
                        (client.frequency_received, self._on_frequency_received),
                        (client.frequency_received, self._on_frequency_for_transmit),
                        (client.call_selected_received, self._on_call_selected_for_transmit),
                    ]:
                        try:
                            sig.disconnect(slot)
                        except (TypeError, RuntimeError):
                            pass
        super().closeEvent(event)

    # -------------------------------------------------------------------------
    # Signal handlers
    # -------------------------------------------------------------------------

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

        self._swap_message_widget(is_internet)

        if is_internet:
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
                for sig, slot in [
                    (c.callsign_received, self._on_callsign_received),
                    (c.frequency_received, self._on_frequency_received),
                ]:
                    try:
                        sig.disconnect(slot)
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
            client.frequency_received.connect(self._on_frequency_received)
            client.get_callsign()
            QtCore.QTimer.singleShot(RIG_FETCH_DELAY_MS, client.get_frequency)
        else:
            self.freq_field.setText("")

    def _on_callsign_received(self, rig_name: str, callsign: str) -> None:
        if self.rig_combo.currentText() == rig_name:
            self.callsign = callsign

    def _on_frequency_received(self, rig_name: str, dial_freq: int) -> None:
        if self.rig_combo.currentText() == rig_name:
            self.freq_field.setText(f"{dial_freq / 1_000_000:.3f}")

    def _swap_message_widget(self, internet_only: bool) -> None:
        self._message_is_expanded = internet_only
        self._enforce_message_limit()

    def _enforce_message_limit(self) -> None:
        limit = MAX_MESSAGE_LENGTH_INTERNET if self._message_is_expanded else MAX_MESSAGE_LENGTH
        text = self.message_expanded.toPlainText()
        if len(text) > limit:
            cursor = self.message_expanded.textCursor()
            pos = min(cursor.position(), limit)
            self.message_expanded.blockSignals(True)
            self.message_expanded.setPlainText(text[:limit])
            cursor.setPosition(pos)
            self.message_expanded.setTextCursor(cursor)
            self.message_expanded.blockSignals(False)
            text = text[:limit]
        self._update_message_count_label(len(text), limit)

    def _update_message_count_label(self, count: Optional[int] = None, limit: Optional[int] = None) -> None:
        """Refresh the 'N of MAX' counter next to the Message label."""
        if not hasattr(self, 'message_count_label'):
            return
        if limit is None:
            limit = MAX_MESSAGE_LENGTH_INTERNET if self._message_is_expanded else MAX_MESSAGE_LENGTH
        if count is None:
            count = len(self.message_expanded.toPlainText())
        self.message_count_label.setText(f"{count} of {limit}")
        color = COLOR_BTN_RED if count >= limit else _COL_COUNTER
        self.message_count_label.setStyleSheet(
            f"QLabel {{ font-family:'Kode Mono'; font-size:13px; color:{color}; }}"
        )

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

    # -------------------------------------------------------------------------
    # Database helpers
    # -------------------------------------------------------------------------

    def _get_active_group_from_db(self) -> str:
        try:
            with sqlite3.connect(DATABASE_FILE, timeout=10) as conn:
                row = conn.execute("SELECT name FROM groups ORDER BY name LIMIT 1").fetchone()
                return row[0] if row else ""
        except sqlite3.Error as e:
            print(f"Error reading group from database: {e}")
        return ""

    def _get_all_groups_from_db(self) -> list:
        try:
            with sqlite3.connect(DATABASE_FILE, timeout=10) as conn:
                return [r[0] for r in conn.execute("SELECT name FROM groups ORDER BY name").fetchall()]
        except sqlite3.Error as e:
            print(f"Error reading groups from database: {e}")
        return []

    def _get_internet_callsign(self) -> str:
        try:
            with sqlite3.connect(DATABASE_FILE, timeout=10) as conn:
                row = conn.execute("SELECT callsign FROM controls WHERE id = 1").fetchone()
                return (row[0] or "").strip().upper() if row else ""
        except sqlite3.Error:
            return ""

    # -------------------------------------------------------------------------
    # Validation / messaging helpers
    # -------------------------------------------------------------------------

    def _show_error(self, message: str) -> None:
        msg = QMessageBox(self)
        msg.setWindowTitle("CommStat Error")
        msg.setText(message)
        msg.setIcon(QMessageBox.Critical)
        msg.setWindowFlag(Qt.WindowStaysOnTopHint)
        msg.exec_()

    def _show_info(self, message: str) -> None:
        msg = QMessageBox(self)
        msg.setWindowTitle("CommStat TX")
        msg.setText(message)
        msg.setIcon(QMessageBox.Information)
        msg.setWindowFlag(Qt.WindowStaysOnTopHint)
        msg.exec_()

    def _validate_input(self) -> Optional[tuple]:
        rig_name = self.rig_combo.currentText()
        if not rig_name:
            self._show_error("Please select a Rig")
            self.rig_combo.setFocus()
            return None

        group_name = self.group_combo.currentText()
        if not group_name:
            self._show_error("Please select a Group")
            self.group_combo.setFocus()
            return None

        message_raw = self.message_expanded.toPlainText()
        message = re.sub(r"[^ -~]+", " ", message_raw)

        if len(message) < MIN_MESSAGE_LENGTH:
            self._show_error("Message too short")
            return None

        return (self.callsign.upper(), message)

    def _build_message(self, message: str) -> str:
        group  = "@" + self.group_combo.currentText()
        marker = "{^%3}" if self.rig_combo.currentText() == INTERNET_RIG else "{^%}"
        return f"{group} MSG ,{self.msg_id},{message},{marker}"

    # -------------------------------------------------------------------------
    # Commsrvr / database
    # -------------------------------------------------------------------------

    def _submit_to_commsrvr_async(self, frequency: int, callsign: str, message_data: str, now: str, on_complete=None, dm_prefix: bool = False) -> None:
        """Start background thread to submit the message to commsrvr.

        Args:
            on_complete: Optional callable(global_id: int) invoked after the
                request completes (success or failure). global_id is 0 on
                failure or when the server returns a non-numeric response.
            dm_prefix: Prepend "DM:" to the submitted data string, marking
                it as an internet-only submission rather than an RF capture.
        """
        def submit_thread():
            import netguard
            global_id = 0
            if not netguard.guard("Message internet submission"):
                self._commsrvr_result.emit("ERR::Off-Grid Mode is enabled — switch back to ONLINE to send.")
                if on_complete:
                    on_complete(global_id)
                return
            try:
                prefix = "DM:" if dm_prefix else ""
                data_string = f"{prefix}{now}\t{frequency}\t0\t30\t{message_data}"
                post_data = urllib.parse.urlencode({'cs': callsign, 'data': data_string}).encode('utf-8')
                print(f"[Commsrvr] POST data: {post_data}")
                req = urllib.request.Request(_DATAFEED, data=post_data, method='POST')
                with urllib.request.urlopen(req, timeout=5, context=create_verified_ssl_context()) as response:
                    result = response.read().decode('utf-8').strip()
                if result.isdigit():
                    global_id = int(result)
                    print(f"[Commsrvr] Message submitted successfully (global_id={global_id})")
                else:
                    print(f"[Commsrvr] Message submission failed — server returned: {result}")
                    self._commsrvr_result.emit(result)
            except Exception as e:
                reason = getattr(e, 'reason', e)
                if isinstance(reason, TimeoutError):
                    err = "ERR::Server timeout — the server did not respond in time."
                else:
                    err = f"ERR::Connection error — {e}"
                print(f"[Commsrvr] Message submission failed — {err[5:]}")
                self._commsrvr_result.emit(err)
            finally:
                if on_complete:
                    on_complete(global_id)

        threading.Thread(target=submit_thread, daemon=True).start()

    def _on_commsrvr_result(self, result: str) -> None:
        if result.startswith("ERR::"):
            from qrz_lookup import InternetDeliveryFailureDialog
            parent = self if self.isVisible() else (self.parent() or self)
            InternetDeliveryFailureDialog(result[5:], parent=parent).exec_()

    def _capture_save_data(self, callsign: str, message: str, frequency: int = 0) -> dict:
        """Snapshot Qt widget state on the main thread before a background submit."""
        now = QDateTime.currentDateTime()
        return {
            'callsign': callsign,
            'message': message,
            'frequency': frequency,
            'datetime_str': now.toUTC().toString("yyyy-MM-dd HH:mm:ss"),
            'date_only': now.toUTC().toString("yyyy-MM-dd"),
            'source': 3 if self.rig_combo.currentText() == INTERNET_RIG else 1,
            'target': "@" + self.group_combo.currentText(),
            'msg_id': self.msg_id,
        }

    def _save_to_database(self, saved_data: dict, global_id: int = 0) -> None:
        """Save the message to the database.

        Args:
            saved_data: Snapshot from _capture_save_data().
            global_id: The global ID returned by the commsrvr server (0 if unknown).
        """
        with sqlite3.connect(DATABASE_FILE, timeout=10) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO messages "
                "(global_id, datetime, date, freq, db, source, msg_id, from_callsign, target, message) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (global_id, saved_data['datetime_str'], saved_data['date_only'], saved_data['frequency'], 30,
                 saved_data['source'], saved_data['msg_id'], saved_data['callsign'],
                 saved_data['target'], saved_data['message'])
            )
            conn.commit()

    def _refresh_and_close(self) -> None:
        if self.refresh_callback:
            self.refresh_callback()
        self.accept()

    # -------------------------------------------------------------------------
    # Actions
    # -------------------------------------------------------------------------

    def _save_only(self) -> None:
        rig_name = self.rig_combo.currentText()
        if rig_name == INTERNET_RIG:
            self.callsign = self._get_internet_callsign()
            if not self.callsign:
                self._show_error(
                    "No callsign configured.\n\nPlease set your callsign in Settings → User Settings."
                )
                return
        elif not self.callsign:
            self._show_error(
                "Callsign not yet received from the rig.\n\nPlease wait a moment and try again."
            )
            return

        result = self._validate_input()
        if result is None:
            return

        callsign, message = result
        tx_message = self._build_message(message)
        self._show_info(f"CommStat has saved:\n{tx_message}")
        self._save_to_database(self._capture_save_data(callsign, message))

        if self.refresh_callback:
            self.refresh_callback()

        self.accept()

    def _transmit(self) -> None:
        rig_name = self.rig_combo.currentText()

        if rig_name == INTERNET_RIG:
            self.callsign = self._get_internet_callsign()
            if not self.callsign:
                self._show_error(
                    "No callsign configured.\n\nPlease set your callsign in Settings → User Settings."
                )
                return

        result = self._validate_input()
        if result is None:
            return

        callsign, message = result

        if rig_name == INTERNET_RIG:
            self._pending_message  = self._build_message(message)
            self._pending_save_data = self._capture_save_data(callsign, message, 0)
            now = QDateTime.currentDateTimeUtc().toString("yyyy-MM-dd HH:mm:ss")
            message_data = (
                f"{callsign}: @{self.group_combo.currentText()}"
                f" MSG ,{self.msg_id},{message},{{^%3}}"
            )

            def _on_internet_commsrvr_complete(global_id: int) -> None:
                self._save_to_database(self._pending_save_data, global_id)
                QtCore.QTimer.singleShot(0, self._refresh_and_close)

            self._submit_to_commsrvr_async(0, callsign, message_data, now, on_complete=_on_internet_commsrvr_complete, dm_prefix=True)
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
                "Callsign not yet received from the rig.\n\nPlease wait a moment and try again."
            )
            return

        self._pending_message = self._build_message(message)

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

            message_raw = self.message_expanded.toPlainText()
            message = re.sub(r"[^ -~]+", " ", message_raw)

            self._pending_save_data = self._capture_save_data(self.callsign, message, frequency)

            if self.delivery_combo.currentText() == "Limited Reach":
                # No commsrvr submission — save immediately with no global_id
                self._save_to_database(self._pending_save_data)
                self._refresh_and_close()
            else:
                group = "@" + self.group_combo.currentText()
                message_data = f"{self.callsign}: {group} MSG ,{self.msg_id},{message},{{^%}}"

                def _on_radio_commsrvr_complete(global_id: int) -> None:
                    self._save_to_database(self._pending_save_data, global_id)
                    QtCore.QTimer.singleShot(0, self._refresh_and_close)

                self._submit_to_commsrvr_async(frequency, self.callsign, message_data, self._pending_save_data['datetime_str'], on_complete=_on_radio_commsrvr_complete)
        except Exception as e:
            self._show_error(f"Failed to transmit message: {e}")

    def _generate_msg_id(self) -> None:
        self.msg_id = generate_time_based_id()


# Legacy alias
Ui_FormMessage = GroupMessageDialog
