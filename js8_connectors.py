# Copyright (c) 2025 Manuel Ochoa
# This file is part of CommStat.
# Licensed under the GNU General Public License v3.0.
"""
js8_connectors.py - JS8 Connectors Management Dialog

Displays a table of configured JS8Call TCP connectors with Add, Edit, Delete,
and Reconnect actions. Editing is done inline directly in the table row.
The Status column (index 4) is live and read-only — never replaced with a widget.
"""

from typing import Optional

from PyQt5 import QtGui
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QTableWidget, QTableWidgetItem,
    QHeaderView, QMessageBox, QAbstractItemView, QWidget, QCheckBox,
)

from connector_manager import ConnectorManager, DEFAULT_SERVER, DEFAULT_TCP_PORT
from constants import DEFAULT_COLORS
from ui_helpers import make_button, make_input, mono_font, confirm, apply_standard_dialog_chrome

# ── Constants ──────────────────────────────────────────────────────────────────

_PROG_BG   = DEFAULT_COLORS.get("program_background",   "#A52A2A")
_PROG_FG   = DEFAULT_COLORS.get("program_foreground",   "#FFFFFF")
_PANEL_BG  = DEFAULT_COLORS.get("module_background",    "#DDDDDD")
_PANEL_FG  = DEFAULT_COLORS.get("module_foreground",    "#000000")
_TITLE_BG  = DEFAULT_COLORS.get("title_bar_background", "#F07800")
_TITLE_FG  = DEFAULT_COLORS.get("title_bar_foreground", "#FFFFFF")
_DATA_BG   = DEFAULT_COLORS.get("data_background",      "#F8F6F4")
_DATA_FG   = DEFAULT_COLORS.get("data_foreground",      "#000000")

_COL_ADD        = "#28a745"
_COL_EDIT       = "#007bff"
_COL_DELETE     = "#dc3545"
_COL_RECONNECT  = "#17a2b8"
_COL_CLOSE      = "#555555"
_COL_SAVE       = "#28a745"
_COL_CANCEL     = "#555555"

_COL_CONNECTED    = "#1a7f37"
_COL_DISCONNECTED = "#cc0000"
_COL_DISABLED     = "#888888"

_WIN_W = 940
_WIN_H = 380

_TABLE_COLS = ["Rig Name", "Server", "Port", "State", "Status", "Auto", "RF Ack", "Comment"]

_STATUS_COL  = 4   # live read-only column — never gets setCellWidget
_AUTO_COL    = 5   # auto-connect-at-startup flag (Yes/No, QCheckBox in edit mode)
_RF_ACK_COL  = 6   # RF RRSR-ack-on-receipt flag (Yes/No, QCheckBox in edit mode)
_COMMENT_COL = 7


# ── Dialog ─────────────────────────────────────────────────────────────────────

