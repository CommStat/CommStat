# Copyright (c) 2026 Manuel Ochoa
# This file is part of CommStat.
# Licensed under the GNU General Public License v3.0.
"""
watchlist_members.py - Watchlist Members Dialog

Lists the member callsigns of one watchlist with Add, Bulk Add, and Remove
actions. Adding searches the local qrz table first, then the QRZ.com XML
API; a callsign found nowhere can be entered manually. Bulk Add takes a
pasted comma-separated callsign list and resolves each the same way (no
manual fallback — unresolved callsigns are reported). Members render on the
map as persistent objects when their watchlist is checked under
Map -> Watchlist Overlay.

The Grid field stays editable on a search hit: QRZ/FCC location data goes
stale when people move, so a changed grid is saved to qrz.grid_override —
a local-only column that QRZ.com API refreshes never overwrite.
"""

import re

from PyQt5 import QtGui, QtWidgets
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPlainTextEdit, QTableWidget, QTableWidgetItem,
    QHeaderView, QMessageBox, QAbstractItemView, QWidget,
)

from constants import (
    DEFAULT_COLORS,
    COLOR_BTN_GREEN, COLOR_BTN_BLUE, COLOR_BTN_CLOSE,
    COLOR_INPUT_TEXT, COLOR_INPUT_BORDER, FONT_MONO_STACK,
)
from ui_helpers import (
    make_button, make_input, confirm, apply_standard_dialog_chrome,
    make_title_strip, make_checkbox_cell, DIALOG_TABLE_QSS, UpperCaseLineEdit,
)
from qrz_client import QRZClient, get_qrz_cached, load_qrz_config
from gridfinder import format_grid

# ── Constants ──────────────────────────────────────────────────────────────────

_PANEL_BG = DEFAULT_COLORS.get("module_background",    "#DDDDDD")
_PANEL_FG = DEFAULT_COLORS.get("module_foreground",    "#000000")

_COL_ADD    = "#28a745"
_COL_REMOVE = "#dc3545"

_WIN_W = 640
_WIN_H = 480

_TABLE_COLS = ["Callsign", "Name", "City", "State", "Grid"]
_LIST_COLS = ["Active"] + _TABLE_COLS

# QThreads started for a lookup are parked here so closing the dialog
# mid-lookup never destroys a still-running QThread.
_live_threads = set()


# ── Bulk lookup worker ─────────────────────────────────────────────────────────

class _BulkLookupThread(QThread):
    """Resolves a list of callsigns sequentially in the background: local qrz
    cache first (stale rows are valid member identities), then the QRZ.com API
    when the subscription is active. Only lookups happen here — all
    watchlistMembers writes stay on the main thread in _on_bulk_done."""

    progress = pyqtSignal(int, int, str)          # done, total, callsign
    lookup_done = pyqtSignal(list, list, list)    # resolved, failed, api_fetched

    def __init__(self, callsigns, username, password, use_api: bool):
        super().__init__()
        self._callsigns = callsigns
        self._username = username
        self._password = password
        self._use_api = use_api

    def run(self) -> None:
        client = QRZClient(self._username, self._password) if self._use_api else None
        resolved, failed, fetched = [], [], []
        total = len(self._callsigns)
        for done, cs in enumerate(self._callsigns, 1):
            self.progress.emit(done, total, cs)
            row = get_qrz_cached(cs, include_stale=True)
            if row is None and client is not None:
                if client.lookup(cs):
                    fetched.append(cs)
                    row = get_qrz_cached(cs, include_stale=True)
            if row:
                resolved.append(cs)
            else:
                failed.append(cs)
        self.lookup_done.emit(resolved, failed, fetched)


# ── Bulk add failure report ────────────────────────────────────────────────────

