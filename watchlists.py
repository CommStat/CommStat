# Copyright (c) 2026 Manuel Ochoa
# This file is part of CommStat.
# Licensed under the GNU General Public License v3.0.
"""
watchlists.py - Manage Watchlists Dialog

A watchlist is a named list of contacts drawn on the map as persistent
objects (Map -> Watchlist Overlay). Watchlists are independent of the JS8
@GROUPS used for message routing. Displays all watchlists in a table with
Add, Edit, Delete, and Members actions; editing is done inline directly in
the table row.
"""

from typing import Optional

from PyQt5 import QtGui, QtWidgets
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QTableWidget, QTableWidgetItem,
    QHeaderView, QMessageBox, QAbstractItemView, QWidget,
)

from constants import (
    DEFAULT_COLORS, WATCHLIST_OBJECT_COLORS, WATCHLIST_OBJECT_SHAPES,
    DEFAULT_OBJECT_COLOR, DEFAULT_OBJECT_SHAPE, COLOR_BTN_HELP,
)
from ui_helpers import (
    make_button, make_input, make_combobox, confirm, apply_standard_dialog_chrome,
    make_title_strip, wrap_fixed, DIALOG_TABLE_QSS, UpperCaseLineEdit,
)
from help import show_watchlist_pins_help

# ── Constants ──────────────────────────────────────────────────────────────────

_PANEL_BG = DEFAULT_COLORS.get("module_background",    "#DDDDDD")
_PANEL_FG = DEFAULT_COLORS.get("module_foreground",    "#000000")

_COL_ADD     = "#28a745"
_COL_EDIT    = "#007bff"
_COL_DELETE  = "#dc3545"
_COL_MEMBERS = "#6f42c1"
_COL_PRESETS = "#17a2b8"
_COL_CLOSE   = "#555555"
_COL_SAVE    = "#28a745"
_COL_CANCEL  = "#555555"
_COL_HELP    = COLOR_BTN_HELP

_WIN_W = 760
_WIN_H = 400

_TABLE_COLS = ["Watchlist Name", "Members", "Active", "Object Color", "Object Shape", "Comment"]
_COL_NAME, _COL_COUNT, _COL_ACTIVE, _COL_OBJCOLOR, _COL_OBJSHAPE, _COL_COMMENT = range(6)

_SHAPE_GLYPHS = {"CIRCLE": "●", "SQUARE": "■", "TRIANGLE": "▲"}


# ── Dialog ─────────────────────────────────────────────────────────────────────

