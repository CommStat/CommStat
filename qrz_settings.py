# Copyright (c) 2025, 2026 Manuel Ochoa
# This file is part of CommStat.
# Licensed under the GNU General Public License v3.0.
"""
qrz_settings.py - QRZ Settings Management Dialog

Displays QRZ.com credentials in a single-row table with Add, Edit, Delete,
and Test actions.  At most one entry is allowed.
Editing is done inline directly in the table row.

Note: The QRZ password is intentionally shown in plaintext in both the table
and the inline edit field. This is by design — CommStat is a single-user
desktop app and the user has explicitly chosen visibility over masking.
"""

import os
import threading
import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET
from typing import Optional, Tuple

from PyQt5 import QtGui, QtWidgets
from PyQt5.QtCore import Qt, QObject, pyqtSignal
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QComboBox,
    QTableWidget, QTableWidgetItem,
    QHeaderView, QMessageBox, QAbstractItemView, QWidget,
)

from constants import DEFAULT_COLORS
from qrz_client import reset_subscription_status
from ui_helpers import make_button, make_input, confirm, apply_standard_dialog_chrome

# ── Constants ──────────────────────────────────────────────────────────────────

_PROG_BG  = DEFAULT_COLORS.get("program_background",   "#A52A2A")
_PROG_FG  = DEFAULT_COLORS.get("program_foreground",   "#FFFFFF")
_PANEL_BG = DEFAULT_COLORS.get("module_background",    "#DDDDDD")
_PANEL_FG = DEFAULT_COLORS.get("module_foreground",    "#000000")
_TITLE_BG = DEFAULT_COLORS.get("title_bar_background", "#F07800")
_TITLE_FG = DEFAULT_COLORS.get("title_bar_foreground", "#FFFFFF")
_DATA_BG  = DEFAULT_COLORS.get("data_background",      "#F8F6F4")
_DATA_FG  = DEFAULT_COLORS.get("data_foreground",      "#000000")

_COL_ADD    = "#28a745"
_COL_EDIT   = "#007bff"
_COL_DELETE = "#dc3545"
_COL_TEST   = "#17a2b8"
_COL_CLOSE  = "#555555"
_COL_SAVE   = "#28a745"
_COL_CANCEL = "#555555"

_WIN_W = 520
_WIN_H = 345

_QRZ_API_URL = "https://xmldata.qrz.com/xml/current/"

_TABLE_COLS = ["Username", "Password", "Enabled"]


# ── Helpers ────────────────────────────────────────────────────────────────────

