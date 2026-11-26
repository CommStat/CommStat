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
  - A `MODULE::url1;url2;...` payload (hidden codes, not listed in
    _PRESET_CODES): each URL is an https file on the CommStat server. All of
    them are downloaded, then staged into updates/update.zip — the same file a
    heartbeat program_update drops — so commstat.py installs them on the next
    launch, and the user is asked to close CommStat just like a program update.
    Each file lands in the install folder under its URL's file name. Nothing is
    staged unless every download succeeds.
"""

import os
import posixpath
import re
import sqlite3
import urllib.parse
import urllib.request
import zipfile

from PyQt5 import QtGui
from PyQt5.QtCore import Qt, QThread, QTimer, QUrl, pyqtSignal
from PyQt5.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QScrollArea, QWidget

from constants import (
    DEFAULT_COLORS, COLOR_BTN_GREEN, COLOR_BTN_CLOSE, COMMSRVR_URL,
    COLOR_BTN_BLUE,
)
from db_utils import db_connect
from ui_helpers import (
    apply_standard_dialog_chrome, make_button, make_input, make_title_strip, connect_single,
    show_help_dialog, open_external_url, UpperCaseLineEdit,
)
from ssl_utils import create_verified_ssl_context

# ── Constants ──────────────────────────────────────────────────────────────────

_PANEL_BG = DEFAULT_COLORS.get("module_background", "#DDDDDD")
_PANEL_FG = DEFAULT_COLORS.get("module_foreground", "#000000")
_BOX_BG = QtGui.QColor(_PANEL_BG).lighter(110).name()

_MAINTENANCE_URL = COMMSRVR_URL + "/maintenance-808585.php"

_WIN_W = 720
_WIN_H = 400

# Module updates (MODULE:: payload) — staged where commstat.py looks on launch.
_INSTALL_DIR = os.path.dirname(os.path.abspath(__file__))
_UPDATES_DIR = os.path.join(_INSTALL_DIR, "updates")
_UPDATE_ZIP = os.path.join(_UPDATES_DIR, "update.zip")
_MODULE_HOST = urllib.parse.urlsplit(COMMSRVR_URL).hostname
_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# User data a module update must never replace (mirrors commstat.PROTECTED_FILES).
_PROTECTED_FILES = {"traffic.db3", "config.ini"}

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
    ("AC1984", "Download Latest Version of CommStat",
     "You can check the latest version of <b>CommStat</b> here: "
     "<a href='https://commstat.app/downloads.php'>https://commstat.app/downloads.php</a>."
     "<br><br>"
     "If you missed this version, use this code to retrieve it again.<br><br>"
     "<b><span style='color:#CC0000;'>Note:</span></b> The download trigger "
     "may take up to three minutes to complete."),
    ("DY4088", "Remove All 1-click Watchlists",
     "Removes every <b>1-click Watchlist</b> you have installed, along with "
     "its members and contacts (the 1-click data spans three tables in the "
     "local database). Watchlists you created yourself are not affected.<br><br>"
     "On <b>November 18, 2026</b>, photos were added to every 1-click "
     "Watchlist, and the watchlists that were missing addresses now have "
     "them. To get the updated data, run this code, then reinstall the "
     "watchlists you want from <b>Manage Watchlists</b> → <b>Presets</b>.<br><br>"
     "<b><span style='color:#CC0000;'>Note:</span></b> This update runs "
     "instantly — there is no waiting period."),
]


def _module_file_name(url: str) -> str:
    """Install-folder file name for a module URL; raises ValueError if the URL is
    not an https file on the CommStat server or would replace user data."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.hostname != _MODULE_HOST:
        raise ValueError(f"refusing file from outside the CommStat server: {url}")
    name = posixpath.basename(urllib.parse.unquote(parts.path))
    if not _SAFE_FILENAME.match(name):
        raise ValueError(f"bad file name in {url}")
    lowered = name.lower()
    if lowered in _PROTECTED_FILES or lowered.startswith("traffic.db3-"):
        raise ValueError(f"{name} is user data and is never replaced")
    return name


def _stage_module_update(urls: list) -> list:
    """Download every URL, then stage them all into updates/update.zip.

    Runs on the worker thread. All downloads must succeed before anything is
    written, so a failure leaves no half-staged update. If a program update is
    already waiting in update.zip, its files are kept (the module files replace
    any of the same name) so this doesn't throw away a pending build.
    Returns the staged file names.
    """
    files = {}
    for url in urls:
        name = _module_file_name(url)
        with urllib.request.urlopen(url, timeout=30, context=create_verified_ssl_context()) as resp:
            data = resp.read()
        if not data:
            raise ValueError(f"{name} downloaded empty")
        files[name] = data

    os.makedirs(_UPDATES_DIR, exist_ok=True)
    tmp_zip = _UPDATE_ZIP + ".tmp"
    try:
        with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as out:
            if os.path.exists(_UPDATE_ZIP):
                try:
                    with zipfile.ZipFile(_UPDATE_ZIP) as pending:
                        for info in pending.infolist():
                            if info.filename not in files:
                                out.writestr(info, pending.read(info))
                except zipfile.BadZipFile:
                    pass  # unreadable leftover — commstat.py would reject it anyway
            for name, data in files.items():
                out.writestr(name, data)
        os.replace(tmp_zip, _UPDATE_ZIP)
    finally:
        if os.path.exists(tmp_zip):
            os.remove(tmp_zip)
    return list(files)


