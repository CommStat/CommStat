# Copyright (c) 2025 Manuel Ochoa
# This file is part of CommStat.
# Licensed under the GNU General Public License v3.0.
# AI Assistance: Claude (Anthropic), ChatGPT (OpenAI)

"""
Request for Information Dialog for CommStat
Creates RFI messages and RFI replies, sent to a group by Internet only.
"""

import re
import sqlite3
from typing import Optional

from PyQt5 import QtWidgets
from PyQt5.QtCore import QDateTime, Qt
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout,
    QLabel, QPlainTextEdit,
)

from constants import (
    DEFAULT_COLORS, COLOR_INPUT_TEXT, COLOR_INPUT_BORDER,
    COLOR_BTN_BLUE, COLOR_BTN_RED, COLOR_BTN_HELP,
)
from db_utils import db_connect
from id_utils import generate_time_based_id
from ui_helpers import (show_error, get_internet_user_settings, make_title_strip,
    make_button, apply_standard_dialog_chrome, connect_single, show_help_dialog,
    make_combobox,
)
from commsrvr_client import submit_to_commsrvr


# =============================================================================
# Constants
# =============================================================================

MIN_MESSAGE_LENGTH   = 4
MAX_MESSAGE_LENGTH   = 1500
NEWLINE_PLACEHOLDER  = "||"

_PANEL_BG = DEFAULT_COLORS.get("module_background",    "#DDDDDD")
_PANEL_FG = DEFAULT_COLORS.get("module_foreground",    "#000000")

_COL_CANCEL = "#555555"
_COL_COUNTER = "#444444"  # muted but legible counter text (COLOR_DISABLED_TEXT is too light here)

_WIN_W          = 700  # matches the Group Message dialog
_WIN_H          = 470

# "RFI 2610" (global ID) or the older "RFI Y24" (msg ID) tag
_RFI_TAG_RE    = re.compile(r"\bRFI (?:\d+|[A-Y]\d{2})\b")
_RFI_PREFIX_RE = re.compile(r"^RFI (?:\d+|[A-Y]\d{2}) - ")
_RFI_REPLY_PREFIX_RE = re.compile(r"^RFI(?: (?:\d+|[A-Y]\d{2}))? Reply - ")

_INTERNET_NOTE = "Note: Request for Information messages are sent by internet only."

_RFI_NOTE = (
    "RFIs may request information on any topic. Some groups, including AmRRON and MAGNET,\n"
    "use RFIs for emergencies only. In their traffic, please follow the group's suggested uses."
)

# ── Help content ──────────────────────────────────────────────────────────────
# Beside the feature it documents. Chrome comes from ui_helpers.

_HELP_HTML = """
<div style="font-family: Roboto; font-size: 13px; color: #333333;">

<h3 style="color:#555555;">What Is an RFI?</h3>
<p>A <b>Request for Information (RFI)</b> is a CommStat message that asks
other operators for information. RFIs are highly visible, so other operators
can see at a glance that someone is asking for information.</p>
<p>RFIs are sent to a group by <b>Internet only</b>.</p>

<h3 style="color:#555555;">When to Use It</h3>
<p>RFIs can be used for any purpose. You can request information on any
topic.</p>
<p>However, some groups, including <b>AmRRON</b> and <b>MAGNET</b>, have
chosen to use RFIs for <b>emergencies only</b>, such as a regional
communications outage, severe weather, or another grid-down event. If you are
participating in their traffic, please follow the group's suggested uses.</p>

<h3 style="color:#555555;">How an RFI Moves</h3>
<p>In an emergency, a grid-down operator may send a request over radio to an
<b>RFI Relay Operator</b>, who enters it into CommStat as an RFI. Grid-up
operators monitoring CommStat see the request, gather the information from
available resources, and reply through CommStat. The relay operator then
transmits the answer back over radio to the operator who asked.</p>
<p>In this way, CommStat serves as an information bridge between an isolated
area and operators who still have access to outside resources.</p>

<h3 style="color:#555555;">RFI Tracking</h3>
<p>You don't need to type any tracking information. When you transmit, the
CommStat server assigns the RFI a <b>Global ID</b>, and CommStat adds the
correct label and that ID to the beginning of the message, so requests and
their replies can be matched up.</p>
<p>For example, a new RFI is shown as <b>RFI 2610 - &lt;your message&gt;</b>,
and a reply to it is sent as <b>RFI 2610 Reply - &lt;your reply&gt;</b>. The
ID is captured from the original RFI as soon as you start a reply, so it is
kept even if you delete the quoted text.</p>

</div>
"""


# =============================================================================
# Helpers
# =============================================================================

def _labeled_col(lbl_text: str, ctrl: QtWidgets.QWidget) -> QVBoxLayout:
    col = QVBoxLayout()
    col.setSpacing(2)
    lbl = QLabel(lbl_text)
    lbl.setStyleSheet(
        f"QLabel {{ font-family:Roboto; font-size:13px; font-weight:bold; color:{_PANEL_FG}; }}"
    )
    col.addWidget(lbl)
    col.addWidget(ctrl)
    return col


