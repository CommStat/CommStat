# Copyright (c) 2026 Manuel Ochoa
# This file is part of CommStat.
# Licensed under the GNU General Public License v3.0.
"""
one_click_watchlists.py - 1-click Watchlists Dialog

Installs ready-made watchlists with a single click. Each preset is fetched
from the commsrvr as a db_update payload (the same format the heartbeat
channel delivers) and executed through the main window's _handle_db_update,
which creates the watchlist, its qrz contacts, and its member rows locally.
The server SQL uses INSERT OR IGNORE/REPLACE, so re-installing a preset is
harmless. Opened from the Manage Watchlists dialog's Presets button.
"""

import sqlite3
import urllib.parse
import urllib.request

from PyQt5 import QtGui
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
)

from constants import DEFAULT_COLORS, COMMSRVR_URL
from db_utils import db_connect
from ui_helpers import make_button, connect_single, apply_standard_dialog_chrome

# ── Constants ──────────────────────────────────────────────────────────────────

_PROG_BG  = DEFAULT_COLORS.get("program_background",   "#A52A2A")
_PROG_FG  = DEFAULT_COLORS.get("program_foreground",   "#FFFFFF")
_PANEL_BG = DEFAULT_COLORS.get("module_background",    "#DDDDDD")
_PANEL_FG = DEFAULT_COLORS.get("module_foreground",    "#000000")
_TITLE_BG = DEFAULT_COLORS.get("title_bar_background", "#F07800")
_TITLE_FG = DEFAULT_COLORS.get("title_bar_foreground", "#FFFFFF")
_DATA_BG  = DEFAULT_COLORS.get("data_background",      "#F8F6F4")
_DATA_FG  = DEFAULT_COLORS.get("data_foreground",      "#000000")

_COL_INSTALL = "#28a745"
_COL_CLOSE   = "#555555"

_WIN_W = 640
_WIN_H = 340

_COMMSRVR = COMMSRVR_URL
_WATCHLIST_URL = _COMMSRVR + "/watchlist-808585.php"

_TABLE_COLS = ["Watchlist", "Status", "Description"]
_COL_NAME, _COL_STATUS, _COL_DESC = range(3)

# One row per preset: (name shown in table, server request number, qrz.type
# the preset's contacts carry, description). The watchlist's actual name comes
# from the server SQL; installed detection goes by qrz.type, not name, so a
# renamed watchlist still reads as installed.
_CATALOG = [
    ("US Nuclear Power Plants", 32, 2, "All operating U.S. commercial nuclear power plants"),
    ("US Strategic Dams",       33, 3, "Major U.S. dams of strategic significance"),
]

# QThreads started for an install are parked here so closing the dialog
# mid-install never destroys a still-running QThread.
_live_threads = set()


# ── Install worker ─────────────────────────────────────────────────────────────

class _InstallThread(QThread):
    """Fetches one preset's db_update payload from the commsrvr and executes
    it via the main window's _handle_db_update (safe off the GUI thread — it
    opens its own sqlite connection, as the heartbeat channel already relies
    on). The reply's db: line echoes the client's own db_version back, so the
    handler's version write is a deliberate no-op."""

    install_done = pyqtSignal(int, bool, str)  # request number, ok, error text

    def __init__(self, request_number: int, callsign: str, db_update_handler):
        super().__init__()
        self._request = request_number
        self._callsign = callsign
        self._db_update_handler = db_update_handler

    def run(self) -> None:
        import netguard
        if not netguard.guard("1-click Watchlist install"):
            self.install_done.emit(self._request, False, "Blocked by Off-Grid mode")
            return
        try:
            db_version, build_number = 0, 500
            try:
                with db_connect() as conn:
                    row = conn.execute(
                        "SELECT db_version, build_number FROM controls WHERE id = 1"
                    ).fetchone()
                    if row:
                        db_version, build_number = row
            except sqlite3.Error:
                pass

            url = (f"{_WATCHLIST_URL}?request={self._request}"
                   f"&cs={urllib.parse.quote(self._callsign)}"
                   f"&db={db_version}&build={build_number}")
            # Verified TLS is mandatory here: the reply drives SQL execution.
            from ssl_utils import create_verified_ssl_context
            with urllib.request.urlopen(
                    url, timeout=10, context=create_verified_ssl_context()) as resp:
                content = resp.read().decode('utf-8').strip()
        except Exception:
            self.install_done.emit(self._request, False, "Network error")
            return

        if not content.startswith("db_update"):
            # The server replies "0" (heartbeat convention) when the preset
            # has no dataset yet.
            err = ("This watchlist is not yet available on the server"
                   if content == "0" else "Unexpected server response")
            self.install_done.emit(self._request, False, err)
            return

        ok = bool(self._db_update_handler(content))
        self.install_done.emit(self._request, ok, "" if ok else "Database update failed")