class WatchlistsDialog(QDialog):
    """Manage Watchlists dialog — add, edit, and delete watchlists with inline
    row editing, plus a Members button for each watchlist's contact roster."""

    def __init__(self, db_manager, parent=None):
        super().__init__(parent)
        self.db = db_manager

        self._in_edit_mode: bool = False
        self._edit_row: int = -1
        self._adding: bool = False
        self._edit_name: str = ""
        self._iw_name: Optional[QLineEdit] = None
        self._iw_color: Optional[QtWidgets.QComboBox] = None
        self._iw_shape: Optional[QtWidgets.QComboBox] = None
        self._iw_comment: Optional[QLineEdit] = None

        apply_standard_dialog_chrome(self, "Watchlists", _WIN_W, _WIN_H)

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
        title_lbl = make_title_strip("Watchlists")
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

        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(_COL_NAME,     QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(_COL_COUNT,    QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(_COL_ACTIVE,   QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(_COL_OBJCOLOR, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(_COL_OBJSHAPE, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(_COL_COMMENT,  QHeaderView.Stretch)

        self.table.setStyleSheet(DIALOG_TABLE_QSS)
        self.table.selectionModel().selectionChanged.connect(self._on_selection_changed)
        self.table.doubleClicked.connect(self._on_edit)
        body.addWidget(self.table)

        # ── Note ──────────────────────────────────────────────────────────────
        note = QLabel(
            f"<b><span style='color:#AA0000'>Note:</span></b>"
            f" <span style='color:{_PANEL_FG}'>Watchlist members appear as objects"
            f" on the map — turn them on under Map &rarr; Watchlist Overlay</span>"
        )
        note.setStyleSheet("QLabel { font-family:Roboto; font-size:13px; }")
        body.addWidget(note)

        # ── Action buttons ────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self.btn_add     = make_button("Add",     _COL_ADD,     80)
        self.btn_edit    = make_button("Edit",    _COL_EDIT,    80)
        self.btn_delete  = make_button("Delete",  _COL_DELETE,  80)
        self.btn_members = make_button("Members", _COL_MEMBERS, 80)
        self.btn_presets = make_button("Presets", _COL_PRESETS, 80)
        self.btn_save    = make_button("Save",    _COL_SAVE,    80)
        self.btn_cancel  = make_button("Cancel",  _COL_CANCEL,  80)
        self.btn_help    = make_button("Help",    _COL_HELP,    80)
        self.btn_close   = make_button("Close",   _COL_CLOSE,   80)

        self.btn_edit.setEnabled(False)
        self.btn_delete.setEnabled(False)
        self.btn_members.setEnabled(False)
        self.btn_save.setVisible(False)
        self.btn_cancel.setVisible(False)
        self.btn_save.setEnabled(False)

        self.btn_add.clicked.connect(self._on_add)
        self.btn_edit.clicked.connect(self._on_edit)
        self.btn_delete.clicked.connect(self._on_delete)
        self.btn_members.clicked.connect(self._on_members)
        self.btn_presets.clicked.connect(self._on_presets)
        self.btn_save.clicked.connect(lambda: self._exit_edit_mode(save=True))
        self.btn_cancel.clicked.connect(lambda: self._exit_edit_mode(save=False))
        self.btn_help.clicked.connect(self._on_help)
        self.btn_close.clicked.connect(self.accept)

        btn_row.addWidget(self.btn_add)
        btn_row.addWidget(self.btn_edit)
        btn_row.addWidget(self.btn_delete)
        btn_row.addWidget(self.btn_members)
        btn_row.addWidget(self.btn_presets)
        btn_row.addWidget(self.btn_save)
        btn_row.addWidget(self.btn_cancel)
        btn_row.addStretch()
        btn_row.addWidget(self.btn_help)
        btn_row.addWidget(self.btn_close)
        body.addLayout(btn_row)

    # ── Data loading ───────────────────────────────────────────────────────────

    def _load(self) -> None:
        self._in_edit_mode = False
        self._edit_row = -1
        self._edit_name = ""
        watchlists = self.db.get_all_watchlists_details()
        counts = self.db.get_watchlist_member_counts()
        active_counts = self.db.get_watchlist_active_member_counts()
        self.table.setRowCount(0)
        mono = QtGui.QFont("Kode Mono")

        for w in watchlists:
            row = self.table.rowCount()
            self.table.insertRow(row)
            color = w.get("object_color", DEFAULT_OBJECT_COLOR)
            shape = w.get("object_shape", DEFAULT_OBJECT_SHAPE)
            values = [
                w["name"],
                str(counts.get(w["name"], 0)),
                str(active_counts.get(w["name"], 0)),
                color.title(),
                f"{_SHAPE_GLYPHS.get(shape, '●')} {shape.title()}",
                w["comment"],
            ]
            for col, val in enumerate(values):
                item = QTableWidgetItem(val)
                item.setFont(mono)
                item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
                if col in (_COL_COUNT, _COL_ACTIVE):
                    item.setTextAlignment(Qt.AlignCenter)
                elif col == _COL_OBJCOLOR:
                    # Swatch of the actual object color next to its name.
                    item.setData(Qt.DecorationRole, QtGui.QColor(
                        WATCHLIST_OBJECT_COLORS.get(color, WATCHLIST_OBJECT_COLORS[DEFAULT_OBJECT_COLOR])))
                    item.setData(Qt.UserRole, color)
                elif col == _COL_OBJSHAPE:
                    item.setData(Qt.UserRole, shape)
                self.table.setItem(row, col, item)

        self._on_selection_changed()

    # ── Selection ──────────────────────────────────────────────────────────────

    def _on_selection_changed(self) -> None:
        if self._in_edit_mode:
            return
        has_sel = bool(self.table.selectedItems())
        self.btn_add.setEnabled(True)
        self.btn_edit.setEnabled(has_sel)
        self.btn_delete.setEnabled(has_sel)
        self.btn_members.setEnabled(has_sel)

    # ── Inline edit ────────────────────────────────────────────────────────────

    def _enter_edit_mode(self, row: int, adding: bool) -> None:
        self._in_edit_mode = True
        self._edit_row = row
        self._adding = adding

        if adding:
            name_val = ""
            comment_val = ""
            color_val = DEFAULT_OBJECT_COLOR
            shape_val = DEFAULT_OBJECT_SHAPE
        else:
            name_item = self.table.item(row, _COL_NAME)
            color_item = self.table.item(row, _COL_OBJCOLOR)
            shape_item = self.table.item(row, _COL_OBJSHAPE)
            comment_item = self.table.item(row, _COL_COMMENT)
            name_val = name_item.text() if name_item else ""
            comment_val = comment_item.text() if comment_item else ""
            color_val = color_item.data(Qt.UserRole) if color_item else DEFAULT_OBJECT_COLOR
            shape_val = shape_item.data(Qt.UserRole) if shape_item else DEFAULT_OBJECT_SHAPE
            self._edit_name = name_val

        self._iw_name = make_input(
            placeholder="Watchlist name (max 15 chars)",
            default=name_val,
            max_len=15,
            widget=UpperCaseLineEdit(),
        )
        self._iw_name.textChanged.connect(lambda _: self._on_inline_changed())

        self._iw_color = make_combobox(
            [(c.title(), c) for c in WATCHLIST_OBJECT_COLORS])
        idx = self._iw_color.findData(color_val)
        self._iw_color.setCurrentIndex(idx if idx >= 0 else 0)

        self._iw_shape = make_combobox(
            [(f"{_SHAPE_GLYPHS[s]} {s.title()}", s) for s in WATCHLIST_OBJECT_SHAPES])
        idx = self._iw_shape.findData(shape_val)
        self._iw_shape.setCurrentIndex(idx if idx >= 0 else 0)

        self._iw_comment = make_input(
            placeholder="Optional description",
            default=comment_val,
            max_len=80,
        )

        _name_w  = 136
        _color_w = 112
        _shape_w = 128

        # Cols 0/2/3 are ResizeToContents and would clamp back to data width,
        # clipping the inputs. Switch to Interactive and force widths that hold
        # them; _exit_edit_mode restores ResizeToContents.
        _hh = self.table.horizontalHeader()
        _hh.setSectionResizeMode(_COL_NAME, QHeaderView.Interactive)
        self.table.setColumnWidth(_COL_NAME, _name_w + 16)
        _hh.setSectionResizeMode(_COL_OBJCOLOR, QHeaderView.Interactive)
        self.table.setColumnWidth(_COL_OBJCOLOR, _color_w)
        _hh.setSectionResizeMode(_COL_OBJSHAPE, QHeaderView.Interactive)
        self.table.setColumnWidth(_COL_OBJSHAPE, _shape_w)

        self.table.setCellWidget(row, _COL_NAME, wrap_fixed(self._iw_name, _name_w))
        self.table.setCellWidget(row, _COL_OBJCOLOR, self._iw_color)
        self.table.setCellWidget(row, _COL_OBJSHAPE, self._iw_shape)
        self.table.setCellWidget(row, _COL_COMMENT, self._iw_comment)
        self.table.setRowHeight(row, 42)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)

        self.btn_add.setVisible(False)
        self.btn_edit.setVisible(False)
        self.btn_delete.setVisible(False)
        self.btn_members.setVisible(False)
        self.btn_presets.setVisible(False)
        self.btn_save.setVisible(True)
        self.btn_cancel.setVisible(True)
        self.btn_close.setEnabled(False)

        QWidget.setTabOrder(self._iw_name, self._iw_color)
        QWidget.setTabOrder(self._iw_color, self._iw_shape)
        QWidget.setTabOrder(self._iw_shape, self._iw_comment)
        QWidget.setTabOrder(self._iw_comment, self.btn_save)

        self._on_inline_changed()
        self._iw_name.setFocus()

    def _on_inline_changed(self) -> None:
        if not self._in_edit_mode:
            return
        has_name = bool(self._iw_name and self._iw_name.text().strip())
        self.btn_save.setEnabled(has_name)

    def _exit_edit_mode(self, save: bool) -> None:
        row = self._edit_row

        if save:
            object_color = self._iw_color.currentData()
            object_shape = self._iw_shape.currentData()
            name = self._iw_name.text().strip().upper()
            if not name:
                QMessageBox.warning(self, "Watchlists", "Watchlist name is required.")
                return
            if "," in name:
                QMessageBox.warning(self, "Watchlists", "A watchlist name cannot contain a comma.")
                return
            comment = self._iw_comment.text().strip()
            if self._adding:
                ok = self.db.add_watchlist(name, comment, object_color, object_shape)
                if not ok:
                    QMessageBox.warning(
                        self, "Watchlists",
                        "Could not add watchlist. The name may already exist."
                    )
                    return
            else:
                ok = self.db.update_watchlist(
                    self._edit_name, name, comment, object_color, object_shape)
                if not ok:
                    QMessageBox.critical(self, "Error", "Could not update watchlist.")
                    return

        for col in range(len(_TABLE_COLS)):
            self.table.removeCellWidget(row, col)

        self._iw_name = self._iw_color = self._iw_shape = self._iw_comment = None
        self._in_edit_mode = False
        self._edit_name = ""

        self.table.setRowHeight(row, self.table.verticalHeader().defaultSectionSize())
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)

        # Restore cols 0/2/3 to auto-fit after edit-mode widening.
        _hh = self.table.horizontalHeader()
        _hh.setSectionResizeMode(_COL_NAME,     QHeaderView.ResizeToContents)
        _hh.setSectionResizeMode(_COL_OBJCOLOR, QHeaderView.ResizeToContents)
        _hh.setSectionResizeMode(_COL_OBJSHAPE, QHeaderView.ResizeToContents)

        self.btn_add.setVisible(True)
        self.btn_edit.setVisible(True)
        self.btn_delete.setVisible(True)
        self.btn_members.setVisible(True)
        self.btn_presets.setVisible(True)
        self.btn_save.setVisible(False)
        self.btn_cancel.setVisible(False)
        self.btn_close.setEnabled(True)

        self._load()

    # ── Actions ────────────────────────────────────────────────────────────────

    def _on_add(self) -> None:
        if self._in_edit_mode:
            return
        new_row = self.table.rowCount()
        self.table.insertRow(new_row)
        self._enter_edit_mode(row=new_row, adding=True)

    def _on_edit(self) -> None:
        if self._in_edit_mode:
            return
        row = self.table.currentRow()
        if row < 0:
            return
        self._enter_edit_mode(row=row, adding=False)

    def _on_delete(self) -> None:
        if self._in_edit_mode:
            return
        row = self.table.currentRow()
        if row < 0:
            return
        name_item = self.table.item(row, 0)
        name = name_item.text() if name_item else ""
        if not name:
            return
        if not confirm(self, "Delete Watchlist", f"Delete watchlist '{name}'?",
                       no_label="Cancel"):
            return
        if not self.db.remove_watchlist(name):
            QMessageBox.critical(self, "Error", "Could not delete watchlist.")
            return
        self._load()

    def _on_help(self) -> None:
        show_watchlist_pins_help(self)

    def _on_members(self) -> None:
        if self._in_edit_mode:
            return
        row = self.table.currentRow()
        if row < 0:
            return
        name_item = self.table.item(row, 0)
        name = name_item.text() if name_item else ""
        if not name:
            return
        watchlist_id = self.db.get_watchlist_id(name)
        if watchlist_id is None:
            QMessageBox.critical(self, "Error", "Could not resolve watchlist.")
            return
        # Deferred import keeps this module importable standalone.
        from watchlist_members import WatchlistMembersDialog
        dlg = WatchlistMembersDialog(self.db, watchlist_id, name, self)
        dlg.exec_()
        dlg.deleteLater()
        # Refresh the Members count column.
        self._load()

    def _on_presets(self) -> None:
        if self._in_edit_mode:
            return
        # Parent is the main window (see _on_manage_watchlists); the preset
        # install reuses its _handle_db_update to execute the server SQL.
        main_win = self.parent()
        handler = getattr(main_win, "_handle_db_update", None)
        if handler is None:
            QMessageBox.critical(self, "Error", "Preset installs are unavailable.")
            return

        # Same callsign priority chain as the heartbeat: connected rig first,
        # then the configured callsign.
        callsign = ""
        try:
            callsign = next(
                (cs for cs in main_win.rig_callsigns.values() if cs), "")
        except Exception:
            pass
        if not callsign:
            try:
                callsign = self.db.get_user_settings()[0] or ""
            except Exception:
                pass
        callsign = callsign or "UNKNOWN"

        # Deferred import keeps this module importable standalone.
        from one_click_watchlists import OneClickWatchlistsDialog
        OneClickWatchlistsDialog(self.db, handler, callsign, self).exec_()
        # Show any freshly installed watchlist and its member count.
        self._load()


if __name__ == "__main__":
    import sys
    app = QtWidgets.QApplication(sys.argv)
    print("This dialog requires a DatabaseManager instance.")
    sys.exit(1)
