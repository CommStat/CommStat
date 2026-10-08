# Copyright (c) 2025 Manuel Ochoa
# This file is part of CommStat.
# Licensed under the GNU General Public License v3.0.
# AI Assistance: Claude (Anthropic), ChatGPT (OpenAI)

"""
Group Message Dialog for CommStat
Allows creating and transmitting group messages via JS8Call.
"""

import re
import sqlite3
from typing import Optional, TYPE_CHECKING

from PyQt5 import QtCore, QtWidgets
from PyQt5.QtCore import QDateTime, Qt
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout,
    QLabel, QPlainTextEdit,
    QCheckBox,
)

from constants import (
    SPEED_OPTIONS, INTERNET_RIG,
    COMMSRVR_URL,
    DEFAULT_COLORS, COLOR_INPUT_TEXT, COLOR_INPUT_BORDER,
    COLOR_BTN_CYAN, COLOR_BTN_BLUE, COLOR_BTN_RED, COLOR_BTN_HELP,
    COLOR_BTN_GREEN, COLOR_DISABLED_BG, COLOR_DISABLED_TEXT,
    RIG_FETCH_DELAY_MS,
)
from db_utils import db_connect
from id_utils import generate_time_based_id
from transmit_base import RigDialogMixin
from ui_helpers import (show_error, show_info, get_internet_user_settings, make_title_strip,
    make_button, apply_standard_dialog_chrome, connect_single, show_help_dialog,
    make_combobox, make_input,
)
from commsrvr_client import submit_to_commsrvr

if TYPE_CHECKING:
    from js8_tcp_client import TCPConnectionPool
    from connector_manager import ConnectorManager


# =============================================================================
# Constants
# =============================================================================

MIN_MESSAGE_LENGTH   = 4
MAX_MESSAGE_LENGTH   = 1500
NEWLINE_PLACEHOLDER  = "||"

_COMMSRVR = COMMSRVR_URL


_PANEL_BG = DEFAULT_COLORS.get("module_background",    "#DDDDDD")
_PANEL_FG = DEFAULT_COLORS.get("module_foreground",    "#000000")
_DATA_BG  = DEFAULT_COLORS.get("data_background",      "#F8F6F4")
_DATA_FG  = DEFAULT_COLORS.get("data_foreground",      "#000000")

_COL_CANCEL = "#555555"
_COL_COUNTER = "#444444"  # muted but legible counter text (COLOR_DISABLED_TEXT is too light here)

_WIN_W          = 700  # matches the Incident dialog
_WIN_H          = 514  # room for the RFI note below the checkbox

# Shown below the RFI checkbox while it is checked (same idea as the Incident
# dialog's type description).
_RFI_NOTE = (
    "RFIs are for emergencies only: a grid-down station requesting information\n"
    "it cannot obtain itself, such as severe weather, floods, evacuation routes, or wellness checks."
)

# ── Help content ──────────────────────────────────────────────────────────────
# Beside the feature it documents. Chrome comes from ui_helpers.

_HELP_HTML = """
<div style="font-family: Roboto; font-size: 13px; color: #333333;">

<h3 style="color:#555555;">What Is an RFI?</h3>
<p>A <b>Request for Information (RFI)</b> is a CommStat emergency message.
It is sent on behalf of a <b>grid-down</b> operator who needs information
they cannot obtain themselves. Checking the <b>RFI</b> box marks the message
as an RFI and makes it highly visible, so emergency communicators can see
at a glance that someone needs information or communications help.</p>

<h3 style="color:#555555;">When to Use It</h3>
<p>RFIs are reserved for <b>emergency situations</b>, such as a regional
communications outage, severe weather, or another grid-down event. In these
situations an operator may still have radio contact but no Internet, cellular
service, or other normal sources of information. Typical requests include:</p>
<ul>
<li>Severe weather reports and forecasts</li>
<li>Flood and road conditions</li>
<li>Evacuation routes and shelter locations</li>
<li>Wellness checks on family or others outside the affected area</li>
</ul>
<p>Please do not use the RFI checkbox for routine traffic. Keeping RFIs for
real emergencies ensures they get immediate attention.</p>

<h3 style="color:#555555;">How an RFI Moves</h3>
<p>The grid-down operator sends a request over radio to an
<b>RFI Relay Operator</b>, who enters it into CommStat as an RFI. Grid-up
operators monitoring CommStat see the request, gather the information from
available resources, and reply through CommStat. The relay operator then
transmits the answer back over radio to the operator who asked.</p>
<p>In this way, CommStat serves as an information bridge between an isolated
area and operators who still have access to outside resources.</p>

</div>
"""


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