class _BulkFailedDialog(QDialog):
    """Styled report of the callsigns a bulk add could not resolve, shown as a
    selectable comma-separated list with a one-click Copy button."""

    def __init__(self, watchlist_name: str, failed, parent=None):
        super().__init__(parent)
        self._failed_text = ", ".join(failed)

        apply_standard_dialog_chrome(self, "Bulk Add", 520, 280)
        self.setStyleSheet(
            f"QDialog {{ background-color:{_PANEL_BG}; color:{_PANEL_FG}; }}"
            f"QLabel {{ font-family:Roboto; font-size:13px; color:{_PANEL_FG}; }}"
        )

        body = QVBoxLayout(self)
        body.setContentsMargins(15, 15, 15, 15)
        body.setSpacing(10)

        title_lbl = make_title_strip("Bulk Add Results")
        body.addWidget(title_lbl)

        msg = QLabel(
            f"{len(failed)} callsign{'s' if len(failed) != 1 else ''} could not"
            f" be added to the {watchlist_name} watchlist:"
        )
        msg.setWordWrap(True)
        msg.setStyleSheet("QLabel { font-family:Roboto; font-size:13px; }")
        body.addWidget(msg)

        self.list_box = QPlainTextEdit()
        self.list_box.setPlainText(self._failed_text)
        self.list_box.setReadOnly(True)
        self.list_box.setStyleSheet(
            f"QPlainTextEdit {{ background-color:white; color:{COLOR_INPUT_TEXT};"
            f" border:1px solid {COLOR_INPUT_BORDER}; border-radius:4px;"
            f" padding:4px 8px; font-family:{FONT_MONO_STACK}; font-size:13px; }}"
        )
        body.addWidget(self.list_box)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self.btn_copy = make_button("Copy List", COLOR_BTN_BLUE, 100)
        self.btn_copy.clicked.connect(self._on_copy)
        btn_close = make_button("Close", COLOR_BTN_CLOSE, 80)
        btn_close.clicked.connect(self.accept)
        btn_row.addWidget(self.btn_copy)
        btn_row.addStretch()
        btn_row.addWidget(btn_close)
        body.addLayout(btn_row)

    def _on_copy(self) -> None:
        QtWidgets.QApplication.clipboard().setText(self._failed_text)
        self.btn_copy.setText("Copied!")


# ── Dialog ─────────────────────────────────────────────────────────────────────

