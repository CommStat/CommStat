# Copyright (c) 2025, 2026 Manuel Ochoa
# This file is part of CommStat.
# Licensed under the GNU General Public License v3.0.
# AI Assistance: Claude (Anthropic), ChatGPT (OpenAI)

"""
JS8 Direct Message Dialog for CommStat
Point-to-point JS8 message to a single callsign, sent directly or via a relay
that has been observed hearing the target.
"""

import os
import re
import sqlite3
from datetime import datetime, timezone
from html import escape
from typing import TYPE_CHECKING

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import Qt, QDateTime
from PyQt5.QtGui import QStandardItem, QStandardItemModel
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QComboBox, QPlainTextEdit,
    QMessageBox,
)

from constants import (
    DEFAULT_COLORS,
    COLOR_INPUT_TEXT, COLOR_INPUT_BORDER,
    COLOR_DISABLED_BG, COLOR_DISABLED_TEXT,
    COLOR_BTN_BLUE, COLOR_BTN_RED, COLOR_BTN_CYAN,
    COLOR_BTN_HELP, COLOR_BTN_CLOSE,
    CONTACTS_RETENTION_HOURS,
)
from id_utils import generate_time_based_id
from qrz_client import get_qrz_cached
from ui_helpers import (make_button, apply_standard_dialog_chrome, connect_single,
                        show_help_dialog)

if TYPE_CHECKING:
    from js8_tcp_client import TCPConnectionPool
    from connector_manager import ConnectorManager


# =============================================================================
# Constants
# =============================================================================

DATABASE_FILE = "traffic.db3"

MIN_MESSAGE_LENGTH = 1
MAX_MESSAGE_LENGTH = 500

WINDOW_WIDTH  = 600
WINDOW_HEIGHT = 460

QRZ_MISS_TEXT = "Callsign not found in local cache"

NO_RELAY_LABEL = "NO-RELAY"
UNKNOWN_SNR_SENTINEL = -99

# Newlines in the message body are encoded as || for transmission, matching
# the StatRep / Message convention. The receiving side (little_gucci.py
# table display and qrz_lookup MessageDetailDialog) decodes || back to \n.
NEWLINE_PLACEHOLDER = "||"

# Loose JS8 callsign shape — accepts base calls and slash suffixes
# (KO4BIA, K2DHS, W3BFO/P, KO4BIA/QRP). Validates typed entries in the
# Target / Relay combos so the Transmit button only enables on something
# that looks like a callsign.
_CALLSIGN_PATTERN = re.compile(r"^[A-Z0-9/]{3,12}$")

_PROG_BG    = DEFAULT_COLORS.get("program_background",   "#A52A2A")
_PROG_FG    = DEFAULT_COLORS.get("program_foreground",   "#FFFFFF")
_PANEL_BG   = DEFAULT_COLORS.get("module_background",    "#DDDDDD")
_PANEL_FG   = DEFAULT_COLORS.get("module_foreground",    "#000000")
_COL_HELP   = COLOR_BTN_HELP
_COL_CANCEL = COLOR_BTN_CLOSE
_COL_COUNTER = "#444444"  # muted but legible counter text (COLOR_DISABLED_TEXT is too light here)

# ── Help content ──────────────────────────────────────────────────────────────
# Beside the feature it documents. Two columns are a table here rather than a
# pair of QVBoxLayouts; chrome comes from ui_helpers.

_HELP_HTML = f"""
<div style="font-family: Roboto; font-size: 13px; color: #333333;">
<table width="100%" cellspacing="0" cellpadding="0"><tr>
<td width="50%" valign="top" style="padding-right:14px;">

<h3 style="color:#555555;">How It Works</h3>
<p>CommStat continuously monitors the JS8Call live feed and builds a roster of
callsigns that have been recently heard on the air. This dialog lets you pick a
destination from that roster and send a point-to-point JS8 directed message
&mdash; no typing of callsigns required.</p>
<p>Callsigns are removed from the roster if they have not been heard in the past
<b>{CONTACTS_RETENTION_HOURS} hours</b>.</p>

<h3 style="color:#555555;">Target &amp; Relay</h3>
<p><b>Target</b> &mdash; the callsign you want to reach.</p>
<p><b>Relay</b> &mdash; <i>optional.</i> A station that has recently been heard
hearing your Target. Pick a Relay when you can't reach the Target directly
&mdash; CommStat builds the standard JS8 relay payload
(<i>RELAY&gt; TARGET MSG ...</i>) for you. Leave Relay blank to send a
non-relayed transmission straight to the Target.</p>
<p><b>{NO_RELAY_LABEL}</b> &mdash; appears in the Relay list (in bold) whenever
the Target itself has been heard directly. Selecting it has the same effect as
leaving Relay blank &mdash; a direct transmission to the Target.</p>
<p>Both dropdowns are <b>editable</b> &mdash; type a callsign manually when the
station you want isn't in the roster yet.</p>

<h3 style="color:#555555;">Refresh</h3>
<p>Re-queries the contacts roster at the current rig frequency and rebuilds the
Target dropdown. Use it to pick up callsigns that have come on the air since you
opened this dialog. The Relay dropdown is cleared so you re-pick a relay after
the Target.</p>

</td>
<td width="50%" valign="top" style="padding-left:14px;">

<h3 style="color:#555555;">Signal Reports (SNR)</h3>
<p>The number next to each Relay entry is the SNR at which the Relay reported
hearing the Target &mdash; higher is better. An SNR of
<b>{UNKNOWN_SNR_SENTINEL:+d}</b> is a placeholder meaning the SNR is unknown.
This happens when the roster row came from a HEARING report (e.g.,
<i>W3BFO: KC1OSZ HEARING K4KBT NY5V K2DHS</i>), which lists stations but
carries no signal numbers.</p>

<h3 style="color:#555555;">Mode</h3>
<table cellspacing="2" cellpadding="2">
<tr><td><b>Slow</b></td><td>&nbsp;&nbsp;8 WPM</td></tr>
<tr><td><b>Normal</b></td><td>&nbsp;&nbsp;16 WPM</td></tr>
<tr><td><b>Fast</b></td><td>&nbsp;&nbsp;24 WPM</td></tr>
<tr><td><b>Turbo</b></td><td>&nbsp;&nbsp;40 WPM</td></tr>
<tr><td><b>Ultra</b></td><td>&nbsp;&nbsp;60 WPM&nbsp;&nbsp;<b>(Use only for JS8Call 3.0.1 or greater)</b></td></tr>
</table>
<p>Changing Mode here also re-tunes the selected rig.</p>

<h3 style="color:#555555;">Tips</h3>
<ul>
<li>The Rig dropdown only lists radios that are currently connected &mdash;
    <i>INTERNET ONLY</i> is intentionally excluded.</li>
<li>Frequency is read live from the rig and is read-only here; change it in
    JS8Call if you need a different band.</li>
<li>Messages are capped at {MAX_MESSAGE_LENGTH} characters; non-printable
    characters are stripped on transmit.</li>
<li><b>Clear</b> wipes only the message body &mdash; Rig, Mode, Target, and
    Relay are preserved so you can fire off several messages in a row.</li>
</ul>

</td></tr></table>
</div>
"""