class _MaintenanceWorker(QThread):
    """Sends the code to the maintenance endpoint and applies whatever it returns.

    config_updates is always emitted (empty dict on the SQL path) so the
    slot only ever has one place to look for config values to apply — see
    MaintenanceDialog._on_worker_result.
    """
    # (status, message, config_updates) — status in {"success","invalid","error","module_update"};
    # for "module_update" the message is the comma-separated staged file names.
    result_ready = pyqtSignal(str, str, dict)

    def __init__(self, code: str, callsign: str = ""):
        super().__init__()
        self.code = code
        self.callsign = callsign

    def run(self) -> None:
        import netguard
        if not netguard.guard("Maintenance code check"):
            self.result_ready.emit("error", "Off-Grid Mode is enabled — switch back to ONLINE to run this.", {})
            return

        try:
            url = _MAINTENANCE_URL + "?" + urllib.parse.urlencode(
                {"code": self.code, "cs": self.callsign}, quote_via=urllib.parse.quote)
            with urllib.request.urlopen(url, timeout=10, context=create_verified_ssl_context()) as resp:
                content = resp.read().decode("utf-8").strip()
        except Exception as e:
            self.result_ready.emit("error", f"Could not reach the server: {e}", {})
            return

        if content == "0" or not content:
            self.result_ready.emit("invalid", "Code is invalid.", {})
            return

        if content.startswith("MODULE::"):
            urls = [u.strip() for u in content[len("MODULE::"):].split(";") if u.strip()]
            if not urls:
                self.result_ready.emit("invalid", "Code is invalid.", {})
                return
            try:
                names = _stage_module_update(urls)
            except Exception as e:
                self.result_ready.emit("error", f"Module update failed: {e}", {})
                return
            self.result_ready.emit("module_update", ", ".join(names), {})
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
            with db_connect() as conn:
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

    def __init__(self, parent=None, callsign: str = ""):
        super().__init__(parent)
        self._callsign = callsign
        apply_standard_dialog_chrome(self, "Maintenance", _WIN_W, _WIN_H)
        self.setStyleSheet(f"QDialog {{ background-color: {_PANEL_BG}; }}")

        self._worker = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(12)

        layout.addWidget(make_title_strip("Maintenance"))

        input_row = QHBoxLayout()
        input_row.setSpacing(8)
        lbl_code = QLabel("Code:")
        lbl_code.setStyleSheet(
            f"QLabel {{ font-family:Roboto; font-size:13px; font-weight:bold; color:{_PANEL_FG}; }}"
        )
        input_row.addWidget(lbl_code)

        self.code_edit = make_input(max_len=6, widget=UpperCaseLineEdit())
        self.code_edit.setFixedWidth(100)
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

        presets_container = QWidget()
        presets_container.setStyleSheet(f"QWidget {{ background-color:{_PANEL_BG}; }}")
        presets_layout = QVBoxLayout(presets_container)
        presets_layout.setContentsMargins(0, 0, 0, 0)
        presets_layout.setSpacing(8)
        for code, title, description in _PRESET_CODES:
            presets_layout.addWidget(self._build_preset_code_box(code, title, description))
        presets_layout.addStretch()

        presets_scroll = QScrollArea()
        presets_scroll.setWidgetResizable(True)
        presets_scroll.setFrameShape(QFrame.NoFrame)
        presets_scroll.setWidget(presets_container)
        presets_scroll.setStyleSheet(f"QScrollArea {{ background-color:{_PANEL_BG}; border:none; }}")
        layout.addWidget(presets_scroll, 1)

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
        lbl_description.linkActivated.connect(
            lambda url: open_external_url(self, QUrl(url), panel_bg=_PANEL_BG)
        )
        lbl_description.setTextInteractionFlags(Qt.TextBrowserInteraction)
        lbl_description.setStyleSheet(
            f"QLabel {{ font-family:Roboto; font-size:13px; color:{_PANEL_FG}; border:none; }}"
            f"QLabel a {{ color:{COLOR_BTN_BLUE}; }}"
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

        self._worker = _MaintenanceWorker(code, self._callsign)
        self._worker.result_ready.connect(self._on_worker_result)
        self._worker.start()

    def release(self) -> None:
        """Free this dialog after exec_() returns (call instead of deleteLater()).

        The worker thread belongs to the dialog, and destroying a QThread that is
        still running aborts the program. If a request is still in flight, the
        dialog stays alive (hidden) until the worker finishes, so the result can
        still be applied."""
        worker = self._worker
        if worker is not None and worker.isRunning():
            worker.finished.connect(self.deleteLater)
        else:
            self.deleteLater()

    def _prompt_module_restart(self, file_names: str) -> None:
        """Mirror of MainWindow._show_program_update_notification for a module update.

        Runs even if the user closed Maintenance while the download was in
        flight — the files are already staged, so they still need to hear about it.
        """
        from ui_helpers import confirm
        main_window = self.parent()
        visible = self.isVisible()
        if confirm(
            self if visible else main_window,
            "Module Updated",
            f"A CommStat module has been updated ({file_names}).\n\n"
            f"Please restart the application to finish installing the update.\n\n"
            f"Close CommStat now?",
            default_yes=True,
            on_top=True,
        ):
            if visible:
                self.accept()
            # Deferred so exec_() in MainWindow._on_maintenance unwinds first; close()
            # runs closeEvent() (saves state, disconnects), and commstat.py
            # installs updates/update.zip on the next launch.
            if main_window is not None:
                QTimer.singleShot(0, main_window.close)
        elif visible:
            self.accept()

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

        if status == "module_update":
            self._prompt_module_restart(message)
            return

        if self.isVisible():
            show_help_dialog(self, "Maintenance", message, width=360, height=220)
            # A successful code is done; closing the popup closes Maintenance too.
            # Invalid/error results keep it open so the user can try again.
            if status == "success":
                self.accept()