def _test_qrz_credentials(username: str, password: str) -> Tuple[bool, str]:
    """
    Attempt a QRZ XML API login and return (success, message).

    Distinguishes between network errors, credential errors, and subscription errors
    so the user gets a meaningful explanation rather than a generic failure.
    """
    if not username or not password:
        return False, "Username and password are required."

    import netguard
    if not netguard.guard("QRZ credential test"):
        return False, "Off-Grid Mode is enabled — switch back to ONLINE to test QRZ credentials."

    params = urllib.parse.urlencode({
        "username": username,
        "password": password,
        "agent": "CommStat/2.5"
    })
    url = _QRZ_API_URL + "?" + params

    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            xml_data = response.read().decode("utf-8")
    except urllib.error.URLError as e:
        reason = str(getattr(e, "reason", e))
        return False, (
            f"Could not reach QRZ.com.\n\n"
            f"Check your internet connection.\n\nDetail: {reason}"
        )
    except OSError as e:
        return False, f"Network error: {e}"

    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError:
        return False, "QRZ returned an unexpected response. Try again later."

    ns = {"qrz": "http://xmldata.qrz.com"}
    session = root.find(".//qrz:Session", ns)
    if session is None:
        session = root.find(".//Session")

    if session is None:
        return False, "QRZ returned an unexpected response. Try again later."

    # Success path — Key element present
    key_elem = session.find("qrz:Key", ns)
    if key_elem is None:
        key_elem = session.find("Key")
    if key_elem is not None and key_elem.text:
        sub_exp = session.find("qrz:SubExp", ns)
        if sub_exp is None:
            sub_exp = session.find("SubExp")
        exp_text = (sub_exp.text or "").strip() if sub_exp is not None else ""
        msg = "Login successful!\nQRZ XML connection verified."
        if exp_text.lower() == "non-subscriber":
            msg += ("\nNo XML data subscription: CommStat will use its "
                    "local contact database only.")
        elif exp_text:
            msg += f"\nSubscription expires: {exp_text}"
        return True, msg

    # Error path — parse QRZ error text for a clear message
    error_elem = session.find("qrz:Error", ns)
    if error_elem is None:
        error_elem = session.find("Error")
    raw_error = (error_elem.text or "").strip() if error_elem is not None else ""
    lower_err = raw_error.lower()

    if "not found" in lower_err or "callsign not found" in lower_err:
        return False, (
            "Login failed: username not found.\n"
            "Check that your QRZ.com callsign is entered correctly."
        )
    if "password" in lower_err or "invalid" in lower_err or "incorrect" in lower_err:
        return False, (
            "Login failed: username or password is incorrect.\n"
            "Check your QRZ.com credentials and try again."
        )
    if "subscri" in lower_err:
        return False, (
            "Login failed: no XML data subscription.\n"
            "A QRZ XML Data subscription is required for callsign lookups.\n"
            "Visit qrz.com to subscribe."
        )
    if "session" in lower_err:
        return False, (
            "Login failed: session error from QRZ.\n"
            "Try again in a few seconds."
        )

    if raw_error:
        return False, f"QRZ error: {raw_error}"

    return False, "Login failed for an unknown reason. Try again later."


# ── Signal bridge for thread → GUI ─────────────────────────────────────────────

class _TestSignals(QObject):
    done = pyqtSignal(bool, str)   # (success, message)


# ── Dialog ─────────────────────────────────────────────────────────────────────

