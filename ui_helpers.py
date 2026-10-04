# Copyright (c) 2025, 2026 Manuel Ochoa
# This file is part of CommStat.
# Licensed under the GNU General Public License v3.0.
"""
ui_helpers.py - Shared UI helpers for CommStat dialogs.

The styled widget factories (make_button, make_input, make_combobox,
make_checkbox_cell, UpperCaseLineEdit), the shared dialog scaffolding
(apply_standard_dialog_chrome, make_title_strip, DIALOG_TABLE_QSS, wrap_fixed),
the message boxes (show_error, show_info, confirm, confirm_delete_record,
prompt_text), the help dialogs (make_help_dialog, show_help_dialog) and the
Off-Grid-aware link opener (open_external_url).
"""

import os
import re
import sqlite3
from typing import Tuple

from PyQt5 import QtGui
from PyQt5.QtCore import Qt, QTimer, QUrl, pyqtSlot
from PyQt5.QtGui import QClipboard, QDesktopServices, QKeySequence
from PyQt5.QtWidgets import (
    QPushButton, QLineEdit, QCheckBox, QComboBox, QWidget, QHBoxLayout, QMessageBox,
    QDialog, QVBoxLayout, QLabel, QTextBrowser, QStyledItemDelegate, QApplication,
)

from constants import (
    FONT_ROBOTO, FONT_MONO, FONT_ROBOTO_STACK, FONT_MONO_STACK,
    COLOR_BTN_GREEN, COLOR_BTN_RED, COLOR_BTN_GRAY, COLOR_BTN_BLUE, COLOR_BTN_CLOSE,
    COLOR_INPUT_TEXT, COLOR_INPUT_BORDER, COLOR_DISABLED_BG, COLOR_DISABLED_TEXT,
    DEFAULT_COLORS, ICON_FILE,
)
from db_utils import db_connect


# ── Button ─────────────────────────────────────────────────────────────────────

def make_button(label: str, color: str, min_w: int = 90) -> QPushButton:
    """Standard styled action button: Roboto Bold 15px, colored background.

    Hover is a darker shade of *color* and pressed a darker one still
    (Qt style sheets have no opacity property, so the shades are computed).
    """
    base = QtGui.QColor(color)
    if base.isValid():
        hover, pressed = base.darker(125).name(), base.darker(150).name()
    else:
        hover = pressed = color
    b = QPushButton(label)
    b.setMinimumWidth(min_w)
    b.setStyleSheet(
        f"QPushButton {{ background-color:{color}; color:#ffffff; border:none;"
        f" padding:6px 14px; border-radius:4px; font-family:{FONT_ROBOTO_STACK}; font-size:15px;"
        f" font-weight:bold; }}"
        f"QPushButton:hover {{ background-color:{hover}; }}"
        f"QPushButton:pressed {{ background-color:{pressed}; }}"
        f"QPushButton:disabled {{ background-color:#cccccc; color:#888888; }}"
    )
    return b


def connect_single(button, handler, window_ms: int = 1000):
    """Connect button.clicked to handler so rapid repeat clicks (e.g. a
    double-click) fire the handler only once. A second click within window_ms
    is ignored. Does NOT touch setEnabled(), so it never interferes with
    validation- or mode-driven enable/disable logic."""
    state = {"busy": False}

    def _wrapped(*_args):
        if state["busy"]:
            return
        state["busy"] = True
        try:
            handler()
        finally:
            QTimer.singleShot(window_ms, lambda: state.update(busy=False))

    button.clicked.connect(_wrapped)
    return _wrapped


# ── Error / info boxes ────────────────────────────────────────────────────────

def show_error(parent, message: str, title: str = "CommStat Error") -> None:
    """Modal critical message box that stays on top of the dialog that raised it."""
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(message)
    box.setIcon(QMessageBox.Critical)
    box.setWindowFlag(Qt.WindowStaysOnTopHint)
    box.exec_()
    box.deleteLater()   # parented to a long-lived window: free it once closed


