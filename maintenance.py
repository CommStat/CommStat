# Copyright (c) 2026 Manuel Ochoa
# This file is part of CommStat.
# Licensed under the GNU General Public License v3.0.
"""
maintenance.py - Maintenance Dialog

Lets support staff enter a short code given to a user who needs special
attention. The code is sent to the commsrvr maintenance endpoint, which
replies with "0" (invalid code) and otherwise one of two payload shapes:
  - One or more `;`-separated SQL statements, run against the local database
    (the original behavior — a manually-triggered variant of the same
    trusted server-push channel the heartbeat's db_update uses; see
    little_gucci.py:_handle_db_update).
  - A `CONFIG::key=value;key2=value2` payload, applied to the running
    ConfigManager (and persisted to config.ini) via `config.set_<key>()` —
    used to deliver values that shouldn't be hardcoded/committed in the repo,
    e.g. the CARTO map tile API key. Only keys with an existing `set_<key>`
    method on ConfigManager are ever applied; anything else is silently
    ignored, so the server can't push arbitrary config.
"""

import sqlite3
import urllib.parse
import urllib.request

from PyQt5 import QtGui, QtWidgets
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QFrame

from constants import (
    DEFAULT_COLORS, COLOR_BTN_GREEN, COLOR_BTN_CLOSE, DATABASE_FILE,
    COLOR_BTN_BLUE, COLOR_INPUT_TEXT, COLOR_INPUT_BORDER, FONT_MONO_STACK,
)
from ui_helpers import apply_standard_dialog_chrome, make_button, connect_single, show_help_dialog
from little_gucci import _COMMSRVR, create_verified_ssl_context, UpperCaseLineEdit

# ── Constants ──────────────────────────────────────────────────────────────────

_PROG_BG = DEFAULT_COLORS.get("program_background", "#A52A2A")
_PROG_FG = DEFAULT_COLORS.get("program_foreground", "#FFFFFF")
_PANEL_BG = DEFAULT_COLORS.get("module_background", "#DDDDDD")
_PANEL_FG = DEFAULT_COLORS.get("module_foreground", "#000000")
_BOX_BG = QtGui.QColor(_PANEL_BG).lighter(110).name()

_MAINTENANCE_URL = _COMMSRVR + "/maintenance-808585.php"

_WIN_W = 720
_WIN_H = 540

_PRESET_CODES = [
    ("GY9875", "Install Dark Map API Key",
     "Fetches the map tile API key from the <b>CommStat</b> server and saves "
     "it locally, so the Dark Map basemap renders without the "
     "\"API key required\" watermark.<br><br>"
     "Safe to run more than once — re-running it just refreshes the saved key."),
    ("AA3815", "Refresh QRZ Contacts",
     "If you <b>DO NOT</b> have a <b>QRZ subscription</b>, running this code "
     "will trigger an update from the <b>CommStat</b> server and refresh all "
     "of your QRZ contacts in the local database.<br><br>"
     "If you have a <b>QRZ subscription</b>, <b>do not</b> run this code. "
     "Your <b>QRZ subscription</b> keeps your contact information updated "
     "automatically.<br><br>"
     "For users without a <b>QRZ subscription</b>, you can run this code "
     "twice a year. The <b>CommStat</b> server’s QRZ data will be updated "
     "in January and July.<br><br>"
     "<b><span style='color:#CC0000;'>Note:</span></b> This update may take "
     "up to 10 minutes to complete, depending on the number of records "
     "being updated."),
]


class _MaintenanceWorker(QThread):
    """Sends the code to the maintenance endpoint and applies whatever it returns.

    config_updates is always emitted (empty dict on the SQL path) so the
    slot only ever has one place to look for config values to apply — see
    MaintenanceDialog._on_worker_result.
    """
    result_ready = pyqtSignal(str, str, dict)  # (status, message, config_updates) — status in {"success","invalid","error"}

    def __init__(self, code: str):
        super().__init__()
        self.code = code

    def run(self) -> None:
        import netguard
        if not netguard.guard("Maintenance code check"):
            self.result_ready.emit("error", "Off-Grid Mode is enabled — switch back to ONLINE to run this.", {})
            return

        try:
            url = f"{_MAINTENANCE_URL}?code={urllib.parse.quote(self.code)}"
            with urllib.request.urlopen(url, timeout=10, context=create_verified_ssl_context()) as resp:
                content = resp.read().decode("utf-8").strip()
        except Exception as e:
            self.result_ready.emit("error", f"Could not reach the server: {e}", {})
            return

        if content == "0" or not content:
            self.result_ready.emit("invalid", "Code is invalid.", {})
            return

        if content.startswith("CONFIG::"):
            pairs = [p.strip() for p in content[len("CONFIG::"):].split(";") if p.strip()]
            config_updates = dict(p.split("=", 1) for p in pairs if "=" in p)
            if not config_updates:
                self.result_ready.emit("invalid", "Code is invalid.", {})
                return
            self.result_ready.emit(
                "success",
                "The update was applied successfully.",
                config_updates,
            )
            return

        statements = [s.strip() for s in content.split(";") if s.strip()]
        if not statements:
            self.result_ready.emit("invalid", "Code is invalid.", {})
            return

        try:
            with sqlite3.connect(DATABASE_FILE, timeout=10) as conn:
                cursor = conn.cursor()
                for sql in statements:
                    cursor.execute(sql)
                conn.commit()
        except sqlite3.Error as e:
            self.result_ready.emit("error", f"Update failed: {e}", {})
            return

        self.result_ready.emit("success", "The update was applied successfully.", {})


