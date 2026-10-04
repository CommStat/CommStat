# Copyright (c) 2025 Manuel Ochoa
# This file is part of CommStat.
# Licensed under the GNU General Public License v3.0.
"""
groups.py - Manage Groups Dialog

Displays all groups in a table with Add, Edit, and Delete actions.
Editing is done inline directly in the table row.
"""

from typing import Optional

from PyQt5 import QtGui, QtWidgets
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QTableWidget, QTableWidgetItem,
    QHeaderView, QMessageBox, QAbstractItemView, QWidget,
)

from constants import DEFAULT_COLORS
from ui_helpers import (
    make_button, make_input, confirm, apply_standard_dialog_chrome,
    make_title_strip, wrap_fixed, DIALOG_TABLE_QSS,
)

# ── Constants ──────────────────────────────────────────────────────────────────

_PANEL_BG = DEFAULT_COLORS.get("module_background",    "#DDDDDD")
_PANEL_FG = DEFAULT_COLORS.get("module_foreground",    "#000000")

_COL_ADD    = "#28a745"
_COL_EDIT   = "#007bff"
_COL_DELETE = "#dc3545"
_COL_CLOSE  = "#555555"
_COL_SAVE   = "#28a745"
_COL_CANCEL = "#555555"

_WIN_W = 520
_WIN_H = 400

_TABLE_COLS = ["Group Name", "Comment"]


# ── Dialog ─────────────────────────────────────────────────────────────────────