def show_info(parent, message: str, title: str = "CommStat") -> None:
    """Modal information message box that stays on top of the dialog that raised it."""
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(message)
    box.setIcon(QMessageBox.Information)
    box.setWindowFlag(Qt.WindowStaysOnTopHint)
    box.exec_()
    box.deleteLater()


# ── User Settings lookup ──────────────────────────────────────────────────────

def get_internet_user_settings() -> Tuple[str, str, str]:
    """Return (callsign, gridsquare, state) from User Settings for Internet-only sends.

    Callsign and state are uppercased; any of them is "" when unset or the DB is unreadable."""
    try:
        with db_connect() as conn:
            row = conn.execute(
                "SELECT callsign, gridsquare, state FROM controls WHERE id = 1"
            ).fetchone()
            if row:
                return (
                    (row[0] or "").strip().upper(),
                    (row[1] or "").strip(),
                    (row[2] or "").strip().upper(),
                )
    except sqlite3.Error:
        pass
    return ("", "", "")


# ── Confirmation dialog ───────────────────────────────────────────────────────

def confirm(parent, title: str, text: str,
            yes_label: str = "Yes", no_label: str = "No",
            default_yes: bool = False) -> bool:
    """Styled Yes/No confirmation. Returns True if user clicked the affirmative button."""
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(text)
    yes_btn = make_button(yes_label, COLOR_BTN_GREEN)
    no_btn = make_button(no_label, COLOR_BTN_GRAY)
    box.addButton(yes_btn, QMessageBox.YesRole)
    box.addButton(no_btn, QMessageBox.NoRole)
    box.setDefaultButton(yes_btn if default_yes else no_btn)
    box.exec_()
    answered_yes = box.clickedButton() is yes_btn
    box.deleteLater()
    return answered_yes


def confirm_delete_record(parent, record_kind: str) -> str:
    """Ask whether to also delete a record the user owns from all CommStat users.

    Returns "all" (Yes), "local" (No) or "cancel" (Cancel / dialog closed).
    """
    box = QMessageBox(parent)
    box.setWindowTitle("Confirm Delete")
    box.setText(
        f"You are about to delete a {record_kind}.\n"
        "Do you also want to delete this record from all CommStat users?"
    )
    yes_btn = make_button("Yes", COLOR_BTN_GREEN)
    no_btn = make_button("No", COLOR_BTN_RED)
    cancel_btn = make_button("Cancel", COLOR_BTN_GRAY)
    box.addButton(yes_btn, QMessageBox.YesRole)
    box.addButton(no_btn, QMessageBox.NoRole)
    box.addButton(cancel_btn, QMessageBox.RejectRole)
    box.setDefaultButton(cancel_btn)
    box.setEscapeButton(cancel_btn)
    box.exec_()
    clicked = box.clickedButton()
    box.deleteLater()
    if clicked is yes_btn:
        return "all"
    if clicked is no_btn:
        return "local"
    return "cancel"


# ── Text prompt dialog ─────────────────────────────────────────────────────────