class WatchlistMembersDialog(QDialog):
    """Watchlist Members dialog — list, add, and remove member callsigns for a
    watchlist."""

    def __init__(self, db_manager, watchlist_id: int, watchlist_name: str, parent=None):
        super().__init__(parent)
        self.db = db_manager
        self.watchlist_id = watchlist_id
        self.watchlist_name = watchlist_name

        self._thread = None
        self._bulk_thread = None
        self._pending_cs: str = ""
        self._manual_mode: bool = False
        self._found_fcc_grid: str = ""
        self._found_override: str = ""

        apply_standard_dialog_chrome(
            self, f"{watchlist_name} Watchlist Members", _WIN_W, _WIN_H)

        self._setup_ui()
        self._load()

    # ── UI construction ────────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        self.setStyleSheet(
            f"QDialog {{ background-color:{_PANEL_BG}; color:{_PANEL_FG}; }}"
            f"QLabel {{ font-family:Roboto; font-size:13px; color:{_PANEL_FG}; }}"
        )

        body = QVBoxLayout(self)
        body.setContentsMargins(15, 15, 15, 15)
        body.setSpacing(10)

        # ── Title ─────────────────────────────────────────────────────────────
        title_lbl = make_title_strip(f"{self.watchlist_name} Watchlist Members")
        body.addWidget(title_lbl)

        # ── Add panel (hidden until Add is clicked) ───────────────────────────
        self.add_panel = QWidget()
        panel_layout = QVBoxLayout(self.add_panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        panel_layout.setSpacing(8)

        # Row A: callsign input + Search
        search_row = QHBoxLayout()
        search_row.setSpacing(8)
        search_row.addWidget(QLabel("Callsign:"))

        self.cs_edit = make_input("Callsign", max_len=15, widget=UpperCaseLineEdit())
        self.cs_edit.setFixedWidth(140)
        self.cs_edit.returnPressed.connect(self._on_search)
        search_row.addWidget(self.cs_edit)

        self.btn_search = make_button("Search", COLOR_BTN_BLUE, 80)
        self.btn_search.clicked.connect(self._on_search)
        search_row.addWidget(self.btn_search)

        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet("QLabel { font-family:Roboto; font-size:13px; }")
        search_row.addWidget(self.lbl_status)
        search_row.addStretch()
        panel_layout.addLayout(search_row)

        # Row B: detail fields + Save/Cancel (hidden until a search resolves)
        self.detail_row = QWidget()
        detail_grid = QGridLayout(self.detail_row)
        detail_grid.setContentsMargins(0, 0, 0, 0)
        detail_grid.setHorizontalSpacing(8)
        detail_grid.setVerticalSpacing(2)

        self.f_callsign = make_input(max_len=12)
        self.f_name     = make_input(max_len=40)
        self.f_city     = make_input(max_len=30)
        self.f_state    = make_input(max_len=2)
        self.f_grid     = make_input(max_len=8)
        self.f_grid.setToolTip(
            "Editable — change the grid to override an out-of-date location.\n"
            "The override sticks even when QRZ.com data is refreshed."
        )
        self._fields = [self.f_callsign, self.f_name, self.f_city, self.f_state, self.f_grid]

        self.btn_save_member = make_button("Save", COLOR_BTN_GREEN, 80)
        self.btn_save_member.clicked.connect(self._on_save)
        self.btn_cancel_add = make_button("Cancel", COLOR_BTN_CLOSE, 80)
        self.btn_cancel_add.clicked.connect(self._reset_add_panel)

        for col, (label, field) in enumerate(zip(_TABLE_COLS, self._fields)):
            detail_grid.addWidget(QLabel(label), 0, col)
            detail_grid.addWidget(field, 1, col)
        detail_grid.addWidget(self.btn_save_member, 1, 5)
        detail_grid.addWidget(self.btn_cancel_add, 1, 6)
        detail_grid.setColumnStretch(1, 2)   # Name
        detail_grid.setColumnStretch(2, 2)   # City
        self.detail_row.setVisible(False)
        panel_layout.addWidget(self.detail_row)

        self.add_panel.setVisible(False)
        body.addWidget(self.add_panel)

        # ── Bulk add panel (hidden until Bulk Add is clicked) ─────────────────
        self.bulk_panel = QWidget()
        bulk_layout = QVBoxLayout(self.bulk_panel)
        bulk_layout.setContentsMargins(0, 0, 0, 0)
        bulk_layout.setSpacing(4)

        bulk_row = QHBoxLayout()
        bulk_row.setSpacing(8)
        bulk_row.addWidget(QLabel("Callsigns:"))

        self.bulk_edit = make_input("Paste comma-separated callsigns…", widget=UpperCaseLineEdit())
        self.bulk_edit.returnPressed.connect(self._on_bulk_submit)
        bulk_row.addWidget(self.bulk_edit, 1)

        self.btn_submit = make_button("Submit", COLOR_BTN_GREEN, 80)
        self.btn_submit.clicked.connect(self._on_bulk_submit)
        bulk_row.addWidget(self.btn_submit)
        bulk_layout.addLayout(bulk_row)

        self.lbl_bulk_status = QLabel("")
        self.lbl_bulk_status.setStyleSheet("QLabel { font-family:Roboto; font-size:13px; }")
        bulk_layout.addWidget(self.lbl_bulk_status)

        self.bulk_panel.setVisible(False)
        body.addWidget(self.bulk_panel)

        # ── Table ─────────────────────────────────────────────────────────────
        self.table = QTableWidget(0, len(_LIST_COLS))
        self.table.setHorizontalHeaderLabels(_LIST_COLS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setTabKeyNavigation(False)
        self.table.setShowGrid(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(False)

        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)   # Active
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)   # Callsign
        hh.setSectionResizeMode(2, QHeaderView.Stretch)            # Name
        hh.setSectionResizeMode(3, QHeaderView.Stretch)            # City
        hh.setSectionResizeMode(4, QHeaderView.ResizeToContents)   # State
        hh.setSectionResizeMode(5, QHeaderView.ResizeToContents)   # Grid

        self.table.setStyleSheet(DIALOG_TABLE_QSS)
        self.table.selectionModel().selectionChanged.connect(self._on_selection_changed)
        body.addWidget(self.table)

        # ── Action buttons ────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self.btn_add      = make_button("Add",      _COL_ADD,        80)
        self.btn_bulk_add = make_button("Bulk Add", _COL_ADD,        80)
        self.btn_remove   = make_button("Remove",   _COL_REMOVE,     80)
        self.btn_close    = make_button("Close",    COLOR_BTN_CLOSE, 80)

        self.btn_remove.setEnabled(False)

        self.btn_add.clicked.connect(self._on_add)
        self.btn_bulk_add.clicked.connect(self._on_bulk_add)
        self.btn_remove.clicked.connect(self._on_remove)
        self.btn_close.clicked.connect(self.accept)

        btn_row.addWidget(self.btn_add)
        btn_row.addWidget(self.btn_bulk_add)
        btn_row.addWidget(self.btn_remove)
        btn_row.addStretch()
        btn_row.addWidget(self.btn_close)
        body.addLayout(btn_row)

    # ── Data loading ───────────────────────────────────────────────────────────

    def _load(self) -> None:
        members = self.db.get_watchlist_members(self.watchlist_id)
        self.table.setRowCount(0)
        mono = QtGui.QFont("Kode Mono")

        for m in members:
            row = self.table.rowCount()
            self.table.insertRow(row)

            active_cell, active_cb = make_checkbox_cell(m["active"])
            active_cb.toggled.connect(
                lambda checked, member_id=m["id"]: self._on_active_toggled(member_id, checked)
            )
            self.table.setCellWidget(row, 0, active_cell)

            values = [m["callsign"], m["name"], m["city"], m["state"], m["grid"]]
            for col, val in enumerate(values):
                item = QTableWidgetItem(val)
                item.setFont(mono)
                item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
                if col == 0:
                    item.setData(Qt.UserRole, m["id"])
                self.table.setItem(row, col + 1, item)

        self._on_selection_changed()

    def _on_active_toggled(self, member_id: int, checked: bool) -> None:
        self.db.set_watchlist_member_active(member_id, checked)

    # ── Selection ──────────────────────────────────────────────────────────────

    def _on_selection_changed(self) -> None:
        self.btn_remove.setEnabled(bool(self.table.selectedItems()))

    # ── Add / search flow ──────────────────────────────────────────────────────

    def _on_add(self) -> None:
        self.bulk_panel.setVisible(False)
        self.add_panel.setVisible(True)
        self._reset_add_panel()

    def _reset_add_panel(self) -> None:
        """Clear the detail fields and return to the callsign+Search row."""
        self._manual_mode = False
        self._found_fcc_grid = ""
        self._found_override = ""
        self.detail_row.setVisible(False)
        for field in self._fields:
            field.clear()
        self.lbl_status.setText("")
        self.cs_edit.clear()
        self.cs_edit.setFocus()

    def _on_search(self) -> None:
        if self._thread is not None and self._thread.isRunning():
            return
        cs = self.cs_edit.text().strip().upper()
        if not cs:
            return
        # Captured before the thread starts; the result slot never re-reads
        # the widget for identity.
        self._pending_cs = cs
        self.detail_row.setVisible(False)

        # Local database first — stale and manually entered rows are still
        # valid member identities.
        cached = get_qrz_cached(cs, include_stale=True)
        if cached:
            self.lbl_status.setText(f"{cs} found in local database")
            self._show_found(cached)
            return

        is_active, username, password = load_qrz_config()
        if not username or not is_active:
            reason = "not configured" if not username else "not enabled"
            self.lbl_status.setText(f"QRZ Subscription {reason} — enter details manually")
            self._enter_manual_mode(cs)
            return

        self.lbl_status.setText(f"Looking up {cs}…")
        self.btn_search.setEnabled(False)
        from qrz_lookup import _QRZThread
        self._thread = _QRZThread(cs, username, password)
        _live_threads.add(self._thread)
        self._thread.finished.connect(
            lambda t=self._thread: _live_threads.discard(t)
        )
        self._thread.result_ready.connect(self._on_qrz_result)
        self._thread.start()

    def _on_qrz_result(self, result) -> None:
        self.btn_search.setEnabled(True)
        # Ignore the raw API dict's shape — on success lookup() has already
        # normalized and saved the record, so re-read the local cache.
        row = get_qrz_cached(self._pending_cs, include_stale=True)
        if row:
            self.lbl_status.setText(f"{self._pending_cs} found on QRZ.com")
            self._show_found(row)
        else:
            self.lbl_status.setText(
                f"{self._pending_cs} not found — enter details manually"
            )
            self._enter_manual_mode(self._pending_cs)

    def _show_found(self, row: dict) -> None:
        """Populate the detail fields from a qrz table row. Everything is
        read-only except Grid — an existing override is shown in place of the
        FCC grid, and editing the field updates the override on Save."""
        self._manual_mode = False
        self._found_fcc_grid = format_grid((row.get("grid") or "").strip())
        self._found_override = format_grid((row.get("grid_override") or "").strip())
        values = [
            row.get("callsign") or "", row.get("name") or "",
            row.get("city") or "", row.get("state") or "",
            self._found_override or self._found_fcc_grid,
        ]
        for field, val in zip(self._fields, values):
            field.setText(val)
            make_input(widget=field, read_only=field is not self.f_grid)
        self.detail_row.setVisible(True)

    def _enter_manual_mode(self, cs: str) -> None:
        """Let the user type the details for a callsign found nowhere."""
        self._manual_mode = True
        self._found_fcc_grid = ""
        self._found_override = ""
        for field in self._fields:
            field.clear()
            make_input(widget=field, read_only=field is self.f_callsign)
        self.f_callsign.setText(cs)
        self.detail_row.setVisible(True)
        self.f_name.setFocus()

    def _on_save(self) -> None:
        cs = self.f_callsign.text().strip().upper()
        if not cs:
            return
        if self._manual_mode:
            qrz_id = self.db.add_qrz_manual(
                cs,
                self.f_name.text(),
                self.f_city.text(),
                self.f_state.text(),
                format_grid(self.f_grid.text()),
            )
        else:
            qrz_id = self.db.get_qrz_id(cs)
            # A grid edited away from the FCC value becomes the override; put
            # it back to the FCC value and the override clears.
            new_grid = format_grid(self.f_grid.text().strip())
            override = "" if new_grid == self._found_fcc_grid else new_grid
            if override != self._found_override:
                self.db.set_qrz_grid_override(cs, override)
        if qrz_id is None:
            QMessageBox.critical(self, "Error", f"Could not save {cs}.")
            return
        if not self.db.add_watchlist_member(self.watchlist_id, qrz_id):
            QMessageBox.warning(
                self, "Members",
                f"{cs} is already on the {self.watchlist_name} watchlist."
            )
        self._load()
        # Rapid entry: the callsign box and Search stay up for the next one.
        self._reset_add_panel()

    # ── Bulk add flow ──────────────────────────────────────────────────────────

    def _on_bulk_add(self) -> None:
        self.add_panel.setVisible(False)
        self.bulk_panel.setVisible(True)
        self.bulk_edit.clear()
        self.lbl_bulk_status.setText("")
        self.bulk_edit.setFocus()

    def _on_bulk_submit(self) -> None:
        if self._bulk_thread is not None and self._bulk_thread.isRunning():
            return
        # Comma-separated per the UI hint, but stray whitespace/newline
        # separators parse the same. Duplicates collapse, order kept.
        seen = set()
        callsigns = []
        for token in re.split(r"[,\s]+", self.bulk_edit.text().upper()):
            if token and token not in seen:
                seen.add(token)
                callsigns.append(token)
        if not callsigns:
            return

        is_active, username, password = load_qrz_config()
        self.btn_submit.setEnabled(False)
        self.lbl_bulk_status.setText(f"Processing {len(callsigns)} callsigns…")
        self._bulk_thread = _BulkLookupThread(
            callsigns, username, password, use_api=bool(is_active and username))
        _live_threads.add(self._bulk_thread)
        self._bulk_thread.finished.connect(
            lambda t=self._bulk_thread: _live_threads.discard(t)
        )
        self._bulk_thread.progress.connect(self._on_bulk_progress)
        self._bulk_thread.lookup_done.connect(self._on_bulk_done)
        self._bulk_thread.start()

    def _on_bulk_progress(self, done: int, total: int, cs: str) -> None:
        self.lbl_bulk_status.setText(f"Looking up {cs}… ({done}/{total})")

    def _on_bulk_done(self, resolved, failed, fetched) -> None:
        self.btn_submit.setEnabled(True)

        # API hits wrote the qrz cache from the worker thread; fire the
        # module-level notifier from here (main thread) for each of them.
        if fetched:
            from qrz_lookup import qrz_cache_notifier
            for cs in fetched:
                qrz_cache_notifier.record_written.emit(cs)

        failed = list(failed)
        added = existing = 0
        for cs in resolved:
            qrz_id = self.db.get_qrz_id(cs)
            if qrz_id is None:
                failed.append(cs)
            elif self.db.add_watchlist_member(self.watchlist_id, qrz_id):
                added += 1
            else:
                existing += 1

        self._load()
        parts = [f"{added} added"]
        if existing:
            parts.append(f"{existing} already member{'s' if existing != 1 else ''}")
        if failed:
            parts.append(f"{len(failed)} failed")
        self.lbl_bulk_status.setText(", ".join(parts))
        self.bulk_edit.clear()

        if failed:
            report = _BulkFailedDialog(self.watchlist_name, failed, self)
            report.exec_()
            report.deleteLater()

    # ── Remove ─────────────────────────────────────────────────────────────────

    def _on_remove(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        item = self.table.item(row, 1)
        if item is None:
            return
        cs = item.text()
        qrz_id = item.data(Qt.UserRole)
        if not confirm(self, "Remove Member",
                       f"Remove {cs} from the {self.watchlist_name} watchlist?",
                       no_label="Cancel"):
            return
        if not self.db.remove_watchlist_member(self.watchlist_id, qrz_id):
            QMessageBox.critical(self, "Error", "Could not remove member.")
            return
        self._load()

    # ── Teardown ───────────────────────────────────────────────────────────────

    def done(self, result: int) -> None:
        # A lookup may still be in flight; detach it from this dialog and let
        # it finish on its own (kept alive by _live_threads).
        if self._thread is not None and self._thread.isRunning():
            try:
                self._thread.result_ready.disconnect(self._on_qrz_result)
            except TypeError:
                pass
        if self._bulk_thread is not None and self._bulk_thread.isRunning():
            try:
                self._bulk_thread.progress.disconnect(self._on_bulk_progress)
                self._bulk_thread.lookup_done.disconnect(self._on_bulk_done)
            except TypeError:
                pass
        super().done(result)


if __name__ == "__main__":
    import sys
    app = QtWidgets.QApplication(sys.argv)
    print("This dialog requires a DatabaseManager instance.")
    sys.exit(1)
