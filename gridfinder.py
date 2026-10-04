# Copyright (c) 2025, 2026 Manuel Ochoa
# This file is part of CommStat.
# Licensed under the GNU General Public License v3.0.
# AI Assistance: Claude (Anthropic), ChatGPT (OpenAI)

"""
Grid Finder for CommStat
Look up a Maidenhead grid square by city, state/country, or grid using
gridsearchdata.csv. Opened from Tools > Grid Finder, and from the StatRep and
Incident dialogs to fill in a grid.
"""

import sys
import os
import pandas as pd
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QTableWidget, QTableWidgetItem,
    QMessageBox, QHeaderView, QAbstractButton,
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QFontDatabase, QIcon

from constants import (
    DEFAULT_COLORS, COLOR_INPUT_BORDER,
    COLOR_BTN_RED, COLOR_BTN_CYAN,
)
from ui_helpers import (
    make_button, make_input, make_title_strip, apply_standard_dialog_chrome, dialog_table_qss,
)

_PANEL_BG = DEFAULT_COLORS.get("module_background",    "#DDDDDD")
_PANEL_FG = DEFAULT_COLORS.get("module_foreground",    "#000000")
_TITLE_BG = DEFAULT_COLORS.get("title_bar_background", "#F07800")
_TITLE_FG = DEFAULT_COLORS.get("title_bar_foreground", "#FFFFFF")
_DATA_BG  = DEFAULT_COLORS.get("data_background",      "#F8F6F4")
_DATA_FG  = DEFAULT_COLORS.get("data_foreground",      "#000000")

_COL_CANCEL = "#555555"


def format_grid(grid: str) -> str:
    """Format grid as EM83cv: first 2 uppercase, digits unchanged, last 2 lowercase."""
    g = grid.strip()
    if len(g) >= 6:
        return g[:2].upper() + g[2:4] + g[4:6].lower()
    if len(g) >= 4:
        return g[:2].upper() + g[2:4]
    return g.upper()