def prompt_text(parent, title: str, label: str, default: str = "",
                max_len: int = 30, ok_label: str = "Save",
                panel_bg: str = None, prog_bg: str = None,
                prog_fg: str = None, panel_fg: str = None):
    """Ask for a single line of text in a styled dialog. Returns the stripped
    text, or None if the user cancelled or left it empty.

    Same chrome as every other CommStat dialog - Roboto Slab title strip in the
    program colors over a panel-colored body - so a prompt never falls back to
    Qt's unstyled QInputDialog.

    panel_bg / panel_fg / prog_bg / prog_fg: optional live theme colors; callers
    with a running ConfigManager should pass them so the prompt tracks a theme
    change (pass panel_fg with panel_bg, or the labels can end up unreadable).
    """
    panel_bg = panel_bg or DEFAULT_COLORS.get("module_background", "#E4E4E4")
    panel_fg = panel_fg or DEFAULT_COLORS.get("module_foreground", "#000000")
    prog_bg = prog_bg or DEFAULT_COLORS.get("program_background", "#A52A2A")
    prog_fg = prog_fg or DEFAULT_COLORS.get("program_foreground", "#FFFFFF")

    dlg = QDialog(parent)
    # Width is fixed, height is left to the layout: macOS and most Linux desktops
    # render the same 13px labels taller than Windows does, and a hardcoded
    # height would clip the button row there.
    apply_standard_dialog_chrome(dlg, title)
    dlg.setFixedWidth(380)
    dlg.setStyleSheet(
        f"QDialog {{ background-color:{panel_bg}; color:{panel_fg}; }}"
        f"QLabel {{ font-size:13px; color:{panel_fg}; }}"
    )

    body = QVBoxLayout(dlg)
    body.setContentsMargins(15, 15, 15, 15)
    body.setSpacing(10)

    body.addWidget(make_title_strip(title, prog_bg, prog_fg))

    field_lbl = QLabel(label)
    field_lbl.setFont(label_font())
    body.addWidget(field_lbl)

    field = make_input(default=default, max_len=max_len)
    body.addWidget(field)
    body.addSpacing(4)

    result = {"text": None}

    def _accept():
        text = field.text().strip()
        if not text:
            return          # Nothing to save; leave the dialog open.
        result["text"] = text
        dlg.accept()

    btn_row = QHBoxLayout()
    btn_row.setSpacing(8)
    btn_row.addStretch()
    ok_btn = make_button(ok_label, COLOR_BTN_GREEN, 80)
    ok_btn.clicked.connect(_accept)
    cancel_btn = make_button("Cancel", COLOR_BTN_CLOSE, 80)
    cancel_btn.clicked.connect(dlg.reject)
    btn_row.addWidget(ok_btn)
    btn_row.addWidget(cancel_btn)
    body.addLayout(btn_row)

    field.returnPressed.connect(_accept)
    field.setFocus()
    dlg.exec_()
    return result["text"]


# ── Input ──────────────────────────────────────────────────────────────────────

def make_input(placeholder: str = "", default: str = "", max_len: int = 0,
               widget: QLineEdit = None, read_only: bool = False) -> QLineEdit:
    """Standard styled QLineEdit: white background, Kode Mono 13px.

    widget: style an existing QLineEdit (or subclass, e.g. UpperCaseLineEdit)
    instead of creating a plain one.
    read_only: make the field read-only and give it the grey "display only"
    look (no blue focus border), so it doesn't look editable."""
    e = widget if widget is not None else QLineEdit()
    e.setReadOnly(read_only)
    if placeholder:
        e.setPlaceholderText(placeholder)
    if default:
        e.setText(default)
    if max_len:
        e.setMaxLength(max_len)
    e.setMinimumHeight(30)
    if read_only:
        e.setStyleSheet(
            f"QLineEdit {{ background-color:{COLOR_DISABLED_BG}; color:{COLOR_INPUT_TEXT};"
            f" border:1px solid {COLOR_INPUT_BORDER}; border-radius:4px; padding:2px 6px;"
            f" font-family:{FONT_MONO_STACK}; font-size:13px; }}"
        )
        return e
    e.setStyleSheet(
        f"QLineEdit {{ background-color:white; color:{COLOR_INPUT_TEXT}; border:1px solid {COLOR_INPUT_BORDER};"
        f" border-radius:4px; padding:2px 6px; font-family:{FONT_MONO_STACK}; font-size:13px; }}"
        f"QLineEdit:focus {{ border:1px solid {COLOR_BTN_BLUE}; }}"
        f"QLineEdit:disabled {{ background-color:{COLOR_DISABLED_BG}; color:{COLOR_DISABLED_TEXT}; }}"
    )
    return e