# =============================================================================
# Dialog
# =============================================================================

class RFIDialog(QDialog):
    """Request for Information dialog — compose and send an RFI (or RFI Reply) by Internet."""

    def __init__(self, refresh_callback=None, parent=None):
        super().__init__(parent)
        self.refresh_callback    = refresh_callback
        self.msg_id: str         = ""
        self._pending_save_data: Optional[dict] = None
        self._is_reply: bool     = False
        self._reply_tag: str     = ""   # e.g. "RFI 2610", captured from the replied-to RFI

        apply_standard_dialog_chrome(self, "Request for Information", _WIN_W, _WIN_H)

        self.setStyleSheet(
            f"QDialog {{ background-color:{_PANEL_BG}; }}"
            f"QLabel {{ color:{_PANEL_FG}; font-family:Roboto; font-size:13px; }}"
            f"QPlainTextEdit {{ background-color:white; color:{COLOR_INPUT_TEXT};"
            f" border:1px solid {COLOR_INPUT_BORDER}; border-radius:4px; padding:4px;"
            f" font-family:'Kode Mono'; font-size:13px; }}"
        )

        self._setup_ui()
        self._load_config()

    def _select_group(self, group_name: str) -> None:
        group_name = (group_name or "").strip().lstrip("@").upper()
        if not group_name:
            return
        idx = self.group_combo.findText(group_name, Qt.MatchFixedString)  # case-insensitive
        if idx < 0:
            self.group_combo.addItem(group_name)
            idx = self.group_combo.count() - 1
        self.group_combo.setCurrentIndex(idx)

    def set_reply_context(self, group_name: str, body: str = "", global_id: int = 0) -> None:
        """Pre-populate the dialog when opened via 'GRP Reply' on an RFI.

        Locks the Group selector to the replied-to group (a reply must go back
        to the same group). The reply is sent with a trailing "-" marker (rfi=2)
        and an "RFI <global id> Reply - " prefix. The tag comes from the original
        RFI's global ID, or from its body when that is unknown, and is captured
        now since the user may delete the body before transmitting.
        """
        self._is_reply = True
        if global_id:
            self._reply_tag = f"RFI {global_id}"
        else:
            tag_match = _RFI_TAG_RE.search(body or "")
            self._reply_tag = tag_match.group(0) if tag_match else ""
        self._select_group(group_name)
        self.group_combo.setEnabled(False)
        self.setWindowTitle("Request for Information Reply")
        self.title_lbl.setText("Request for Information Reply")
        if body:
            self.message_edit.setPlainText(body)

    def set_forward_context(self, group_name: str, body: str) -> None:
        """Pre-populate the dialog when opened via 'Forward' on an RFI received by RF.

        The group stays editable. Any old "RFI <id> - " prefix is dropped, since
        the new RFI gets its own global ID.
        """
        self._select_group(group_name)
        self.message_edit.setPlainText(_RFI_PREFIX_RE.sub("", body or ""))

    def set_saved_context(self, target: str, message: str, rfi: int = 1) -> None:
        """Pre-populate the dialog from a Save Only RFI (rfi=1) or RFI Reply (rfi=2)
        so it can be transmitted as a new record."""
        body = (message or "").replace(NEWLINE_PLACEHOLDER, "\n")
        if rfi == 2:
            self.set_reply_context(target, body)
            body = _RFI_REPLY_PREFIX_RE.sub("", body)
        else:
            self._select_group(target)
            body = _RFI_PREFIX_RE.sub("", body)
        self.message_edit.setPlainText(body)

    # -------------------------------------------------------------------------
    # UI construction
    # -------------------------------------------------------------------------

    def _setup_ui(self) -> None:
        body = QVBoxLayout(self)
        body.setContentsMargins(15, 15, 15, 15)
        body.setSpacing(10)

        # Title
        self.title_lbl = make_title_strip("Request for Information")
        body.addWidget(self.title_lbl)

        note_lbl = QLabel(_INTERNET_NOTE)
        note_lbl.setAlignment(Qt.AlignCenter)
        note_lbl.setStyleSheet(
            f"QLabel {{ color:{_PANEL_FG}; background-color:transparent;"
            f" font-family:Roboto; font-size:13px; font-weight:bold; }}"
        )
        body.addWidget(note_lbl)

        # Group row
        group_row = QHBoxLayout()
        group_row.setSpacing(8)
        self.group_combo = make_combobox([], list_popup=True)
        self.group_combo.setFixedWidth(180)
        group_row.addLayout(_labeled_col("Group:", self.group_combo))
        group_row.addStretch()
        body.addLayout(group_row)

        # RFI guidance, always shown
        rfi_note = QLabel(_RFI_NOTE)
        rfi_note.setAlignment(Qt.AlignCenter)
        rfi_note.setWordWrap(True)
        rfi_note.setStyleSheet(
            f"QLabel {{ color:{_PANEL_FG}; background-color:transparent;"
            f" font-family:Roboto; font-size:15px; padding:2px 10px 4px 10px; }}"
        )
        body.addWidget(rfi_note)

        # Message label + input
        msg_row = QHBoxLayout()
        msg_lbl = QLabel("Message:")
        msg_lbl.setStyleSheet(
            f"QLabel {{ font-family:Roboto; font-size:13px; font-weight:bold; color:{_PANEL_FG}; }}"
        )
        msg_row.addWidget(msg_lbl)
        msg_row.addStretch()
        self.message_count_label = QLabel()
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

        self.transmit_btn = make_button("Transmit", COLOR_BTN_BLUE)
        connect_single(self.transmit_btn, self._transmit)
        btn_row.addWidget(self.transmit_btn)

        self.cancel_btn = make_button("Cancel", _COL_CANCEL)
        self.cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(self.cancel_btn)

        body.addLayout(btn_row)

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

    def _get_all_groups_from_db(self) -> list:
        try:
            with db_connect() as conn:
                return [r[0] for r in conn.execute("SELECT name FROM groups ORDER BY name").fetchall()]
        except sqlite3.Error as e:
            print(f"Error reading groups from database: {e}")
        return []

    # -------------------------------------------------------------------------
    # Signal handlers
    # -------------------------------------------------------------------------

    def _on_help_clicked(self) -> None:
        """Explain Requests for Information and how they are tracked."""
        show_help_dialog(self, "Request for Information Help", _HELP_HTML, width=520)

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
    # Validation / messaging helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def _clean_message(text: str) -> str:
        """Encode newlines as "||" (decoded back to newlines in the detail view),
        then replace any remaining non-printable/non-ASCII runs with a space."""
        text = text.replace('\r\n', NEWLINE_PLACEHOLDER).replace('\n', NEWLINE_PLACEHOLDER).replace('\r', NEWLINE_PLACEHOLDER)
        return re.sub(r"[^ -~]+", " ", text)

    def _validate_input(self) -> Optional[str]:
        if not self.group_combo.currentText():
            show_error(self, "Please select a Group")
            self.group_combo.setFocus()
            return None

        # Drop leading/trailing blank lines and spaces the user typed
        message = self._clean_message(self.message_edit.toPlainText().strip()).strip()

        if len(message) < MIN_MESSAGE_LENGTH:
            show_error(self, "Message too short")
            return None

        if self._is_reply:
            message = f"{self._reply_tag or 'RFI'} Reply - {message}"
        return message

    # -------------------------------------------------------------------------
    # Commsrvr / database
    # -------------------------------------------------------------------------

    def _capture_save_data(self, callsign: str, message: str) -> dict:
        """Snapshot Qt widget state on the main thread before a background submit."""
        now = QDateTime.currentDateTime()
        return {
            'callsign': callsign,
            'message': message,
            'datetime_str': now.toUTC().toString("yyyy-MM-dd HH:mm:ss"),
            'date_only': now.toUTC().toString("yyyy-MM-dd"),
            'target': "@" + self.group_combo.currentText(),
            'msg_id': self.msg_id,
            'rfi': 2 if self._is_reply else 1,
        }

    def _save_to_database(self, saved_data: dict, global_id: int) -> None:
        """Save the RFI (Internet Only, source 3) with the server's global ID."""
        with db_connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO messages "
                "(global_id, datetime, date, freq, db, source, msg_id, from_callsign, target, message, rfi) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (global_id, saved_data['datetime_str'], saved_data['date_only'], 0, 30,
                 3, saved_data['msg_id'], saved_data['callsign'],
                 saved_data['target'], saved_data['message'], saved_data['rfi'])
            )
            conn.commit()

    # -------------------------------------------------------------------------
    # Actions
    # -------------------------------------------------------------------------

    def _transmit(self) -> None:
        callsign = get_internet_user_settings()[0]
        if not callsign:
            show_error(self,
                "No callsign configured.\n\nPlease set your callsign in Settings → User Settings."
            )
            return

        import netguard
        if not netguard.is_network_enabled():
            show_error(self,
                "Cannot transmit — Off-Grid Mode is enabled.\n\n"
                "Switch to Online mode to transmit via the Internet."
            )
            return

        message = self._validate_input()
        if message is None:
            return

        self.msg_id = generate_time_based_id()
        self._pending_save_data = self._capture_save_data(callsign, message)
        marker = "-" if self._is_reply else "+"
        message_data = (
            f"{callsign}: {self._pending_save_data['target']}"
            f" MSG ,{self.msg_id},{message},{{^%3}}{marker}"
        )

        def _on_commsrvr_complete(global_id: int) -> None:
            # Save only once the server has assigned the global ID; on failure the
            # delivery-failure popup is shown and the dialog stays open to retry.
            if not global_id:
                return
            saved = self._pending_save_data
            if not self._is_reply:
                saved['message'] = f"RFI {global_id} - {saved['message']}"
            self._save_to_database(saved, global_id)
            if self.refresh_callback:
                self.refresh_callback()
            self.accept()

        submit_to_commsrvr(self, 0, callsign, message_data,
                           self._pending_save_data['datetime_str'],
                           on_complete=_on_commsrvr_complete)