class GridFinderApp(QMainWindow):
    grid_selected = pyqtSignal(str)

    def __init__(self, panel_bg: str = "#F8F6F4", panel_fg: str = "#333333",
                 data_bg: str = "#F8F6F4", data_fg: str = "#333333", parent=None):
        super().__init__(parent)
        self.panel_bg = panel_bg
        self.panel_fg = panel_fg
        self.data_bg  = data_bg
        self.data_fg  = data_fg

        apply_standard_dialog_chrome(self, "Grid Finder")
        self.resize(620, 520)
        self.setMinimumSize(500, 440)

        self.data = self._load_data()
        if not self.data.empty:
            self.data['City_lower']  = self.data['City'].str.lower().str.strip()
            self.data['State_lower'] = self.data['State'].str.lower().str.strip()
            self.data['MGrid_lower'] = self.data['MGrid'].str.lower().str.strip()

        self.debounce_timer = QTimer()
        self.debounce_timer.setSingleShot(True)
        self.debounce_timer.timeout.connect(self._filter_data)

        self._setup_ui()
        self._apply_stylesheet()

    # ── Data loading ──────────────────────────────────────────────────────────

    def _load_data(self) -> pd.DataFrame:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        csv_path = os.path.join(script_dir, "gridsearchdata.csv")
        if not os.path.exists(csv_path):
            QMessageBox.critical(None, "Grid Finder Error",
                                 f"gridsearchdata.csv not found in:\n{script_dir}")
            return pd.DataFrame()
        try:
            df = pd.read_csv(csv_path, encoding='utf-8')
            df['MGrid'] = df['MGrid'].astype(str).str.strip().str.upper()
            df['City']  = df['City'].astype(str).str.strip()
            df['State'] = df['State'].astype(str).str.strip()
            return df
        except Exception as e:
            QMessageBox.critical(None, "Grid Finder Error", f"Failed to load data:\n{e}")
            return pd.DataFrame()

    # ── UI construction ───────────────────────────────────────────────────────

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setSpacing(10)
        layout.setContentsMargins(15, 15, 15, 10)

        # Title
        layout.addWidget(make_title_strip("Grid Finder"))

        # City field
        self.city_input = make_input(placeholder="City")
        layout.addWidget(self.city_input)

        # State + Grid row
        row2 = QHBoxLayout()
        row2.setSpacing(8)

        self.state_input = make_input(placeholder="State (US) or Country")
        row2.addWidget(self.state_input, stretch=2)

        self.grid_input = make_input(placeholder="Grid", max_len=6)
        row2.addWidget(self.grid_input, stretch=1)

        layout.addLayout(row2)

        # Tab order: city → state → grid → city
        self.setTabOrder(self.city_input, self.state_input)
        self.setTabOrder(self.state_input, self.grid_input)
        self.setTabOrder(self.grid_input, self.city_input)

        # Results table
        self.table = QTableWidget()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["City", "State / Country", "Grid"])
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.table.setFocusPolicy(Qt.NoFocus)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.Interactive)
        hh.setSectionResizeMode(2, QHeaderView.Interactive)
        layout.addWidget(self.table)

        # Button row
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch()

        self.clear_btn = make_button("Clear", COLOR_BTN_RED)
        btn_row.addWidget(self.clear_btn)

        self.copy_btn = make_button("Copy", COLOR_BTN_CYAN)
        btn_row.addWidget(self.copy_btn)

        self.cancel_btn = make_button("Cancel", _COL_CANCEL)
        btn_row.addWidget(self.cancel_btn)

        layout.addLayout(btn_row)

        # Signals
        self.city_input.textChanged.connect(self._on_text_changed)
        self.state_input.textChanged.connect(self._on_text_changed)
        self.grid_input.textChanged.connect(self._on_text_changed)
        self.table.clicked.connect(self._on_row_clicked)
        self.clear_btn.clicked.connect(self._on_clear)
        self.copy_btn.clicked.connect(self._on_copy)
        self.cancel_btn.clicked.connect(self.close)

        self.city_input.setFocus()

    def _apply_stylesheet(self):
        self.setStyleSheet(f"""
            QMainWindow {{ background-color: {self.panel_bg}; }}
            QWidget {{ background-color: {self.panel_bg}; color: {self.panel_fg}; }}
            QLabel {{ background-color: transparent; color: {self.panel_fg};
                      font-family: Roboto; font-size: 13px; }}
        """)
        self.table.setStyleSheet(dialog_table_qss(self.data_bg, self.data_fg))
        # Set on the header widget itself: when Grid Finder has a parent that carries its
        # own stylesheet (e.g. StatRep), a section rule in the window sheet is overridden
        # and the header falls back to the native (dark on macOS) look.
        self.table.horizontalHeader().setStyleSheet(f"""
            QHeaderView::section {{
                background-color: {_TITLE_BG}; color: {_TITLE_FG};
                border: 1px solid {COLOR_INPUT_BORDER};
                padding: 4px; font-family: Roboto; font-size: 13px; font-weight: bold;
            }}
        """)
        # Row-number header: same orange as the column headers, with "Row" on the
        # corner button above it. Set on the widgets themselves for the same reason.
        vh = self.table.verticalHeader()
        vh.setFixedWidth(48)
        vh.setDefaultAlignment(Qt.AlignCenter)
        vh.setStyleSheet(f"""
            QHeaderView {{ background-color: {self.data_bg}; }}
            QHeaderView::section {{
                background-color: {_TITLE_BG}; color: {_TITLE_FG};
                border: 1px solid {COLOR_INPUT_BORDER};
                padding: 2px; font-family: Roboto; font-size: 13px; font-weight: bold;
            }}
        """)
        corner = self.table.findChild(QAbstractButton)
        if corner is not None:
            corner.setStyleSheet(
                f"QAbstractButton {{ background-color: {_TITLE_BG}; "
                f"border: 1px solid {COLOR_INPUT_BORDER}; }}"
            )
            lbl = QLabel("Row", corner)
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setStyleSheet(
                f"QLabel {{ background-color: {_TITLE_BG}; color: {_TITLE_FG}; "
                f"font-family: Roboto; font-size: 13px; font-weight: bold; }}"
            )
            lay = QHBoxLayout(corner)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.addWidget(lbl)
            corner.setAutoFillBackground(True)

    # ── Layout helpers ────────────────────────────────────────────────────────

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_column_widths()

    def _update_column_widths(self):
        w = self.table.viewport().width()
        quarter = max(60, int(w * 0.25))
        self.table.setColumnWidth(1, quarter)  # State: 25%
        self.table.setColumnWidth(2, quarter)  # Grid:  25%
        # City (col 0) fills the remainder via QHeaderView.Stretch

    # ── Event handlers ────────────────────────────────────────────────────────

    def _on_text_changed(self):
        self.debounce_timer.start(400)

    def _filter_data(self):
        city_q  = self.city_input.text().strip().lower()
        state_q = self.state_input.text().strip().lower()
        grid_q  = self.grid_input.text().strip().lower()

        if not any([city_q, state_q, grid_q]):
            self._populate_table(pd.DataFrame())
            return

        filtered = self.data
        if city_q:
            filtered = filtered[filtered['City_lower'].str.contains(city_q, na=False)]
        if state_q:
            filtered = filtered[filtered['State_lower'].str.contains(state_q, na=False)]
        if grid_q:
            filtered = filtered[filtered['MGrid_lower'].str.contains(grid_q, na=False)]

        self._populate_table(self._rank_results(filtered, city_q, state_q))

    @staticmethod
    def _rank_results(df: pd.DataFrame, city_q: str, state_q: str) -> pd.DataFrame:
        """Order matches by relevance: exact city, then city starting with the query,
        then city containing it; exact state before partial; then city, state, grid."""
        if df.empty:
            return df
        df = df.copy()
        if city_q:
            city = df['City_lower']
            df['_city_rank'] = 2
            df.loc[city.str.startswith(city_q), '_city_rank'] = 1
            df.loc[city == city_q, '_city_rank'] = 0
        else:
            df['_city_rank'] = 0
        if state_q:
            df['_state_rank'] = (df['State_lower'] != state_q).astype(int)
        else:
            df['_state_rank'] = 0
        return df.sort_values(
            ['_state_rank', '_city_rank', 'State_lower', 'City_lower', 'MGrid_lower'],
            kind='stable')

    def _populate_table(self, df: pd.DataFrame):
        self.table.setSortingEnabled(False)
        self.table.clearContents()
        self.table.setRowCount(0)

        if df.empty:
            return

        self.table.setRowCount(len(df))
        for i, (_, row) in enumerate(df.iterrows()):
            self.table.setItem(i, 0, QTableWidgetItem(row['City']))
            self.table.setItem(i, 1, QTableWidgetItem(row['State']))
            self.table.setItem(i, 2, QTableWidgetItem(format_grid(row['MGrid'])))

        # No column sort indicator: keep the relevance order until a header is clicked.
        self.table.horizontalHeader().setSortIndicator(-1, Qt.AscendingOrder)
        self.table.setSortingEnabled(True)
        self._update_column_widths()

    def _on_row_clicked(self, index):
        row = index.row()
        grid_item = self.table.item(row, 2)
        if not grid_item:
            return
        formatted = format_grid(grid_item.text())
        self.grid_input.blockSignals(True)
        self.grid_input.setText(formatted)
        self.grid_input.blockSignals(False)

    def _on_clear(self):
        self.city_input.clear()
        self.state_input.clear()
        self.grid_input.clear()
        self.table.clearContents()
        self.table.setRowCount(0)
        self.city_input.setFocus()

    def _on_copy(self):
        grid = self.grid_input.text().strip()
        if grid:
            QApplication.clipboard().setText(grid)
            self.grid_selected.emit(grid)


if __name__ == '__main__':
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    app.setStyle('Fusion')

    font_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fonts')
    for font_file in ('Roboto-Regular.ttf', 'Roboto-Bold.ttf',
                      'RobotoSlab-Regular.ttf', 'RobotoSlab-Bold.ttf', 'RobotoSlab-Black.ttf',
                      'KodeMono-Regular.ttf', 'KodeMono-Medium.ttf', 'KodeMono-Bold.ttf'):
        font_path = os.path.join(font_dir, font_file)
        if os.path.exists(font_path):
            QFontDatabase.addApplicationFont(font_path)

    if os.path.exists("radiation-32.png"):
        app.setWindowIcon(QIcon("radiation-32.png"))

    window = GridFinderApp(
        DEFAULT_COLORS.get("module_background", "#DDDDDD"), DEFAULT_COLORS.get("module_foreground", "#000000"),
        DEFAULT_COLORS.get("data_background", "#F8F6F4"), DEFAULT_COLORS.get("data_foreground", "#000000"),
    )
    window.show()
    sys.exit(app.exec_())