class UpperCaseLineEdit(QLineEdit):
    """QLineEdit that auto-uppercases typed and pasted characters.

    Use it standalone, or install it inside an editable QComboBox with
    combo.setLineEdit(UpperCaseLineEdit(combo)).

    Typing is uppercased in keyPressEvent. Pasting is handled in three places,
    because QLineEdit has no insertFromMimeData() hook (that belongs to
    QTextEdit) and its own Ctrl+V / context menu / middle-click code paths
    never call back into Python:
      - Ctrl+V / Shift+Insert: intercepted in keyPressEvent;
      - the context-menu Paste item: triggers the paste() slot, which this
        class overrides with @pyqtSlot so Qt dispatches to it;
      - middle-click paste (X11 selection): intercepted in mouseReleaseEvent.
    Pasted text is forced onto one line (line breaks become spaces).
    Dropping text onto the field is not handled.

    Why not a QValidator: on an editable QComboBox with NoInsert, a
    validator that rewrites text inside validate() silently blocks typed
    input until the line edit's text matches an existing item.

    Why not a textChanged / textEdited slot: calling setText() inside a
    signal slot re-enters Qt's signal-dispatch loop and corrupts the C++
    iterator on Linux/Qt5. blockSignals() and QTimer.singleShot() do not
    avoid this.

    An event filter on the line edit was tried as well, but on Windows the
    QLineEdit inside an editable QComboBox does not reliably fire installed
    eventFilters for KeyPress, so typed characters bypass it.
    """

    def _insert_upper(self, mode) -> None:
        text = QApplication.clipboard().text(mode)
        if text:
            self.insert(re.sub(r"[\r\n\t]+", " ", text).upper())

    @pyqtSlot()
    def paste(self) -> None:
        self._insert_upper(QClipboard.Clipboard)

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.Paste):
            self.paste()
            event.accept()
            return
        text = event.text()
        if text and text != text.upper():
            self.insert(text.upper())
            event.accept()
            return
        super().keyPressEvent(event)

    def mouseReleaseEvent(self, event):
        clipboard = QApplication.clipboard()
        if event.button() == Qt.MiddleButton and clipboard.supportsSelection():
            self.setFocus()
            self._insert_upper(QClipboard.Selection)
            event.accept()
            return
        super().mouseReleaseEvent(event)


# ── Combo box ──────────────────────────────────────────────────────────────────

def make_combobox(items, list_popup: bool = False, editable: bool = False,
                  compact: bool = False) -> QComboBox:
    """
    Standard styled QComboBox matching make_input.
    items: iterable of (label, value) tuples. Stored value via itemData.
    list_popup: use the dropdown-list popup style (combobox-popup:0, roomier
    items, up to 30 rows visible) that the Transmit dialogs use.
    editable: callsign-style editable combo: typed text is uppercased
    (UpperCaseLineEdit), nothing is inserted into the list and there is no
    completer. The items are added first, then the line edit is installed, so
    wire any combo.lineEdit() signals AFTER calling this.
    compact: skip the 30px minimum height (for dense grids of combos).
    """
    cb = QComboBox()
    if not compact:
        cb.setMinimumHeight(30)
    popup_qss = ""
    if list_popup:
        cb.setMaxVisibleItems(30)
        cb.setItemDelegate(QStyledItemDelegate(cb))
        popup_qss = (
            "QComboBox { combobox-popup:0; }"
            "QComboBox QAbstractItemView::item { min-height:22px; padding:0 6px; }"
        )
    cb.setStyleSheet(
        popup_qss +
        f"QComboBox {{ background-color:white; color:{COLOR_INPUT_TEXT}; border:1px solid {COLOR_INPUT_BORDER};"
        f" border-radius:4px; padding:2px 6px; font-family:{FONT_MONO_STACK}; font-size:13px; }}"
        f"QComboBox:focus {{ border:1px solid {COLOR_BTN_BLUE}; }}"
        f"QComboBox:disabled {{ background-color:{COLOR_DISABLED_BG}; color:{COLOR_DISABLED_TEXT}; }}"
        f"QComboBox QAbstractItemView {{ background-color:white; color:{COLOR_INPUT_TEXT};"
        " selection-background-color:#cce5ff; selection-color:#000000;"
        f" font-family:{FONT_MONO_STACK}; font-size:13px; }}"
    )
    for label, value in items:
        cb.addItem(label, value)
    if editable:
        cb.setEditable(True)
        cb.setInsertPolicy(QComboBox.NoInsert)
        cb.setCompleter(None)
        # setLineEdit() must come after setEditable(True); it replaces the editor.
        cb.setLineEdit(UpperCaseLineEdit(cb))
    return cb