class GroupsDialog(QDialog):
    """Manage Groups dialog — add, edit, and delete groups with inline row editing."""

    def __init__(self, db_manager, parent=None):
        super().__init__(parent)
        self.db = db_manager

        self._in_edit_mode: bool = False
        self._edit_row: int = -1
        self._adding: bool = False
        self._edit_name: str = ""
        self._iw_name: Optional[QLineEdit] = None
        self._iw_comment: Optional[QLineEdit] = None

        apply_standard_dialog_chrome(self, "Groups", _WIN_W, _WIN_H)

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
        title_lbl = make_title_strip("Groups")
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
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)

        self.table.setStyleSheet(DIALOG_TABLE_QSS)
        self.table.selectionModel().selectionChanged.connect(self._on_selection_changed)
        self.table.doubleClicked.connect(self._on_edit)
        body.addWidget(self.table)

        # ── Note ──────────────────────────────────────────────────────────────
        note = QLabel(
            f"<b><span style='color:#AA0000'>Note:</span></b>"
            f" <span style='color:{_PANEL_FG}'>The @ symbol is not required"
            f" (e.g., enter MAGNET, not @MAGNET)</span>"
        )
        note.setStyleSheet("QLabel { font-family:Roboto; font-size:13px; }")
        body.addWidget(note)

        # ── Action buttons ────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self.btn_add    = make_button("Add",    _COL_ADD,    80)
        self.btn_edit   = make_button("Edit",   _COL_EDIT,   80)
        self.btn_delete = make_button("Delete", _COL_DELETE, 80)
        self.btn_save   = make_button("Save",   _COL_SAVE,   80)
        self.btn_cancel = make_button("Cancel", _COL_CANCEL, 80)
        self.btn_close  = make_button("Close",  _COL_CLOSE,  80)

        self.btn_edit.setEnabled(False)
        self.btn_delete.setEnabled(False)
        self.btn_save.setVisible(False)
        self.btn_cancel.setVisible(False)
        self.btn_save.setEnabled(False)

        self.btn_add.clicked.connect(self._on_add)
        self.btn_edit.clicked.connect(self._on_edit)
        self.btn_delete.clicked.connect(self._on_delete)
        self.btn_save.clicked.connect(lambda: self._exit_edit_mode(save=True))
        self.btn_cancel.clicked.connect(lambda: self._exit_edit_mode(save=False))
        self.btn_close.clicked.connect(self.accept)

        btn_row.addWidget(self.btn_add)
        btn_row.addWidget(self.btn_edit)
        btn_row.addWidget(self.btn_delete)
        btn_row.addWidget(self.btn_save)
        btn_row.addWidget(self.btn_cancel)
        btn_row.addStretch()
        btn_row.addWidget(self.btn_close)
        body.addLayout(btn_row)

    # ── Data loading ───────────────────────────────────────────────────────────

    def _load(self) -> None:
        self._in_edit_mode = False
        self._edit_row = -1
        self._edit_name = ""
        groups = self.db.get_all_groups_details()
        self.table.setRowCount(0)
        mono = QtGui.QFont("Kode Mono")

        for g in groups:
            row = self.table.rowCount()
            self.table.insertRow(row)
            for col, val in enumerate([g["name"], g["comment"]]):
                item = QTableWidgetItem(val)
                item.setFont(mono)
                item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
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

    # ── Inline edit ────────────────────────────────────────────────────────────

    def _enter_edit_mode(self, row: int, adding: bool) -> None:
        self._in_edit_mode = True
        self._edit_row = row
        self._adding = adding

        if adding:
            name_val = ""
            comment_val = ""
        else:
            name_item = self.table.item(row, 0)
            comment_item = self.table.item(row, 1)
            name_val = name_item.text() if name_item else ""
            comment_val = comment_item.text() if comment_item else ""
            self._edit_name = name_val

        self._iw_name = make_input(
            placeholder="Group name (max 15 chars)",
            default=name_val,
            max_len=15,
        )
        self._iw_name.textChanged.connect(
            lambda t: self._iw_name.setText(t.upper()) if t != t.upper() else None
        )
        self._iw_name.textChanged.connect(lambda _: self._on_inline_changed())

        self._iw_comment = make_input(
            placeholder="Optional description",
            default=comment_val,
            max_len=80,
        )

        _name_w = 136

        # Col 0 is ResizeToContents and would clamp back to data width, clipping
        # the input. Switch to Interactive and force a width that holds it;
        # _exit_edit_mode restores ResizeToContents.
        _hh = self.table.horizontalHeader()
        _hh.setSectionResizeMode(0, QHeaderView.Interactive)
        self.table.setColumnWidth(0, _name_w + 16)

        self.table.setCellWidget(row, 0, wrap_fixed(self._iw_name, _name_w))
        self.table.setCellWidget(row, 1, self._iw_comment)
        self.table.setRowHeight(row, 42)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)

        self.btn_add.setVisible(False)
        self.btn_edit.setVisible(False)
        self.btn_delete.setVisible(False)
        self.btn_save.setVisible(True)
        self.btn_cancel.setVisible(True)
        self.btn_close.setEnabled(False)

        QWidget.setTabOrder(self._iw_name, self._iw_comment)
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
            if self._adding:
                raw = self._iw_name.text().strip()
                name = raw.lstrip("@").strip().upper()
                if not name:
                    QMessageBox.warning(self, "Groups", "Group name is required.")
                    return
                comment = self._iw_comment.text().strip()
                ok = self.db.add_group(name, comment, "", "")
                if not ok:
                    QMessageBox.warning(
                        self, "Groups",
                        "Could not add group. The name may already exist."
                    )
                    return
            else:
                raw = self._iw_name.text().strip()
                new_name = raw.lstrip("@").strip().upper()
                if not new_name:
                    QMessageBox.warning(self, "Groups", "Group name is required.")
                    return
                comment = self._iw_comment.text().strip()
                ok = self.db.update_group_full(self._edit_name, new_name, comment)
                if not ok:
                    QMessageBox.critical(self, "Error", "Could not update group.")
                    return

        for col in range(2):
            self.table.removeCellWidget(row, col)

        self._iw_name = self._iw_comment = None
        self._in_edit_mode = False
        self._edit_name = ""

        self.table.setRowHeight(row, self.table.verticalHeader().defaultSectionSize())
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)

        # Restore col 0 to auto-fit after edit-mode widening.
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)

        self.btn_add.setVisible(True)
        self.btn_edit.setVisible(True)
        self.btn_delete.setVisible(True)
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
        if not confirm(self, "Delete Group", f"Delete group '{name}'?",
                       no_label="Cancel"):
            return
        if not self.db.remove_group(name):
            QMessageBox.critical(self, "Error", "Could not delete group.")
            return
        self._load()


if __name__ == "__main__":
    import sys
    app = QtWidgets.QApplication(sys.argv)
    print("This dialog requires a DatabaseManager instance.")
    sys.exit(1)