class JS8ConnectorsDialog(QDialog):
    """JS8 Connectors management dialog — list, add, edit, delete, reconnect."""

    def __init__(self, connector_manager: ConnectorManager, tcp_pool, parent=None):
        super().__init__(parent)
        self.connector_manager = connector_manager
        self.tcp_pool = tcp_pool

        self._edit_id: Optional[int] = None
        self._in_edit_mode: bool = False
        self._edit_row: int = -1
        self._adding: bool = False
        self._iw_rig:     Optional[QLineEdit] = None
        self._iw_server:  Optional[QLineEdit] = None
        self._iw_port:    Optional[QLineEdit] = None
        self._iw_state:   Optional[QLineEdit] = None
        self._iw_auto:    Optional[QCheckBox] = None
        self._iw_rf_ack:  Optional[QCheckBox] = None
        self._iw_comment: Optional[QLineEdit] = None

        apply_standard_dialog_chrome(self, "JS8 Connectors", _WIN_W, _WIN_H)

        self._setup_ui()
        self._load_connectors()

        if self.tcp_pool and hasattr(self.tcp_pool, "any_connection_changed"):
            self.tcp_pool.any_connection_changed.connect(self._on_connection_changed)

    # ── UI construction ────────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        self.setStyleSheet(
            f"QDialog {{ background-color:{_PANEL_BG}; }}"
            f"QLabel {{ font-size:13px; }}"
        )

        body = QVBoxLayout(self)
        body.setContentsMargins(15, 15, 15, 15)
        body.setSpacing(10)

        # ── Title ─────────────────────────────────────────────────────────────
        title_lbl = QLabel("JS8 Connectors")
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
        self.table.setMinimumHeight(160)

        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(6, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(7, QHeaderView.Stretch)

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

        # ── Action buttons ────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self.btn_add       = make_button("Add",       _COL_ADD,       80)
        self.btn_edit      = make_button("Edit",      _COL_EDIT,      80)
        self.btn_delete    = make_button("Delete",    _COL_DELETE,    80)
        self.btn_reconnect = make_button("Reconnect", _COL_RECONNECT, 100)
        self.btn_save      = make_button("Save",      _COL_SAVE,      80)
        self.btn_cancel    = make_button("Cancel",    _COL_CANCEL,    80)
        self.btn_close     = make_button("Close",     _COL_CLOSE,     80)

        self.btn_edit.setEnabled(False)
        self.btn_delete.setEnabled(False)
        self.btn_reconnect.setEnabled(False)
        self.btn_save.setVisible(False)
        self.btn_cancel.setVisible(False)
        self.btn_save.setEnabled(False)

        self.btn_add.clicked.connect(self._on_add)
        self.btn_edit.clicked.connect(self._on_edit)
        self.btn_delete.clicked.connect(self._on_delete)
        self.btn_reconnect.clicked.connect(self._on_reconnect)
        self.btn_save.clicked.connect(lambda: self._exit_edit_mode(save=True))
        self.btn_cancel.clicked.connect(lambda: self._exit_edit_mode(save=False))
        self.btn_close.clicked.connect(self.accept)

        btn_row.addWidget(self.btn_add)
        btn_row.addWidget(self.btn_edit)
        btn_row.addWidget(self.btn_delete)
        btn_row.addWidget(self.btn_reconnect)
        btn_row.addWidget(self.btn_save)
        btn_row.addWidget(self.btn_cancel)
        btn_row.addStretch()
        btn_row.addWidget(self.btn_close)
        body.addLayout(btn_row)

        # ── Tip / Note ────────────────────────────────────────────────────────
        tip_lbl = QLabel(
            f"<b><span style='color:#AA0000'>Tip:</span></b>"
            f" <span style='color:{_PANEL_FG}'>Enable both TCP settings"
            f" in JS8Call under File &gt; Settings &gt; Reporting</span><br>"
            f"<b><span style='color:#AA0000'>Edit:</span></b>"
            f" <span style='color:{_PANEL_FG}'>Increase TCP Max Connections by 1</span><br>"
            f"<b><span style='color:#AA0000'>Note:</span></b>"
            f" <span style='color:{_PANEL_FG}'>Each connector requires a unique IP address and port combination</span><br>"
            f"<b><span style='color:#AA0000'>Auto:</span></b>"
            f" <span style='color:{_PANEL_FG}'>Uncheck to keep CommStat from auto-connecting at startup; use Reconnect on demand</span><br>"
            f"<b><span style='color:#AA0000'>RF Ack:</span></b>"
            f" <span style='color:{_PANEL_FG}'>Uncheck to stop auto-transmitting an RF acknowledgment for STATREPs received on this connector</span>"
        )
        tip_lbl.setWordWrap(True)
        body.addWidget(tip_lbl)

    # ── Data loading ───────────────────────────────────────────────────────────

    def _load_connectors(self) -> None:
        connectors = self.connector_manager.get_all_connectors()
        status_map: dict = {}
        if self.tcp_pool and hasattr(self.tcp_pool, "get_connection_status"):
            status_map = self.tcp_pool.get_connection_status()

        self.table.setRowCount(0)
        mono = mono_font()

        for row_idx, conn in enumerate(connectors):
            self.table.insertRow(row_idx)

            is_enabled = bool(conn.get("enabled", 1))
            auto       = bool(conn.get("auto_connect", 1))
            rf_ack     = bool(conn.get("rf_ack", 1))
            rig     = conn.get("rig_name", "")
            server  = conn.get("server",   DEFAULT_SERVER)
            port    = str(conn.get("tcp_port", DEFAULT_TCP_PORT))
            state   = conn.get("state",    "") or ""
            comment = conn.get("comment",  "") or ""

            if not is_enabled:
                status_text  = "Disabled"
                status_color = _COL_DISABLED
            elif status_map.get(rig, False):
                status_text  = "Connected"
                status_color = _COL_CONNECTED
            else:
                status_text  = "Disconnected"
                status_color = _COL_DISCONNECTED

            auto_text   = "Yes" if auto else "No"
            rf_ack_text = "Yes" if rf_ack else "No"

            for col_idx, val in enumerate(
                [rig, server, port, state, status_text, auto_text, rf_ack_text, comment]
            ):
                item = QTableWidgetItem(val)
                item.setFont(mono)
                item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
                if col_idx == _STATUS_COL:
                    item.setForeground(QtGui.QColor(status_color))
                if col_idx in (_AUTO_COL, _RF_ACK_COL):
                    item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(row_idx, col_idx, item)

            self.table.item(row_idx, 0).setData(Qt.UserRole, conn["id"])

        self._on_selection_changed()

    # ── Selection ──────────────────────────────────────────────────────────────

    def _on_selection_changed(self) -> None:
        if self._in_edit_mode:
            return
        has_sel = bool(self.table.selectedItems())
        self.btn_edit.setEnabled(has_sel)
        self.btn_delete.setEnabled(has_sel)
        self.btn_reconnect.setEnabled(has_sel)

    def _selected_connector_id(self) -> Optional[int]:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        return item.data(Qt.UserRole) if item else None

    # ── Inline edit ────────────────────────────────────────────────────────────

    def _enter_edit_mode(self, row: int, adding: bool) -> None:
        self._in_edit_mode = True
        self._edit_row = row
        self._adding = adding

        def _cell(col):
            item = self.table.item(row, col)
            return item.text() if item else ""

        self._iw_rig     = make_input(placeholder="e.g. IC-7300, FTDX10", max_len=20)
        self._iw_server  = make_input(default=DEFAULT_SERVER)
        self._iw_port    = make_input(default=str(DEFAULT_TCP_PORT), max_len=5)
        self._iw_state   = make_input(placeholder="e.g. TX", max_len=2)
        self._iw_auto    = self._make_checkbox()
        self._iw_rf_ack  = self._make_checkbox()
        self._iw_comment = make_input(placeholder="Optional Description", max_len=60)

        # Default Auto/RF Ack = checked for a new row; for an edit, read from the underlying DB row.
        auto_default = True
        rf_ack_default = True
        if not adding and self._edit_id is not None:
            conn = self.connector_manager.get_connector_by_id(self._edit_id)
            if conn:
                auto_default = bool(conn.get("auto_connect", 1))
                rf_ack_default = bool(conn.get("rf_ack", 1))
        self._iw_auto.setChecked(auto_default)
        self._iw_rf_ack.setChecked(rf_ack_default)

        self._iw_rig.setText("" if adding else _cell(0))
        self._iw_server.setText(_cell(1) if not adding and _cell(1) else DEFAULT_SERVER)
        self._iw_port.setText(_cell(2) if not adding and _cell(2) else str(DEFAULT_TCP_PORT))
        self._iw_state.setText("" if adding else _cell(3))
        self._iw_comment.setText("" if adding else _cell(_COMMENT_COL))

        self._iw_state.textChanged.connect(
            lambda t: self._iw_state.setText(t.upper()) if t != t.upper() else None
        )
        self._iw_rig.textChanged.connect(lambda _: self._on_inline_changed())
        self._iw_port.textChanged.connect(lambda _: self._on_inline_changed())

        # Qt ignores setMaximumWidth on a QLineEdit installed directly via
        # setCellWidget — the embedded widget gets stretched to fill the cell.
        # Wrap each constrained input in a QWidget+HBoxLayout so the container
        # fills the cell while the input itself keeps its fixed width.
        def _wrap_fixed(input_widget: QLineEdit, width_px: int) -> QWidget:
            input_widget.setFixedWidth(width_px)
            container = QWidget()
            layout = QHBoxLayout(container)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(0)
            layout.addWidget(input_widget)
            layout.addStretch()
            return container

        _rig_w, _server_w, _port_w, _state_w = 112, 121, 60, 40

        # Cols 0–3 are ResizeToContents and would clamp back to data width,
        # cutting off the inputs. Switch them to Interactive and force a width
        # that holds the input plus a little cell padding; _exit_edit_mode
        # restores ResizeToContents.
        _hh = self.table.horizontalHeader()
        for _c, _w in ((0, _rig_w), (1, _server_w), (2, _port_w), (3, _state_w)):
            _hh.setSectionResizeMode(_c, QHeaderView.Interactive)
            self.table.setColumnWidth(_c, _w + 16)

        # Install on all columns except the live Status column
        self.table.setCellWidget(row, 0, _wrap_fixed(self._iw_rig,    _rig_w))
        self.table.setCellWidget(row, 1, _wrap_fixed(self._iw_server, _server_w))
        self.table.setCellWidget(row, 2, _wrap_fixed(self._iw_port,   _port_w))
        self.table.setCellWidget(row, 3, _wrap_fixed(self._iw_state,  _state_w))
        # col 4 (_STATUS_COL) intentionally skipped
        self.table.setCellWidget(row, _AUTO_COL, self._iw_auto)
        self.table.setCellWidget(row, _RF_ACK_COL, self._iw_rf_ack)
        self.table.setCellWidget(row, _COMMENT_COL, self._iw_comment)
        self.table.setRowHeight(row, 42)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)

        self.btn_add.setVisible(False)
        self.btn_edit.setVisible(False)
        self.btn_delete.setVisible(False)
        self.btn_reconnect.setVisible(False)
        self.btn_save.setVisible(True)
        self.btn_cancel.setVisible(True)
        self.btn_close.setEnabled(False)

        QWidget.setTabOrder(self._iw_rig, self._iw_server)
        QWidget.setTabOrder(self._iw_server, self._iw_port)
        QWidget.setTabOrder(self._iw_port, self._iw_state)
        QWidget.setTabOrder(self._iw_state, self._iw_auto)
        QWidget.setTabOrder(self._iw_auto, self._iw_rf_ack)
        QWidget.setTabOrder(self._iw_rf_ack, self._iw_comment)
        QWidget.setTabOrder(self._iw_comment, self.btn_save)

        self._on_inline_changed()
        self._iw_rig.setFocus()

    def _make_checkbox(self) -> QCheckBox:
        """Centered checkbox cell widget for a flag column (Auto, RF Ack) during inline edit."""
        cb = QCheckBox()
        cb.setStyleSheet(
            f"QCheckBox {{ background-color:{_DATA_BG}; padding-left:14px; }}"
        )
        return cb

    def _on_inline_changed(self) -> None:
        if not self._in_edit_mode:
            return
        rig_ok  = bool(self._iw_rig and self._iw_rig.text().strip())
        port_ok = bool(self._iw_port and self._iw_port.text().strip().isdigit())
        self.btn_save.setEnabled(rig_ok and port_ok)

    def _exit_edit_mode(self, save: bool) -> None:
        row = self._edit_row

        if save:
            rig      = self._iw_rig.text().strip()
            server   = self._iw_server.text().strip() or DEFAULT_SERVER
            port_str = self._iw_port.text().strip()
            state    = self._iw_state.text().strip().upper()[:2]
            auto     = bool(self._iw_auto.isChecked())
            rf_ack   = bool(self._iw_rf_ack.isChecked())
            comment  = self._iw_comment.text().strip()

            if not rig:
                QMessageBox.warning(self, "JS8 Connectors", "Rig name is required.")
                return
            if not port_str.isdigit():
                QMessageBox.warning(self, "JS8 Connectors", "Port must be a number.")
                return
            port = int(port_str)

            if self._edit_id is None:
                ok = self.connector_manager.add_connector(
                    rig_name=rig, tcp_port=port, state=state,
                    comment=comment, server=server, auto_connect=auto, rf_ack=rf_ack,
                )
                action = "add"
            else:
                ok = self.connector_manager.update_connector(
                    connector_id=self._edit_id, rig_name=rig,
                    tcp_port=port, state=state, comment=comment, server=server,
                    auto_connect=auto, rf_ack=rf_ack,
                )
                action = "update"

            if not ok:
                QMessageBox.critical(
                    self, "Error",
                    f"Could not {action} connector.\n"
                    "Rig name or IP address/port combination may already be in use."
                )
                return

            if self.tcp_pool and hasattr(self.tcp_pool, "refresh_connections"):
                self.tcp_pool.refresh_connections()

        # Remove cell widgets — skip Status column (live, read-only)
        for col in [0, 1, 2, 3, _AUTO_COL, _RF_ACK_COL, _COMMENT_COL]:
            self.table.removeCellWidget(row, col)

        self._iw_rig = self._iw_server = self._iw_port = None
        self._iw_state = self._iw_auto = self._iw_rf_ack = self._iw_comment = None
        self._in_edit_mode = False
        self._edit_id = None

        self.table.setRowHeight(row, self.table.verticalHeader().defaultSectionSize())
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)

        # Restore the four columns to auto-fit after edit-mode widening.
        _hh = self.table.horizontalHeader()
        for _c in (0, 1, 2, 3):
            _hh.setSectionResizeMode(_c, QHeaderView.ResizeToContents)

        self.btn_add.setVisible(True)
        self.btn_edit.setVisible(True)
        self.btn_delete.setVisible(True)
        self.btn_reconnect.setVisible(True)
        self.btn_save.setVisible(False)
        self.btn_cancel.setVisible(False)
        self.btn_close.setEnabled(True)

        self._load_connectors()

    # ── Actions ────────────────────────────────────────────────────────────────

    def _on_add(self) -> None:
        if self._in_edit_mode:
            return
        connectors = self.connector_manager.get_all_connectors()
        if len(connectors) >= 5:
            QMessageBox.warning(self, "Limit Reached", "Maximum 5 connectors allowed.")
            return
        row = self.table.rowCount()
        self.table.insertRow(row)
        # Placeholder Status item so col 4 is never None during inline edit
        status_item = QTableWidgetItem("—")
        status_item.setFont(mono_font())
        status_item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
        status_item.setForeground(QtGui.QColor(_COL_DISABLED))
        self.table.setItem(row, _STATUS_COL, status_item)
        self._edit_id = None
        self._enter_edit_mode(row=row, adding=True)

    def _on_edit(self) -> None:
        if self._in_edit_mode:
            return
        cid = self._selected_connector_id()
        if cid is None:
            return
        self._edit_id = cid
        self._enter_edit_mode(row=self.table.currentRow(), adding=False)

    def _on_delete(self) -> None:
        cid = self._selected_connector_id()
        if cid is None:
            return
        row  = self.table.currentRow()
        name = self.table.item(row, 0).text() if self.table.item(row, 0) else "this connector"
        if not confirm(self, "Delete Connector", f"Delete connector '{name}'?",
                       no_label="Cancel"):
            return
        ok = self.connector_manager.remove_connector(cid)
        if ok:
            if self.tcp_pool and hasattr(self.tcp_pool, "refresh_connections"):
                self.tcp_pool.refresh_connections()
            self._load_connectors()
        else:
            QMessageBox.critical(
                self, "Cannot Delete",
                "Cannot delete this connector.\n\n"
                "You cannot delete the default connector or the last connector."
            )

    def _on_reconnect(self) -> None:
        cid = self._selected_connector_id()
        if cid is None:
            return
        self.connector_manager.set_enabled(cid, True)
        if self.tcp_pool and hasattr(self.tcp_pool, "refresh_connections"):
            self.tcp_pool.refresh_connections()
        self._load_connectors()

    # ── Live status refresh (safe during inline edit) ──────────────────────────

    def _refresh_status_column(self) -> None:
        status_map: dict = {}
        if self.tcp_pool and hasattr(self.tcp_pool, "get_connection_status"):
            status_map = self.tcp_pool.get_connection_status()
        connectors = self.connector_manager.get_all_connectors()

        for row_idx in range(self.table.rowCount()):
            if row_idx == self._edit_row and self._adding:
                continue  # new unsaved row has no connector record yet

            item = self.table.item(row_idx, _STATUS_COL)
            if item is None:
                continue

            if row_idx == self._edit_row:
                rig = self._iw_rig.text().strip() if self._iw_rig else ""
            else:
                col0 = self.table.item(row_idx, 0)
                rig = col0.text() if col0 else ""

            conn = next((c for c in connectors if c.get("rig_name") == rig), None)
            is_enabled = bool(conn.get("enabled", 1)) if conn else True

            if not is_enabled:
                text, color = "Disabled", _COL_DISABLED
            elif status_map.get(rig, False):
                text, color = "Connected", _COL_CONNECTED
            else:
                text, color = "Disconnected", _COL_DISCONNECTED

            item.setText(text)
            item.setForeground(QtGui.QColor(color))

    def _on_connection_changed(self, _rig_name: str, _connected: bool) -> None:
        if self._in_edit_mode:
            self._refresh_status_column()
        else:
            self._load_connectors()

    def closeEvent(self, event) -> None:
        if self.tcp_pool and hasattr(self.tcp_pool, "any_connection_changed"):
            try:
                self.tcp_pool.any_connection_changed.disconnect(self._on_connection_changed)
            except RuntimeError:
                pass
        super().closeEvent(event)