# ── Checkbox cell ──────────────────────────────────────────────────────────────

def make_checkbox_cell(checked: bool = False) -> Tuple[QWidget, QCheckBox]:
    """Return (container_widget, checkbox) with the checkbox centered in the cell."""
    container = QWidget()
    container.setStyleSheet("background-color: transparent;")
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setAlignment(Qt.AlignCenter)
    cb = QCheckBox()
    cb.setChecked(checked)
    # The indicator is drawn explicitly: with only a size rule, the Fusion style
    # (Linux default) draws no box at all for an unchecked box. Checked = filled green.
    cb.setStyleSheet(
        "QCheckBox { background-color: transparent; }"
        f"QCheckBox::indicator {{ width:16px; height:16px; background-color:white;"
        f" border:1px solid {COLOR_INPUT_BORDER}; border-radius:3px; }}"
        f"QCheckBox::indicator:checked {{ background-color:{COLOR_BTN_GREEN};"
        f" border:1px solid {COLOR_BTN_GREEN}; }}"
        f"QCheckBox::indicator:disabled {{ background-color:{COLOR_DISABLED_BG};"
        f" border:1px solid {COLOR_INPUT_BORDER}; }}"
        f"QCheckBox::indicator:checked:disabled {{ background-color:{COLOR_DISABLED_TEXT};"
        f" border:1px solid {COLOR_DISABLED_TEXT}; }}"
    )
    layout.addWidget(cb)
    return container, cb


# ── Dialog scaffolding ─────────────────────────────────────────────────────────

_PROG_BG  = DEFAULT_COLORS.get("program_background",   "#A52A2A")
_PROG_FG  = DEFAULT_COLORS.get("program_foreground",   "#FFFFFF")
_TITLE_BG = DEFAULT_COLORS.get("title_bar_background", "#F07800")
_TITLE_FG = DEFAULT_COLORS.get("title_bar_foreground", "#FFFFFF")
_DATA_BG  = DEFAULT_COLORS.get("data_background",      "#F8F6F4")
_DATA_FG  = DEFAULT_COLORS.get("data_foreground",      "#000000")

def dialog_table_qss(data_bg: str = None, data_fg: str = None) -> str:
    """Stylesheet shared by every dialog's data table (settings dialogs and siblings).

    data_bg / data_fg: override the default data colors, for windows that receive
    the user's configured colors from the main window (e.g. Grid Finder)."""
    bg = data_bg or _DATA_BG
    fg = data_fg or _DATA_FG
    return (
        f"QTableWidget {{ background-color:{bg}; alternate-background-color:{bg};"
        f" gridline-color:#cccccc; color:{fg};"
        f" font-family:'Kode Mono'; font-size:13px; }}"
        f"QTableWidget::item {{ padding:4px 6px; }}"
        f"QHeaderView::section {{ background-color:{_TITLE_BG}; color:{_TITLE_FG};"
        f" padding:5px 6px; border:none; font-family:Roboto; font-size:13px;"
        f" font-weight:bold; }}"
        f"QTableWidget::item:selected {{ background-color:#cce5ff; color:#000000; }}"
    )


DIALOG_TABLE_QSS = dialog_table_qss()