class _UpperCaseLineEdit(QtWidgets.QLineEdit):
    """QLineEdit that auto-uppercases typed characters and pasted text.

    Installed as the line edit inside the editable Target / Relay combos via
    QComboBox.setLineEdit(). Uppercasing happens in keyPressEvent (typing)
    and insertFromMimeData (paste / drag-drop), which are the widget-level
    hooks that run before any text-modified signals are emitted.

    Why not a QValidator: on an editable QComboBox with NoInsert, a
    validator that rewrites text inside validate() silently blocks typed
    input until the line edit's text matches an existing item.

    Why not a textChanged / textEdited slot: calling setText() inside a
    signal slot re-enters Qt's signal-dispatch loop and corrupts the C++
    iterator on Linux/Qt5 (target textChanged → setText → relay model.clear
    cascade → relay line-edit textChanged → crash). blockSignals() and
    QTimer.singleShot() do not avoid this.

    An event filter on the line edit was tried as well, but on Windows the
    QLineEdit inside an editable QComboBox does not reliably fire installed
    eventFilters for KeyPress, so typed characters bypass it.
    """

    def keyPressEvent(self, event):
        text = event.text()
        if text and text != text.upper():
            self.insert(text.upper())
            event.accept()
            return
        super().keyPressEvent(event)

    def insertFromMimeData(self, source):
        if source is not None and source.hasText():
            mime = QtCore.QMimeData()
            mime.setText(source.text().upper())
            super().insertFromMimeData(mime)
        else:
            super().insertFromMimeData(source)


class _UpperCaseEventFilter(QtCore.QObject):
    """Intercept key presses on a QPlainTextEdit and force them to uppercase.

    Used by the Message body. Line edits (Target / Relay combos) use
    _UpperCaseLineEdit instead — see that class for the reasoning.

    An event filter runs before the widget processes the key and before any
    signals are emitted, so insertPlainText() here is always called outside
    a signal handler — no re-entry, no crash.
    """

    def eventFilter(self, obj, event):
        if event.type() == QtCore.QEvent.KeyPress and isinstance(obj, QPlainTextEdit):
            text = event.text()
            if text and text != text.upper():
                obj.insertPlainText(text.upper())
                return True                # consume original lowercase event
        return False


# =============================================================================
# JS8 Direct Message Dialog
# =============================================================================