class MaintenanceDialog(QDialog):
    """Small dialog: enter a code, click Run, apply whatever the server returns."""

    def __init__(self, parent=None):
        super().__init__(parent)
        apply_standard_dialog_chrome(self, "Maintenance", _WIN_W, _WIN_H)
        self.setStyleSheet(f"QDialog {{ background-color: {_PANEL_BG}; }}")

        self._worker = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(12)

        title = QLabel("Maintenance")
        title.setAlignment(Qt.AlignCenter)
        title.setFont(QtGui.QFont("Roboto Slab", -1, QtGui.QFont.Black))
        title.setFixedHeight(36)
        title.setStyleSheet(
            f"QLabel {{ background-color: {_PROG_BG}; color: {_PROG_FG}; "
            "font-size: 16px; padding-top: 9px; padding-bottom: 9px; }"
        )
        layout.addWidget(title)

        input_row = QHBoxLayout()
        input_row.setSpacing(8)
        lbl_code = QLabel("Code:")
        lbl_code.setStyleSheet(
            f"QLabel {{ font-family:Roboto; font-size:12px; font-weight:bold; color:{_PANEL_FG}; }}"
        )
        input_row.addWidget(lbl_code)

        self.code_edit = UpperCaseLineEdit()
        self.code_edit.setMaxLength(6)
        self.code_edit.setMinimumHeight(30)
        self.code_edit.setFixedWidth(100)
        self.code_edit.setStyleSheet(
            f"QLineEdit {{ background-color:white; color:{COLOR_INPUT_TEXT}; border:1px solid {COLOR_INPUT_BORDER};"
            f" border-radius:4px; padding:2px 6px; font-family:{FONT_MONO_STACK}; font-size:13px; }}"
            f"QLineEdit:focus {{ border:1px solid {COLOR_BTN_BLUE}; }}"
        )
        self.code_edit.returnPressed.connect(self._on_run)
        input_row.addWidget(self.code_edit)

        self.btn_run = make_button("Run", COLOR_BTN_GREEN, 80)
        connect_single(self.btn_run, self._on_run)
        input_row.addWidget(self.btn_run)
        input_row.addStretch()

        btn_close = make_button("Close", COLOR_BTN_CLOSE, 80)
        btn_close.clicked.connect(self.close)
        input_row.addWidget(btn_close)
        layout.addLayout(input_row)

        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet(
            f"QLabel {{ font-family:Roboto; font-size:13px; color:{_PANEL_FG}; }}"
        )
        layout.addWidget(self.lbl_status)

        lbl_presets_heading = QLabel("Available Codes")
        lbl_presets_heading.setStyleSheet(
            f"QLabel {{ font-family:Roboto; font-size:13px; font-weight:bold; color:{_PANEL_FG}; }}"
        )
        layout.addWidget(lbl_presets_heading)

        for code, title, description in _PRESET_CODES:
            layout.addWidget(self._build_preset_code_box(code, title, description))

        layout.addStretch()

    def _build_preset_code_box(self, code: str, title: str, description: str) -> QFrame:
        """Builds a bordered "code + title + description" box for the Available Codes section."""
        box = QFrame()
        box.setStyleSheet(
            f"QFrame {{ background-color:{_BOX_BG}; border:1px solid #999999; border-radius:4px; }}"
        )

        row = QVBoxLayout(box)
        row.setContentsMargins(8, 8, 8, 8)
        row.setSpacing(4)

        lbl_code = QLabel(
            f"<span style='font-family:Roboto;'>{code}</span> "
            f"<span style='font-family:Roboto;'>— {title}</span>"
        )
        lbl_code.setStyleSheet(
            f"QLabel {{ font-size:13px; font-weight:bold; border:none; color:{_PANEL_FG}; }}"
        )
        row.addWidget(lbl_code)

        lbl_description = QLabel(description)
        lbl_description.setWordWrap(True)
        lbl_description.setStyleSheet(
            "QLabel { font-family:Roboto; font-size:13px; color:#333333; border:none; }"
        )
        row.addWidget(lbl_description)

        return box

    def _on_run(self) -> None:
        code = self.code_edit.text().strip()
        if not code:
            self.lbl_status.setText("Enter a code.")
            return

        self.code_edit.setEnabled(False)
        self.btn_run.setEnabled(False)
        self.lbl_status.setText("Checking…")

        self._worker = _MaintenanceWorker(code)
        self._worker.result_ready.connect(self._on_worker_result)
        self._worker.start()

    def _on_worker_result(self, status: str, message: str, config_updates: dict) -> None:
        self.code_edit.setEnabled(True)
        self.btn_run.setEnabled(True)
        self.lbl_status.setText("")

        if config_updates:
            config = getattr(self.parent(), "config", None)
            for key, value in config_updates.items():
                setter = getattr(config, f"set_{key}", None) if config else None
                if setter:
                    setter(value)

        show_help_dialog(self, "Maintenance", message, width=360, height=220)