def make_title_strip(text: str, bg: str = None, fg: str = None) -> QLabel:
    """Standard dialog title strip: Roboto Slab Black 16px in the program colors.

    bg / fg: override the default program colors (for dialogs that receive the
    user's configured colors from the main window)."""
    lbl = QLabel(text)
    lbl.setAlignment(Qt.AlignCenter)
    lbl.setFont(QtGui.QFont("Roboto Slab", -1, QtGui.QFont.Black))
    lbl.setFixedHeight(36)
    lbl.setStyleSheet(
        f"QLabel {{ background-color:{bg or _PROG_BG}; color:{fg or _PROG_FG};"
        f" font-family:'Roboto Slab'; font-size:16px; font-weight:900;"
        f" padding-top:9px; padding-bottom:9px; }}"
    )
    return lbl


def wrap_fixed(input_widget: QWidget, width_px: int) -> QWidget:
    """Wrap a fixed-width input for use with QTableWidget.setCellWidget.

    Qt force-stretches a widget installed directly via setCellWidget, ignoring
    its maximum width. The container fills the cell while the input keeps its
    fixed width (input on the left, stretch on the right)."""
    input_widget.setFixedWidth(width_px)
    container = QWidget()
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    layout.addWidget(input_widget)
    layout.addStretch()
    return container


def make_button_cell(button: QPushButton) -> QWidget:
    """Wrap a button for QTableWidget.setCellWidget so the cell keeps its padding
    (a widget set directly stretches to fill the whole cell)."""
    cell = QWidget()
    cell.setStyleSheet("background-color: transparent;")
    layout = QHBoxLayout(cell)
    layout.setContentsMargins(6, 4, 10, 4)
    layout.addWidget(button)
    return cell


# ── Fonts ──────────────────────────────────────────────────────────────────────

def label_font() -> QtGui.QFont:
    """Roboto Bold — for QLabel headings within dialogs."""
    return QtGui.QFont(FONT_ROBOTO, -1, QtGui.QFont.Bold)


def mono_font() -> QtGui.QFont:
    """Kode Mono — for table cells and data display."""
    return QtGui.QFont(FONT_MONO)


# ── Dialog chrome ──────────────────────────────────────────────────────────────

def apply_standard_dialog_chrome(dialog, title: str, w: int = 0, h: int = 0) -> None:
    """Set window flags, title, optional fixed size, and app icon for any CommStat dialog."""
    dialog.setWindowFlags(
        Qt.Window |
        Qt.CustomizeWindowHint |
        Qt.WindowTitleHint |
        Qt.WindowCloseButtonHint |
        Qt.WindowStaysOnTopHint
    )
    dialog.setWindowTitle(title)
    if w and h:
        dialog.setFixedSize(w, h)
    if os.path.exists(ICON_FILE):
        dialog.setWindowIcon(QtGui.QIcon(ICON_FILE))


# ── Help dialogs ───────────────────────────────────────────────────────────────

# Every CommStat help popup shares this shape: a Roboto Slab title strip in the
# program colors, an HTML body, and a Close button. Feature modules supply only
# the body — the scaffolding lives here so all five popups stay identical and
# nobody re-implements the chrome.
_HELP_BODY_CSS = f"""
QTextBrowser {{ background-color: #FFFFFF; color: {COLOR_INPUT_TEXT}; border: 1px solid #C8C8C8; padding: 10px;
    font-family: {FONT_ROBOTO_STACK}; font-size: 13px; }}
"""