class JS8DirectMessageDialog(QDialog):
    """Send a JS8 directed message to a single callsign, optionally via a relay."""

    def __init__(
        self,
        tcp_pool: "TCPConnectionPool" = None,
        connector_manager: "ConnectorManager" = None,
        refresh_callback=None,
        parent=None
    ):
        super().__init__(parent)
        self.tcp_pool = tcp_pool
        self.connector_manager = connector_manager
        self.refresh_callback = refresh_callback

        self._current_freq_mhz = None
        self._pending_payload = ""

        self.setWindowTitle("JS8 Direct Message")
        self.setMinimumSize(WINDOW_WIDTH, WINDOW_HEIGHT)
        self.resize(WINDOW_WIDTH, WINDOW_HEIGHT)
        self.setWindowFlags(
            Qt.Window |
            Qt.CustomizeWindowHint |
            Qt.WindowTitleHint |
            Qt.WindowCloseButtonHint |
            Qt.WindowMaximizeButtonHint |
            Qt.WindowStaysOnTopHint
        )

        if os.path.exists("radiation-32.png"):
            self.setWindowIcon(QtGui.QIcon("radiation-32.png"))

        self._setup_ui()
        self._load_rigs()
        self._update_transmit_state()

    def set_reply_context(self, callsign: str, body: str = "") -> None:
        """Pre-populate the dialog when opened from a StatRep 'JS8 Reply'.

        Shows the read-only yellow Target reminder box with *callsign* (a hint
        of who is being replied to — the operator still picks the real Target /
        Relay from the live roster) and seeds the message body. The body's
        textChanged handler uppercases and caps it at MAX_MESSAGE_LENGTH.
        """
        cs = (callsign or "").strip().upper()
        self.reply_target_field.setText(cs)
        self.reply_target_label.setVisible(bool(cs))
        self.reply_target_field.setVisible(bool(cs))
        if body:
            self.body.setPlainText(body)   # _on_body_changed normalizes/caps

    # -------------------------------------------------------------------------
    # UI
    # -------------------------------------------------------------------------

    def _setup_ui(self) -> None:
        self.setStyleSheet(
            f"QDialog {{ background-color:{_PANEL_BG}; }}"
            f"QLabel {{ color:{_PANEL_FG}; font-family:Roboto; font-size:13px; }}"
            f"QLineEdit {{ background-color:white; color:{COLOR_INPUT_TEXT};"
            f" border:1px solid {COLOR_INPUT_BORDER}; border-radius:4px; padding:2px 4px;"
            f" font-family:'Kode Mono'; font-size:13px; }}"
            f"QPlainTextEdit {{ background-color:white; color:{COLOR_INPUT_TEXT};"
            f" border:1px solid {COLOR_INPUT_BORDER}; border-radius:4px; padding:4px;"
            f" font-family:'Kode Mono'; font-size:13px; }}"
            f"QComboBox {{ background-color:white; color:{COLOR_INPUT_TEXT};"
            f" border:1px solid {COLOR_INPUT_BORDER}; border-radius:4px; padding:2px 4px;"
            f" font-family:'Kode Mono'; font-size:13px; combobox-popup:0; }}"
            f"QComboBox:disabled {{ background-color:{COLOR_DISABLED_BG}; color:{COLOR_DISABLED_TEXT}; }}"
            f"QComboBox QAbstractItemView {{ background-color:white; color:{COLOR_INPUT_TEXT};"
            f" selection-background-color:#cce5ff; selection-color:#000000; }}"
            f"QComboBox QAbstractItemView::item {{ min-height:22px; padding:0 6px; }}"
        )

        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(15, 15, 15, 15)

        # Title
        title = QLabel("JS8 Direct Message")
        title.setAlignment(Qt.AlignCenter)
        title.setFont(QtGui.QFont("Roboto Slab", -1, QtGui.QFont.Black))
        title.setFixedHeight(36)
        title.setStyleSheet(
            f"QLabel {{ background-color:{_PROG_BG}; color:{_PROG_FG};"
            f" font-family:'Roboto Slab'; font-size:16px; font-weight:900;"
            f" padding-top:9px; padding-bottom:9px; }}"
        )
        layout.addWidget(title)
        layout.addSpacing(4)

        # Row 1: Rig / Mode / Freq
        rig_row = QHBoxLayout()
        rig_row.setSpacing(10)

        self.rig_combo = QComboBox()
        self.rig_combo.setMinimumWidth(150)
        self.rig_combo.setMaxVisibleItems(30)
        self.rig_combo.setItemDelegate(QtWidgets.QStyledItemDelegate(self.rig_combo))
        self.rig_combo.currentTextChanged.connect(self._on_rig_changed)
        rig_row.addLayout(self._labeled_col("Rig:", self.rig_combo))

        self.mode_combo = QComboBox()
        self.mode_combo.setFixedWidth(130)
        self.mode_combo.setMaxVisibleItems(30)
        self.mode_combo.setItemDelegate(QtWidgets.QStyledItemDelegate(self.mode_combo))
        self.mode_combo.addItem("Slow",   4)
        self.mode_combo.addItem("Normal", 0)
        self.mode_combo.addItem("Fast",   1)
        self.mode_combo.addItem("Turbo",  2)
        self.mode_combo.addItem("Ultra",  8)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        rig_row.addLayout(self._labeled_col("Mode:", self.mode_combo))

        self.freq_field = QLineEdit()
        self.freq_field.setFixedWidth(90)
        self.freq_field.setReadOnly(True)
        rig_row.addLayout(self._labeled_col("Freq:", self.freq_field))

        # Read-only "Target:" reminder box — shown only when this dialog is
        # opened via the StatRep "JS8 Reply" button (see set_reply_context).
        # Built inline (not via _labeled_col) so both label and field can be
        # toggled. Hidden by default so a normal menu-launch shows nothing.
        self.reply_target_label = QLabel("Target:")
        self.reply_target_label.setStyleSheet(
            "QLabel { font-family:Roboto; font-size:13px; font-weight:bold; }"
        )
        self.reply_target_field = QLineEdit()
        self.reply_target_field.setFixedWidth(110)
        self.reply_target_field.setReadOnly(True)
        self.reply_target_field.setStyleSheet(
            "QLineEdit { background-color:#FFFF00; color:#000000;"
            " border:1px solid #999999; border-radius:4px; padding:2px 4px;"
            " font-family:'Kode Mono'; font-size:13px; }"
        )
        rt_col = QVBoxLayout()
        rt_col.setSpacing(2)
        rt_col.addWidget(self.reply_target_label)
        rt_col.addWidget(self.reply_target_field)
        rig_row.addLayout(rt_col)
        self.reply_target_label.setVisible(False)
        self.reply_target_field.setVisible(False)

        rig_row.addStretch()
        layout.addLayout(rig_row)

        # Row 2: Target / Relay
        sta_row = QHBoxLayout()
        sta_row.setSpacing(10)

        self.target_combo = QComboBox()
        self.target_combo.setFixedWidth(150)
        self.target_combo.setEditable(True)
        self.target_combo.setInsertPolicy(QComboBox.NoInsert)
        self.target_combo.setCompleter(None)
        self.target_combo.setMaxVisibleItems(30)
        self.target_combo.setItemDelegate(QtWidgets.QStyledItemDelegate(self.target_combo))
        self._wire_uppercase(self.target_combo)  # replaces the line edit; connect line-edit signals AFTER this
        self.target_combo.currentTextChanged.connect(self._on_target_changed)
        self.target_combo.activated.connect(lambda _: self._on_target_committed())
        self.target_combo.lineEdit().editingFinished.connect(self._on_target_committed)
        sta_row.addLayout(self._labeled_col("Target:", self.target_combo))

        self.relay_combo = QComboBox()
        self.relay_combo.setFixedWidth(220)
        self.relay_combo.setEditable(True)
        self.relay_combo.setInsertPolicy(QComboBox.NoInsert)
        self.relay_combo.setCompleter(None)
        self.relay_combo.setMaxVisibleItems(30)
        self.relay_combo.setItemDelegate(QtWidgets.QStyledItemDelegate(self.relay_combo))
        self.relay_combo.setModel(QStandardItemModel(self.relay_combo))
        self.relay_combo.currentTextChanged.connect(lambda _t: self._update_transmit_state())
        self._wire_uppercase(self.relay_combo)
        sta_row.addLayout(self._labeled_col("Relay:", self.relay_combo))

        self.btn_refresh = make_button("Refresh", COLOR_BTN_CYAN, min_w=90)
        self.btn_refresh.clicked.connect(self._on_refresh_targets)
        self._refresh_btn_style = self.btn_refresh.styleSheet()
        sta_row.addLayout(self._labeled_col(" ", self.btn_refresh))

        sta_row.addStretch()
        layout.addLayout(sta_row)

        # QRZ info for selected Target (cache-only lookup)
        self.qrz_info_label = QLabel("")
        self.qrz_info_label.setFixedHeight(22)
        self.qrz_info_label.setTextFormat(Qt.PlainText)
        self.qrz_info_label.setStyleSheet(
            "QLabel { background-color:transparent; color:#000000;"
            " padding-left:2px; font-size:13px; }"
        )
        layout.addWidget(self.qrz_info_label)

        # Message body (~4 visual rows)
        msg_row = QHBoxLayout()
        msg_label = QLabel("Message:")
        msg_label.setStyleSheet("QLabel { font-family:Roboto; font-size:13px; font-weight:bold; }")
        msg_row.addWidget(msg_label)
        msg_row.addStretch()
        self.message_count_label = QLabel()
        self.message_count_label.setStyleSheet(
            "QLabel { font-family:'Kode Mono'; font-size:13px; }"
        )
        msg_row.addWidget(self.message_count_label)
        layout.addLayout(msg_row)

        self.body = QPlainTextEdit()
        self.body.setMinimumHeight(144)
        self.body.setPlaceholderText(f"{MAX_MESSAGE_LENGTH} characters max")
        self.body.installEventFilter(_UpperCaseEventFilter(self.body))
        self.body.textChanged.connect(self._on_body_changed)
        layout.addWidget(self.body, 1)  # stretch factor — absorbs extra vertical space
        self._update_message_count_label()

        # Buttons: Help (left) · Clear · Transmit · Cancel (right)
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self.btn_help = make_button("Help", _COL_HELP, min_w=90)
        self.btn_help.clicked.connect(self._on_help_clicked)
        btn_row.addWidget(self.btn_help)

        btn_row.addStretch()

        self.btn_clear = make_button("Clear", COLOR_BTN_CYAN, min_w=100)
        self.btn_clear.clicked.connect(self._on_clear)
        btn_row.addWidget(self.btn_clear)

        self.btn_transmit = make_button("Transmit", COLOR_BTN_BLUE, min_w=100)
        connect_single(self.btn_transmit, self._on_transmit)
        btn_row.addWidget(self.btn_transmit)

        self.btn_cancel = make_button("Cancel", COLOR_BTN_RED, min_w=100)
        self.btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(self.btn_cancel)

        layout.addLayout(btn_row)

    def _labeled_col(self, lbl_text: str, ctrl) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setSpacing(2)
        lbl = QLabel(lbl_text)
        lbl.setStyleSheet("QLabel { font-family:Roboto; font-size:13px; font-weight:bold; }")
        col.addWidget(lbl)
        col.addWidget(ctrl)
        return col

    @staticmethod
    def _wire_uppercase(combo: QComboBox) -> None:
        """Replace the combo's default line edit with _UpperCaseLineEdit so
        typed and pasted text are forced to uppercase.

        Must be called AFTER setEditable(True) and BEFORE wiring any signals
        on combo.lineEdit() — setLineEdit() destroys the previous editor, so
        connections made before this call are lost."""
        combo.setLineEdit(_UpperCaseLineEdit(combo))

    def _effective_target_cs(self) -> str:
        """Target callsign currently in the dropdown — typed or selected."""
        return self.target_combo.currentText().strip().upper()

    def _update_qrz_info(self, target_cs: str) -> None:
        """
        Refresh the QRZ info label for *target_cs* from the local cache.

        Only hits the cache (no network) — keeps every keystroke cheap and
        leaves online lookups to the standalone QRZ Lookup dialog.
        """
        cs = (target_cs or "").strip().upper()
        if not cs:
            self.qrz_info_label.clear()
            return
        if not _CALLSIGN_PATTERN.match(cs):
            self.qrz_info_label.clear()
            return

        # "Last Seen" replaces the QRZ country field — sourced from the
        # contacts roster (insert_date), so it's available even when QRZ
        # has no cache entry for the callsign.
        last_seen = self._last_seen_text(cs)

        cached = get_qrz_cached(cs, include_stale=True)
        name = city = state = ""
        if cached:
            name  = (cached.get("name")  or "").strip()
            city  = (cached.get("city")  or "").strip()
            state = (cached.get("state") or "").strip()

        location_parts = []
        if city and state:
            location_parts.append(f"{city}, {state}")
        elif city:
            location_parts.append(city)
        elif state:
            location_parts.append(state)

        location = ", ".join(location_parts) if location_parts else ""
        info_text = " | ".join(p for p in (name, location) if p)

        if not info_text and not last_seen:
            self.qrz_info_label.setTextFormat(Qt.PlainText)
            self.qrz_info_label.setText(QRZ_MISS_TEXT)
            self.qrz_info_label.setStyleSheet(
                "QLabel { background-color:transparent; color:#000000;"
                " padding-left:2px; font-family:Roboto; font-size:13px;"
                " font-weight:bold; }"
            )
            return

        # Everything renders Kode Mono except the literal words "Last Seen",
        # which are Roboto bold via an inline span (RichText). The elapsed
        # time that follows stays Kode Mono.
        segments = []
        if info_text:
            segments.append(
                f"<span style=\"font-family:'Kode Mono';\">{escape(info_text)}</span>"
            )
        if last_seen:
            if info_text:
                segments.append("<span style=\"font-family:'Kode Mono';\"> | </span>")
            prefix = "Last Seen"
            remainder = last_seen[len(prefix):] if last_seen.startswith(prefix) else ""
            segments.append(
                "<span style=\"font-family:Roboto; font-weight:bold;\">Last Seen</span>"
            )
            if remainder:
                segments.append(
                    f"<span style=\"font-family:'Kode Mono';\">{escape(remainder)}</span>"
                )

        self.qrz_info_label.setTextFormat(Qt.RichText)
        self.qrz_info_label.setText("".join(segments))
        self.qrz_info_label.setStyleSheet(
            "QLabel { background-color:transparent; color:#000000;"
            " padding-left:2px; font-family:'Kode Mono'; font-size:13px; }"
        )

    def _last_seen_text(self, target_cs: str) -> str:
        """Return 'Last Seen <elapsed> ago' for the most recent contacts
        observation of *target_cs*, or '' if the station isn't in the roster.

        insert_date is stored as 'YYYY-MM-DD HH:MM:SS UTC' (see little_gucci
        upsert_contacts_*); its fixed width lets MAX() pick the newest row
        across all relays / frequencies by plain string comparison.
        """
        cs = (target_cs or "").strip().upper()
        if not cs:
            return ""

        insert_date = None
        try:
            with sqlite3.connect(DATABASE_FILE, timeout=10) as conn:
                cur = conn.execute(
                    "SELECT MAX(insert_date) FROM contacts WHERE target_cs = ?",
                    (cs,),
                )
                row = cur.fetchone()
                insert_date = row[0] if row else None
        except sqlite3.Error as e:
            print(f"[JS8DirectMessage] last-seen query failed: {e}")
            return ""

        ago = self._ago_text(insert_date)
        return f"Last Seen {ago}" if ago else ""

    def _ago_text(self, insert_date: str) -> str:
        """Elapsed-since string for a contacts insert_date: 'just now',
        '1h 21m ago', '2d 3h ago', or '' if the timestamp can't be parsed."""
        seen = self._parse_insert_date(insert_date)
        if seen is None:
            return ""
        elapsed = (datetime.now(timezone.utc) - seen).total_seconds()
        if elapsed < 60:
            return "just now"
        return f"{self._format_elapsed(elapsed)} ago"

    @staticmethod
    def _parse_insert_date(value: str):
        """Parse a 'YYYY-MM-DD HH:MM:SS UTC' contacts timestamp into a
        timezone-aware UTC datetime, or None if it can't be parsed."""
        s = (value or "").strip()
        if s.endswith(" UTC"):
            s = s[:-4].strip()
        try:
            dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
        return dt.replace(tzinfo=timezone.utc)

    @staticmethod
    def _format_elapsed(seconds: float) -> str:
        """Compact elapsed string: '45m', '6h 21m', or '2d 3h'."""
        total = max(0, int(seconds))
        days    = total // 86400
        hours   = (total % 86400) // 3600
        minutes = (total % 3600) // 60
        if days > 0:
            return f"{days}d {hours}h"
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"

    def _effective_relay_cs(self) -> str:
        """
        Relay callsign currently in the dropdown.

        Dropdown items store the bare callsign in Qt.UserRole — display text
        may be 'KO4BIA  +05' or the bold 'NO-RELAY' label. For typed entries
        (no UserRole data) we fall back to the first whitespace-delimited
        token of the visible text so the user can paste 'KO4BIA  +05' and
        still have it interpreted correctly.
        """
        data = self.relay_combo.currentData(Qt.UserRole)
        if data:
            return str(data).strip().upper()
        text = self.relay_combo.currentText().strip().upper()
        if not text:
            return ""
        return text.split()[0]

    # -------------------------------------------------------------------------
    # Rig wiring
    # -------------------------------------------------------------------------

    def _load_rigs(self) -> None:
        """Populate rig dropdown with connected radio rigs only (no INTERNET ONLY)."""
        if not self.tcp_pool:
            return

        self.rig_combo.blockSignals(True)
        self.rig_combo.clear()

        enabled = (self.connector_manager.get_all_connectors(enabled_only=True)
                   if self.connector_manager else [])
        connected = set(self.tcp_pool.get_connected_rig_names())
        available = [c['rig_name'] for c in enabled if c['rig_name'] in connected]

        if len(available) == 1:
            self.rig_combo.addItem(available[0])
        elif len(available) > 1:
            self.rig_combo.addItem("")
            for name in available:
                self.rig_combo.addItem(name)

        self.rig_combo.blockSignals(False)

        current = self.rig_combo.currentText()
        if current:
            self._on_rig_changed(current)

    def _on_rig_changed(self, rig_name: str) -> None:
        if not rig_name or not self.tcp_pool:
            self.freq_field.setText("")
            self._current_freq_mhz = None
            self._populate_targets([])
            return

        # Disconnect from prior clients to avoid double-fires
        for client_name in self.tcp_pool.get_all_rig_names():
            client = self.tcp_pool.get_client(client_name)
            if client:
                try:
                    client.frequency_received.disconnect(self._on_frequency_received)
                except TypeError:
                    pass

        client = self.tcp_pool.get_client(rig_name)
        if not (client and client.is_connected()):
            self.freq_field.setText("")
            self._current_freq_mhz = None
            self._populate_targets([])
            self._update_transmit_state()
            return

        client.frequency_received.connect(self._on_frequency_received)

        # Sync mode dropdown from rig's current speed
        speed_name = (client.speed_name or "").upper()
        mode_map = {"SLOW": 0, "NORMAL": 1, "FAST": 2, "TURBO": 3, "ULTRA": 4}
        idx = mode_map.get(speed_name, 1)
        self.mode_combo.blockSignals(True)
        self.mode_combo.setCurrentIndex(idx)
        self.mode_combo.blockSignals(False)

        # Use cached freq if available, then request a fresh one
        freq = client.frequency
        if freq:
            self.freq_field.setText(f"{freq:.3f}")
            self._current_freq_mhz = float(freq)
            self._load_targets(self._current_freq_mhz)
        else:
            self.freq_field.setText("")
            self._current_freq_mhz = None
            self._populate_targets([])

        client.get_frequency()
        self._update_transmit_state()

    def _on_frequency_received(self, rig_name: str, dial_freq: int) -> None:
        if self.rig_combo.currentText() != rig_name:
            return
        freq_mhz = dial_freq / 1_000_000
        self.freq_field.setText(f"{freq_mhz:.3f}")
        # JS8Call re-broadcasts RIG.FREQ roughly once a second even when the
        # radio hasn't moved. Only rebuild the Target list (which clears the
        # combo) when the frequency has actually changed — otherwise the
        # periodic poll wipes the user's current selection. Use Refresh to
        # pick up newly-heard stations at the same frequency.
        if self._current_freq_mhz is not None and abs(freq_mhz - self._current_freq_mhz) < 1e-6:
            return
        self._current_freq_mhz = float(freq_mhz)
        self._load_targets(self._current_freq_mhz)

    def _on_mode_changed(self, _index: int) -> None:
        rig_name = self.rig_combo.currentText()
        if not rig_name or not self.tcp_pool:
            return
        client = self.tcp_pool.get_client(rig_name)
        if client and client.is_connected():
            speed_value = self.mode_combo.currentData()
            client.send_message("MODE.SET_SPEED", "", {"SPEED": speed_value})

    # -------------------------------------------------------------------------
    # Contacts queries
    # -------------------------------------------------------------------------

    def _load_targets(self, freq_mhz: float) -> None:
        rows = []
        try:
            with sqlite3.connect(DATABASE_FILE, timeout=10) as conn:
                cur = conn.execute(
                    "SELECT DISTINCT target_cs FROM contacts "
                    "WHERE freq = ? ORDER BY target_cs",
                    (freq_mhz,),
                )
                rows = [r[0] for r in cur.fetchall() if r[0]]
        except sqlite3.Error as e:
            print(f"[JS8DirectMessage] target query failed: {e}")
        self._populate_targets(rows)

    def _populate_targets(self, target_list) -> None:
        self.target_combo.blockSignals(True)
        self.target_combo.clear()
        self.target_combo.addItem("")
        for cs in target_list:
            self.target_combo.addItem(cs)
        self.target_combo.blockSignals(False)
        self._populate_relays([], "")
        self._update_transmit_state()

    def _on_target_changed(self, _target_cs: str) -> None:
        """Called on every keystroke. Clears stale relay/QRZ state only."""
        self.qrz_info_label.clear()
        self._populate_relays([], "")
        self._update_transmit_state()

    def _on_target_committed(self) -> None:
        """Run QRZ lookup and relay query when the user finishes typing.

        Connected to editingFinished (focus loss / Enter) and activated
        (dropdown selection) so the DB is only hit once per completed entry,
        not on every keystroke.
        """
        target_cs = self._effective_target_cs()
        self._update_qrz_info(target_cs)
        if not target_cs or self._current_freq_mhz is None:
            self._populate_relays([], "")
            return

        rows = []
        try:
            with sqlite3.connect(DATABASE_FILE, timeout=10) as conn:
                cur = conn.execute(
                    "SELECT relay_cs, target_snr, insert_date FROM contacts "
                    "WHERE target_cs = ? AND freq = ? "
                    "ORDER BY target_snr DESC",
                    (target_cs, self._current_freq_mhz),
                )
                rows = cur.fetchall()
        except sqlite3.Error as e:
            print(f"[JS8DirectMessage] relay query failed: {e}")

        self._populate_relays(rows, target_cs)

    def _populate_relays(self, rows, target_cs: str) -> None:
        model: QStandardItemModel = self.relay_combo.model()
        self.relay_combo.blockSignals(True)
        model.clear()

        blank = QStandardItem("")
        blank.setData("", Qt.UserRole)
        model.appendRow(blank)

        for relay_cs, target_snr, insert_date in rows:
            if not relay_cs:
                continue
            try:
                snr_int = int(target_snr)
            except (TypeError, ValueError):
                snr_int = 0

            ago = self._ago_text(insert_date)
            suffix = f" | {ago}" if ago else ""

            if relay_cs == target_cs:
                item = QStandardItem(f"{NO_RELAY_LABEL}  {snr_int:+d}{suffix}")
                f = item.font()
                f.setBold(True)
                item.setFont(f)
            else:
                item = QStandardItem(f"{relay_cs}  {snr_int:+d}{suffix}")
            item.setData(relay_cs, Qt.UserRole)
            model.appendRow(item)

        self.relay_combo.setCurrentIndex(0)
        self.relay_combo.blockSignals(False)
        self._update_transmit_state()

    # -------------------------------------------------------------------------
    # Transmit state + actions
    # -------------------------------------------------------------------------

    def _on_body_changed(self) -> None:
        """Force body text to uppercase, cap length at MAX_MESSAGE_LENGTH
        (handles paste in both cases), then refresh transmit state."""
        text = self.body.toPlainText()
        normalized = text.upper()
        if len(normalized) > MAX_MESSAGE_LENGTH:
            normalized = normalized[:MAX_MESSAGE_LENGTH]
        if text != normalized:
            cursor = self.body.textCursor()
            pos = cursor.position()
            self.body.blockSignals(True)
            self.body.setPlainText(normalized)
            self.body.blockSignals(False)
            cursor = self.body.textCursor()
            cursor.setPosition(min(pos, len(normalized)))
            self.body.setTextCursor(cursor)
        self._update_message_count_label(len(normalized))
        self._update_transmit_state()

    def _update_message_count_label(self, count: int = None) -> None:
        """Refresh the 'N of MAX' counter next to the Message label."""
        if not hasattr(self, 'message_count_label'):
            return
        if count is None:
            count = len(self.body.toPlainText())
        self.message_count_label.setText(f"{count} of {MAX_MESSAGE_LENGTH}")
        color = COLOR_BTN_RED if count >= MAX_MESSAGE_LENGTH else _COL_COUNTER
        self.message_count_label.setStyleSheet(
            f"QLabel {{ font-family:'Kode Mono'; font-size:13px; color:{color}; }}"
        )

    def _update_transmit_state(self) -> None:
        target_ok = bool(_CALLSIGN_PATTERN.match(self._effective_target_cs()))
        relay_cs = self._effective_relay_cs()
        relay_ok = (not relay_cs) or bool(_CALLSIGN_PATTERN.match(relay_cs))
        body_ok = bool(self.body.toPlainText().strip())
        rig_ok = self._rig_client_connected()
        self.btn_transmit.setEnabled(target_ok and relay_ok and body_ok and rig_ok)

    def _rig_client_connected(self) -> bool:
        if not self.tcp_pool:
            return False
        rig_name = self.rig_combo.currentText()
        if not rig_name:
            return False
        client = self.tcp_pool.get_client(rig_name)
        return bool(client and client.is_connected())

    def _show_error(self, message: str) -> None:
        msg = QMessageBox(self)
        msg.setWindowTitle("JS8 Direct Message")
        msg.setText(message)
        msg.setIcon(QMessageBox.Critical)
        msg.setWindowFlag(Qt.WindowStaysOnTopHint)
        msg.exec_()

    def _on_help_clicked(self) -> None:
        """Show a styled help dialog explaining how JS8 Direct Message works."""
        show_help_dialog(self, "JS8 Direct Message Help", _HELP_HTML, width=900)

    def _on_clear(self) -> None:
        """Clear message body only; preserve rig/mode/target/relay."""
        self.body.clear()

    def _on_refresh_targets(self) -> None:
        """Re-query the contacts table for the current rig frequency and repopulate Target."""
        self.btn_refresh.setEnabled(False)
        self.btn_refresh.setText("Refreshing...")

        if self._current_freq_mhz is None:
            self._populate_targets([])
        else:
            self._load_targets(self._current_freq_mhz)

        self.btn_refresh.setText("Done!")
        self.btn_refresh.setStyleSheet(
            "QPushButton { background-color:#28a745; color:#ffffff; border:none;"
            " padding:6px 14px; border-radius:4px; font-family:Roboto;"
            " font-size:15px; font-weight:bold; }"
        )
        QtCore.QTimer.singleShot(1500, self._restore_refresh_button)

    def _restore_refresh_button(self) -> None:
        self.btn_refresh.setText("Refresh")
        self.btn_refresh.setStyleSheet(self._refresh_btn_style)
        self.btn_refresh.setEnabled(True)

    def _on_transmit(self) -> None:
        if not self._rig_client_connected():
            self._show_error("Cannot transmit: rig is not connected.")
            return

        target = self._effective_target_cs()
        relay_cs = self._effective_relay_cs()
        if not _CALLSIGN_PATTERN.match(target):
            self._show_error("Enter or pick a valid Target callsign.")
            return
        if relay_cs and not _CALLSIGN_PATTERN.match(relay_cs):
            self._show_error("Enter or pick a valid Relay callsign, or leave Relay blank for a direct transmission.")
            return

        raw = self.body.toPlainText().strip()
        encoded = (raw.replace('\r\n', NEWLINE_PLACEHOLDER)
                      .replace('\n',   NEWLINE_PLACEHOLDER)
                      .replace('\r',   NEWLINE_PLACEHOLDER))
        text = re.sub(r"[^ -~]+", " ", encoded).strip()
        if len(text) < MIN_MESSAGE_LENGTH:
            self._show_error("Message is empty.")
            return
        if len(text) > MAX_MESSAGE_LENGTH:
            text = text[:MAX_MESSAGE_LENGTH]
            # Avoid leaving a half-encoded newline (single "|") at the end
            if text.endswith("|") and not text.endswith(NEWLINE_PLACEHOLDER):
                text = text[:-1]

        msg_id = generate_time_based_id()
        direct = (not relay_cs) or (relay_cs == target)
        if direct:
            payload = f"{target} MSG ,{msg_id},{text},{{^%}}"
        else:
            payload = f"{relay_cs}> {target} MSG ,{msg_id},{text},{{^%}}"

        # Stash transmit context so the JS8Call selection check can complete
        # the send when the response arrives.
        rig_name = self.rig_combo.currentText()
        self._pending_payload      = payload
        self._pending_msg_id       = msg_id
        self._pending_target       = target
        self._pending_relay        = "(direct)" if direct else relay_cs
        self._pending_rig          = rig_name
        self._pending_message_text = text
        self._pending_freq_mhz     = self._current_freq_mhz

        # Mirror the StatRep / Group Message / Alert pattern: ask JS8Call
        # whether a call is currently selected. If yes, abort with the
        # standard "Deselect" instruction; if no, transmit in the callback.
        client = self.tcp_pool.get_client(rig_name)
        try:
            client.call_selected_received.disconnect(self._on_call_selected_for_transmit)
        except TypeError:
            pass
        client.call_selected_received.connect(self._on_call_selected_for_transmit)
        client.get_call_selected()

    def _refresh_and_close(self) -> None:
        if self.refresh_callback:
            self.refresh_callback()
        self.accept()

    def _save_to_local_messages(self, from_callsign: str) -> None:
        """Write the just-transmitted direct message to the local messages
        table so it shows up in the message log, mirroring group_message.py.

        This dialog is RF-only (the Rig dropdown excludes INTERNET ONLY), so
        source is always 1 (Radio). The recipient callsign is stored in the
        target column — matching how received direct messages are stored (see
        little_gucci.py _parse_message) — and our own station callsign in
        from_callsign. db follows the group_message convention of 30 for a
        locally-originated message. The message body retains its ||-encoded
        newlines; the table display decodes them back to \\n.

        No commsrvr submission happens for this dialog, so global_id is
        always 0.
        """
        now = QDateTime.currentDateTimeUtc()
        datetime_str = now.toString("yyyy-MM-dd HH:mm:ss")
        date_only    = now.toString("yyyy-MM-dd")
        freq_hz = int(round(self._pending_freq_mhz * 1_000_000)) if self._pending_freq_mhz else 0
        from_cs = (from_callsign or "").split("/")[0].strip().upper()

        try:
            with sqlite3.connect(DATABASE_FILE, timeout=10) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO messages "
                    "(global_id, datetime, date, freq, db, source, msg_id, from_callsign, target, message) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (0, datetime_str, date_only, freq_hz, 30, 1,
                     self._pending_msg_id, from_cs,
                     self._pending_target, self._pending_message_text)
                )
                conn.commit()
        except sqlite3.Error as e:
            print(f"[JS8DirectMessage] failed to save message to local table: {e}")

    def _on_call_selected_for_transmit(self, rig_name: str, selected_call: str) -> None:
        """JS8Call response to RX.GET_CALL_SELECTED — proceed only if nothing
        is selected over there. Matches the pattern used by statrep.py,
        group_message.py, and alert.py."""
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

        if not client:
            return

        try:
            client.send_tx_message(self._pending_payload)

            now = QDateTime.currentDateTimeUtc().toString("yyyy-MM-dd HH:mm:ss")
            print(f"\n{'='*60}")
            print(f"RF DIRECT MESSAGE TRANSMITTED - {now} UTC")
            print(f"{'='*60}")
            print(f"  Rig:      {self._pending_rig}")
            print(f"  Target:   {self._pending_target}")
            print(f"  Relay:    {self._pending_relay}")
            print(f"  Msg ID:   {self._pending_msg_id}")
            print(f"  Full TX:  {self._pending_payload}")
            print(f"{'='*60}\n")

            self._save_to_local_messages(client.callsign)
            QtCore.QTimer.singleShot(0, self._refresh_and_close)
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

    dialog = JS8DirectMessageDialog(tcp_pool, connector_manager)
    dialog.show()
    sys.exit(app.exec_())
