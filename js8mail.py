# Copyright (c) 2025, 2026 Manuel Ochoa
# This file is part of CommStat.
# Licensed under the GNU General Public License v3.0.
# AI Assistance: Claude (Anthropic), ChatGPT (OpenAI)

"""
JS8 Email Dialog for CommStat
Allows sending emails via JS8Call APRS gateway.
"""

import re
from typing import TYPE_CHECKING

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import QDateTime, Qt
from PyQt5.QtWidgets import QMessageBox, QDialog

from constants import (
    DEFAULT_COLORS,
    COLOR_INPUT_TEXT, COLOR_INPUT_BORDER,
    COLOR_DISABLED_BG, COLOR_DISABLED_TEXT,
    COLOR_BTN_BLUE, COLOR_BTN_RED,
)
from ui_helpers import make_button, apply_standard_dialog_chrome, connect_single

if TYPE_CHECKING:
    from js8_tcp_client import TCPConnectionPool
    from connector_manager import ConnectorManager


# =============================================================================
# Constants
# =============================================================================

MIN_EMAIL_LENGTH = 8
MIN_SUBJECT_LENGTH = 8
MAX_SUBJECT_LENGTH = 67
EMAIL_PATTERN = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"

WINDOW_WIDTH = 560
WINDOW_HEIGHT = 335

_PROG_BG  = DEFAULT_COLORS.get("program_background",   "#A52A2A")
_PROG_FG  = DEFAULT_COLORS.get("program_foreground",   "#FFFFFF")
_PANEL_BG = DEFAULT_COLORS.get("module_background",    "#DDDDDD")
_PANEL_FG = DEFAULT_COLORS.get("module_foreground",    "#000000")
_COL_COUNTER = "#444444"  # muted but legible counter text (COLOR_DISABLED_TEXT is too light here)


# =============================================================================
# JS8Mail Dialog
# =============================================================================