# ── Dialog ─────────────────────────────────────────────────────────────────────

class OneClickWatchlistsDialog(QDialog):
    """1-click Watchlists dialog — a table of ready-made watchlists with an
    Install button. The caller supplies the main window's _handle_db_update
    and the resolved callsign so this module stays main-window-agnostic."""

    def __init__(self, db_manager, db_update_handler, callsign: str, parent=None):
        super().__init__(parent)
        self.db = db_manager
        self._db_update_handler = db_update_handler
        self._callsign = callsign
        self._installing = False

        apply_standard_dialog_chrome(self, "1-click Watchlists", _WIN_W, _WIN_H)

        self._setup_ui()
        self._refresh_status()

    # ── UI construction ────────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        self.setStyleSheet(
            f"QDialog {{ background-color:{_PANEL_BG}; color:{_PANEL_FG}; }}"
            f"QLabel {{ font-size:13px; color:{_PANEL_FG}; }}"
        )

        body = QVBoxLayout(self)
        body.setContentsMargins(15, 15, 15, 15)
        body.setSpacing(10)

        title_lbl = QLabel("1-click Watchlists")
        title_lbl.setAlignment(Qt.AlignCenter)
        title_lbl.setFont(QtGui.QFont("Roboto Slab", -1, QtGui.QFont.Black))
        title_lbl.setFixedHeight(36)
        title_lbl.setStyleSheet(
            f"QLabel {{ background-color:{_PROG_BG}; color:{_PROG_FG};"
            f" font-family:'Roboto Slab'; font-size:16px; font-weight:900;"
            f" padding-top:9px; padding-bottom:9px; }}"
        )
        body.addWidget(title_lbl)

        intro = QLabel(
            "Install a ready-made watchlist with one click. Members are added"
            " to your local database and appear as objects on the map — turn"
            " them on under Map &rarr; Watchlist Overlay."
        )
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.RichText)
        intro.setStyleSheet("QLabel { font-family:Roboto; font-size:13px; }")
        body.addWidget(intro)

        self.table = QTableWidget(0, len(_TABLE_COLS))
        self.table.setHorizontalHeaderLabels(_TABLE_COLS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setTabKeyNavigation(False)
        self.table.setShowGrid(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(False)

        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(_COL_NAME,   QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(_COL_STATUS, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(_COL_DESC,   QHeaderView.Stretch)

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

        self.table.setRowCount(len(_CATALOG))
        for row, (name, _req, _qtype, desc) in enumerate(_CATALOG):
            self.table.setItem(row, _COL_NAME, QTableWidgetItem(name))
            self.table.setItem(row, _COL_STATUS, QTableWidgetItem(""))
            self.table.setItem(row, _COL_DESC, QTableWidgetItem(desc))
        body.addWidget(self.table)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self.btn_install = make_button("Install", _COL_INSTALL, 80)
        self.btn_close = make_button("Close", _COL_CLOSE, 80)

        self.btn_install.setEnabled(False)
        connect_single(self.btn_install, self._on_install)
        self.btn_close.clicked.connect(self.accept)

        btn_row.addWidget(self.btn_install)
        btn_row.addStretch()
        btn_row.addWidget(self.btn_close)
        body.addLayout(btn_row)

    # ── Status ─────────────────────────────────────────────────────────────────

    def _refresh_status(self) -> None:
        """'Installed' marks go by each preset's qrz.type — watchlist names
        can be edited, so a name match would break on a renamed watchlist.
        The server SQL is idempotent, so a wrong blank only costs a harmless
        re-install."""
        for row, (_name, _req, qtype, _desc) in enumerate(_CATALOG):
            try:
                installed = self.db.qrz_type_exists(qtype)
            except Exception:
                installed = False
            self.table.item(row, _COL_STATUS).setText("Installed" if installed else "")
        self._on_selection_changed()

    def _row_installed(self, row: int) -> bool:
        item = self.table.item(row, _COL_STATUS)
        return bool(item) and item.text() == "Installed"

    def _on_selection_changed(self, *_args) -> None:
        row = self.table.currentRow()
        self.btn_install.setEnabled(
            not self._installing and row >= 0 and not self._row_installed(row)
        )

    # ── Install flow ───────────────────────────────────────────────────────────

    def _on_install(self) -> None:
        row = self.table.currentRow()
        if self._installing or row < 0 or self._row_installed(row):
            return
        name, request_number, _qtype, _desc = _CATALOG[row]

        self._installing = True
        self.btn_install.setEnabled(False)
        self.table.item(row, _COL_STATUS).setText("Installing…")

        thread = _InstallThread(request_number, self._callsign, self._db_update_handler)
        _live_threads.add(thread)
        thread.install_done.connect(self._on_install_done)
        thread.finished.connect(lambda t=thread: _live_threads.discard(t))
        thread.start()

    def _on_install_done(self, request_number: int, ok: bool, err: str) -> None:
        self._installing = False
        row = next(
            (r for r, (_n, req, _t, _d) in enumerate(_CATALOG) if req == request_number), -1)
        if row >= 0:
            name = _CATALOG[row][0]
            if ok:
                self.table.item(row, _COL_STATUS).setText("Installed")
                self._notice(
                    "Watchlist Installed",
                    f"<b>{name}</b> was installed. Open Members to review the"
                    f" list, and turn it on under Map &rarr; Watchlist Overlay.",
                )
            else:
                self.table.item(row, _COL_STATUS).setText("")
                self._notice("Install Failed", f"Could not install <b>{name}</b>: {err}.")
        self._on_selection_changed()

    # ── Styled notice ──────────────────────────────────────────────────────────

    def _notice(self, title: str, html: str) -> None:
        dlg = QDialog(self)
        apply_standard_dialog_chrome(dlg, title)
        dlg.setStyleSheet(f"QDialog {{ background-color:{_PANEL_BG}; }}")

        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(15, 15, 15, 15)
        lay.setSpacing(10)

        strip = QLabel(title)
        strip.setAlignment(Qt.AlignCenter)
        strip.setFixedHeight(36)
        strip.setStyleSheet(
            f"QLabel {{ background-color:{_PROG_BG}; color:{_PROG_FG};"
            f" font-family:'Roboto Slab'; font-size:16px; font-weight:900;"
            f" padding-top:9px; padding-bottom:9px; }}"
        )
        lay.addWidget(strip)

        msg = QLabel(html)
        msg.setWordWrap(True)
        msg.setStyleSheet(
            f"QLabel {{ color:{_PANEL_FG}; font-family:Roboto; font-size:13px; }}")
        msg.setMinimumWidth(360)
        lay.addWidget(msg)

        row = QHBoxLayout()
        row.addStretch()
        btn = make_button("OK", _COL_CLOSE, 80)
        btn.clicked.connect(dlg.accept)
        row.addWidget(btn)
        lay.addLayout(row)

        dlg.exec_()


if __name__ == "__main__":
    import sys
    from PyQt5 import QtWidgets
    app = QtWidgets.QApplication(sys.argv)
    print("This dialog requires a DatabaseManager instance.")
    sys.exit(1)