class GroupMessageDialog(RigDialogMixin, QDialog):
    """Group Message dialog — compose and transmit a group message."""

    ALLOW_INTERNET_RIG = True


    def __init__(
        self,
        tcp_pool: "TCPConnectionPool" = None,
        connector_manager: "ConnectorManager" = None,
        refresh_callback=None,
        internet_available: Optional[bool] = None,
        parent=None,
    ):
        super().__init__(parent)
        self.tcp_pool            = tcp_pool
        self.connector_manager   = connector_manager
        self.refresh_callback    = refresh_callback
        self._internet_available_override = internet_available
        self.callsign: str       = ""
        self.msg_id: str         = ""
        self._pending_message: str   = ""
        self._pending_save_data: Optional[dict] = None
        self._is_rfi_reply: bool = False


        apply_standard_dialog_chrome(self, "Group Message", _WIN_W, _WIN_H)

        self.setStyleSheet(
            f"QDialog {{ background-color:{_PANEL_BG}; }}"
            f"QLabel {{ color:{_PANEL_FG}; font-family:Roboto; font-size:13px; }}"
            f"QPlainTextEdit {{ background-color:white; color:{COLOR_INPUT_TEXT};"
            f" border:1px solid {COLOR_INPUT_BORDER}; border-radius:4px; padding:4px;"
            f" font-family:'Kode Mono'; font-size:13px; }}"
        )

        self._setup_ui()
        self._generate_msg_id()
        self._load_config()
        self._load_rigs()

    def set_group_reply_context(self, group_name: str, body: str = "",
                                is_rfi_reply: bool = False) -> None:
        """Pre-populate the dialog when opened via 'GRP Reply' from a Message detail view.

        Locks the Group selector to the replied-to group (a reply must go back
        to the same group) and disables the RFI checkbox (a reply is never itself an
        RFI). When is_rfi_reply is set (the replied-to message has rfi=1), the
        transmitted marker gets a trailing "-" (see _build_message) identifying it
        as an RFI Reply on the wire.
        """
        self._is_rfi_reply = is_rfi_reply
        group_name = (group_name or "").strip().lstrip("@").upper()
        if group_name:
            idx = self.group_combo.findText(group_name)
            if idx < 0:
                self.group_combo.addItem(group_name)
                idx = self.group_combo.findText(group_name)
            self.group_combo.setCurrentIndex(idx)
        self.group_combo.setEnabled(False)
        self.rfi_checkbox.setChecked(False)
        self.rfi_checkbox.setEnabled(False)
        if body:
            self.message_edit.setPlainText(body)

    def set_relay_context(self, body: str) -> None:
        """Pre-populate the dialog when opened via 'Relay' from a Message detail view.

        Unlike set_group_reply_context(), the Group selector and RFI checkbox
        stay editable, and the original message text is copied in as-is
        (no reply separator) since this is a rebroadcast, not a reply.
        """
        self.rfi_checkbox.setChecked(True)
        if body:
            self.message_edit.setPlainText(body)

    # -------------------------------------------------------------------------
    # UI construction
    # -------------------------------------------------------------------------

    def _setup_ui(self) -> None:
        body = QVBoxLayout(self)
        body.setContentsMargins(15, 15, 15, 15)
        body.setSpacing(10)

        # Title
        title_lbl = make_title_strip("Group Message")
        body.addWidget(title_lbl)

        # Settings row: Rig | Mode | Freq | Delivery
        settings_row = QHBoxLayout()
        settings_row.setSpacing(8)

        self.rig_combo = make_combobox([], list_popup=True)
        self.rig_combo.setFixedWidth(150)
        settings_row.addLayout(_labeled_col("Rig:", self.rig_combo))

        self.mode_combo = make_combobox(
            SPEED_OPTIONS,
            list_popup=True,
        )
        self.mode_combo.setFixedWidth(100)
        settings_row.addLayout(_labeled_col("Mode:", self.mode_combo))

        self.freq_field = make_input(read_only=True)
        self.freq_field.setFixedWidth(90)
        settings_row.addLayout(_labeled_col("Freq:", self.freq_field))

        self.delivery_combo = make_combobox(
            [("Maximum Reach", None), ("Limited Reach", None)], list_popup=True
        )
        self.delivery_combo.setFixedWidth(160)
        settings_row.addLayout(_labeled_col("Delivery:", self.delivery_combo))

        settings_row.addStretch()
        body.addLayout(settings_row)

        # Group row
        group_row = QHBoxLayout()
        group_row.setSpacing(8)
        self.group_combo = make_combobox([], list_popup=True)
        self.group_combo.setFixedWidth(180)
        group_row.addLayout(_labeled_col("Group:", self.group_combo))
        group_row.addStretch()

        body.addLayout(group_row)

        # RFI row
        rfi_row = QHBoxLayout()
        self.rfi_checkbox = QCheckBox("Request for Information")
        self.rfi_checkbox.setStyleSheet(
            "QCheckBox { font-family:Roboto; font-size:13px; font-weight:bold;"
            f" color:{_PANEL_FG}; }}"
            f"QCheckBox::indicator {{ width:16px; height:16px; background-color:white;"
            f" border:1px solid {COLOR_INPUT_BORDER}; border-radius:3px; }}"
            f"QCheckBox::indicator:checked {{ background-color:{COLOR_BTN_GREEN};"
            f" border:1px solid {COLOR_BTN_GREEN}; }}"
            f"QCheckBox::indicator:disabled {{ background-color:{COLOR_DISABLED_BG};"
            f" border:1px solid {COLOR_INPUT_BORDER}; }}"
            f"QCheckBox::indicator:checked:disabled {{ background-color:{COLOR_DISABLED_TEXT};"
            f" border:1px solid {COLOR_DISABLED_TEXT}; }}"
        )
        rfi_row.addWidget(self.rfi_checkbox)
        rfi_row.addStretch()

        body.addLayout(rfi_row)

        # RFI note, shown directly below the checkbox while it is checked
        self.rfi_note = QLabel("")
        self.rfi_note.setMinimumHeight(48)  # Two-line note height; keeps layout stable while blank
        self.rfi_note.setAlignment(Qt.AlignCenter)
        self.rfi_note.setWordWrap(True)
        self.rfi_note.setStyleSheet(
            f"QLabel {{ color:{_PANEL_FG}; background-color:transparent;"
            f" font-family:Roboto; font-size:15px; padding:2px 10px 4px 10px; }}"
        )
        body.addWidget(self.rfi_note)
        self.rfi_checkbox.toggled.connect(self._on_rfi_toggled)

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

        self.message_edit = QPlainTextEdit()
        self.message_edit.setMinimumHeight(160)
        self.message_edit.setPlaceholderText(f"{MAX_MESSAGE_LENGTH} characters max, multiple lines allowed")
        self.message_edit.textChanged.connect(self._enforce_message_limit)
        body.addWidget(self.message_edit)
        self._update_message_count_label()

        body.addStretch()

        # Button row
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self.help_btn = make_button("Help", COLOR_BTN_HELP, 60)
        self.help_btn.clicked.connect(self._on_help_clicked)
        btn_row.addWidget(self.help_btn)

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
        all_groups = self._get_all_groups_from_db()
        if len(all_groups) == 1:
            self.group_combo.addItem(all_groups[0])
        else:
            self.group_combo.addItem("")
            for group in all_groups:
                self.group_combo.addItem(group)

    # -------------------------------------------------------------------------
    # Signal handlers
    # -------------------------------------------------------------------------

    def _on_help_clicked(self) -> None:
        """Explain the Request for Information (RFI) checkbox."""
        show_help_dialog(self, "Group Message Help", _HELP_HTML, width=520)

    def _on_rfi_toggled(self, checked: bool) -> None:
        self.rfi_note.setText(_RFI_NOTE if checked else "")

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

        if is_internet:
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
            self._connect_rig_signal(client, "frequency_received", self._on_frequency_received)
            client.get_callsign()
            QtCore.QTimer.singleShot(RIG_FETCH_DELAY_MS, client.get_frequency)
        else:
            self.freq_field.setText("")

    def _on_callsign_received(self, rig_name: str, callsign: str) -> None:
        if self.rig_combo.currentText() == rig_name:
            self.callsign = callsign

    def _enforce_message_limit(self) -> None:
        """Hard-cap the message at the character limit (measured post-cleaning,
        with newlines expanded to "||", same as what is transmitted/stored)
        and refresh the counter."""
        limit = MAX_MESSAGE_LENGTH
        raw = self.message_edit.toPlainText()
        cleaned = self._clean_message(raw)
        if len(cleaned) > limit:
            cursor = self.message_edit.textCursor()
            pos = cursor.position()
            # Pre-trim to the limit so a large paste doesn't loop char-by-char
            # over thousands of characters
            raw = raw[:limit]
            while raw and len(self._clean_message(raw)) > limit:
                raw = raw[:-1]
            cleaned = self._clean_message(raw)
            self.message_edit.blockSignals(True)
            self.message_edit.setPlainText(raw)
            self.message_edit.blockSignals(False)
            cursor = self.message_edit.textCursor()
            cursor.setPosition(min(pos, len(raw)))
            self.message_edit.setTextCursor(cursor)
        self._update_message_count_label(len(cleaned), limit)

    def _update_message_count_label(self, count: Optional[int] = None, limit: Optional[int] = None) -> None:
        """Refresh the 'N of MAX' counter next to the Message label."""
        if not hasattr(self, 'message_count_label'):
            return
        if limit is None:
            limit = MAX_MESSAGE_LENGTH
        if count is None:
            count = len(self._clean_message(self.message_edit.toPlainText()))
        self.message_count_label.setText(f"{count} of {limit}")
        color = COLOR_BTN_RED if count >= limit else _COL_COUNTER
        self.message_count_label.setStyleSheet(
            f"QLabel {{ font-family:'Kode Mono'; font-size:13px; color:{color}; }}"
        )

    # -------------------------------------------------------------------------
    # Database helpers
    # -------------------------------------------------------------------------

    def _get_all_groups_from_db(self) -> list:
        try:
            with db_connect() as conn:
                return [r[0] for r in conn.execute("SELECT name FROM groups ORDER BY name").fetchall()]
        except sqlite3.Error as e:
            print(f"Error reading groups from database: {e}")
        return []

    # -------------------------------------------------------------------------
    # Validation / messaging helpers
    # -------------------------------------------------------------------------

    def _validate_input(self) -> Optional[tuple]:
        rig_name = self.rig_combo.currentText()
        if not rig_name:
            show_error(self, "Please select a Rig")
            self.rig_combo.setFocus()
            return None

        group_name = self.group_combo.currentText()
        if not group_name:
            show_error(self, "Please select a Group")
            self.group_combo.setFocus()
            return None

        message = self._clean_message(self.message_edit.toPlainText())

        if len(message) < MIN_MESSAGE_LENGTH:
            show_error(self, "Message too short")
            return None

        return (self.callsign.upper(), self._apply_rfi_text(message))

    @staticmethod
    def _clean_message(text: str) -> str:
        """Encode newlines as "||" (decoded back to newlines in the detail view),
        then replace any remaining non-printable/non-ASCII runs with a space."""
        text = text.replace('\r\n', NEWLINE_PLACEHOLDER).replace('\n', NEWLINE_PLACEHOLDER).replace('\r', NEWLINE_PLACEHOLDER)
        return re.sub(r"[^ -~]+", " ", text)

    def _apply_rfi_text(self, message: str) -> str:
        """For an RFI, prefix "RFI - " and append the "||" newline marker, UTC date, and message id.
        For a group reply, prefix "RFI REPLY - "."""
        if self._is_rfi_reply:
            return f"RFI REPLY - {message}"
        if not self.rfi_checkbox.isChecked():
            return message
        date = QDateTime.currentDateTimeUtc().toString("yyyy-MM-dd")
        return f"RFI - {message}||{date} {self.msg_id}"

    def _build_message(self, message: str) -> str:
        group  = "@" + self.group_combo.currentText()
        marker = "{^%3}" if self.rig_combo.currentText() == INTERNET_RIG else "{^%}"
        if self.rfi_checkbox.isChecked():
            marker += "+"
        if self._is_rfi_reply:
            marker += "-"
        return f"{group} MSG ,{self.msg_id},{message},{marker}"

    # -------------------------------------------------------------------------
    # Commsrvr / database
    # -------------------------------------------------------------------------

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
            'rfi': 2 if self._is_rfi_reply else (1 if self.rfi_checkbox.isChecked() else 0),
        }

    def _save_to_database(self, saved_data: dict, global_id: int = 0) -> None:
        """Save the message to the database.

        Args:
            saved_data: Snapshot from _capture_save_data().
            global_id: The global ID returned by the commsrvr server (0 if unknown).
        """
        with db_connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO messages "
                "(global_id, datetime, date, freq, db, source, msg_id, from_callsign, target, message, rfi) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (global_id, saved_data['datetime_str'], saved_data['date_only'], saved_data['frequency'], 30,
                 saved_data['source'], saved_data['msg_id'], saved_data['callsign'],
                 saved_data['target'], saved_data['message'], saved_data['rfi'])
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
            self.callsign = get_internet_user_settings()[0]
            if not self.callsign:
                show_error(self, 
                    "No callsign configured.\n\nPlease set your callsign in Settings → User Settings."
                )
                return
        elif not self.callsign:
            show_error(self, 
                "Callsign not yet received from the rig.\n\nPlease wait a moment and try again."
            )
            return

        result = self._validate_input()
        if result is None:
            return

        callsign, message = result
        tx_message = self._build_message(message)
        show_info(self, f"CommStat has saved:\n{tx_message}")
        saved_data = self._capture_save_data(callsign, message)
        saved_data['source'] = 0  # saved locally, never transmitted
        self._save_to_database(saved_data)

        if self.refresh_callback:
            self.refresh_callback()

        self.accept()

    def _transmit(self) -> None:
        rig_name = self.rig_combo.currentText()

        if rig_name == INTERNET_RIG:
            self.callsign = get_internet_user_settings()[0]
            if not self.callsign:
                show_error(self, 
                    "No callsign configured.\n\nPlease set your callsign in Settings → User Settings."
                )
                return

        result = self._validate_input()
        if result is None:
            return

        callsign, message = result

        if rig_name == INTERNET_RIG:
            import netguard
            if not netguard.is_network_enabled():
                show_error(self, 
                    "Cannot transmit — Off-Grid Mode is enabled.\n\n"
                    "Switch to Online mode to transmit via the Internet."
                )
                return
            self._pending_message  = self._build_message(message)
            self._pending_save_data = self._capture_save_data(callsign, message, 0)
            now = QDateTime.currentDateTimeUtc().toString("yyyy-MM-dd HH:mm:ss")
            rfi_suffix = "+" if self.rfi_checkbox.isChecked() else ""
            grp_reply_suffix = "-" if self._is_rfi_reply else ""
            message_data = (
                f"{callsign}: @{self.group_combo.currentText()}"
                f" MSG ,{self.msg_id},{message},{{^%3}}{rfi_suffix}{grp_reply_suffix}"
            )

            def _on_internet_commsrvr_complete(global_id: int) -> None:
                # Internet-only: save only when the server accepted it (numeric global_id)
                if global_id:
                    self._save_to_database(self._pending_save_data, global_id)
                    self._refresh_and_close()

            submit_to_commsrvr(self, 0, callsign, message_data, now, on_complete=_on_internet_commsrvr_complete)
            return

        client = self._connected_client(rig_name)
        if client is None:
            return

        if not callsign:
            show_error(self, 
                "Callsign not yet received from the rig.\n\nPlease wait a moment and try again."
            )
            return

        self._pending_message = self._build_message(message)

        self._begin_rf_transmit(client)

    def _transmit_with_frequency(self, client, frequency: int) -> None:
        """Send over the rig and save; runs once the rig has reported its frequency."""
        try:
            client.send_tx_message(self._pending_message)

            message = self._apply_rfi_text(self._clean_message(self.message_edit.toPlainText()))

            self._pending_save_data = self._capture_save_data(self.callsign, message, frequency)

            if self.delivery_combo.currentText() == "Limited Reach":
                # No commsrvr submission — save immediately with no global_id
                self._save_to_database(self._pending_save_data)
                self._refresh_and_close()
            else:
                group = "@" + self.group_combo.currentText()
                rfi_suffix = "+" if self.rfi_checkbox.isChecked() else ""
                grp_reply_suffix = "-" if self._is_rfi_reply else ""
                message_data = f"{self.callsign}: {group} MSG ,{self.msg_id},{message},{{^%}}{rfi_suffix}{grp_reply_suffix}"

                def _on_radio_commsrvr_complete(global_id: int) -> None:
                    self._save_to_database(self._pending_save_data, global_id)
                    self._refresh_and_close()

                submit_to_commsrvr(self, frequency, self.callsign, message_data, self._pending_save_data['datetime_str'], on_complete=_on_radio_commsrvr_complete)
        except Exception as e:
            show_error(self, f"Failed to transmit message: {e}")

    def _generate_msg_id(self) -> None:
        self.msg_id = generate_time_based_id()


# Legacy alias
Ui_FormMessage = GroupMessageDialog
