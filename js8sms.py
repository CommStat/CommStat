# Copyright (c) 2025, 2026 Manuel Ochoa
# This file is part of CommStat.
# Licensed under the GNU General Public License v3.0.
# AI Assistance: Claude (Anthropic), ChatGPT (OpenAI)

"""
JS8 SMS Dialog for CommStat
Allows sending SMS messages via JS8Call APRS gateway.
"""

from typing import TYPE_CHECKING

from PyQt5 import QtWidgets
from PyQt5.QtCore import QDateTime, QUrl
from PyQt5.QtWidgets import QDialog

from constants import DEFAULT_COLORS, COLOR_BTN_BLUE, COLOR_BTN_RED, SPEED_OPTIONS
from transmit_base import RigDialogMixin
from ui_helpers import (
    make_button, make_input, make_combobox, make_title_strip, show_error, UpperCaseLineEdit,
    apply_standard_dialog_chrome, connect_single, open_external_url,
)

if TYPE_CHECKING:
    from js8_tcp_client import TCPConnectionPool
    from connector_manager import ConnectorManager


# =============================================================================
# Constants
# =============================================================================

MIN_PHONE_LENGTH = 10
MIN_MESSAGE_LENGTH = 8
MAX_MESSAGE_LENGTH = 67

WINDOW_WIDTH = 560
WINDOW_HEIGHT = 360

_PANEL_BG = DEFAULT_COLORS.get("module_background",    "#DDDDDD")
_PANEL_FG = DEFAULT_COLORS.get("module_foreground",    "#000000")
_COL_COUNTER = "#444444"  # muted but legible counter text (COLOR_DISABLED_TEXT is too light here)


# =============================================================================
# JS8SMS Dialog
# =============================================================================

class JS8SMSDialog(RigDialogMixin, QDialog):
    """JS8 SMS form for sending text messages via APRS gateway."""

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

        apply_standard_dialog_chrome(self, "JS8 SMS", WINDOW_WIDTH, WINDOW_HEIGHT)

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
        layout.setSpacing(2)
        layout.setContentsMargins(15, 15, 15, 15)

        # Title
        layout.addWidget(make_title_strip("JS8 SMS"))
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
            SPEED_OPTIONS, list_popup=True,
        )
        self.mode_combo.setFixedWidth(160)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        rig_row.addLayout(_labeled_col("Mode:", self.mode_combo))

        self.freq_field = make_input()
        self.freq_field.setFixedWidth(80)
        self.freq_field.setReadOnly(True)
        rig_row.addLayout(_labeled_col("Freq:", self.freq_field))

        rig_row.addStretch()
        layout.addLayout(rig_row)

        # Phone number
        phone_label = QtWidgets.QLabel("Phone Number:")
        phone_label.setStyleSheet(
            "QLabel { font-family:Roboto; font-size:13px; font-weight:bold; }"
        )
        layout.addWidget(phone_label)
        self.phone_field = make_input(placeholder="xxx-xxx-xxxx")
        self.phone_field.setInputMask("999-999-9999")
        layout.addWidget(self.phone_field)

        # Message
        message_row = QtWidgets.QHBoxLayout()
        message_label = QtWidgets.QLabel("Text Message:")
        message_label.setStyleSheet(
            "QLabel { font-family:Roboto; font-size:13px; font-weight:bold; }"
        )
        message_row.addWidget(message_label)
        message_row.addStretch()
        self.message_count_label = QtWidgets.QLabel()
        self.message_count_label.setStyleSheet(
            "QLabel { font-family:'Kode Mono'; font-size:13px; }"
        )
        message_row.addWidget(self.message_count_label)
        layout.addLayout(message_row)

        self.message_field = make_input(
            placeholder="Your message here (67 characters max)",
            max_len=MAX_MESSAGE_LENGTH, widget=UpperCaseLineEdit(),
        )
        self.message_field.textChanged.connect(self._update_message_count_label)
        layout.addWidget(self.message_field)
        self._update_message_count_label()

        # Note + Opt-in + Limitations
        note = QtWidgets.QLabel(
            '<span style="color:#CC0000; font-weight:bold;">Note:</span> '
            "Recipients must often opt-in on the SMS gateway before delivery will work."
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"QLabel {{ color:{_PANEL_FG}; font-family:Roboto; font-size:13px; }}")
        layout.addWidget(note)

        optin = QtWidgets.QLabel(
            'To opt in, the recipient must register their phone number at '
            '<a href="https://aprs.wiki/">https://aprs.wiki/</a>.'
        )
        optin.linkActivated.connect(
            lambda url: open_external_url(self, QUrl(url), panel_bg=_PANEL_BG)
        )
        optin.setWordWrap(True)
        optin.setStyleSheet(f"QLabel {{ color:{_PANEL_FG}; font-family:Roboto; font-size:13px; }}")
        layout.addWidget(optin)

        limitations = QtWidgets.QLabel(
            '<span style="color:#CC0000; font-weight:bold;">Limitations:</span> '
            "Sending SMS depends on APRS services being available."
        )
        limitations.setWordWrap(True)
        limitations.setStyleSheet(f"QLabel {{ color:{_PANEL_FG}; font-family:Roboto; font-size:13px; }}")
        layout.addWidget(limitations)

        layout.addStretch()

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

    def _update_message_count_label(self, _text: str = "") -> None:
        """Refresh the 'N of MAX' counter next to the Text Message label."""
        count = len(self.message_field.text())
        self.message_count_label.setText(f"{count} of {MAX_MESSAGE_LENGTH}")
        color = COLOR_BTN_RED if count >= MAX_MESSAGE_LENGTH else _COL_COUNTER
        self.message_count_label.setStyleSheet(
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
        phone = self.phone_field.text().replace("-", "").strip()
        message = self.message_field.text().strip()

        if len(phone) < MIN_PHONE_LENGTH:
            show_error(self, "Please enter a valid 10-digit phone number.")
            self.phone_field.setFocus()
            return False

        if len(message) < MIN_MESSAGE_LENGTH:
            show_error(self, f"Message is too short (minimum {MIN_MESSAGE_LENGTH} characters).")
            self.message_field.setFocus()
            return False

        return True

    def _on_transmit(self) -> None:
        """Validate and transmit the SMS."""
        if not self._validate():
            return

        rig_name = self.rig_combo.currentText()
        client = self._connected_client(rig_name)
        if client is None:
            return

        # SMSGTE wants the bare 10 digits after "@" (the field's dashes are display only)
        phone = self.phone_field.text().replace("-", "").strip()
        message_text = self.message_field.text().strip()

        self._pending_message = f"@APRSIS CMD :SMSGTE   :@{phone}  {message_text} {{04}}"
        self._pending_phone = phone
        self._pending_text = message_text

        self._begin_rf_transmit(client)

    def _on_call_clear(self, client, rig_name: str) -> None:
        """No call selected in JS8Call: transmit now."""
        try:
            client.send_tx_message(self._pending_message)

            now = QDateTime.currentDateTimeUtc().toString("yyyy-MM-dd HH:mm:ss")
            print(f"\n{'='*60}")
            print(f"JS8SMS TRANSMITTED - {now} UTC")
            print(f"{'='*60}")
            print(f"  Rig:      {rig_name}")
            print(f"  To:       {self._pending_phone}")
            print(f"  Message:  {self._pending_text}")
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

    dialog = JS8SMSDialog(tcp_pool, connector_manager)
    dialog.show()
    sys.exit(app.exec_())