class JS8MailDialog(QDialog):
    """JS8 Email form for sending emails via APRS gateway."""

    def __init__(
        self,
        tcp_pool: "TCPConnectionPool" = None,
        connector_manager: "ConnectorManager" = None,
        parent=None
    ):
        super().__init__(parent)
        self.tcp_pool = tcp_pool
        self.connector_manager = connector_manager

        apply_standard_dialog_chrome(self, "JS8 Email", WINDOW_WIDTH, WINDOW_HEIGHT)

        self._setup_ui()
        self._load_rigs()

    # -------------------------------------------------------------------------
    # Setup
    # -------------------------------------------------------------------------

    def _setup_ui(self) -> None:
        """Build the user interface."""
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
        )

        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(3)
        layout.setContentsMargins(15, 15, 15, 15)

        # Title
        title = QtWidgets.QLabel("JS8 Email")
        title.setAlignment(Qt.AlignCenter)
        title.setFont(QtGui.QFont("Roboto Slab", -1, QtGui.QFont.Black))
        title.setFixedHeight(36)
        title.setStyleSheet(
            f"QLabel {{ background-color:{_PROG_BG}; color:{_PROG_FG};"
            f" font-family:'Roboto Slab'; font-size:16px; font-weight:900;"
            f" padding-top:9px; padding-bottom:9px; }}"
        )
        layout.addWidget(title)
        layout.addSpacing(7)

        # Rig / Mode / Frequency row
        def _labeled_col(lbl_text, ctrl):
            col = QtWidgets.QVBoxLayout()
            col.setSpacing(2)
            lbl = QtWidgets.QLabel(lbl_text)
            lbl.setStyleSheet(
                "QLabel { font-family:Roboto; font-size:13px; font-weight:bold; }"
            )
            col.addWidget(lbl)
            col.addWidget(ctrl)
            return col

        rig_row = QtWidgets.QHBoxLayout()
        rig_row.setSpacing(8)

        self.rig_combo = QtWidgets.QComboBox()
        self.rig_combo.setMinimumWidth(140)
        self.rig_combo.setMaxVisibleItems(30)
        self.rig_combo.setItemDelegate(QtWidgets.QStyledItemDelegate(self.rig_combo))
        self.rig_combo.currentTextChanged.connect(self._on_rig_changed)
        rig_row.addLayout(_labeled_col("Rig:", self.rig_combo))

        self.mode_combo = QtWidgets.QComboBox()
        self.mode_combo.setFixedWidth(160)
        self.mode_combo.setMaxVisibleItems(30)
        self.mode_combo.setItemDelegate(QtWidgets.QStyledItemDelegate(self.mode_combo))
        self.mode_combo.addItem("Slow", 4)
        self.mode_combo.addItem("Normal", 0)
        self.mode_combo.addItem("Fast", 1)
        self.mode_combo.addItem("Turbo", 2)
        self.mode_combo.addItem("Ultra", 8)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        rig_row.addLayout(_labeled_col("Mode:", self.mode_combo))

        self.freq_field = QtWidgets.QLineEdit()
        self.freq_field.setFixedWidth(80)
        self.freq_field.setReadOnly(True)
        self.freq_field.setStyleSheet(
            f"QLineEdit {{ background-color:white; color:{COLOR_INPUT_TEXT};"
            f" border:1px solid {COLOR_INPUT_BORDER}; border-radius:4px; padding:2px 4px;"
            f" font-family:'Kode Mono'; font-size:13px; }}"
        )
        rig_row.addLayout(_labeled_col("Freq:", self.freq_field))

        rig_row.addStretch()
        layout.addLayout(rig_row)

        # Email address
        email_label = QtWidgets.QLabel("Email Address:")
        email_label.setStyleSheet(
            "QLabel { font-family:Roboto; font-size:13px; font-weight:bold; }"
        )
        layout.addWidget(email_label)
        self.email_field = QtWidgets.QLineEdit()
        self.email_field.setMinimumHeight(30)
        self.email_field.setMaxLength(40)
        self.email_field.setPlaceholderText("recipient@example.com")
        layout.addWidget(self.email_field)

        # Subject / message
        subject_row = QtWidgets.QHBoxLayout()
        subject_label = QtWidgets.QLabel("Message (Subject Line):")
        subject_label.setStyleSheet(
            "QLabel { font-family:Roboto; font-size:13px; font-weight:bold; }"
        )
        subject_row.addWidget(subject_label)
        subject_row.addStretch()
        self.subject_count_label = QtWidgets.QLabel()
        self.subject_count_label.setStyleSheet(
            "QLabel { font-family:'Kode Mono'; font-size:13px; }"
        )
        subject_row.addWidget(self.subject_count_label)
        layout.addLayout(subject_row)

        self.subject_field = QtWidgets.QLineEdit()
        self.subject_field.setMinimumHeight(30)
        self.subject_field.setMaxLength(MAX_SUBJECT_LENGTH)
        self.subject_field.setPlaceholderText("Your message here (67 characters max)")
        self.subject_field.textChanged.connect(self._force_uppercase_subject)
        self.subject_field.textChanged.connect(self._update_subject_count_label)
        layout.addWidget(self.subject_field)
        self._update_subject_count_label()

        # Note + Limitations
        note = QtWidgets.QLabel(
            '<span style="color:#CC0000; font-weight:bold;">Note:</span> '
            "APRS emails are sent in the subject line. Replies are not supported."
        )
        note.setStyleSheet(f"QLabel {{ color:{_PANEL_FG}; font-family:Roboto; font-size:13px; }}")
        layout.addWidget(note)

        limitations = QtWidgets.QLabel(
            '<span style="color:#CC0000; font-weight:bold;">Limitations:</span> '
            "Sending email depends on APRS services being available."
        )
        limitations.setStyleSheet(f"QLabel {{ color:{_PANEL_FG}; font-family:Roboto; font-size:13px; }}")
        layout.addWidget(limitations)

        layout.addSpacing(12)

        # Button row
        btn_row = QtWidgets.QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch()

        self.btn_transmit = make_button("Transmit", COLOR_BTN_BLUE, min_w=100)
        connect_single(self.btn_transmit, self._on_transmit)
        btn_row.addWidget(self.btn_transmit)

        btn_cancel = make_button("Cancel", COLOR_BTN_RED, min_w=100)
        btn_cancel.clicked.connect(self.close)
        btn_row.addWidget(btn_cancel)

        layout.addLayout(btn_row)

    def _force_uppercase_subject(self, text: str) -> None:
        upper = text.upper()
        if upper != text:
            pos = self.subject_field.cursorPosition()
            self.subject_field.blockSignals(True)
            self.subject_field.setText(upper)
            self.subject_field.blockSignals(False)
            self.subject_field.setCursorPosition(pos)

    def _update_subject_count_label(self, _text: str = "") -> None:
        """Refresh the 'N of MAX' counter next to the Subject label."""
        count = len(self.subject_field.text())
        self.subject_count_label.setText(f"{count} of {MAX_SUBJECT_LENGTH}")
        color = COLOR_BTN_RED if count >= MAX_SUBJECT_LENGTH else _COL_COUNTER
        self.subject_count_label.setStyleSheet(
            f"QLabel {{ font-family:'Kode Mono'; font-size:13px; color:{color}; }}"
        )

    # -------------------------------------------------------------------------
    # Rig management
    # -------------------------------------------------------------------------

    def _load_rigs(self) -> None:
        """Load connected rigs into the rig dropdown."""
        if not self.tcp_pool:
            return

        self.rig_combo.blockSignals(True)
        self.rig_combo.clear()

        connected_rigs = self.tcp_pool.get_connected_rig_names()

        if len(connected_rigs) == 1:
            self.rig_combo.addItem(connected_rigs[0])
        elif connected_rigs:
            self.rig_combo.addItem("")
            for rig_name in connected_rigs:
                self.rig_combo.addItem(rig_name)

        self.rig_combo.blockSignals(False)

        current_text = self.rig_combo.currentText()
        if current_text:
            self._on_rig_changed(current_text)

    def closeEvent(self, event) -> None:
        if self.tcp_pool:
            for rig_name in self.tcp_pool.get_all_rig_names():
                client = self.tcp_pool.get_client(rig_name)
                if client:
                    for sig, slot in [
                        (client.frequency_received, self._on_frequency_received),
                        (client.call_selected_received, self._on_call_selected_for_transmit),
                    ]:
                        try:
                            sig.disconnect(slot)
                        except (TypeError, RuntimeError):
                            pass
        super().closeEvent(event)

    def _on_rig_changed(self, rig_name: str) -> None:
        """Handle rig selection change — update mode/frequency display."""
        if not rig_name or "(disconnected)" in rig_name:
            self.freq_field.setText("")
            return

        if not self.tcp_pool:
            return

        for client_name in self.tcp_pool.get_all_rig_names():
            client = self.tcp_pool.get_client(client_name)
            if client:
                try:
                    client.frequency_received.disconnect(self._on_frequency_received)
                except TypeError:
                    pass

        client = self.tcp_pool.get_client(rig_name)
        if client and client.is_connected():
            client.frequency_received.connect(self._on_frequency_received)

            speed_name = (client.speed_name or "").upper()
            mode_map = {"SLOW": 0, "NORMAL": 1, "FAST": 2, "TURBO": 3, "ULTRA": 4}
            idx = mode_map.get(speed_name, 1)
            self.mode_combo.blockSignals(True)
            self.mode_combo.setCurrentIndex(idx)
            self.mode_combo.blockSignals(False)

            frequency = client.frequency
            if frequency:
                self.freq_field.setText(f"{frequency:.3f}")
            else:
                self.freq_field.setText("")

            client.get_frequency()
        else:
            self.freq_field.setText("")

    def _on_frequency_received(self, rig_name: str, dial_freq: int) -> None:
        """Handle frequency received from JS8Call."""
        if self.rig_combo.currentText() == rig_name:
            self.freq_field.setText(f"{dial_freq / 1000000:.3f}")

    def _on_mode_changed(self, index: int) -> None:
        """Send MODE.SET_SPEED to JS8Call when mode dropdown changes."""
        rig_name = self.rig_combo.currentText()
        if not rig_name or "(disconnected)" in rig_name or not self.tcp_pool:
            return

        client = self.tcp_pool.get_client(rig_name)
        if client and client.is_connected():
            speed_value = self.mode_combo.currentData()
            client.send_message("MODE.SET_SPEED", "", {"SPEED": speed_value})
            print(f"[JS8Mail] Set mode to {self.mode_combo.currentText()} (speed={speed_value})")

    # -------------------------------------------------------------------------
    # Validation & transmit
    # -------------------------------------------------------------------------

    def _show_error(self, message: str) -> None:
        msg = QMessageBox(self)
        msg.setWindowTitle("JS8 Email")
        msg.setText(message)
        msg.setIcon(QMessageBox.Critical)
        msg.setWindowFlag(Qt.WindowStaysOnTopHint)
        msg.exec_()

    def _validate(self) -> bool:
        email = self.email_field.text().strip()
        subject = self.subject_field.text().strip()

        if len(email) < MIN_EMAIL_LENGTH or not re.match(EMAIL_PATTERN, email, re.IGNORECASE):
            self._show_error("Please enter a valid email address.")
            self.email_field.setFocus()
            return False

        if len(subject) < MIN_SUBJECT_LENGTH:
            self._show_error(f"Message is too short (minimum {MIN_SUBJECT_LENGTH} characters).")
            self.subject_field.setFocus()
            return False

        return True

    def _on_transmit(self) -> None:
        """Validate and transmit the email."""
        if not self._validate():
            return

        rig_name = self.rig_combo.currentText()
        if "(disconnected)" in rig_name:
            self._show_error("Cannot transmit: rig is disconnected.")
            return

        if not self.tcp_pool:
            self._show_error("Cannot transmit: TCP pool not available.")
            return

        client = self.tcp_pool.get_client(rig_name)
        if not client or not client.is_connected():
            self._show_error("Cannot transmit: not connected to rig.")
            return

        email = self.email_field.text().strip()
        subject = self.subject_field.text().strip()

        self._pending_message = f"@APRSIS CMD :EMAIL-2  :{email} {subject}{{03}}"
        self._pending_email = email
        self._pending_subject = subject

        try:
            client.call_selected_received.disconnect(self._on_call_selected_for_transmit)
        except TypeError:
            pass
        client.call_selected_received.connect(self._on_call_selected_for_transmit)
        client.get_call_selected()

    def _on_call_selected_for_transmit(self, rig_name: str, selected_call: str) -> None:
        """Check call selection before transmitting."""
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

        try:
            client.send_tx_message(self._pending_message)

            now = QDateTime.currentDateTimeUtc().toString("yyyy-MM-dd HH:mm:ss")
            print(f"\n{'='*60}")
            print(f"JS8MAIL TRANSMITTED - {now} UTC")
            print(f"{'='*60}")
            print(f"  Rig:      {rig_name}")
            print(f"  To:       {self._pending_email}")
            print(f"  Message:  {self._pending_subject}")
            print(f"  Full TX:  {self._pending_message}")
            print(f"{'='*60}\n")

            self.accept()

        except Exception as e:
            self._show_error(f"Failed to transmit: {e}")


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

    dialog = JS8MailDialog(tcp_pool, connector_manager)
    dialog.show()
    sys.exit(app.exec_())
