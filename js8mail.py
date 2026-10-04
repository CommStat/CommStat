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

from PyQt5 import QtWidgets
from PyQt5.QtCore import QDateTime
from PyQt5.QtWidgets import QDialog

from constants import (
    SPEED_OPTIONS,
    DEFAULT_COLORS,
    COLOR_BTN_BLUE, COLOR_BTN_RED,
)
from transmit_base import RigDialogMixin
from ui_helpers import make_button, apply_standard_dialog_chrome, connect_single, make_title_strip, show_error, make_combobox, make_input

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

_PANEL_BG = DEFAULT_COLORS.get("module_background",    "#DDDDDD")
_PANEL_FG = DEFAULT_COLORS.get("module_foreground",    "#000000")
_COL_COUNTER = "#444444"  # muted but legible counter text (COLOR_DISABLED_TEXT is too light here)


# =============================================================================
# JS8Mail Dialog
# =============================================================================

class JS8MailDialog(RigDialogMixin, QDialog):
    """JS8 Email form for sending emails via APRS gateway."""

    AUTO_SELECT_SINGLE_RIG = True

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
        )

        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(3)
        layout.setContentsMargins(15, 15, 15, 15)

        # Title
        title = make_title_strip("JS8 Email")
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

        self.rig_combo = make_combobox([], list_popup=True)
        self.rig_combo.setMinimumWidth(140)
        self.rig_combo.currentTextChanged.connect(self._on_rig_changed)
        rig_row.addLayout(_labeled_col("Rig:", self.rig_combo))

        self.mode_combo = make_combobox(
            SPEED_OPTIONS,
            list_popup=True,
        )
        self.mode_combo.setFixedWidth(160)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        rig_row.addLayout(_labeled_col("Mode:", self.mode_combo))

        self.freq_field = make_input(read_only=True)
        self.freq_field.setFixedWidth(80)
        rig_row.addLayout(_labeled_col("Freq:", self.freq_field))

        rig_row.addStretch()
        layout.addLayout(rig_row)

        # Email address
        email_label = QtWidgets.QLabel("Email Address:")
        email_label.setStyleSheet(
            "QLabel { font-family:Roboto; font-size:13px; font-weight:bold; }"
        )
        layout.addWidget(email_label)
        self.email_field = make_input("recipient@example.com", max_len=40)
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

        self.subject_field = make_input(
            "Your message here (67 characters max)", max_len=MAX_SUBJECT_LENGTH
        )
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
        btn_cancel.clicked.connect(self.reject)
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

    def _on_rig_changed(self, rig_name: str) -> None:
        """Handle rig selection change — update mode/frequency display."""
        if not rig_name:
            self.freq_field.setText("")
            return

        if not self.tcp_pool:
            return

        self._disconnect_rig_signals("rig")
        client = self.tcp_pool.get_client(rig_name)
        if client and client.is_connected():
            self._connect_rig_signal(client, "frequency_received", self._on_frequency_received)

            self._sync_mode_combo(client)

            self._show_frequency(client)

            client.get_frequency()
        else:
            self.freq_field.setText("")

    # -------------------------------------------------------------------------
    # Validation & transmit
    # -------------------------------------------------------------------------

    def _validate(self) -> bool:
        email = self.email_field.text().strip()
        subject = self.subject_field.text().strip()

        if len(email) < MIN_EMAIL_LENGTH or not re.match(EMAIL_PATTERN, email, re.IGNORECASE):
            show_error(self, "Please enter a valid email address.")
            self.email_field.setFocus()
            return False

        if len(subject) < MIN_SUBJECT_LENGTH:
            show_error(self, f"Message is too short (minimum {MIN_SUBJECT_LENGTH} characters).")
            self.subject_field.setFocus()
            return False

        return True

    def _on_transmit(self) -> None:
        """Validate and transmit the email."""
        if not self._validate():
            return

        rig_name = self.rig_combo.currentText()
        client = self._connected_client(rig_name)
        if client is None:
            return

        email = self.email_field.text().strip()
        subject = self.subject_field.text().strip()

        self._pending_message = f"@APRSIS CMD :EMAIL-2  :{email} {subject}{{03}}"
        self._pending_email = email
        self._pending_subject = subject

        self._begin_rf_transmit(client)

    def _on_call_clear(self, client, rig_name: str) -> None:
        """No call selected in JS8Call: transmit now."""
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
            show_error(self, f"Failed to transmit: {e}")


# =============================================================================
# Standalone Entry Point
# =============================================================================

if __name__ == "__main__":
    import sys
    from connector_manager import ConnectorManager
    from js8_tcp_client import TCPConnectionPool

    app = QtWidgets.QApplication(sys.argv)

    connector_manager = ConnectorManager()
    tcp_pool = TCPConnectionPool(connector_manager)
    tcp_pool.connect_all()

    dialog = JS8MailDialog(tcp_pool, connector_manager)
    dialog.show()
    sys.exit(app.exec_())