def make_help_dialog(parent, title: str, body_html: str,
                     width: int = 460, height: int = 0,
                     panel_bg: str = None, prog_bg: str = None,
                     prog_fg: str = None) -> QDialog:
    """Build (but do not show) a standard CommStat help dialog.

    Args:
        parent:     Dialog parent.
        title:      Shown both in the OS title bar and the colored title strip.
        body_html:  Rich-text body. Qt supports a subset of HTML/CSS —
                    headings, <b>/<i>, <ul>, <table bgcolor=…> all render.
        width:      Dialog width.
        height:     Fixed height; 0 makes the dialog resizable, which is the
                    better default once the body is long enough to scroll.
        panel_bg / prog_bg / prog_fg:
                    Optional live theme colors. Callers with access to the
                    running ConfigManager can pass them so the popup tracks a
                    theme change; everyone else gets the DEFAULT_COLORS values.

    Returns the QDialog so callers can exec_() or show() it.
    """
    panel_bg = panel_bg or DEFAULT_COLORS.get("module_background", "#E4E4E4")
    prog_bg = prog_bg or DEFAULT_COLORS.get("program_background", "#A52A2A")
    prog_fg = prog_fg or DEFAULT_COLORS.get("program_foreground", "#FFFFFF")

    dlg = QDialog(parent)
    apply_standard_dialog_chrome(dlg, title)
    if height:
        dlg.setFixedSize(width, height)
    else:
        dlg.setMinimumSize(min(width, 620), 420)
        dlg.resize(width, 560)
    dlg.setStyleSheet(f"QDialog {{ background-color: {panel_bg}; }}")

    layout = QVBoxLayout(dlg)
    layout.setContentsMargins(15, 15, 15, 15)
    layout.setSpacing(10)

    layout.addWidget(make_title_strip(title, prog_bg, prog_fg))

    body = QTextBrowser()
    body.setOpenLinks(False)            # links are opened by open_external_url (Off-Grid aware)

    def _on_link(url: QUrl) -> None:
        if not url.scheme() and url.fragment():      # in-page anchor
            body.scrollToAnchor(url.fragment())
            return
        open_external_url(dlg, url, panel_bg=panel_bg, prog_bg=prog_bg, prog_fg=prog_fg)

    body.anchorClicked.connect(_on_link)
    body.setStyleSheet(_HELP_BODY_CSS)
    body.setHtml(body_html)
    body.moveCursor(QtGui.QTextCursor.Start)
    layout.addWidget(body, 1)

    btn_row = QHBoxLayout()
    btn_row.setSpacing(8)
    btn_row.addStretch()
    close_btn = make_button("Close", COLOR_BTN_CLOSE, 80)
    close_btn.clicked.connect(dlg.close)
    btn_row.addWidget(close_btn)
    layout.addLayout(btn_row)

    return dlg


def show_offgrid_notice(parent, feature: str, needs: str = "opens an external website", **colors) -> None:
    """Styled popup for a feature that is disabled while Off-Grid Mode is on.

    feature: the menu/button label, e.g. "Live Radiation Map".
    needs:   what it does, completing '"<feature>" <needs> and is disabled ...'.
    colors:  optional panel_bg / prog_bg / prog_fg (live theme) for the popup."""
    from html import escape
    body = (
        '<div style="font-family: Roboto; font-size: 13px;">'
        f"<p>&ldquo;{escape(feature)}&rdquo; {needs} and is disabled while Off-Grid Mode is on.</p>"
        "<p>Switch back to ONLINE in the header to use it.</p></div>"
    )
    show_help_dialog(parent, "Off-Grid Mode", body, width=420, **colors)


def open_external_url(parent, url, **colors) -> None:
    """Open a link in the OS browser, unless Off-Grid Mode is on (then show the styled notice).

    url:    a QUrl or a string. Only http and https links are ever opened.
    colors: optional panel_bg / prog_bg / prog_fg (live theme) for the Off-Grid notice.
    Use this for every link the user can click, so none of them bypasses Off-Grid Mode."""
    import netguard
    if isinstance(url, str):
        url = QUrl(url)
    if url.scheme().lower() not in ("http", "https"):
        return
    if not netguard.guard(f"{url.host()} link"):
        show_offgrid_notice(parent, url.host() or url.toString(), **colors)
        return
    if not QDesktopServices.openUrl(url):
        show_error(parent, f"Could not open the link in your web browser:\n{url.toString()}")


def show_help_dialog(parent, title: str, body_html: str,
                     width: int = 460, height: int = 0, **colors) -> None:
    """Build and modally show a standard CommStat help dialog, then free it."""
    dlg = make_help_dialog(parent, title, body_html, width, height, **colors)
    dlg.exec_()
    dlg.deleteLater()