class QRZSettingsDialog(QDialog):
    """QRZ Settings dialog — credentials and enable toggle; one entry max."""

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db

        self._in_edit_mode: bool = False
        self._edit_row: int = -1
        self._adding: bool = False
        self._iw_username: Optional[QLineEdit] = None
        self._iw_password: Optional[QLineEdit] = None
        self._iw_enable: Optional[QComboBox] = None

        apply_standard_dialog_chrome(self, "QRZ Subscription Settings", _WIN_W, _WIN_H)

        self._test_signals = _TestSignals()
        self._test_signals.done.connect(self._on_test_done)

        self._setup_ui()
        self._load()

    # ── UI construction ────────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        self.setStyleSheet(
            f"QDialog {{ background-color:{_PANEL_BG}; color:{_PANEL_FG}; }}"
            f"QLabel {{ font-size:13px; color:{_PANEL_FG}; }}"
        )

        body = QVBoxLayout(self)
        body.setContentsMargins(15, 15, 15, 15)
        body.setSpacing(10)

        # ── Title ─────────────────────────────────────────────────────────────
        title_lbl = QLabel("QRZ Subscription Settings")
        title_lbl.setAlignment(Qt.AlignCenter)
        title_lbl.setFont(QtGui.QFont("Roboto Slab", -1, QtGui.QFont.Black))
        title_lbl.setFixedHeight(36)
        title_lbl.setStyleSheet(
            f"QLabel {{ background-color:{_PROG_BG}; color:{_PROG_FG};"
            f" font-family:'Roboto Slab'; font-size:16px; font-weight:900;"
            f" padding-top:9px; padding-bottom:9px; }}"
        )
        body.addWidget(title_lbl)

        # ── Table ─────────────────────────────────────────────────────────────
        self.table = QTableWidget(0, len(_TABLE_COLS))
        self.table.setHorizontalHeaderLabels(_TABLE_COLS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setTabKeyNavigation(False)
        self.table.setShowGrid(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(False)
        self.table.setMinimumHeight(100)
        self.table.setMaximumHeight(120)

        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Interactive)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.Fixed)
        self.table.setColumnWidth(0, 185)
        self.table.setColumnWidth(2, 100)

        self.table.setStyleSheet(
            f"QTableWidget {{ background-color:{_DATA_BG}; alternate-background-color:{_DATA_BG};"
            f" gridline-color:#cccccc; color:{_DATA_FG};"
            f" font-family:'Kode Mono'; font-size:13px; }}"
            f"QTableWidget::item {{ padding:4px 6px; }}"
            f"QHeaderView::section {{ background-color:{_TITLE_BG}; color:{_TITLE_FG};"
            f" padding:5px 6px; border:none; font-family:Roboto; font-size:13px;"
            f" font-weight:bold; }}"
            f"QTableWidget::item:selected {{ background-color:#cce5ff; color:#000000; }}"
        )
        self.table.selectionModel().selectionChanged.connect(self._on_selection_changed)
        self.table.doubleClicked.connect(self._on_edit)
        body.addWidget(self.table)

        # ── Note ──────────────────────────────────────────────────────────────
        note_lbl = QLabel(
            f"<b><span style='color:#FF0000'>Note:</span></b>"
            f" <span style='color:{_PANEL_FG}'>QRZ callsign lookups require a paid"
            f" QRZ XML Data subscription. Any active paid QRZ subscription qualifies.</span>"
        )
        note_lbl.setWordWrap(True)
        note_lbl.setStyleSheet(f"QLabel {{ font-family:Roboto; font-size:13px; }}")
        body.addWidget(note_lbl)

        # ── Action buttons ────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self.btn_add    = make_button("Add",    _COL_ADD,    80)
        self.btn_edit   = make_button("Edit",   _COL_EDIT,   80)
        self.btn_delete = make_button("Delete", _COL_DELETE, 80)
        self.btn_test   = make_button("Test",   _COL_TEST,   80)
        self.btn_save   = make_button("Save",   _COL_SAVE,   80)
        self.btn_cancel = make_button("Cancel", _COL_CANCEL, 80)
        self.btn_close  = make_button("Close",  _COL_CLOSE,  80)

        self.btn_edit.setEnabled(False)
        self.btn_delete.setEnabled(False)
        self.btn_test.setEnabled(False)
        self.btn_save.setVisible(False)
        self.btn_cancel.setVisible(False)
        self.btn_save.setEnabled(False)

        self.btn_add.clicked.connect(self._on_add)
        self.btn_edit.clicked.connect(self._on_edit)
        self.btn_delete.clicked.connect(self._on_delete)
        self.btn_test.clicked.connect(self._on_test)
        self.btn_save.clicked.connect(lambda: self._exit_edit_mode(save=True))
        self.btn_cancel.clicked.connect(lambda: self._exit_edit_mode(save=False))
        self.btn_close.clicked.connect(self.accept)

        btn_row.addWidget(self.btn_add)
        btn_row.addWidget(self.btn_edit)
        btn_row.addWidget(self.btn_delete)
        btn_row.addWidget(self.btn_test)
        btn_row.addWidget(self.btn_save)
        btn_row.addWidget(self.btn_cancel)
        btn_row.addStretch()
        btn_row.addWidget(self.btn_close)
        body.addLayout(btn_row)

        # ── Test status label (shown after a test runs) ────────────────────────
        self.test_status_lbl = QLabel("")
        self.test_status_lbl.setWordWrap(True)
        self.test_status_lbl.setStyleSheet(
            "QLabel { color:#333333; border:1px solid #cccccc; border-radius:4px;"
            " background-color:#f0f0f0; padding:6px; font-family:Roboto; font-size:13px; }"
        )
        self.test_status_lbl.setVisible(False)
        body.addWidget(self.test_status_lbl)

    # ── Data loading ───────────────────────────────────────────────────────────

    def _load(self) -> None:
        self._in_edit_mode = False
        self._edit_row = -1
        username, password, is_active = self.db.get_qrz_settings()
        self.table.setRowCount(0)
        mono = QtGui.QFont("Kode Mono")

        if username:
            self.table.insertRow(0)
            enabled_text = "Yes" if is_active else "No"
            for col, val in enumerate([username, password, enabled_text]):
                item = QTableWidgetItem(val)
                item.setFont(mono)
                item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
                if col == 2:
                    item.setForeground(
                        QtGui.QColor("#1a7f37") if is_active else QtGui.QColor("#cc0000")
                    )
                self.table.setItem(0, col, item)

        self._on_selection_changed()

    # ── Selection ──────────────────────────────────────────────────────────────

    def _on_selection_changed(self) -> None:
        if self._in_edit_mode:
            return
        has_row = self.table.rowCount() > 0
        has_sel = bool(self.table.selectedItems())
        self.btn_add.setEnabled(not has_row)
        self.btn_edit.setEnabled(has_sel)
        self.btn_delete.setEnabled(has_sel)
        self.btn_test.setEnabled(has_sel)

    # ── Inline edit ────────────────────────────────────────────────────────────

    def _enter_edit_mode(self, row: int, adding: bool) -> None:
        self._in_edit_mode = True
        self._edit_row = row
        self._adding = adding

        if adding:
            username, password, enabled = "", "", False
        else:
            db_user, db_pass, db_active = self.db.get_qrz_settings()
            username = db_user or ""
            password = db_pass or ""
            enabled  = bool(db_active)

        self._iw_username = make_input(placeholder="Your Callsign", max_len=20)
        self._iw_username.setText(username)
        self._iw_username.textChanged.connect(
            lambda t: self._iw_username.setText(t.upper()) if t != t.upper() else None
        )
        self._iw_username.textChanged.connect(lambda _: self._on_inline_changed())

        self._iw_password = make_input(placeholder="QRZ.com password", max_len=64)
        self._iw_password.setText(password)
        self._iw_password.textChanged.connect(lambda _: self._on_inline_changed())

        self._iw_enable = QComboBox()
        self._iw_enable.addItems(["Yes", "No"])
        self._iw_enable.setCurrentIndex(0 if enabled else 1)
        self._iw_enable.setStyleSheet(
            "QComboBox { background-color:white; color:#333333;"
            " border:1px solid #cccccc; border-radius:4px; padding:2px 4px;"
            " font-family:'Kode Mono'; font-size:13px; }"
            "QComboBox QAbstractItemView { background-color:white; color:#333333;"
            " selection-background-color:#cce5ff; selection-color:#000000; }"
        )

        # Qt force-stretches a QLineEdit installed via setCellWidget. Wrap the
        # fixed-width username input in a QWidget + HBoxLayout so the container
        # fills the cell while the input keeps its size. Password stays
        # unwrapped — col 1 is Stretch and passwords benefit from the room.
        # Enable combo is unwrapped — combos look fine at column width.
        def _wrap_fixed(input_widget: QLineEdit, width_px: int) -> QWidget:
            input_widget.setFixedWidth(width_px)
            container = QWidget()
            layout = QHBoxLayout(container)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(0)
            layout.addWidget(input_widget)
            layout.addStretch()
            return container

        self.table.setCellWidget(row, 0, _wrap_fixed(self._iw_username, 136))
        self.table.setCellWidget(row, 1, self._iw_password)
        self.table.setCellWidget(row, 2, self._iw_enable)
        self.table.setRowHeight(row, 42)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)

        # Hide Add/Edit/Delete; Test stays visible for inline testing
        self.btn_add.setVisible(False)
        self.btn_edit.setVisible(False)
        self.btn_delete.setVisible(False)
        self.btn_save.setVisible(True)
        self.btn_cancel.setVisible(True)
        self.btn_close.setEnabled(False)

        # Clear any previous test result
        self.test_status_lbl.setText("")
        self.test_status_lbl.setVisible(False)

        QWidget.setTabOrder(self._iw_username, self._iw_password)
        QWidget.setTabOrder(self._iw_password, self._iw_enable)
        QWidget.setTabOrder(self._iw_enable, self.btn_save)

        self._on_inline_changed()
        self._iw_username.setFocus()

    def _on_inline_changed(self) -> None:
        if not self._in_edit_mode:
            return
        has_both = (
            bool(self._iw_username and self._iw_username.text().strip()) and
            bool(self._iw_password and self._iw_password.text())
        )
        self.btn_save.setEnabled(has_both)
        self.btn_test.setEnabled(has_both)

    def _exit_edit_mode(self, save: bool) -> None:
        row = self._edit_row

        if save:
            username  = self._iw_username.text().strip().upper()
            password  = self._iw_password.text()
            is_active = (self._iw_enable.currentText() == "Yes") if self._iw_enable else False

            if not username or not password:
                QMessageBox.warning(self, "QRZ Settings", "Username and password are required.")
                return
            ok = self.db.set_qrz_settings(username, password, is_active)
            if not ok:
                QMessageBox.critical(self, "Error", "Could not save QRZ settings.")
                return
            # New credentials may belong to a different account — re-detect
            # the XML subscription status on the next lookup.
            reset_subscription_status()

        for col in range(3):
            self.table.removeCellWidget(row, col)

        self._iw_username = self._iw_password = self._iw_enable = None
        self._in_edit_mode = False

        self.table.setRowHeight(row, self.table.verticalHeader().defaultSectionSize())
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)

        self.btn_add.setVisible(True)
        self.btn_edit.setVisible(True)
        self.btn_delete.setVisible(True)
        self.btn_save.setVisible(False)
        self.btn_cancel.setVisible(False)
        self.btn_close.setEnabled(True)

        self.test_status_lbl.setText("")
        self.test_status_lbl.setVisible(False)

        self._load()

    # ── Actions ────────────────────────────────────────────────────────────────

    def _on_add(self) -> None:
        if self._in_edit_mode:
            return
        self.table.insertRow(0)
        self._enter_edit_mode(row=0, adding=True)

    def _on_edit(self) -> None:
        if self._in_edit_mode or self.table.rowCount() == 0:
            return
        self._enter_edit_mode(row=0, adding=False)

    def _on_delete(self) -> None:
        if not confirm(self, "Delete QRZ Settings", "Delete QRZ credentials?",
                       no_label="Cancel"):
            return
        self.db.set_qrz_settings("", "", False)
        reset_subscription_status()
        self._load()

    # ── Test ──────────────────────────────────────────────────────────────────

    def _on_test(self) -> None:
        if self._in_edit_mode:
            username = self._iw_username.text().strip().upper() if self._iw_username else ""
            password = self._iw_password.text() if self._iw_password else ""
        else:
            username, password, _ = self.db.get_qrz_settings()

        if not username or not password:
            QMessageBox.warning(self, "Test QRZ", "Username and password are required.")
            return

        self.btn_test.setEnabled(False)
        self.btn_test.setText("Testing...")
        self.test_status_lbl.setStyleSheet(
            "color:#555555; border:1px solid #cccccc; border-radius:4px;"
            " background-color:#f0f0f0; padding:6px; font-family:Roboto; font-size:13px;"
        )
        self.test_status_lbl.setText("Connecting to QRZ.com…")
        self.test_status_lbl.setVisible(True)
        self._run_test_thread(username, password)

    def _run_test_thread(self, username: str, password: str) -> None:
        def worker():
            success, msg = _test_qrz_credentials(username, password)
            self._test_signals.done.emit(success, msg)

        threading.Thread(target=worker, daemon=True).start()

    def _on_test_done(self, success: bool, message: str) -> None:
        self.btn_test.setText("Test")
        color = "#1a7f37" if success else "#AA0000"
        self.test_status_lbl.setStyleSheet(
            f"color:{color}; border:1px solid #cccccc; border-radius:4px;"
            " background-color:#f0f0f0; padding:6px; font-family:Roboto; font-size:13px;"
        )
        self.test_status_lbl.setText(message)
        self.test_status_lbl.setVisible(True)
        if self._in_edit_mode:
            self._on_inline_changed()
        else:
            self._on_selection_changed()
