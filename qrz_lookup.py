# Copyright (c) 2025 Manuel Ochoa
# This file is part of CommStat.
# Licensed under the GNU General Public License v3.0.
"""
qrz_lookup.py - QRZ Callsign Lookup Dialogs for CommStat

Three modal dialog views:
  - QRZLookupDialog     : standalone search (Tools → QRZ Lookup)
  - StatRepDetailDialog : detail view when clicking a StatRep row
  - MessageDetailDialog : detail view when clicking a Message row
"""

import base64
import datetime
import io
import os
import sqlite3
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from typing import Callable, Dict, Optional

import folium
import maidenhead as mh
from PyQt5 import QtGui
from PyQt5.QtCore import QBuffer, QByteArray, QObject, QSize, Qt, QThread, QUrl, pyqtSignal
from PyQt5.QtGui import QColor, QCursor, QDesktopServices, QFont, QMovie, QPainter, QPixmap
from PyQt5.QtWebEngineWidgets import QWebEngineView
from PyQt5.QtWidgets import (
    QComboBox, QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPlainTextEdit, QPushButton, QSizePolicy, QStyledItemDelegate,
    QTextBrowser, QTextEdit, QVBoxLayout, QWidget,
)

from id_utils import generate_time_based_id
from qrz_client import QRZClient, get_qrz_cached, load_qrz_config, subscription_status
from constants import (
    DEFAULT_COLORS, COLOR_INPUT_TEXT, COLOR_INPUT_BORDER,
    COLOR_BTN_RED, COLOR_BTN_BLUE, COLOR_BTN_CYAN, COLOR_BTN_GREEN,
    RIG_FREQ_DELAY_MS,
)
# Single source of truth for mouse-wheel zoom dampening — see little_gucci.py
from little_gucci import MAP_WHEEL_PX_PER_ZOOM, create_verified_ssl_context
from ui_helpers import apply_standard_dialog_chrome, connect_single

DB_PATH = "traffic.db3"
_COMMSRVR_URL  = base64.b64decode("aHR0cHM6Ly9jb21tc3RhdC5hcHA=").decode()
_DATAFEED_URL  = _COMMSRVR_URL + "/datafeed-808585.php"

_PROG_BG    = DEFAULT_COLORS.get("program_background", "#000000")
_PROG_FG    = DEFAULT_COLORS.get("program_foreground", "#FFFFFF")
_DATA_BG    = DEFAULT_COLORS.get("data_background",    "#F8F6F4")
_COL_CANCEL = "#555555"
_COL_PURPLE = "#6f42c1"
_COL_NAV    = "#e07b39"

# StatRep status field order: (display label, statrep row index)
STATUS_FIELDS = [
    ("Map",    8),
    ("Power",  9),
    ("Water", 10),
    ("Med",   11),
    ("Comms", 12),
    ("Travel",13),
    ("Inet",  14),
    ("Fuel",  15),
    ("Food",  16),
    ("Crime", 17),
    ("Civil", 18),
    ("Pol",   19),
]

# Status value → (CSS color string, tooltip text)
STATUS_COLORS: Dict[str, tuple] = {
    "1": ("rgb(0, 128, 0)",     "Green: Normal"),
    "2": ("rgb(255, 255, 0)",   "Yellow: Warning"),
    "3": ("rgb(255, 0, 0)",     "Red: Critical"),
    "4": ("rgb(128, 128, 128)", "Gray: Unknown"),
    "6": ("rgb(128, 0, 255)", "Event"),
}


# ── Helpers ────────────────────────────────────────────────────────────────

def _lbl_font() -> QFont:
    return QFont("Roboto", -1, QFont.Bold)


def _mono_font() -> QFont:
    return QFont("Kode Mono")


def _btn(label: str, color: str, min_w: int = 90) -> QPushButton:
    b = QPushButton(label)
    b.setMinimumWidth(min_w)
    b.setStyleSheet(
        f"QPushButton {{ background-color:{color}; color:#ffffff; border:none;"
        f" padding:6px 11px; border-radius:4px; font-family:Roboto; font-size:15px;"
        f" font-weight:bold; }}"
        f"QPushButton:hover {{ background-color:{color}; opacity:0.9; }}"
        f"QPushButton:pressed {{ background-color:{color}; }}"
        f"QPushButton:disabled {{ background-color:#cccccc; color:#888888; }}"
    )
    return b


def _normalize_qrz(data: dict) -> dict:
    """Normalize QRZ data to consistent display keys.

    Handles both the raw API response (addr1/addr2/call) and the
    cached DB row (address/city/callsign) transparently.
    """
    # grid_override is a local-only correction (set when a contact's QRZ grid
    # is known to be stale — see watchlist_members.py) that takes priority
    # over the cached grid/lat/lon everywhere else in the app (StatRep table,
    # main-map watchlist pins); do the same here so it isn't silently ignored.
    grid_override = (data.get("grid_override") or "").strip()
    return {
        "call":     (data.get("call") or data.get("callsign") or "").upper(),
        "name":     " ".join(x for x in (
                        (data.get("fname") or "").strip(),
                        (data.get("name") or "").strip()
                    ) if x).title() or "",
        "born":     str(data.get("born") or ""),
        "expdate":  str(data.get("expdate") or ""),
        "addr1":    data.get("addr1") or data.get("address") or "",
        "addr2":    data.get("addr2") or data.get("city") or "",
        "state":    data.get("state") or "",
        "zip":      str(data.get("zip") or ""),
        "county":   data.get("county") or "",
        "country":  data.get("country") or "",
        "license":  data.get("class") or "",
        "grid":     grid_override or data.get("grid") or "",
        "lat":      "" if grid_override else str(data.get("lat") or ""),
        "lon":      "" if grid_override else str(data.get("lon") or ""),
        "email":    data.get("email") or "",
        "image":    data.get("image") or "",
        "moddate":  data.get("moddate") or "",
    }


def _make_map_html(lat: float, lon: float, internet_available: bool = True,
                   extra_lat: float = None, extra_lon: float = None) -> str:
    """Generate folium map HTML with one or two marker pins.

    The primary pin (lat/lon) is blue. If extra_lat/extra_lon are provided,
    a red secondary pin is added and the map fits both markers.
    """
    if extra_lat is not None and extra_lon is not None:
        center_lat = (lat + extra_lat) / 2
        center_lon = (lon + extra_lon) / 2
        m = folium.Map(
            location=[center_lat, center_lon], zoom_start=4,
            wheelPxPerZoomLevel=MAP_WHEEL_PX_PER_ZOOM, zoomSnap=0.25,
        )
    else:
        m = folium.Map(
            location=[lat, lon], zoom_start=4,
            wheelPxPerZoomLevel=MAP_WHEEL_PX_PER_ZOOM, zoomSnap=0.25,
        )

    folium.raster_layers.TileLayer(
        tiles="tiles://local/{z}/{x}/{y}.png",
        name="Local Tiles", attr="Local Tiles",
        max_zoom=8, control=False,
    ).add_to(m)
    if internet_available:
        folium.raster_layers.TileLayer(
            tiles="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
            name="OpenStreetMap", attr="OpenStreetMap",
            min_zoom=8, control=False,
        ).add_to(m)

    if extra_lat is not None and extra_lon is not None:
        folium.Marker(location=[lat, lon], icon=folium.Icon(color="red")).add_to(m)
        folium.Marker(location=[extra_lat, extra_lon]).add_to(m)
        m.fit_bounds([[min(lat, extra_lat), min(lon, extra_lon)],
                      [max(lat, extra_lat), max(lon, extra_lon)]],
                     max_zoom=4)
    else:
        folium.Marker(location=[lat, lon]).add_to(m)

    buf = io.BytesIO()
    m.save(buf, close_file=False)
    html = buf.getvalue().decode()
    html = html.replace(
        "<head>",
        '<head><meta name="referrer" content="no-referrer-when-downgrade">',
        1
    )
    return html


def _hsep() -> QFrame:
    """Return a styled horizontal separator line."""
    sep = QFrame()
    sep.setFrameShape(QFrame.HLine)
    sep.setStyleSheet("color:#cccccc;")
    return sep


# ── Background workers ─────────────────────────────────────────────────────

class _ImageLoader(QThread):
    """Downloads and scales a QRZ profile image in the background.

    If `max_size` (w, h) is provided, the image is scaled to fit within that
    bounding box while preserving aspect ratio (so wide banners get a shorter
    rendered height instead of an oversized width).
    Otherwise, if `target_height` is provided the image is scaled to that exact
    height; with neither set the height is auto-selected (166 for tall, 126 for wide).
    """
    image_loaded = pyqtSignal(QPixmap)
    gif_loaded   = pyqtSignal(bytes)

    def __init__(self, url: str, target_height: Optional[int] = None,
                 max_size: Optional[tuple] = None):
        super().__init__()
        self.url = url
        self.target_height = target_height
        self.max_size = max_size

    def run(self) -> None:
        import netguard
        if not netguard.guard("Image load"):
            return
        try:
            with urllib.request.urlopen(self.url, timeout=10) as resp:
                data = resp.read()
            if self.url.lower().split("?")[0].endswith(".gif"):
                self.gif_loaded.emit(data)
                return
            px = QPixmap()
            px.loadFromData(data)
            if not px.isNull():
                if self.max_size is not None:
                    mw, mh = self.max_size
                    scaled = px.scaled(mw, mh, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                else:
                    if self.target_height is not None:
                        target_h = self.target_height
                    else:
                        target_h = 166 if px.height() * 2.0 > px.width() else 126
                    scaled = px.scaledToHeight(target_h, Qt.SmoothTransformation)
                self.image_loaded.emit(scaled)
        except Exception:
            pass


def _get_local_callsign() -> str:
    """Read the local station callsign from the controls table."""
    try:
        with sqlite3.connect(DB_PATH, timeout=10) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT callsign FROM controls WHERE id = 1")
            row = cursor.fetchone()
            return (row[0] or "").strip() if row else ""
    except Exception:
        return ""


class _ReadCountThread(QThread):
    """Fetches the delivery read-count (and last-seen) from the commsrvr server."""
    count_ready = pyqtSignal(str)

    def __init__(self, commsrvr_url: str, callsign: str, global_id: int):
        super().__init__()
        self.commsrvr_url = commsrvr_url
        self.callsign = callsign
        self.global_id = global_id

    def run(self) -> None:
        import netguard
        if not netguard.guard("Read-count check"):
            self.count_ready.emit("")
            return
        try:
            url = (f"{self.commsrvr_url}/get-read-count-808585.php"
                   f"?cs={urllib.parse.quote(self.callsign)}&id={self.global_id}")
            with urllib.request.urlopen(url, timeout=10, context=create_verified_ssl_context()) as resp:
                text = resp.read().decode().strip()
            self.count_ready.emit(text)
        except Exception:
            self.count_ready.emit("")


class _QRZCacheNotifier(QObject):
    """Module-level emitter fired whenever a QRZ API lookup returns data
    (cache was just written or refreshed in the qrz table)."""
    record_written = pyqtSignal(str)  # callsign (uppercase)


qrz_cache_notifier = _QRZCacheNotifier()


class _QRZThread(QThread):
    """Performs a QRZ callsign lookup in the background."""
    result_ready = pyqtSignal(object)  # dict or None

    def __init__(self, callsign: str, username: Optional[str], password: Optional[str]):
        super().__init__()
        self.callsign = callsign
        self.username = username
        self.password = password

    def run(self) -> None:
        client = QRZClient(self.username, self.password)
        data = client.lookup(self.callsign)
        if data:
            qrz_cache_notifier.record_written.emit(self.callsign.upper())
        self.result_ready.emit(data)


# ── Clickable image label ──────────────────────────────────────────────────

class _ClickableImageLabel(QLabel):
    """QLabel that opens a URL in the browser when clicked (if one is set)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._url: str = ""

    def set_url(self, url: str) -> None:
        self._url = url
        self.setCursor(QCursor(Qt.PointingHandCursor) if url else QCursor(Qt.ArrowCursor))

    def mousePressEvent(self, event) -> None:
        if self._url and event.button() == Qt.LeftButton:
            QDesktopServices.openUrl(QUrl(self._url))
        else:
            super().mousePressEvent(event)


class _MemoTextEdit(QTextEdit):
    """QTextEdit that emits focus_lost when it loses keyboard focus."""
    focus_lost = pyqtSignal()

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        self.focus_lost.emit()


class _GrowPlainTextEdit(QPlainTextEdit):
    # Why: QPlainTextEdit's default sizeHint (~256×192) inflates the parent
    # dialog past its resize() target, and the inherited minimumSizeHint
    # (~4 rows, from QAbstractScrollArea) prevents the field from collapsing
    # to a single row when the user shrinks the window. Override both so the
    # field's preferred AND minimum height track minimumHeight; the Expanding
    # vertical policy still lets it grow on resize.
    def sizeHint(self) -> QSize:
        return QSize(super().sizeHint().width(), self.minimumHeight() or 34)

    def minimumSizeHint(self) -> QSize:
        return QSize(super().minimumSizeHint().width(), self.minimumHeight() or 34)


class _ToggleSwitch(QWidget):
    """iOS-style toggle switch that emits toggled(bool) on state change."""
    toggled = pyqtSignal(bool)

    _W, _H = 50, 26
    _KNOB   = 22
    _MARGIN = 2

    def __init__(self, parent=None):
        super().__init__(parent)
        self._checked = False
        self.setFixedSize(self._W, self._H)
        self.setCursor(QCursor(Qt.PointingHandCursor))

    def isChecked(self) -> bool:
        return self._checked

    def setChecked(self, checked: bool) -> None:
        self._checked = checked
        self.update()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._checked = not self._checked
            self.update()
            self.toggled.emit(self._checked)
        super().mousePressEvent(event)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        track_color = QColor("#28a745") if self._checked else QColor("#aaaaaa")
        p.setBrush(track_color)
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(0, (self._H - 20) // 2, self._W, 20, 10, 10)
        knob_x = self._W - self._KNOB - self._MARGIN if self._checked else self._MARGIN
        knob_y = (self._H - self._KNOB) // 2
        p.setBrush(QColor("white"))
        p.drawEllipse(knob_x, knob_y, self._KNOB, self._KNOB)
        p.end()


# ── Shared QRZ info panel ──────────────────────────────────────────────────

class _QRZInfoSection(QWidget):
    """Three-column QRZ info display used by all three dialogs.

    Left  : section header, callsign, name, address
    Center: last seen, license, grid, lat/lon, email
    Right : profile image, last modified date
    """

    image_width_ready = pyqtSignal(int)
    last_seen_updated = pyqtSignal(str)

    def __init__(self, hdr_bg: str = "", hdr_fg: str = "", skip_last_seen: bool = False, parent=None):
        super().__init__(parent)
        self._img_loader: Optional[_ImageLoader] = None
        self._gif_movie: Optional[QMovie] = None
        self._hdr_bg = hdr_bg
        self._hdr_fg = hdr_fg
        self._skip_last_seen = skip_last_seen
        self._build()

    def _build(self) -> None:
        self._main_layout = QVBoxLayout(self)
        self._main_layout.setContentsMargins(10, 8, 10, 0)
        self._main_layout.setSpacing(0)

        outer = QHBoxLayout()
        outer.setSpacing(24)
        self._main_layout.addLayout(outer)

        # ── Columns 1 & 2 (2/3 total) via QGridLayout for row alignment ─
        self._grid = QGridLayout()
        grid = self._grid
        grid.setSpacing(2)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)

        self.hdr = QLabel("QRZ API Lookup For:")
        self.hdr.setFont(QFont("Roboto Slab", -1, QFont.Black))
        self.hdr.setStyleSheet(
            f"QLabel {{ background-color: {self._hdr_bg}; color: {self._hdr_fg}; font-size: 16px; padding-top: 9px; padding-bottom: 9px; }}"
            if self._hdr_bg else ""
        )
        self.hdr.setAlignment(Qt.AlignCenter)

        self.lbl_call    = QLabel(); self.lbl_call.setFont(_mono_font())
        self.lbl_name    = QLabel(); self.lbl_name.setFont(_mono_font())
        self.lbl_addr1   = QLabel(); self.lbl_addr1.setFont(_mono_font())
        self.lbl_addr2   = QLabel(); self.lbl_addr2.setFont(_mono_font())
        self.lbl_county  = QLabel(); self.lbl_county.setFont(_mono_font())
        self.lbl_country = QLabel(); self.lbl_country.setFont(_mono_font())
        self.lbl_last_seen = QLabel(); self.lbl_last_seen.setFont(_mono_font())
        self.lbl_license    = QLabel(); self.lbl_license.setFont(_mono_font())
        self.lbl_grid    = QLabel(); self.lbl_grid.setFont(_mono_font())
        self.lbl_lat     = QLabel(); self.lbl_lat.setFont(_mono_font())
        self.lbl_lon     = QLabel(); self.lbl_lon.setFont(_mono_font())

        self.lbl_qrz_status = QLabel()
        self.lbl_qrz_status.setStyleSheet("QLabel { font-family:Roboto; font-size:13px; font-weight:bold; }")
        self.lbl_qrz_status.setWordWrap(True)
        self.lbl_qrz_status.setVisible(False)

        self.last_seen_updated.connect(self._on_last_seen_updated)

        grid.addWidget(self.hdr,              0, 0, 1, 2)
        grid.addWidget(self.lbl_qrz_status,   1, 0, 1, 2)
        grid.addWidget(self.lbl_call,         2, 0)
        grid.addWidget(self.lbl_name,         3, 0)
        grid.addWidget(self.lbl_last_seen,    3, 1)
        grid.addWidget(self.lbl_addr1,        4, 0)
        grid.addWidget(self.lbl_license,      4, 1)
        grid.addWidget(self.lbl_addr2,        5, 0)
        grid.addWidget(self.lbl_grid,         5, 1)
        grid.addWidget(self.lbl_county,       6, 0)
        grid.addWidget(self.lbl_lat,          6, 1)
        grid.addWidget(self.lbl_country,      7, 0)
        grid.addWidget(self.lbl_lon,          7, 1)
        grid.setRowStretch(8, 1)
        outer.addLayout(grid, 2)

        # ── Column 3 (1/3): image + photo status + moddate ───────────────
        right = QVBoxLayout()
        right.setAlignment(Qt.AlignTop | Qt.AlignRight)
        right.setSpacing(4)
        self.lbl_image = _ClickableImageLabel()
        self.lbl_image.setAlignment(Qt.AlignTop | Qt.AlignRight)
        self.lbl_image.setStyleSheet("QLabel { border:none; padding:0px; }")
        self.lbl_moddate = QLabel()
        self.lbl_moddate.setFont(QFont("Roboto"))
        self.lbl_moddate.setStyleSheet("QLabel { font-size: 13px; font-weight: normal; }")
        self.lbl_moddate.setAlignment(Qt.AlignRight)
        moddate_row = QHBoxLayout()
        moddate_row.addStretch()
        moddate_row.addWidget(self.lbl_moddate)
        right.addWidget(self.lbl_image)
        right.addLayout(moddate_row)
        right.addStretch()
        outer.addLayout(right, 1)

    def add_memo_row(self, trailing_space: int = 12) -> QLineEdit:
        """Add a contact-note label, input, and separator spanning all three columns."""
        return self._add_note_input("Add a contact note…", trailing_space=trailing_space)

    def add_statrep_memo_row(self) -> QPlainTextEdit:
        """Add a multi-line status-report-note input that grows to fill extra vertical space."""
        self._main_layout.addSpacing(10)
        memo_input = _GrowPlainTextEdit()
        memo_input.setFont(_mono_font())
        memo_input.setMinimumHeight(34)
        memo_input.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        memo_input.setTabChangesFocus(True)
        memo_input.setStyleSheet(
            f"QPlainTextEdit {{ background-color:white; color:{COLOR_INPUT_TEXT};"
            f" border:1px solid {COLOR_INPUT_BORDER}; border-radius:4px; padding:4px 8px; }}"
        )
        memo_input.setPlaceholderText("Add a status report note…")
        self._main_layout.addWidget(memo_input, 1)
        return memo_input

    def _add_note_input(self, placeholder: str, trailing_space: int = 12) -> QLineEdit:
        self._main_layout.addSpacing(10)
        memo_input = QLineEdit()
        memo_input.setFont(_mono_font())
        memo_input.setMinimumHeight(34)
        memo_input.setStyleSheet(
            f"background-color:white; color:{COLOR_INPUT_TEXT};"
            f" border:1px solid {COLOR_INPUT_BORDER}; border-radius:4px; padding:4px 8px;"
        )
        memo_input.setPlaceholderText(placeholder)
        self._main_layout.addWidget(memo_input)
        if trailing_space:
            self._main_layout.addSpacing(trailing_space)

        return memo_input

    def add_statrep_rows(self) -> None:
        """Add separator + StatRep fields below the QRZ section, spanning all three columns."""
        self._grid.setRowStretch(8, 0)

        sr_grid = QGridLayout()
        sr_grid.setSpacing(2)
        sr_grid.setColumnStretch(0, 1)
        sr_grid.setColumnStretch(1, 1)
        sr_grid.setColumnStretch(2, 1)

        self.lbl_sr_posted    = QLabel(); self.lbl_sr_posted.setFont(_mono_font())
        self.lbl_sr_group     = QLabel(); self.lbl_sr_group.setFont(_mono_font())
        self.lbl_sr_freq      = QLabel(); self.lbl_sr_freq.setFont(_mono_font())
        self.lbl_sr_sr_id     = QLabel(); self.lbl_sr_sr_id.setFont(_mono_font())
        self.lbl_sr_global_id = QLabel(); self.lbl_sr_global_id.setFont(_mono_font())
        self.lbl_sr_grid      = QLabel(); self.lbl_sr_grid.setFont(_mono_font())
        self.lbl_sr_source    = QLabel(); self.lbl_sr_source.setFont(_mono_font())
        self.lbl_sr_delivered = QLabel(); self.lbl_sr_delivered.setFont(_mono_font())

        sr_hdr = QLabel("Status Report Details")
        sr_hdr.setFont(_lbl_font())

        # Row 0: header | Freq:    | Grid:
        sr_grid.addWidget(sr_hdr,                 0, 0)
        sr_grid.addWidget(self.lbl_sr_freq,        0, 1)
        sr_grid.addWidget(self.lbl_sr_grid,        0, 2)
        # Row 1: Posted: | Statrep ID: | Received via:
        sr_grid.addWidget(self.lbl_sr_posted,      1, 0)
        sr_grid.addWidget(self.lbl_sr_sr_id,       1, 1)
        sr_grid.addWidget(self.lbl_sr_source,      1, 2)
        # Row 2: Group:  | Global ID:  | Delivered To:
        sr_grid.addWidget(self.lbl_sr_group,       2, 0)
        sr_grid.addWidget(self.lbl_sr_global_id,   2, 1)
        sr_grid.addWidget(self.lbl_sr_delivered,   2, 2)

        self._main_layout.addLayout(sr_grid)

    def add_message_rows(self) -> None:
        """Add Message Details fields below the QRZ section, spanning all three columns."""
        self._grid.setRowStretch(8, 0)

        msg_grid = QGridLayout()
        msg_grid.setSpacing(2)
        msg_grid.setColumnStretch(0, 1)
        msg_grid.setColumnStretch(1, 1)
        msg_grid.setColumnStretch(2, 1)

        self.lbl_msg_target    = QLabel(); self.lbl_msg_target.setFont(_mono_font())
        self.lbl_msg_freq      = QLabel(); self.lbl_msg_freq.setFont(_mono_font())
        self.lbl_msg_posted    = QLabel(); self.lbl_msg_posted.setFont(_mono_font())
        self.lbl_msg_id        = QLabel(); self.lbl_msg_id.setFont(_mono_font())
        self.lbl_msg_source    = QLabel(); self.lbl_msg_source.setFont(_mono_font())
        self.lbl_msg_global_id = QLabel(); self.lbl_msg_global_id.setFont(_mono_font())
        self.lbl_msg_delivered = QLabel(); self.lbl_msg_delivered.setFont(_mono_font())

        msg_hdr = QLabel("Message Details")
        msg_hdr.setFont(_lbl_font())

        # Row 0: header | Freq:
        msg_grid.addWidget(msg_hdr,                 0, 0)
        msg_grid.addWidget(self.lbl_msg_freq,       0, 1)
        # Row 1: Posted: | Message ID: | Received via:
        msg_grid.addWidget(self.lbl_msg_posted,     1, 0)
        msg_grid.addWidget(self.lbl_msg_id,         1, 1)
        msg_grid.addWidget(self.lbl_msg_source,     1, 2)
        # Row 2: To: | Global ID: | Delivered To:
        msg_grid.addWidget(self.lbl_msg_target,     2, 0)
        msg_grid.addWidget(self.lbl_msg_global_id,  2, 1)
        msg_grid.addWidget(self.lbl_msg_delivered,  2, 2)

        self._main_layout.addLayout(msg_grid)

    def set_qrz_status(self, text: str) -> None:
        self.lbl_qrz_status.setText(text)
        self.lbl_qrz_status.setVisible(True)

    def clear_qrz_status(self) -> None:
        self.lbl_qrz_status.setText("")
        self.lbl_qrz_status.setVisible(False)

    # ── Last Seen lookup ──────────────────────────────────────────────────────

    def _fetch_last_seen(self, target: str) -> None:
        _k = "font-family:Roboto; font-weight:bold; font-size:13px;"
        self.lbl_last_seen.setText(f'<span style="{_k}">Last Seen:</span> …')
        threading.Thread(target=self._last_seen_thread, args=(target,), daemon=True).start()

    def _last_seen_thread(self, target: str) -> None:
        import sip
        import netguard
        if not netguard.guard("Last-seen check"):
            if not sip.isdeleted(self):
                self.last_seen_updated.emit("—")
            return
        try:
            my_cs = _get_local_callsign()
            if not my_cs:
                if not sip.isdeleted(self):
                    self.last_seen_updated.emit("—")
                return
            url = (
                f"{_COMMSRVR_URL}/get-last-seen-808585.php"
                f"?cs={urllib.parse.quote(my_cs)}&lookup={urllib.parse.quote(target)}"
            )
            with urllib.request.urlopen(url, timeout=8, context=create_verified_ssl_context()) as resp:
                result = resp.read().decode("utf-8").strip()
            if not sip.isdeleted(self):
                self.last_seen_updated.emit(result if result else "—")
        except Exception as e:
            print(f"[QRZInfoSection] last-seen error: {e}")
            if not sip.isdeleted(self):
                self.last_seen_updated.emit("—")

    def _on_last_seen_updated(self, value: str) -> None:
        _k = "font-family:Roboto; font-weight:bold; font-size:13px;"
        self.lbl_last_seen.setText(f'<span style="{_k}">Last Seen:</span> {value}')

    def update_data(self, data: dict) -> None:
        """Populate all labels from raw QRZ data (API or cached format)."""
        d = _normalize_qrz(data)

        self.hdr.setText(f"QRZ API Lookup For: {d['call']}")
        self.lbl_call.setText("")
        self.lbl_name.setText(f"<b>{d['name']}</b>" if d["name"] else "")

        self.lbl_addr1.setText(d["addr1"])
        city_state = ", ".join(x for x in (d["addr2"], d["state"]) if x)
        if d["zip"]:
            city_state = (city_state + " " + d["zip"]).strip()
        self.lbl_addr2.setText(city_state)

        _k = "font-family:Roboto; font-weight:bold; font-size:13px;"
        self.lbl_county.setText(f'<span style="{_k}">County:</span> {d["county"]}' if d["county"] else "")
        self.lbl_country.setText(f'<span style="{_k}">Country:</span> {d["country"]}' if d["country"] else "")

        if d["call"] and not self._skip_last_seen:
            self.lbl_last_seen.setText(f'<span style="{_k}">Last Seen:</span> —')
            self._fetch_last_seen(d["call"])

        if d["license"] and d["expdate"]:
            self.lbl_license.setText(f'<span style="{_k}">License:</span> {d["license"]} (exp: {d["expdate"]})')
        elif d["expdate"]:
            self.lbl_license.setText(f'(exp: {d["expdate"]})')
        elif d["license"]:
            self.lbl_license.setText(f'<span style="{_k}">License:</span> {d["license"]}')
        else:
            self.lbl_license.setText("")
        self.lbl_grid.setText(f'<span style="{_k}">Grid:</span> {d["grid"]}' if d["grid"] else "")
        self.lbl_lat.setText(f'<span style="{_k}">Lat:</span> {d["lat"]}' if d["lat"] else "")
        self.lbl_lon.setText(f'<span style="{_k}">Lon:</span> {d["lon"]}' if d["lon"] else "")

        profile_url = f"https://www.qrz.com/db/{d['call']}" if d["call"] else ""

        self.lbl_moddate.setText(
            f"QRZ profile last modified: {d['moddate'].split()[0]}" if d["moddate"] else ""
        )

        self.lbl_image.clear()
        self.lbl_image.set_url(profile_url)
        if d["image"]:
            self._img_loader = _ImageLoader(d["image"])
            self._img_loader.image_loaded.connect(self._on_image_loaded)
            self._img_loader.gif_loaded.connect(self._on_gif_loaded)
            self._img_loader.start()
        else:
            self._load_default_image()

    def _on_image_loaded(self, px: QPixmap) -> None:
        self.lbl_image.setPixmap(px)
        self.image_width_ready.emit(px.width())

    def _on_gif_loaded(self, data: bytes) -> None:
        # Load first frame into QPixmap to reliably get the native dimensions
        # (QMovie.currentPixmap() before start() often returns a null pixmap)
        px_probe = QPixmap()
        px_probe.loadFromData(data)
        if not px_probe.isNull():
            target_h = 166 if px_probe.height() * 2.0 > px_probe.width() else 126
            scaled_size = px_probe.scaledToHeight(target_h, Qt.SmoothTransformation).size()
        else:
            scaled_size = None

        buf = QBuffer()
        buf.setData(QByteArray(data))
        buf.open(QBuffer.ReadOnly)
        self._gif_movie = QMovie()
        self._gif_movie.setDevice(buf)
        self._gif_movie._buf = buf
        if scaled_size is not None:
            self._gif_movie.setScaledSize(scaled_size)
        self.lbl_image.setMovie(self._gif_movie)
        self._gif_movie.start()
        self.image_width_ready.emit(self._gif_movie.scaledSize().width())

    def _load_default_image(self) -> None:
        px = QPixmap("00-qrz-default.png")
        if not px.isNull():
            target_h = 166 if px.height() * 2.0 > px.width() else 126
            self.lbl_image.setPixmap(px.scaledToHeight(target_h, Qt.SmoothTransformation))
        else:
            self.lbl_image.clear()

    def show_no_data_placeholder(self) -> None:
        """Show label keys and default image with no QRZ data populated."""
        if self._gif_movie:
            self._gif_movie.stop()
            self._gif_movie = None
        self.hdr.setText("QRZ API Lookup For:")
        self.lbl_call.clear()
        self.lbl_name.clear()
        self.lbl_addr1.clear()
        self.lbl_addr2.clear()
        self.lbl_moddate.clear()
        _k = "font-family:Roboto; font-weight:bold; font-size:13px;"
        self.lbl_county.setText(f"<span style='{_k}'>County:</span>")
        self.lbl_country.setText(f"<span style='{_k}'>Country:</span>")
        if not self._skip_last_seen:
            self.lbl_last_seen.setText(f"<span style='{_k}'>Last Seen:</span>")
        else:
            self.lbl_last_seen.clear()
        self.lbl_license.setText(f"<span style='{_k}'>License:</span>")
        self.lbl_grid.setText(f"<span style='{_k}'>Grid:</span>")
        self.lbl_lat.setText(f"<span style='{_k}'>Lat:</span>")
        self.lbl_lon.setText(f"<span style='{_k}'>Lon:</span>")
        self.lbl_image.set_url("")
        self._load_default_image()

    def clear(self) -> None:
        if self._gif_movie:
            self._gif_movie.stop()
            self._gif_movie = None
        self.hdr.setText("QRZ API Lookup For:")
        self.clear_qrz_status()
        for w in (self.lbl_call, self.lbl_name, self.lbl_addr1, self.lbl_addr2,
                  self.lbl_county, self.lbl_country, self.lbl_last_seen, self.lbl_license,
                  self.lbl_grid, self.lbl_lat, self.lbl_lon,
                  self.lbl_image, self.lbl_moddate):
            w.clear()


# ── Dialog 1: Standalone QRZ Lookup ───────────────────────────────────────

class QRZLookupDialog(QDialog):
    """Standalone QRZ callsign lookup (Tools → QRZ Lookup)."""

    _send_result = pyqtSignal(str)

    def __init__(self, module_background: str = "#f5f5f5",
                 module_foreground: str = "#333333",
                 program_background: str = "",
                 program_foreground: str = "",
                 initial_callsign: str = "",
                 initial_message: str = "",
                 refresh_callback=None,
                 parent=None):
        super().__init__(parent)
        apply_standard_dialog_chrome(self, "QRZ Lookup")
        self.setModal(True)
        self.setMinimumSize(825, 500)
        self.resize(902, 580)
        self._module_bg = module_background
        self._module_fg = module_foreground
        self._program_bg = program_background or _PROG_BG
        self._program_fg = program_foreground or _PROG_FG
        self._thread: Optional[_QRZThread] = None
        self._refresh_callback = refresh_callback
        self._internet_available = bool(self.parent() and getattr(self.parent(), '_internet_available', True))
        self._send_result.connect(self._on_send_result)
        self._setup_ui()

        cs = initial_callsign.strip().upper()
        if cs:
            self.cs_edit.setText(cs)
            self._search()
        if initial_message:
            self.msg_edit.setPlainText(initial_message)
        if cs or initial_message:
            from PyQt5.QtGui import QTextCursor
            cursor = self.msg_edit.textCursor()
            cursor.movePosition(QTextCursor.Start)
            self.msg_edit.setTextCursor(cursor)
            self.msg_edit.setFocus()

    def _setup_ui(self) -> None:
        self.setStyleSheet(
            f"QDialog {{ background-color:{self._module_bg}; }}"
            f"QLabel {{ color:{self._module_fg}; background-color: transparent; font-size: 13px; }}"
            f"QLineEdit {{ background-color:white; color:{COLOR_INPUT_TEXT};"
            f" border:1px solid {COLOR_INPUT_BORDER}; border-radius:4px; padding:4px 8px;"
            f" font-family:'Kode Mono'; font-size:13px; }}"
        )
        main = QVBoxLayout(self)
        main.setContentsMargins(15, 15, 15, 15)
        main.setSpacing(10)

        # Title
        title = QLabel("QRZ Lookup")
        title.setAlignment(Qt.AlignCenter)
        title.setFont(QFont("Roboto Slab", -1, QFont.Black))
        title.setFixedHeight(36)
        title.setStyleSheet(
            f"QLabel {{ background-color: {self._program_bg}; color: {self._program_fg}; "
            "font-size: 16px; padding-top: 9px; padding-bottom: 9px; }"
        )
        main.addWidget(title)

        row = QHBoxLayout()
        self.cs_edit = QLineEdit()
        self.cs_edit.setPlaceholderText("Enter callsign…")
        self.cs_edit.setMaxLength(15)
        self.cs_edit.setFont(_mono_font())
        self.cs_edit.setMinimumHeight(34)
        self.cs_edit.returnPressed.connect(self._search)
        self.cs_edit.textChanged.connect(self._force_upper)
        row.addWidget(self.cs_edit)

        self.btn_search = _btn("Search", COLOR_BTN_BLUE)
        self.btn_search.setFixedWidth(90)
        self.btn_search.setAutoDefault(False)
        self.btn_search.clicked.connect(self._search)
        row.addWidget(self.btn_search)
        main.addLayout(row)

        self.lbl_status = QLabel()
        self.lbl_status.setFont(QFont("Roboto"))
        self.lbl_status.setStyleSheet(
            f"QLabel {{ color:{self._module_fg}; font-family:Roboto; font-size:13px; font-weight:bold; }}"
        )
        main.addWidget(self.lbl_status)

        self.qrz_info = _QRZInfoSection(hdr_bg=self._program_bg, hdr_fg=self._program_fg, parent=self)
        self.qrz_info.image_width_ready.connect(self._adjust_for_image_width)
        self.qrz_info.show_no_data_placeholder()
        self.memo_edit = self.qrz_info.add_memo_row()
        self.memo_edit.editingFinished.connect(self._save_memo)
        main.addWidget(self.qrz_info)

        self.msg_edit = QPlainTextEdit()
        self.msg_edit.setFont(_mono_font())
        self.msg_edit.setPlaceholderText("Enter message…")
        self.msg_edit.setStyleSheet(
            f"background-color:white; color:{COLOR_INPUT_TEXT};"
            f" border:1px solid {COLOR_INPUT_BORDER}; border-radius:4px; padding:4px 8px;"
            f" font-family:'Kode Mono'; font-size:13px;"
        )
        from PyQt5.QtGui import QFontMetrics
        _fm = QFontMetrics(self.msg_edit.font())
        self.msg_edit.setFixedHeight(_fm.lineSpacing() * 6 + 14)
        self.msg_edit.textChanged.connect(self._on_msg_changed)
        if not self._internet_available:
            self.msg_edit.setEnabled(False)
        main.addWidget(self.msg_edit)
        main.addStretch()

        self.btn_clear_msg = _btn("Clear", _COL_CANCEL)
        self.btn_clear_msg.setVisible(False)
        self.btn_clear_msg.clicked.connect(self.msg_edit.clear)
        self.btn_send = _btn("Send", COLOR_BTN_BLUE)
        self.btn_send.setVisible(False)
        connect_single(self.btn_send, self._on_send_internet)
        self.btn_close_lookup = _btn("Close", _COL_CANCEL)
        self.btn_close_lookup.clicked.connect(self.reject)

        if self._internet_available:
            btn_row = QHBoxLayout()
            btn_row.setSpacing(8)
            btn_row.addStretch()
            btn_row.addWidget(self.btn_clear_msg)
            btn_row.addWidget(self.btn_send)
            btn_row.addWidget(self.btn_close_lookup)
            main.addLayout(btn_row)
        else:
            no_inet = QLabel("No Internet Connection  ·  Direct Messaging Unavailable")
            no_inet.setAlignment(Qt.AlignCenter)
            no_inet.setFont(QFont("Roboto Slab", -1, QFont.Black))
            no_inet.setFixedHeight(36)
            no_inet.setStyleSheet(
                f"QLabel {{ background-color: {self._program_bg}; color: {self._program_fg}; "
                "font-size: 16px; padding-top: 9px; padding-bottom: 9px; }}"
            )
            no_inet_row = QHBoxLayout()
            no_inet_row.setSpacing(8)
            no_inet_row.addWidget(no_inet, 1)
            no_inet_row.addWidget(self.btn_close_lookup)
            main.addLayout(no_inet_row)

    def _adjust_for_image_width(self, img_width: int) -> None:
        if img_width > 275:
            self.resize(self.width() + (img_width - 275), self.height())

    def _force_upper(self, text: str) -> None:
        if text != text.upper():
            self.cs_edit.blockSignals(True)
            pos = self.cs_edit.cursorPosition()
            self.cs_edit.setText(text.upper())
            self.cs_edit.setCursorPosition(pos)
            self.cs_edit.blockSignals(False)

    def _search(self) -> None:
        if self._thread is not None and self._thread.isRunning():
            return
        cs = self.cs_edit.text().strip().upper()
        if not cs:
            return

        is_active, username, password = load_qrz_config()
        cached_fresh = get_qrz_cached(cs)
        cached_any   = cached_fresh or get_qrz_cached(cs, include_stale=True)

        self.qrz_info.clear()
        self.memo_edit.blockSignals(True)
        self.memo_edit.clear()
        self.memo_edit.blockSignals(False)

        if not username:
            found_str = "found" if cached_any else "NOT found"
            self.lbl_status.setText(
                f"QRZ Subscription not configured — {cs} {found_str} in local database"
            )
            if cached_any:
                self.qrz_info.update_data(cached_any)
                self.memo_edit.blockSignals(True)
                self.memo_edit.setText(cached_any.get("memo") or "")
                self.memo_edit.blockSignals(False)
            else:
                self.qrz_info.show_no_data_placeholder()
            self.msg_edit.setFocus()
            return

        if not is_active:
            found_str = "found" if cached_any else "NOT found"
            self.lbl_status.setText(
                f"QRZ Subscription not enabled — {cs} {found_str} in local database"
            )
            if cached_any:
                self.qrz_info.update_data(cached_any)
                self.memo_edit.blockSignals(True)
                self.memo_edit.setText(cached_any.get("memo") or "")
                self.memo_edit.blockSignals(False)
            else:
                self.qrz_info.show_no_data_placeholder()
            self.msg_edit.setFocus()
            return

        if cached_fresh:
            self.lbl_status.setText("")
            self.qrz_info.update_data(cached_fresh)
            self.memo_edit.blockSignals(True)
            self.memo_edit.setText(cached_fresh.get("memo") or "")
            self.memo_edit.blockSignals(False)
            self.msg_edit.setFocus()
            return

        self.lbl_status.setText(f"Looking up {cs}…")
        self.btn_search.setEnabled(False)
        self.qrz_info.show_no_data_placeholder()
        self._thread = _QRZThread(cs, username, password)
        self._thread.result_ready.connect(self._on_result)
        self._thread.start()

    def _on_result(self, result) -> None:
        self.btn_search.setEnabled(True)
        if result:
            self.lbl_status.setText("")
            self.qrz_info.update_data(result)
            self.memo_edit.blockSignals(True)
            self.memo_edit.setText(result.get("memo") or "")
            self.memo_edit.blockSignals(False)
        else:
            self.lbl_status.setText("No results found.")
            self.qrz_info.show_no_data_placeholder()
        self.msg_edit.setFocus()

    def _save_memo(self) -> None:
        """Save memo text to the qrz table on focus-out."""
        cs = self.cs_edit.text().strip().upper()
        if not cs:
            return
        try:
            with sqlite3.connect(DB_PATH, timeout=10) as conn:
                conn.execute(
                    "UPDATE qrz SET memo = ? WHERE callsign = ? COLLATE NOCASE",
                    (self.memo_edit.text(), cs)
                )
                conn.commit()
        except sqlite3.Error as e:
            print(f"[QRZLookupDialog] Memo save error: {e}")

    def _on_msg_changed(self) -> None:
        if not self._internet_available:
            return
        has_text = bool(self.msg_edit.toPlainText().strip())
        self.btn_clear_msg.setVisible(has_text)
        self.btn_send.setVisible(has_text)

    def _sanitize_message(self, text: str) -> str:
        import re
        text = text.replace('\r', '').replace('\n', '||')
        return re.sub(r'[^\x20-\x7E]', '', text).strip()

    def _on_send_internet(self) -> None:
        cs = self.cs_edit.text().strip().upper()
        text = self._sanitize_message(self.msg_edit.toPlainText())
        if not cs or not text:
            return
        my_cs = _get_local_callsign()
        if not my_cs:
            QMessageBox.warning(self, "Send Failed", "No operator callsign configured in Settings.")
            return
        now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        msg_id = generate_time_based_id()
        message_data = f"{my_cs}: {cs} MSG ,{msg_id},{text},{{^%3}}"
        data_string  = f"{now}\t0\t0\t30\t{message_data}"
        self._pending_dm = (my_cs, cs, text, msg_id, now)
        threading.Thread(
            target=self._submit_internet, args=(my_cs, data_string), daemon=True
        ).start()

    def _save_to_local_messages(self, from_cs: str, target_cs: str,
                                message: str, msg_id: str, now: str,
                                global_id: int = 0) -> None:
        """Insert a just-sent internet direct message into the local messages
        table, mirroring group_message.py's _save_to_database.

        Internet send → source 3, freq 0; db follows the group_message
        convention of 30 for a locally-originated message. The recipient
        callsign goes in target (matching how received direct messages are
        stored). The message body keeps its ||-encoded newlines; the table
        display decodes them back to \\n.

        Args:
            global_id: The global ID returned by the commsrvr server (0 if unknown).
        """
        try:
            with sqlite3.connect(DB_PATH, timeout=10) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO messages "
                    "(global_id, datetime, date, freq, db, source, msg_id, from_callsign, target, message) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (global_id, now, now[:10], 0, 30, 3, msg_id, from_cs, target_cs, message)
                )
                conn.commit()
        except sqlite3.Error as e:
            print(f"[QRZLookupDialog] failed to save sent message to local table: {e}")

    def _submit_internet(self, callsign: str, data_string: str) -> None:
        import netguard
        if not netguard.guard("Internet direct message"):
            self._send_result.emit("ERR::Off-Grid Mode is enabled — switch back to ONLINE to send.")
            return
        try:
            post = urllib.parse.urlencode({'cs': callsign, 'data': data_string}).encode()
            req  = urllib.request.Request(_DATAFEED_URL, data=post, method='POST')
            with urllib.request.urlopen(req, timeout=5, context=create_verified_ssl_context()) as resp:
                result = resp.read().decode().strip()
            if result.lstrip('-').isdigit():
                print(f"[Commsrvr] Direct message submitted successfully (global_id={result})")
            else:
                print(f"[Commsrvr] Direct message submission failed — server returned: {result}")
            self._send_result.emit(result)
        except Exception as e:
            reason = getattr(e, 'reason', e)
            if isinstance(reason, TimeoutError):
                err = "ERR::Server timeout — the server did not respond in time."
            else:
                err = f"ERR::Connection Error — The CommStat server did not reply.\nURL: {_DATAFEED_URL}"
            print(f"[Commsrvr] Direct message submission failed — {err[5:]}")
            self._send_result.emit(err)

    def _on_send_result(self, result: str) -> None:
        if result.startswith("ERR::"):
            InternetDeliveryFailureDialog(result[5:], parent=self).exec_()
        elif result.lstrip('-').isdigit():
            if self._pending_dm:
                self._save_to_local_messages(*self._pending_dm, global_id=int(result))
                self._pending_dm = None
                if self._refresh_callback:
                    self._refresh_callback()
            self.accept()


# ── Dialog 2: JS8 Message (RF) ─────────────────────────────────────────────

class JS8MessageDialog(QDialog):
    """QRZ lookup with inline JS8 RF transmit controls (JS8 Message menu item)."""

    def __init__(self, program_background: str = "",
                 program_foreground: str = "",
                 module_background: str = "#f5f5f5",
                 module_foreground: str = "#333333",
                 tcp_pool=None,
                 connector_manager=None,
                 parent=None):
        super().__init__(parent)
        apply_standard_dialog_chrome(self, "JS8 Message")
        self.setModal(True)
        self.setMinimumSize(825, 460)
        self.resize(900, 530)
        self._program_bg = program_background or _PROG_BG
        self._program_fg = program_foreground or _PROG_FG
        self._module_bg = module_background
        self._module_fg = module_foreground
        self._tcp_pool = tcp_pool
        self._connector_manager = connector_manager
        self._qrz_thread: Optional[_QRZThread] = None
        self._setup_ui()
        self._populate_rigs()

    def _setup_ui(self) -> None:
        self.setStyleSheet(
            f"QDialog {{ background-color:{self._module_bg}; }}"
            f"QLabel {{ color:{self._module_fg}; background-color: transparent; font-size: 13px; }}"
            f"QLineEdit {{ background-color:white; color:{COLOR_INPUT_TEXT};"
            f" border:1px solid {COLOR_INPUT_BORDER}; border-radius:4px; padding:4px 8px;"
            f" font-family:'Kode Mono'; font-size:13px; }}"
            f"QComboBox {{ background-color:white; color:{COLOR_INPUT_TEXT};"
            f" border:1px solid {COLOR_INPUT_BORDER}; border-radius:4px; padding:4px 8px;"
            f" font-family:'Kode Mono'; font-size:13px; combobox-popup:0; }}"
            f"QComboBox QAbstractItemView::item {{ min-height:22px; padding:0 6px; }}"
        )
        main = QVBoxLayout(self)
        main.setContentsMargins(15, 15, 15, 15)
        main.setSpacing(10)

        title = QLabel("JS8 MESSAGE")
        title.setAlignment(Qt.AlignCenter)
        title.setFont(QFont("Roboto Slab", -1, QFont.Black))
        title.setFixedHeight(36)
        title.setStyleSheet(
            f"QLabel {{ background-color: {self._program_bg}; color: {self._program_fg}; "
            "font-size: 16px; padding-top: 9px; padding-bottom: 9px; }"
        )
        main.addWidget(title)

        search_row = QHBoxLayout()
        self.cs_edit = QLineEdit()
        self.cs_edit.setPlaceholderText("Enter callsign…")
        self.cs_edit.setMaxLength(15)
        self.cs_edit.setFont(_mono_font())
        self.cs_edit.setMinimumHeight(34)
        self.cs_edit.returnPressed.connect(self._search)
        self.cs_edit.textChanged.connect(self._force_upper)
        search_row.addWidget(self.cs_edit)
        self.btn_search = _btn("Search", COLOR_BTN_BLUE)
        self.btn_search.setFixedWidth(90)
        self.btn_search.setAutoDefault(False)
        self.btn_search.clicked.connect(self._search)
        search_row.addWidget(self.btn_search)
        main.addLayout(search_row)

        self.lbl_status = QLabel()
        self.lbl_status.setFont(QFont("Roboto"))
        self.lbl_status.setStyleSheet("QLabel { color:#888888; font-size:10px; font-weight:normal; }")
        main.addWidget(self.lbl_status)

        self.qrz_info = _QRZInfoSection(hdr_bg=self._program_bg, hdr_fg=self._program_fg, parent=self)
        self.qrz_info.image_width_ready.connect(self._adjust_for_image_width)
        self.qrz_info.show_no_data_placeholder()
        self.contact_memo_edit = self.qrz_info.add_memo_row()
        self.contact_memo_edit.editingFinished.connect(self._save_contact_memo)
        main.addWidget(self.qrz_info)

        # RF controls row
        rf_row = QHBoxLayout()
        rf_row.setSpacing(12)

        rig_lbl = QLabel("Rig:")
        rig_lbl.setFont(_lbl_font())
        self.rig_combo = QComboBox()
        self.rig_combo.setFont(_mono_font())
        self.rig_combo.setMinimumWidth(140)
        self.rig_combo.setMaxVisibleItems(30)
        self.rig_combo.setItemDelegate(QStyledItemDelegate(self.rig_combo))
        self.rig_combo.currentTextChanged.connect(self._on_rig_changed)
        rf_row.addWidget(rig_lbl)
        rf_row.addWidget(self.rig_combo)

        mode_lbl = QLabel("Mode:")
        mode_lbl.setFont(_lbl_font())
        self.mode_combo = QComboBox()
        self.mode_combo.setFont(_mono_font())
        self.mode_combo.setMaxVisibleItems(30)
        self.mode_combo.setItemDelegate(QStyledItemDelegate(self.mode_combo))
        self.mode_combo.addItems(["Normal", "Fast", "Turbo", "Ultra", "Slow"])
        rf_row.addWidget(mode_lbl)
        rf_row.addWidget(self.mode_combo)

        freq_lbl = QLabel("Frequency:")
        freq_lbl.setFont(_lbl_font())
        self.freq_edit = QLineEdit()
        self.freq_edit.setReadOnly(True)
        self.freq_edit.setFont(_mono_font())
        self.freq_edit.setPlaceholderText("—")
        self.freq_edit.setMaximumWidth(100)
        self.freq_edit.setStyleSheet(
            f"background-color:white; color:{COLOR_INPUT_TEXT};"
            f" border:1px solid {COLOR_INPUT_BORDER}; border-radius:4px; padding:2px 4px;"
            f" font-family:'Kode Mono'; font-size:13px;"
        )
        rf_row.addWidget(freq_lbl)
        rf_row.addWidget(self.freq_edit)
        rf_row.addStretch()
        main.addLayout(rf_row)

        # Message box — 2 rows, 100 char limit
        self.msg_edit = QPlainTextEdit()
        self.msg_edit.setFont(_mono_font())
        self.msg_edit.setPlaceholderText("Enter message… (100 characters max)")
        self.msg_edit.setStyleSheet(
            f"background-color:white; color:{COLOR_INPUT_TEXT};"
            f" border:1px solid {COLOR_INPUT_BORDER}; border-radius:4px; padding:4px 8px;"
            f" font-family:'Kode Mono'; font-size:13px;"
        )
        self.msg_edit.setFixedHeight(34)
        self.msg_edit.textChanged.connect(self._on_msg_changed)
        main.addWidget(self.msg_edit)

        main.addStretch()

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch()
        self.btn_clear_msg = _btn("Clear", _COL_CANCEL)
        self.btn_clear_msg.setVisible(False)
        self.btn_clear_msg.clicked.connect(self.msg_edit.clear)
        btn_row.addWidget(self.btn_clear_msg)
        self.btn_transmit = _btn("Transmit", COLOR_BTN_CYAN)
        self.btn_transmit.setVisible(False)
        connect_single(self.btn_transmit, self._on_transmit)
        btn_row.addWidget(self.btn_transmit)
        self.btn_close = _btn("Close", _COL_CANCEL)
        self.btn_close.clicked.connect(self.reject)
        btn_row.addWidget(self.btn_close)
        main.addLayout(btn_row)

    def _adjust_for_image_width(self, img_width: int) -> None:
        if img_width > 275:
            self.resize(self.width() + (img_width - 275), self.height())

    def _force_upper(self, text: str) -> None:
        if text != text.upper():
            self.cs_edit.blockSignals(True)
            pos = self.cs_edit.cursorPosition()
            self.cs_edit.setText(text.upper())
            self.cs_edit.setCursorPosition(pos)
            self.cs_edit.blockSignals(False)

    def _populate_rigs(self) -> None:
        self.rig_combo.clear()
        if self._tcp_pool:
            names = self._tcp_pool.get_all_rig_names()
            if len(names) == 1:
                self.rig_combo.addItems(names)
                self._on_rig_changed(names[0])
                return
            if len(names) > 1:
                self.rig_combo.addItem("Select a rig…")
                self.rig_combo.addItems(names)
                return
        self.rig_combo.addItem("No rigs configured")

    def _on_rig_changed(self, rig_name: str) -> None:
        if not self._tcp_pool or not rig_name or rig_name in ("No rigs configured", "Select a rig…"):
            self.freq_edit.setText("—")
            return
        client = self._tcp_pool.get_client(rig_name)
        if not client or not client.is_connected():
            self.freq_edit.setText("—")
            return
        # Show cached frequency immediately
        if client.frequency:
            self.freq_edit.setText(f"{client.frequency:.3f} MHz")
        else:
            self.freq_edit.setText("Fetching…")
        # Connect signal for live updates and request a fresh value
        try:
            client.frequency_received.disconnect(self._on_frequency_received)
        except (TypeError, RuntimeError):
            pass
        client.frequency_received.connect(self._on_frequency_received)
        from PyQt5.QtCore import QTimer
        QTimer.singleShot(RIG_FREQ_DELAY_MS, client.get_frequency)

    def _on_frequency_received(self, rig_name: str, dial_freq: int) -> None:
        self.freq_edit.setText(f"{dial_freq / 1_000_000:.3f} MHz")

    def _search(self) -> None:
        if self._qrz_thread is not None and self._qrz_thread.isRunning():
            return
        cs = self.cs_edit.text().strip().upper()
        if not cs:
            return
        self.lbl_status.setText(f"Looking up {cs}…")
        self.btn_search.setEnabled(False)
        self.qrz_info.clear()
        self.qrz_info.show_no_data_placeholder()
        _, username, password = load_qrz_config()
        self._qrz_thread = _QRZThread(cs, username, password)
        self._qrz_thread.result_ready.connect(self._on_qrz_result)
        self._qrz_thread.start()

    def _on_qrz_result(self, result) -> None:
        self.btn_search.setEnabled(True)
        if result:
            self.lbl_status.setText("")
            self.qrz_info.update_data(result)
            self.contact_memo_edit.blockSignals(True)
            self.contact_memo_edit.setText(result.get("memo") or "")
            self.contact_memo_edit.blockSignals(False)
        else:
            self.lbl_status.setText("No results found.")
            self.qrz_info.show_no_data_placeholder()

    def _on_msg_changed(self) -> None:
        text = self.msg_edit.toPlainText()
        if len(text) > 100:
            cursor = self.msg_edit.textCursor()
            pos = cursor.position()
            self.msg_edit.blockSignals(True)
            self.msg_edit.setPlainText(text[:100])
            from PyQt5.QtGui import QTextCursor
            c = self.msg_edit.textCursor()
            c.setPosition(min(pos, 100))
            self.msg_edit.setTextCursor(c)
            self.msg_edit.blockSignals(False)
            text = text[:100]
        has_text = bool(text.strip())
        self.btn_clear_msg.setVisible(has_text)
        self.btn_transmit.setVisible(has_text)

    def _save_contact_memo(self) -> None:
        cs = self.cs_edit.text().strip().upper()
        if not cs:
            return
        try:
            with sqlite3.connect(DB_PATH, timeout=10) as conn:
                conn.execute(
                    "UPDATE qrz SET memo = ? WHERE callsign = ? COLLATE NOCASE",
                    (self.contact_memo_edit.text(), cs)
                )
                conn.commit()
        except sqlite3.Error as e:
            print(f"[JS8MessageDialog] Contact memo save error: {e}")

    @staticmethod
    def _sanitize(text: str) -> str:
        import re
        text = text.replace('\r', '').replace('\n', '||')
        return re.sub(r'[^\x20-\x7E]', '', text).strip()

    def _on_transmit(self) -> None:
        cs = self.cs_edit.text().strip().upper()
        text = self._sanitize(self.msg_edit.toPlainText())
        if not cs or not text:
            return
        if not self._tcp_pool:
            QMessageBox.warning(self, "Transmit", "No TCP connection available.")
            return
        rig_name = self.rig_combo.currentText()
        if rig_name in ("No rigs configured", "Select a rig…"):
            QMessageBox.warning(self, "Transmit", "Please select a rig.")
            return
        client = self._tcp_pool.get_client(rig_name)
        if not client or not client.is_connected():
            QMessageBox.warning(self, "Transmit", f"Rig '{rig_name}' is not connected.")
            return
        my_cs = _get_local_callsign()
        if not my_cs:
            QMessageBox.warning(self, "Transmit", "No operator callsign configured in Settings.")
            return
        client.send_message("MODE.SET_SPEED", params={"SPEED": self.mode_combo.currentIndex()})
        msg_id = generate_time_based_id()
        payload = f"{my_cs}: {cs} MSG ,{msg_id},{text},{{^%}}"
        client.send_tx_message(payload)
        QMessageBox.information(self, "JS8 Message", "Message queued for transmission.")
        self.msg_edit.clear()


# ── Dialog 3: StatRep Detail ───────────────────────────────────────────────

class StatRepDetailDialog(QDialog):
    """Detail view for a StatRep row: QRZ info + 12 status indicators + map + comments."""

    pin_changed = pyqtSignal(bool)
    record_deleted = pyqtSignal()

    def __init__(self, record_id: str, callsign: str,
                 internet_available: bool = True,
                 commsrvr_url: str = "",
                 module_background: str = "#f5f5f5",
                 module_foreground: str = "#333333",
                 title_bar_background: str = "#555555",
                 title_bar_foreground: str = "#D2D0CF",
                 data_background: str = "#D2D0CF",
                 program_background: str = "",
                 program_foreground: str = "",
                 condition_green: str = "",
                 condition_yellow: str = "",
                 condition_red: str = "",
                 condition_gray: str = "",
                 condition_purple: str = "",
                 tcp_pool=None,
                 connector_manager=None,
                 record_list: list = None,
                 record_list_provider: Optional[Callable[[], list]] = None,
                 refresh_callback=None,
                 parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.Window |
            Qt.CustomizeWindowHint |
            Qt.WindowTitleHint |
            Qt.WindowCloseButtonHint |
            Qt.WindowMaximizeButtonHint |
            Qt.WindowStaysOnTopHint
        )
        self._record_id = record_id
        self.callsign = callsign
        self.internet_available = internet_available
        self._commsrvr_url = commsrvr_url
        self._module_bg = module_background
        self._module_fg = module_foreground
        self._title_bg = title_bar_background
        self._title_fg = title_bar_foreground
        self._data_bg = data_background
        self._program_bg = program_background or _PROG_BG
        self._program_fg = program_foreground or _PROG_FG
        self._status_colors = {
            "1": (condition_green  or STATUS_COLORS["1"][0], STATUS_COLORS["1"][1]),
            "2": (condition_yellow or STATUS_COLORS["2"][0], STATUS_COLORS["2"][1]),
            "3": (condition_red    or STATUS_COLORS["3"][0], STATUS_COLORS["3"][1]),
            "4": (condition_gray   or STATUS_COLORS["4"][0], STATUS_COLORS["4"][1]),
            "6": (condition_purple or STATUS_COLORS["6"][0], STATUS_COLORS["6"][1]),
        }
        self._tcp_pool = tcp_pool
        self._connector_manager = connector_manager
        self._thread: Optional[_QRZThread] = None
        self._rc_thread: Optional[_ReadCountThread] = None
        self._reload_token: int = 0
        self._map_loaded = False
        self._map_ready = False
        self._last_nav: str = "older"
        self._record_list: list = list(record_list) if record_list else []
        self._record_list_provider = record_list_provider
        self._refresh_callback = refresh_callback
        self._global_id = 0
        self._row_data: dict = {}
        self._sr_datetime: str = ""
        self._statrep_lat: Optional[float] = None
        self._statrep_lon: Optional[float] = None
        self._statrep_grid: str = ""
        self._print_view: Optional[QWebEngineView] = None
        self._print_busy: bool = False
        self.setWindowTitle(f"StatRep — {callsign}")
        self.setModal(True)
        self.setMinimumSize(996, 696)
        self.resize(996, 696)
        if os.path.exists("radiation-32.png"):
            self.setWindowIcon(QtGui.QIcon("radiation-32.png"))
        self._setup_ui()
        self._load_statrep()
        self._start_qrz()
        self._update_nav_buttons()

    def _setup_ui(self) -> None:
        self.setStyleSheet(
            f"QDialog {{ background-color:{self._module_bg}; }}"
            f"QLabel {{ color:{self._module_fg}; background-color: transparent; font-size: 13px; }}"
        )
        main = QVBoxLayout(self)
        main.setContentsMargins(10, 10, 10, 10)
        main.setSpacing(8)

        self.qrz_info = _QRZInfoSection(hdr_bg=self._program_bg, hdr_fg=self._program_fg, parent=self)
        self.contact_memo_edit = self.qrz_info.add_memo_row()
        self.contact_memo_edit.editingFinished.connect(self._save_contact_memo)
        self.qrz_info.add_statrep_rows()
        self.statrep_memo_edit = self.qrz_info.add_statrep_memo_row()
        self.qrz_info.image_width_ready.connect(self._adjust_for_image_width)
        main.addWidget(self.qrz_info, 1)

        # Status grid
        sg_widget = QWidget()
        sg_widget.setStyleSheet(f"border-top:1px solid #D2D0CF; border-left:1px solid #D2D0CF;")
        sg_grid = QGridLayout(sg_widget)
        sg_grid.setContentsMargins(0, 0, 0, 0)
        sg_grid.setSpacing(0)
        self._squares: Dict[str, QLabel] = {}
        for col_idx, (label_text, _) in enumerate(STATUS_FIELDS):
            hdr = QLabel(label_text)
            hdr.setAlignment(Qt.AlignCenter)
            hdr.setFont(_lbl_font())
            hdr.setStyleSheet(
                f"QLabel {{ background-color:{self._title_bg}; color:{self._title_fg};"
                "border-right:1px solid #D2D0CF; border-bottom:1px solid #D2D0CF; padding: 5px 2px; }"
            )
            sg_grid.addWidget(hdr, 0, col_idx)
            sq = QLabel()
            sq.setFixedHeight(16)
            sq.setStyleSheet("QLabel { background-color:rgb(255,255,255); border-right:1px solid #D2D0CF; border-bottom:1px solid #D2D0CF; }")
            sq.setToolTip("No status")
            sg_grid.addWidget(sq, 1, col_idx)
            sg_grid.setColumnStretch(col_idx, 1)
            self._squares[label_text] = sq
        main.addWidget(sg_widget)

        lower = QHBoxLayout()
        lower.setSpacing(10)
        self.map_view = QWebEngineView()
        self.map_view.setFixedSize(480, 220)
        self.map_view.loadFinished.connect(self._on_map_view_load_finished)
        lower.addWidget(self.map_view, alignment=Qt.AlignTop)

        self.comments = QTextBrowser()
        self.comments.setFont(_mono_font())
        self.comments.setFixedHeight(220)
        self.comments.setMinimumWidth(480)
        self.comments.setStyleSheet(
            f"background-color:{self._data_bg}; color:#000000;"
            f" border:1px solid {COLOR_INPUT_BORDER}; border-radius:4px;"
        )
        self.comments.setOpenLinks(False)
        self.comments.anchorClicked.connect(lambda url: QDesktopServices.openUrl(url))
        lower.addWidget(self.comments)
        main.addLayout(lower)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self.pin_toggle = _ToggleSwitch()
        self.pin_toggle.toggled.connect(self._save_pinned)
        self.lbl_pin = QLabel("Pin")
        self.lbl_pin.setFont(_lbl_font())
        btn_row.addWidget(self.pin_toggle)
        btn_row.addWidget(self.lbl_pin)
        btn_row.addStretch()

        self.btn_delete = _btn("Delete", COLOR_BTN_RED)
        self.btn_delete.clicked.connect(self._on_delete)
        btn_row.addWidget(self.btn_delete)

        self.btn_older = _btn("Previous", _COL_NAV)
        self.btn_older.clicked.connect(self._on_older)
        btn_row.addWidget(self.btn_older)

        self.btn_newer = _btn("Next", _COL_NAV)
        self.btn_newer.clicked.connect(self._on_newer)
        btn_row.addWidget(self.btn_newer)

        self.btn_reply_sr = _btn("Reply", COLOR_BTN_BLUE)
        self.btn_reply_sr.clicked.connect(self._on_reply_clicked)
        btn_row.addWidget(self.btn_reply_sr)

        self.btn_js8_reply_sr = _btn("JS8 Reply", COLOR_BTN_BLUE)
        self.btn_js8_reply_sr.clicked.connect(self._on_js8_reply_clicked)
        btn_row.addWidget(self.btn_js8_reply_sr)

        btn_brevity = _btn("Brevity", _COL_PURPLE)
        btn_brevity.clicked.connect(self._on_brevity)
        btn_row.addWidget(btn_brevity)

        btn_forward = _btn("Forward", COLOR_BTN_CYAN)
        btn_forward.clicked.connect(self._on_forward)
        btn_row.addWidget(btn_forward)

        btn_print = _btn("Print", COLOR_BTN_GREEN)
        btn_print.clicked.connect(self._on_print)
        btn_row.addWidget(btn_print)

        btn_close = _btn("Close", _COL_CANCEL)
        btn_close.clicked.connect(self.reject)
        btn_row.addWidget(btn_close)

        main.addLayout(btn_row)

    def _adjust_for_image_width(self, img_width: int) -> None:
        if img_width > 400:
            self.resize(self.width() + (img_width - 400), self.height())

    def _load_statrep(self) -> None:
        """Load status fields, comments, and map from the database."""
        try:
            with sqlite3.connect(DB_PATH, timeout=10) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT datetime, global_id, map, power, water, med, telecom, travel,
                           internet, fuel, food, crime, civil, political, comments, grid, sr_id,
                           freq, target, memo, pinned, source, scope
                    FROM statrep WHERE id = ?
                """, (self._record_id,))
                row = cursor.fetchone()
        except sqlite3.Error as e:
            print(f"[StatRepDetailDialog] DB error: {e}")
            return
        if not row:
            return

        self._sr_datetime = row[0] or ""

        self._row_data = {
            "map": row[2], "power": row[3], "water": row[4],
            "med": row[5], "telecom": row[6], "travel": row[7],
            "internet": row[8], "fuel": row[9], "food": row[10],
            "crime": row[11], "civil": row[12], "political": row[13],
            "comments": row[14], "grid": row[15],
            "sr_id": row[16],
            "scope": row[22],
            "origin_callsign": self.callsign,
        }

        global_id = row[1] or 0
        self._global_id = global_id
        freq_mhz = (float(row[17]) / 1_000_000) if row[17] else 0.0
        sr_id    = row[16] or ""
        group    = (row[18] or "").strip()
        sr_grid  = row[15] or ""
        source   = row[21] if row[21] is not None else 0
        _source_map = {1: "RF via JS8Call", 2: "Internet", 3: "Internet Only"}
        source_text  = _source_map.get(int(source), "Unknown")

        _k = "font-family:Roboto; font-weight:bold; font-size:13px;"
        self.qrz_info.lbl_sr_posted.setText(
            f'<span style="{_k}">Posted:</span>  {row[0]}' if row[0] else f'<span style="{_k}">Posted:</span>'
        )
        self.qrz_info.lbl_sr_source.setText(f'<span style="{_k}">Received via:</span>  {source_text}')
        self.qrz_info.lbl_sr_global_id.setText(
            f'<span style="{_k}">Global ID:</span>  {global_id}' if global_id else f'<span style="{_k}">Global ID:</span>'
        )
        self.qrz_info.lbl_sr_group.setText(
            f'<span style="{_k}">Group:</span>  {group}' if group else f'<span style="{_k}">Group:</span>'
        )
        self.qrz_info.lbl_sr_grid.setText(
            f'<span style="{_k}">Grid:</span>  {sr_grid}' if sr_grid else f'<span style="{_k}">Grid:</span>'
        )
        self.qrz_info.lbl_sr_freq.setText(
            f'<span style="{_k}">Freq:</span>  {freq_mhz:.3f} MHz' if freq_mhz else f'<span style="{_k}">Freq:</span>'
        )
        self.qrz_info.lbl_sr_sr_id.setText(
            f'<span style="{_k}">Statrep ID:</span>  {sr_id}' if sr_id else f'<span style="{_k}">Statrep ID:</span>'
        )
        self.qrz_info.lbl_sr_delivered.setText(f'<span style="{_k}">Delivered To:</span>')

        self.qrz_info._skip_last_seen = False
        if global_id and self._commsrvr_url and self.internet_available:
            local_cs = _get_local_callsign()
            if local_cs:
                # get-read-count returns both the delivered count and last-seen
                # ("115,50 seconds ago"), so skip the redundant standalone last-seen call.
                self.qrz_info._skip_last_seen = True
                rc_token = self._reload_token
                self._rc_thread = _ReadCountThread(self._commsrvr_url, local_cs, global_id)
                self._rc_thread.count_ready.connect(
                    lambda text, t=rc_token: self._on_read_count(text) if t == self._reload_token else None
                )
                self._rc_thread.start()

        for i, (label_text, _) in enumerate(STATUS_FIELDS):
            val = str(row[i + 2]) if row[i + 2] is not None else ""
            sq = self._squares[label_text]
            color_str, tip = self._status_colors.get(val, ("rgb(255,255,255)", "No status"))
            sq.setStyleSheet(f"QLabel {{ background-color:{color_str}; border:1px solid #D2D0CF; }}")
            sq.setToolTip(tip)

        self.comments.setHtml(_text_to_html((row[14] or "").replace("||", "\n"), self._data_bg))

        self.statrep_memo_edit.blockSignals(True)
        self.statrep_memo_edit.setPlainText(row[19] or "")
        self.statrep_memo_edit.blockSignals(False)

        self.pin_toggle.blockSignals(True)
        self.pin_toggle.setChecked(bool(row[20]))
        self.pin_toggle.blockSignals(False)

        grid = row[15]
        if grid:
            try:
                coords = mh.to_location(grid, center=True)
                lat, lon = float(coords[0]), float(coords[1])
                self._statrep_lat = lat
                self._statrep_lon = lon
                self._statrep_grid = grid[:4].upper()
                self._map_ready = False
                self.map_view.setHtml(
                    _make_map_html(lat, lon, self.internet_available),
                    QUrl("http://localhost/")
                )
                self._map_loaded = True
            except Exception as e:
                print(f"[StatRepDetailDialog] Map error for grid {grid}: {e}")

    def _on_map_view_load_finished(self, ok: bool) -> None:
        """Flag-only readiness signal used by the print/PDF view (_on_print)
        to avoid grabbing a blank frame before the map has actually painted.
        Deliberately does NOT grab the widget here: QWebEngineView.grab() has
        been observed to disrupt the widget's own live rendering on some
        Windows GPU/driver stacks, so it must only ever be called lazily, at
        the moment the user clicks Print — never automatically on every load."""
        self._map_ready = bool(ok)

    def _on_read_count(self, text: str) -> None:
        if not text:
            return
        # Response carries both values, e.g. "115,50 seconds ago"
        # (delivered count before the comma, last-seen after).
        parts = text.split(",", 1)
        count_str = parts[0].strip()
        last_seen_str = parts[1].strip() if len(parts) > 1 else ""
        _k = "font-family:Roboto; font-weight:bold; font-size:13px;"
        self.qrz_info.lbl_sr_delivered.setText(f'<span style="{_k}">Delivered To:</span>  {count_str} CommStat users')
        self.qrz_info._on_last_seen_updated(last_seen_str if last_seen_str else "—")

    def _save_pinned(self, checked: bool) -> None:
        """Save pinned state to the database and notify the main window."""
        try:
            with sqlite3.connect(DB_PATH, timeout=10) as conn:
                conn.execute(
                    "UPDATE statrep SET pinned = ? WHERE id = ?",
                    (1 if checked else 0, self._record_id)
                )
                conn.commit()
            self.pin_changed.emit(checked)
        except sqlite3.Error as e:
            print(f"[StatRepDetailDialog] Pinned save error: {e}")

    def _on_brevity(self) -> None:
        selected = self.comments.textCursor().selectedText().strip()
        if not selected:
            matches = _BREVITY_RE.findall(self.comments.toPlainText())
            if matches:
                selected = matches[0]
        from brevity import BrevityApp
        win = BrevityApp(self._module_bg, self._module_fg, selected or "", parent=self)
        self._brevity_window = win
        win.show()
        parent_center = self.frameGeometry().center()
        win_rect = win.frameGeometry()
        win_rect.moveCenter(parent_center)
        win.move(win_rect.topLeft())

    def _on_forward(self) -> None:
        if not self._tcp_pool or not self._connector_manager or not self._row_data:
            return

        is_event = self._row_data.get("scope") == "EVENT"

        if self._sr_datetime:
            try:
                sr_dt_str = self._sr_datetime.replace(" UTC", "").strip()
                sr_dt = datetime.datetime.strptime(sr_dt_str, "%Y-%m-%d %H:%M:%S")
                sr_dt = sr_dt.replace(tzinfo=datetime.timezone.utc)
                age = datetime.datetime.now(datetime.timezone.utc) - sr_dt
                if age.total_seconds() > 86400:
                    from PyQt5.QtWidgets import QMessageBox
                    msg = QMessageBox(self)
                    msg.setWindowTitle("Cannot Forward")
                    kind = "Event" if is_event else "Status Report"
                    msg.setText(
                        f"This {kind} cannot be forwarded because it is more than 24 hours old."
                    )
                    msg.setIcon(QMessageBox.Warning)
                    msg.setStandardButtons(QMessageBox.Close)
                    msg.exec_()
                    return
            except (ValueError, TypeError):
                pass

        if is_event:
            from group_event import GroupEventDialog
            dlg = GroupEventDialog(
                self._tcp_pool, self._connector_manager, self,
                module_background=self._module_bg,
            )
            dlg.prefill({**self._row_data, "pinned": self.pin_toggle.isChecked()})
            dlg.exec_()
            return

        from statrep import StatRepDialog
        dlg = StatRepDialog(
            self._tcp_pool, self._connector_manager, self,
            module_background=self._module_bg,
        )
        dlg.prefill(self._row_data)
        dlg.exec_()

    def _font_data_uri(self, ttf_path: str) -> str:
        """Read a .ttf file and return a base64 data URI for @font-face embedding."""
        try:
            with open(ttf_path, "rb") as f:
                return "data:font/ttf;base64," + base64.b64encode(f.read()).decode("ascii")
        except Exception:
            return ""

    def _pixmap_to_data_uri(self, pixmap: QPixmap) -> str:
        """Encode a QPixmap as a base64 data URI for embedding in HTML."""
        if pixmap is None or pixmap.isNull():
            return ""
        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(QBuffer.WriteOnly)
        pixmap.save(buf, "PNG")
        buf.close()
        return "data:image/png;base64," + bytes(ba.toBase64()).decode("ascii")

    def _build_print_html(self) -> str:
        """Build a self-contained HTML document mirroring the on-screen detail view."""
        status_key_map = ["map", "power", "water", "med", "telecom", "travel",
                          "internet", "fuel", "food", "crime", "civil", "political"]
        status_cells_hdr = []
        status_cells_val = []
        for (label_text, _), key in zip(STATUS_FIELDS, status_key_map):
            val = str(self._row_data.get(key) or "")
            color, _tip = self._status_colors.get(val, ("rgb(255,255,255)", "No status"))
            status_cells_hdr.append(
                f'<td style="background-color:{self._title_bg};color:{self._title_fg};'
                f'font-weight:bold;text-align:center;padding:4px 2px;'
                f'border:1px solid #D2D0CF;">{label_text}</td>'
            )
            status_cells_val.append(
                f'<td style="background-color:{color};height:18px;'
                f'border:1px solid #D2D0CF;">&nbsp;</td>'
            )
        status_table = (
            '<table style="width:100%;border-collapse:collapse;'
            'font-family:Roboto;font-size:12px;">'
            f'<tr>{"".join(status_cells_hdr)}</tr>'
            f'<tr>{"".join(status_cells_val)}</tr>'
            '</table>'
        )

        qrz_lines = [
            self.qrz_info.lbl_call.text(),
            self.qrz_info.lbl_name.text(),
            self.qrz_info.lbl_addr1.text(),
            self.qrz_info.lbl_addr2.text(),
            self.qrz_info.lbl_county.text(),
            self.qrz_info.lbl_country.text(),
            self.qrz_info.lbl_license.text(),
            self.qrz_info.lbl_grid.text(),
            self.qrz_info.lbl_lat.text(),
            self.qrz_info.lbl_lon.text(),
        ]
        qrz_block = "<br>".join(t for t in qrz_lines if t)

        moddate = self.qrz_info.lbl_moddate.text()
        moddate_html = f'<div style="font-size:11px;color:#555;">{moddate}</div>' if moddate else ""

        photo_html = ""
        try:
            pm = self.qrz_info.lbl_image.pixmap()
            uri = self._pixmap_to_data_uri(pm) if pm is not None else ""
            if uri:
                photo_html = (
                    f'<img src="{uri}" style="max-height:160px;max-width:200px;'
                    f'border:1px solid #ccc;">'
                )
        except Exception:
            photo_html = ""

        sr_meta_pairs = [
            (self.qrz_info.lbl_sr_posted.text(),    self.qrz_info.lbl_sr_source.text()),
            (self.qrz_info.lbl_sr_group.text(),     self.qrz_info.lbl_sr_delivered.text()),
            (self.qrz_info.lbl_sr_global_id.text(), self.qrz_info.lbl_sr_grid.text()),
            (self.qrz_info.lbl_sr_sr_id.text(),     self.qrz_info.lbl_sr_freq.text()),
        ]
        sr_meta_rows = "".join(
            f'<tr><td style="padding:2px 16px 2px 0;width:50%;">{left}</td>'
            f'<td style="padding:2px 0;width:50%;">{right}</td></tr>'
            for left, right in sr_meta_pairs
        )
        sr_meta_html = (
            f'<table style="border-collapse:collapse;width:100%;">{sr_meta_rows}</table>'
        )

        map_html = ""
        if self._map_loaded and self._map_ready:
            try:
                map_pm = self.map_view.grab()
                uri = self._pixmap_to_data_uri(map_pm)
                if uri:
                    map_html = (
                        f'<img src="{uri}" style="max-width:100%;'
                        f'border:1px solid #ccc;">'
                    )
            except Exception:
                map_html = ""
        if not map_html and self._statrep_grid:
            map_html = (
                f'<div style="font-family:\'Kode Mono\',monospace;font-size:13px;'
                f'color:#0000CC;">'
                f'Grid: {self._statrep_grid}</div>'
            )

        raw_comments = (self._row_data.get("comments") or "").replace("||", "\n")
        if raw_comments:
            comments_html = _text_to_html(raw_comments, self._data_bg)
            comments_html = (
                comments_html.replace("<html>", "").replace("</html>", "")
                             .replace("<body", "<div").replace("</body>", "</div>")
            )
        else:
            comments_html = ""

        note_html = ""
        note_text = self.statrep_memo_edit.toPlainText().strip()
        if note_text:
            note_html = (
                '<div class="section-title">Status Report Note</div>'
                f'<pre style="white-space:pre-wrap;font-family:\'Kode Mono\',monospace;'
                f'font-size:12px;margin:0;color:#0000CC;">{note_text}</pre>'
            )

        contact_note_html = ""
        contact_text = self.contact_memo_edit.text().strip()
        if contact_text:
            contact_note_html = (
                '<div class="section-title">Contact Note</div>'
                f'<div style="font-family:\'Kode Mono\',monospace;font-size:12px;'
                f'color:#0000CC;">'
                f'{contact_text}</div>'
            )

        generated = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        title_dt = self._sr_datetime or ""

        font_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
        roboto_reg   = self._font_data_uri(os.path.join(font_dir, "Roboto-Regular.ttf"))
        roboto_bold  = self._font_data_uri(os.path.join(font_dir, "Roboto-Bold.ttf"))
        slab_bold    = self._font_data_uri(os.path.join(font_dir, "RobotoSlab-Bold.ttf"))
        kode_reg     = self._font_data_uri(os.path.join(font_dir, "KodeMono-Regular.ttf"))

        font_face_css = ""
        if roboto_reg:
            font_face_css += (
                f"@font-face {{ font-family:'Roboto'; font-style:normal; font-weight:400;"
                f" src:url({roboto_reg}) format('truetype'); }}"
            )
        if roboto_bold:
            font_face_css += (
                f"@font-face {{ font-family:'Roboto'; font-style:normal; font-weight:700;"
                f" src:url({roboto_bold}) format('truetype'); }}"
            )
        if slab_bold:
            font_face_css += (
                f"@font-face {{ font-family:'Roboto Slab'; font-style:normal; font-weight:700;"
                f" src:url({slab_bold}) format('truetype'); }}"
            )
        if kode_reg:
            font_face_css += (
                f"@font-face {{ font-family:'Kode Mono'; font-style:normal; font-weight:400;"
                f" src:url({kode_reg}) format('truetype'); }}"
            )

        return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>StatRep — {self.callsign}</title>
<style>
  {font_face_css}
  body {{ background:#ffffff; color:#000000;
          font-family: Roboto, Arial; font-size:13px;
          margin:24px; }}
  h1 {{ font-family: 'Roboto Slab', Roboto; font-weight:700;
        font-size:22px; margin:0 0 4px 0; color:{self._program_bg}; }}
  .subhead {{ font-size:13px; color:#555; margin-bottom:14px; }}
  .section-title {{ font-family: 'Roboto Slab', Roboto; font-weight:700;
                    font-size:17px; margin:14px 0 6px 0;
                    border-bottom:1px solid #aaa; padding-bottom:2px; }}
  .qrz-row {{ display:flex; gap:16px; }}
  .qrz-text {{ flex:1; font-family:'Kode Mono'; font-size:12px;
               line-height:1.5; color:#0000CC; }}
  .qrz-text span {{ color:#000000; }}
  .qrz-photo {{ flex:0 0 auto; text-align:right; }}
  .sr-meta {{ font-family:'Kode Mono'; font-size:12px; line-height:1.5;
              color:#0000CC; }}
  .sr-meta span {{ color:#000000; }}
  .map-box {{ text-align:center; page-break-inside:avoid; }}
  .status-box {{ page-break-inside:avoid; }}
  .comments-box {{ font-family:'Kode Mono'; font-size:12px;
                   border:1px solid #ccc; padding:8px; color:#0000CC;
                   background-color:{self._data_bg}; }}
  .comments-box a {{ color:#0078d7; }}
  .footer {{ font-size:10px; color:#888; margin-top:20px;
             border-top:1px solid #ddd; padding-top:6px; }}
</style></head>
<body>
  <h1>Status Report — {self.callsign}</h1>
  <div class="subhead">{title_dt}</div>

  <div class="section-title">QRZ Lookup</div>
  <div class="qrz-row">
    <div class="qrz-text">{qrz_block}</div>
    <div class="qrz-photo">{photo_html}{moddate_html}</div>
  </div>

  <div class="section-title">Status Indicators</div>
  <div class="status-box">{status_table}</div>

  <div class="section-title">Status Report Details</div>
  <div class="sr-meta">{sr_meta_html}</div>

  <div class="section-title">Location</div>
  <div class="map-box">{map_html}</div>

  <div class="section-title">Comments</div>
  <div class="comments-box">{comments_html}</div>

  {note_html}
  {contact_note_html}

  <div class="footer">Generated by CommStat — {generated}</div>
</body></html>"""

    def _on_print(self) -> None:
        if self._print_busy:
            return
        self._print_busy = True
        self._print_wait_deadline = time.monotonic() + 1.5
        self._await_map_then_print()

    def _await_map_then_print(self) -> None:
        # The map loads asynchronously; grabbing before it has painted (its
        # tiles included) captures a blank frame. Wait briefly for it, but
        # don't block Print indefinitely if it never finishes.
        if (self._map_loaded and not self._map_ready
                and time.monotonic() < self._print_wait_deadline):
            from PyQt5.QtCore import QTimer
            QTimer.singleShot(100, self._await_map_then_print)
            return

        try:
            html = self._build_print_html()
        except Exception as e:
            print(f"[StatRepDetailDialog] PDF build error: {e}")
            self._print_busy = False
            return

        safe_cs  = (self.callsign or "unknown").replace("/", "_").replace(" ", "_")
        safe_sr  = (str(self._row_data.get("sr_id") or "")).replace("/", "_").replace(" ", "_")
        ts       = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        fname    = f"statrep_{safe_cs}_{safe_sr}_{ts}.pdf" if safe_sr else f"statrep_{safe_cs}_{ts}.pdf"
        pdf_path = os.path.join(tempfile.gettempdir(), fname)

        view = QWebEngineView()
        self._print_view = view

        def _on_load_finished(ok: bool) -> None:
            if not ok:
                print("[StatRepDetailDialog] PDF error: HTML failed to load")
                self._cleanup_print_view()
                return
            view.page().printToPdf(pdf_path)

        def _on_pdf_done(file_path: str, success: bool) -> None:
            if success and file_path:
                try:
                    abs_path = os.path.abspath(file_path).replace("\\", "/")
                    webbrowser.open("file:///" + abs_path)
                except Exception as e:
                    print(f"[StatRepDetailDialog] PDF open error: {e}")
            else:
                print(f"[StatRepDetailDialog] PDF error: printToPdf failed for {file_path}")
            self._cleanup_print_view()

        view.loadFinished.connect(_on_load_finished)
        view.page().pdfPrintingFinished.connect(_on_pdf_done)
        view.setHtml(html, QUrl("http://localhost/"))

    def _cleanup_print_view(self) -> None:
        v = self._print_view
        self._print_view = None
        self._print_busy = False
        if v is not None:
            try:
                v.deleteLater()
            except Exception:
                pass

    def _on_reply_clicked(self) -> None:
        original = (self._row_data.get("comments") or "").replace("||", "\n")
        prefill = "\n\n----------\n" + original
        dlg = QRZLookupDialog(
            module_background=self._module_bg,
            module_foreground=self._module_fg,
            program_background=self._program_bg,
            program_foreground=self._program_fg,
            initial_callsign=self.callsign,
            initial_message=prefill,
            refresh_callback=self._refresh_callback,
            parent=self,
        )
        dlg.exec_()

    def _on_js8_reply_clicked(self) -> None:
        """Reply to this StatRep over RF via JS8 Direct Message. The clicked
        callsign is shown as a read-only reminder in the JS8 dialog (not forced
        into its roster-driven Target combo); the body is seeded with the
        original comments."""
        from js8_direct_message import JS8DirectMessageDialog
        original = (self._row_data.get("comments") or "").replace("||", "\n")
        prefill = "\n\n----------\n" + original
        dlg = JS8DirectMessageDialog(
            tcp_pool=self._tcp_pool,
            connector_manager=self._connector_manager,
            refresh_callback=self._refresh_callback,
            parent=self,
        )
        dlg.set_reply_context(self.callsign, prefill)
        dlg.exec_()

    def _on_newer(self) -> None:
        self._last_nav = "newer"
        self._navigate("newer")

    def _on_older(self) -> None:
        self._last_nav = "older"
        self._navigate("older")

    def _get_record_list(self) -> list:
        if self._record_list_provider is not None:
            try:
                return self._record_list_provider() or []
            except Exception as e:
                print(f"[StatRepDetailDialog] record_list_provider error: {e}")
        return self._record_list

    def _find_index(self, record_list: list, record_id) -> Optional[int]:
        return next(
            (i for i, (rid, _) in enumerate(record_list)
             if str(rid) == str(record_id)),
            None
        )

    def _update_nav_buttons(self, record_list: Optional[list] = None) -> None:
        if record_list is None:
            record_list = self._get_record_list()
        idx = self._find_index(record_list, self._record_id) if record_list else None
        n = len(record_list)
        self.btn_newer.setEnabled(idx is not None and idx > 0)
        self.btn_older.setEnabled(idx is not None and idx < n - 1)

    def _navigate(self, direction: str) -> None:
        record_list = self._get_record_list()
        if record_list:
            idx = self._find_index(record_list, self._record_id)
            if idx is None:
                self._update_nav_buttons(record_list)
                return
            if direction == "newer":
                if idx <= 0:
                    self._update_nav_buttons(record_list)
                    return
                next_id, next_cs = record_list[idx - 1]
            else:
                if idx >= len(record_list) - 1:
                    self._update_nav_buttons(record_list)
                    return
                next_id, next_cs = record_list[idx + 1]
            self._reload(next_id, next_cs)
            return
        try:
            with sqlite3.connect(DB_PATH, timeout=10) as conn:
                cursor = conn.cursor()
                if direction == "newer":
                    cursor.execute(
                        "SELECT id, from_callsign FROM statrep WHERE id > ? ORDER BY id ASC LIMIT 1",
                        (self._record_id,)
                    )
                else:
                    cursor.execute(
                        "SELECT id, from_callsign FROM statrep WHERE id < ? ORDER BY id DESC LIMIT 1",
                        (self._record_id,)
                    )
                row = cursor.fetchone()
        except sqlite3.Error as e:
            print(f"[StatRepDetailDialog] Navigate error: {e}")
            return
        if not row:
            return
        self._reload(row[0], row[1] or "")

    def _reload(self, record_id, callsign: str) -> None:
        self._save_statrep_memo()
        self._reload_token += 1
        self.btn_newer.setEnabled(False)
        self.btn_older.setEnabled(False)
        if self._thread is not None:
            try:
                self._thread.result_ready.disconnect()
            except (TypeError, RuntimeError):
                pass
            self._thread = None
        if self._rc_thread is not None:
            try:
                self._rc_thread.count_ready.disconnect()
            except (TypeError, RuntimeError):
                pass
            self._rc_thread = None
        self._record_id = record_id
        self.callsign = callsign
        self._map_loaded = False
        self._map_ready = False
        self._global_id = 0
        self._row_data = {}
        self._sr_datetime = ""
        self._statrep_lat = None
        self._statrep_lon = None
        self._statrep_grid = ""
        self.setWindowTitle(f"StatRep — {callsign}")
        self.map_view.setHtml("", QUrl("http://localhost/"))
        self._load_statrep()
        self._start_qrz()
        self._update_nav_buttons()

    def _on_delete(self) -> None:
        from ui_helpers import confirm
        if not confirm(
            self, "Confirm Delete",
            "Status Report records will be deleted from all CommStat users if you are "
            "the creator of the record. If you are not the creator, the record will only "
            "be deleted locally.",
        ):
            return
        import netguard
        if self._global_id and self._commsrvr_url and netguard.guard("Remote statrep delete"):
            try:
                local_cs = _get_local_callsign()
                url = (f"{self._commsrvr_url}/statrep-delete-808585.php"
                       f"?cs={urllib.parse.quote(local_cs)}&id={self._global_id}")
                with urllib.request.urlopen(url, timeout=10, context=create_verified_ssl_context()):
                    pass
            except Exception as e:
                print(f"[StatRepDetailDialog] Delete request failed: {e}")
        deleted_id = self._record_id
        direction = self._last_nav
        full_list = self._get_record_list()
        idx_before = self._find_index(full_list, deleted_id) if full_list else None
        try:
            with sqlite3.connect(DB_PATH, timeout=10) as conn:
                conn.execute("DELETE FROM statrep WHERE id = ?", (deleted_id,))
                conn.commit()
        except sqlite3.Error:
            pass
        self.record_deleted.emit()
        if full_list:
            remaining = [(rid, cs) for (rid, cs) in full_list if str(rid) != str(deleted_id)]
            if not remaining:
                self.accept()
                return
            if idx_before is None:
                next_idx = 0 if direction == "newer" else len(remaining) - 1
            elif direction == "newer":
                next_idx = max(0, idx_before - 1)
            else:
                next_idx = min(idx_before, len(remaining) - 1)
            next_id, next_cs = remaining[next_idx]
            self._reload(next_id, next_cs)
            return
        try:
            with sqlite3.connect(DB_PATH, timeout=10) as conn:
                cursor = conn.cursor()
                if direction == "newer":
                    cursor.execute(
                        "SELECT id, from_callsign FROM statrep WHERE id > ? ORDER BY id ASC LIMIT 1",
                        (deleted_id,)
                    )
                else:
                    cursor.execute(
                        "SELECT id, from_callsign FROM statrep WHERE id < ? ORDER BY id DESC LIMIT 1",
                        (deleted_id,)
                    )
                row = cursor.fetchone()
        except sqlite3.Error:
            row = None
        if row:
            self._record_id = row[0]
            self._reload(row[0], row[1] or "")
        else:
            self.accept()

    def _start_qrz(self) -> None:
        cached_fresh = get_qrz_cached(self.callsign)
        cached_any   = cached_fresh or get_qrz_cached(self.callsign, include_stale=True)
        is_active, username, password = load_qrz_config()

        if not username:
            found_str = "found" if cached_any else "NOT found"
            self.qrz_info.set_qrz_status(
                f"QRZ Subscription not configured — {self.callsign} {found_str} in local database"
            )
            if cached_any:
                self._on_qrz_result(cached_any)
            else:
                self.qrz_info.show_no_data_placeholder()
            return

        if not is_active:
            found_str = "found" if cached_any else "NOT found"
            self.qrz_info.set_qrz_status(
                f"QRZ Subscription not enabled — {self.callsign} {found_str} in local database"
            )
            if cached_any:
                self._on_qrz_result(cached_any)
            else:
                self.qrz_info.show_no_data_placeholder()
            return

        if cached_fresh:
            self._on_qrz_result(cached_fresh)
            return

        # No fresh cache (missing or stale) — live lookup; QRZClient handles stale refresh
        token = self._reload_token
        self._thread = _QRZThread(self.callsign, username, password)
        self._thread.result_ready.connect(
            lambda result, t=token: self._on_qrz_result(result) if t == self._reload_token else None
        )
        self._thread.start()

    def _save_contact_memo(self) -> None:
        try:
            with sqlite3.connect(DB_PATH, timeout=10) as conn:
                conn.execute(
                    "UPDATE qrz SET memo = ? WHERE callsign = ? COLLATE NOCASE",
                    (self.contact_memo_edit.text(), self.callsign)
                )
                conn.commit()
        except sqlite3.Error as e:
            print(f"[StatRepDetailDialog] Contact memo save error: {e}")

    def _save_statrep_memo(self) -> None:
        try:
            with sqlite3.connect(DB_PATH, timeout=10) as conn:
                conn.execute(
                    "UPDATE statrep SET memo = ? WHERE id = ?",
                    (self.statrep_memo_edit.toPlainText(), self._record_id)
                )
                conn.commit()
        except sqlite3.Error as e:
            print(f"[StatRepDetailDialog] StatRep memo save error: {e}")

    def done(self, result: int) -> None:
        self._save_statrep_memo()
        super().done(result)

    def _on_qrz_result(self, result) -> None:
        if not result:
            return
        self.qrz_info.update_data(result)
        if subscription_status() is False:
            self.qrz_info.set_qrz_status(
                f"QRZ XML subscription not found — showing local data for {self.callsign}"
            )
        self.contact_memo_edit.blockSignals(True)
        self.contact_memo_edit.setText(result.get("memo") or "")
        self.contact_memo_edit.blockSignals(False)
        d = _normalize_qrz(result)

        if not self._map_loaded:
            if d["lat"] and d["lon"]:
                try:
                    lat, lon = float(d["lat"]), float(d["lon"])
                    self.map_view.setHtml(
                        _make_map_html(lat, lon, self.internet_available),
                        QUrl("http://localhost/")
                    )
                    self._map_loaded = True
                except (ValueError, TypeError):
                    pass
        else:
            qrz_grid = (d.get("grid") or "")[:4].upper()
            if qrz_grid and qrz_grid != self._statrep_grid and d["lat"] and d["lon"]:
                try:
                    qrz_lat, qrz_lon = float(d["lat"]), float(d["lon"])
                    self.map_view.setHtml(
                        _make_map_html(
                            self._statrep_lat, self._statrep_lon,
                            self.internet_available,
                            extra_lat=qrz_lat, extra_lon=qrz_lon,
                        ),
                        QUrl("http://localhost/")
                    )
                except (ValueError, TypeError):
                    pass


# ── Dialog 3: Message Detail ───────────────────────────────────────────────

import html as _html_mod
import re as _re

_URL_RE = _re.compile(r'(https?://[^\s<>"\']+)', _re.IGNORECASE)
_BREVITY_RE = _re.compile(r'\b([0-9][A-Z]{5})\b')


def _text_to_html(text: str, bg: str) -> str:
    """Convert plain text to HTML, turning URLs into clickable links and highlighting brevity codes."""
    escaped = _html_mod.escape(text)
    highlighted = _BREVITY_RE.sub(
        r'<span style="background-color:#FFD700;font-weight:bold;">\1</span>',
        escaped
    )
    linked = _URL_RE.sub(
        lambda m: f'<a href="{m.group(1)}" style="color:#0078d7;">{m.group(1)}</a>',
        highlighted
    )
    lines = linked.replace("\n", "<br>")
    return (
        f'<html><body style="background-color:{bg};color:#000000;'
        f'font-family:\'Kode Mono\';font-size:13px;">{lines}</body></html>'
    )


class MessageDetailDialog(QDialog):
    """Detail view for a Message row: QRZ info + map + message text."""

    record_deleted = pyqtSignal()

    def __init__(self, record_id, callsign: str, message_text: str,
                 internet_available: bool = True,
                 commsrvr_url: str = "",
                 module_background: str = "#f5f5f5",
                 module_foreground: str = "#333333",
                 data_background: str = "#D2D0CF",
                 program_background: str = "",
                 program_foreground: str = "",
                 msg_id: str = "",
                 tcp_pool=None,
                 connector_manager=None,
                 refresh_callback=None,
                 parent=None):
        super().__init__(parent)
        self._record_id = record_id
        self.setWindowFlags(
            Qt.Window |
            Qt.CustomizeWindowHint |
            Qt.WindowTitleHint |
            Qt.WindowCloseButtonHint |
            Qt.WindowMaximizeButtonHint |
            Qt.WindowStaysOnTopHint
        )
        self.callsign = callsign
        self.message_text = message_text
        self._tcp_pool = tcp_pool
        self._connector_manager = connector_manager
        self.internet_available = internet_available
        self._commsrvr_url = commsrvr_url
        self._module_bg = module_background
        self._module_fg = module_foreground
        self._data_bg = data_background
        self._program_bg = program_background or _PROG_BG
        self._program_fg = program_foreground or _PROG_FG
        self._msg_id = msg_id
        self._refresh_callback = refresh_callback
        self._thread: Optional[_QRZThread] = None
        self._rc_thread: Optional[_ReadCountThread] = None
        self._reload_token: int = 0
        self._map_loaded = False
        self._deleted_any = False
        self._last_nav: str = "older"
        self._msg_datetime: str = ""
        self.setWindowTitle(f"Message — {callsign}")
        self.setModal(True)
        self.setMinimumSize(996, 616)
        self.resize(996, 616)
        if os.path.exists("radiation-32.png"):
            self.setWindowIcon(QtGui.QIcon("radiation-32.png"))
        self._setup_ui()
        self._fetch_message_details()
        self._start_qrz()
        self._update_nav_buttons()

    def _setup_ui(self) -> None:
        self.setStyleSheet(
            f"QDialog {{ background-color:{self._module_bg}; }}"
            f"QLabel {{ color:{self._module_fg}; background-color: transparent; font-size: 13px; }}"
        )
        main = QVBoxLayout(self)
        main.setContentsMargins(10, 10, 10, 10)
        main.setSpacing(8)

        self.qrz_info = _QRZInfoSection(hdr_bg=self._program_bg, hdr_fg=self._program_fg, parent=self)
        self.contact_memo_edit = self.qrz_info.add_memo_row()
        self.contact_memo_edit.editingFinished.connect(self._save_contact_memo)
        self.qrz_info.add_message_rows()
        main.addWidget(self.qrz_info)
        main.addStretch(1)

        lower = QHBoxLayout()
        lower.setSpacing(10)
        self.map_view = QWebEngineView()
        self.map_view.setFixedSize(480, 220)
        lower.addWidget(self.map_view, alignment=Qt.AlignTop)

        self.msg_text = QTextBrowser()
        self.msg_text.setFont(_mono_font())
        self.msg_text.setFixedHeight(220)
        self.msg_text.setMinimumWidth(480)
        self.msg_text.setStyleSheet(
            f"background-color:{self._data_bg}; border:1px solid {COLOR_INPUT_BORDER}; border-radius:4px;"
        )
        self.msg_text.setOpenLinks(False)
        self.msg_text.anchorClicked.connect(lambda url: QDesktopServices.openUrl(url))
        self.msg_text.setHtml(_text_to_html(self.message_text.replace("||", "\n"), self._data_bg))
        lower.addWidget(self.msg_text)
        main.addLayout(lower)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch()

        self.btn_delete = _btn("Delete", COLOR_BTN_RED)
        self.btn_delete.clicked.connect(self._on_delete)
        btn_row.addWidget(self.btn_delete)

        self.btn_older = _btn("Previous", _COL_NAV)
        self.btn_older.clicked.connect(self._on_older)
        btn_row.addWidget(self.btn_older)

        self.btn_newer = _btn("Next", _COL_NAV)
        self.btn_newer.clicked.connect(self._on_newer)
        btn_row.addWidget(self.btn_newer)

        self.btn_reply = _btn("Reply", COLOR_BTN_BLUE)
        self.btn_reply.clicked.connect(self._on_reply_clicked)
        btn_row.addWidget(self.btn_reply)

        self.btn_js8_reply = _btn("JS8 Reply", COLOR_BTN_BLUE)
        self.btn_js8_reply.clicked.connect(self._on_js8_reply_clicked)
        btn_row.addWidget(self.btn_js8_reply)

        self.btn_close = _btn("Close", _COL_CANCEL)
        self.btn_close.clicked.connect(self._on_close_clicked)
        btn_row.addWidget(self.btn_close)

        main.addLayout(btn_row)

    def _save_contact_memo(self) -> None:
        try:
            with sqlite3.connect(DB_PATH, timeout=10) as conn:
                conn.execute(
                    "UPDATE qrz SET memo = ? WHERE callsign = ? COLLATE NOCASE",
                    (self.contact_memo_edit.text(), self.callsign)
                )
                conn.commit()
        except sqlite3.Error as e:
            print(f"[MessageDetailDialog] Contact memo save error: {e}")

    def _on_reply_clicked(self) -> None:
        original = self.message_text.replace("||", "\n")
        prefill = "\n\n----------\n" + original
        dlg = QRZLookupDialog(
            module_background=self._module_bg,
            module_foreground=self._module_fg,
            program_background=self._program_bg,
            program_foreground=self._program_fg,
            initial_callsign=self.callsign,
            initial_message=prefill,
            refresh_callback=self._refresh_callback,
            parent=self,
        )
        dlg.exec_()

    def _on_js8_reply_clicked(self) -> None:
        """Reply to this message over RF via JS8 Direct Message. The clicked
        callsign is shown as a read-only reminder in the JS8 dialog (not forced
        into its roster-driven Target combo); the body is seeded with the
        original message text."""
        from js8_direct_message import JS8DirectMessageDialog
        original = self.message_text.replace("||", "\n")
        prefill = "\n\n----------\n" + original
        dlg = JS8DirectMessageDialog(
            tcp_pool=self._tcp_pool,
            connector_manager=self._connector_manager,
            refresh_callback=self._refresh_callback,
            parent=self,
        )
        dlg.set_reply_context(self.callsign, prefill)
        dlg.exec_()

    def _on_close_clicked(self) -> None:
        if self._deleted_any:
            self.accept()
        else:
            self.reject()

    def _fetch_message_details(self) -> None:
        if self._record_id is None:
            self._populate_message_labels("", None, "", None)
            return
        try:
            with sqlite3.connect(DB_PATH, timeout=10) as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT datetime, freq, target, source, global_id, msg_id FROM messages WHERE id = ?",
                    (self._record_id,)
                )
                row = cur.fetchone()
        except sqlite3.Error:
            row = None
        if row:
            self._msg_datetime = row[0] or ""
            self._msg_id = row[5] or self._msg_id
            self._populate_message_labels(row[0] or "", row[1], row[2] or "", row[3], row[4] or 0)
        else:
            self._populate_message_labels(self._msg_datetime, None, "", None)

    def _populate_message_labels(self, datetime_str: str, freq, target: str, source,
                                  global_id: int = 0) -> None:
        _k = "font-family:Roboto; font-weight:bold; font-size:13px;"
        _source_map = {1: "RF via JS8Call", 2: "Internet", 3: "Internet Only"}

        self.qrz_info.lbl_msg_posted.setText(
            f'<span style="{_k}">Posted:</span>  {datetime_str}' if datetime_str
            else f'<span style="{_k}">Posted:</span>'
        )
        self.qrz_info.lbl_msg_id.setText(
            f'<span style="{_k}">Message ID:</span>  {self._msg_id}' if self._msg_id
            else f'<span style="{_k}">Message ID:</span>'
        )
        target_text = target.strip() if target else ""
        self.qrz_info.lbl_msg_target.setText(
            f'<span style="{_k}">To:</span>  {target_text}' if target_text
            else f'<span style="{_k}">To:</span>'
        )
        self.qrz_info.lbl_msg_global_id.setText(
            f'<span style="{_k}">Global ID:</span>  {global_id}' if global_id
            else f'<span style="{_k}">Global ID:</span>'
        )
        self.qrz_info.lbl_msg_delivered.setText(f'<span style="{_k}">Delivered To:</span>')
        local_cs = _get_local_callsign()
        target_cs = target_text.lstrip("@").strip().upper() if target_text else ""
        if target_cs and local_cs and target_cs == local_cs.strip().upper():
            self.qrz_info.lbl_msg_delivered.setText(
                f'<span style="{_k}">Delivered To:</span>  0 CommStat users'
            )
        elif global_id and self._commsrvr_url and self.internet_available and local_cs:
            rc_token = self._reload_token
            self._rc_thread = _ReadCountThread(self._commsrvr_url, local_cs, global_id)
            self._rc_thread.count_ready.connect(
                lambda text, t=rc_token: self._on_read_count(text) if t == self._reload_token else None
            )
            self._rc_thread.start()
        try:
            freq_mhz = (float(freq) / 1_000_000) if freq else 0.0
        except (TypeError, ValueError):
            freq_mhz = 0.0
        self.qrz_info.lbl_msg_freq.setText(
            f'<span style="{_k}">Freq:</span>  {freq_mhz:.3f} MHz' if freq_mhz
            else f'<span style="{_k}">Freq:</span>'
        )
        if source is None:
            self.qrz_info.lbl_msg_source.setText(f'<span style="{_k}">Received via:</span>')
        else:
            try:
                source_text = _source_map.get(int(source), "Unknown")
            except (TypeError, ValueError):
                source_text = "Unknown"
            self.qrz_info.lbl_msg_source.setText(
                f'<span style="{_k}">Received via:</span>  {source_text}'
            )

    def _on_read_count(self, text: str) -> None:
        if not text:
            return
        # Response carries both values, e.g. "115,50 seconds ago"
        # (delivered count before the comma, last-seen after).
        count_str = text.split(",", 1)[0].strip()
        _k = "font-family:Roboto; font-weight:bold; font-size:13px;"
        self.qrz_info.lbl_msg_delivered.setText(
            f'<span style="{_k}">Delivered To:</span>  {count_str} CommStat users'
        )

    def _reload(self, record_id, callsign: str, message_text: str, msg_datetime: str,
                msg_id: str = "", freq=None, target: str = "", source=None, global_id: int = 0) -> None:
        self._reload_token += 1
        self.btn_newer.setEnabled(False)
        self.btn_older.setEnabled(False)
        if self._rc_thread is not None:
            try:
                self._rc_thread.count_ready.disconnect()
            except (TypeError, RuntimeError):
                pass
            self._rc_thread = None
        self._record_id = record_id
        self._msg_id = msg_id
        self.callsign = callsign
        self.message_text = message_text
        self._msg_datetime = msg_datetime
        self.setWindowTitle(f"Message — {callsign}")
        self.msg_text.setHtml(_text_to_html(message_text.replace("||", "\n"), self._data_bg))
        self._map_loaded = False
        self.map_view.setHtml("", QUrl("http://localhost/"))
        self.contact_memo_edit.blockSignals(True)
        self.contact_memo_edit.clear()
        self.contact_memo_edit.blockSignals(False)
        self.qrz_info.update_data({"call": callsign})
        self._populate_message_labels(msg_datetime, freq, target, source, global_id)
        if self._thread is not None:
            try:
                self._thread.result_ready.disconnect()
            except (TypeError, RuntimeError):
                pass
            self._thread = None
        self._start_qrz()
        self._update_nav_buttons()

    def _update_nav_buttons(self) -> None:
        has_newer = False
        has_older = False
        if self._record_id is not None:
            try:
                with sqlite3.connect(DB_PATH, timeout=10) as conn:
                    cur = conn.cursor()
                    cur.execute(
                        "SELECT 1 FROM messages WHERE id > ? LIMIT 1",
                        (self._record_id,)
                    )
                    has_newer = cur.fetchone() is not None
                    cur.execute(
                        "SELECT 1 FROM messages WHERE id < ? LIMIT 1",
                        (self._record_id,)
                    )
                    has_older = cur.fetchone() is not None
            except sqlite3.Error:
                pass
        self.btn_newer.setEnabled(has_newer)
        self.btn_older.setEnabled(has_older)

    def _on_newer(self) -> None:
        self._last_nav = "newer"
        self._navigate("newer")

    def _on_older(self) -> None:
        self._last_nav = "older"
        self._navigate("older")

    def _navigate(self, direction: str) -> None:
        # Ordered by the messages table's own primary key (id), not msg_id
        # (a 3-char hour+minute code recycled daily, not unique across
        # senders/days) or datetime (which can tie) — id is the only column
        # guaranteed to identify this exact row and order unambiguously.
        if self._record_id is None:
            self._update_nav_buttons()
            return
        try:
            with sqlite3.connect(DB_PATH, timeout=10) as conn:
                cur = conn.cursor()
                if direction == "newer":
                    cur.execute(
                        "SELECT id, msg_id, from_callsign, message, datetime, freq, target, source, global_id "
                        "FROM messages WHERE id > ? ORDER BY id ASC LIMIT 1",
                        (self._record_id,)
                    )
                else:
                    cur.execute(
                        "SELECT id, msg_id, from_callsign, message, datetime, freq, target, source, global_id "
                        "FROM messages WHERE id < ? ORDER BY id DESC LIMIT 1",
                        (self._record_id,)
                    )
                row = cur.fetchone()
        except sqlite3.Error as e:
            print(f"[MessageDetailDialog] Navigate error: {e}")
            return
        if not row:
            self._update_nav_buttons()
            return
        self._reload(row[0], row[2] or "", row[3] or "", row[4] or "",
                     msg_id=row[1] or "", freq=row[5], target=row[6] or "", source=row[7], global_id=row[8] or 0)

    def _on_delete(self) -> None:
        # Delete by the unique primary key — msg_id is not unique (see
        # _navigate), so keying the DELETE off it could remove unrelated
        # rows from other days/senders that happen to share the same code.
        deleted_id = self._record_id
        direction = self._last_nav
        try:
            with sqlite3.connect(DB_PATH, timeout=10) as conn:
                cur = conn.cursor()
                cur.execute("DELETE FROM messages WHERE id = ?", (deleted_id,))
                conn.commit()
                self._deleted_any = True
                next_row = None
                if deleted_id is not None:
                    if direction == "newer":
                        cur.execute(
                            "SELECT id, msg_id, from_callsign, message, datetime, freq, target, source, global_id "
                            "FROM messages WHERE id > ? ORDER BY id ASC LIMIT 1",
                            (deleted_id,)
                        )
                    else:
                        cur.execute(
                            "SELECT id, msg_id, from_callsign, message, datetime, freq, target, source, global_id "
                            "FROM messages WHERE id < ? ORDER BY id DESC LIMIT 1",
                            (deleted_id,)
                        )
                    next_row = cur.fetchone()
        except sqlite3.Error:
            self.accept()
            return
        self.record_deleted.emit()
        if not next_row:
            self.accept()
            return
        self._reload(next_row[0], next_row[2] or "", next_row[3] or "", next_row[4] or "",
                     msg_id=next_row[1] or "", freq=next_row[5], target=next_row[6] or "", source=next_row[7],
                     global_id=next_row[8] or 0)

    def _start_qrz(self) -> None:
        cached_fresh = get_qrz_cached(self.callsign)
        cached_any   = cached_fresh or get_qrz_cached(self.callsign, include_stale=True)
        is_active, username, password = load_qrz_config()

        if not username:
            found_str = "found" if cached_any else "NOT found"
            self.qrz_info.set_qrz_status(
                f"QRZ Subscription not configured — {self.callsign} {found_str} in local database"
            )
            if cached_any:
                self._on_qrz_result(cached_any)
            else:
                self.qrz_info.show_no_data_placeholder()
            return

        if not is_active:
            found_str = "found" if cached_any else "NOT found"
            self.qrz_info.set_qrz_status(
                f"QRZ Subscription not enabled — {self.callsign} {found_str} in local database"
            )
            if cached_any:
                self._on_qrz_result(cached_any)
            else:
                self.qrz_info.show_no_data_placeholder()
            return

        if cached_fresh:
            self._on_qrz_result(cached_fresh)
            return

        # No fresh cache — show stale cached data now (the map pin doesn't go
        # stale) while a live lookup refreshes the profile fields in the background.
        if cached_any:
            self._on_qrz_result(cached_any)

        token = self._reload_token
        self._thread = _QRZThread(self.callsign, username, password)
        self._thread.result_ready.connect(
            lambda result, t=token: self._on_qrz_result(result) if t == self._reload_token else None
        )
        self._thread.start()

    def _on_qrz_result(self, result) -> None:
        if not result:
            return
        self.qrz_info.update_data(result)
        if subscription_status() is False:
            self.qrz_info.set_qrz_status(
                f"QRZ XML subscription not found — showing local data for {self.callsign}"
            )
        self.contact_memo_edit.blockSignals(True)
        self.contact_memo_edit.setText(result.get("memo") or "")
        self.contact_memo_edit.blockSignals(False)
        d = _normalize_qrz(result)
        if not self._map_loaded:
            lat, lon = None, None
            if d["lat"] and d["lon"]:
                try:
                    lat, lon = float(d["lat"]), float(d["lon"])
                except (ValueError, TypeError):
                    pass
            if lat is None and d["grid"]:
                try:
                    coords = mh.to_location(d["grid"], center=True)
                    lat, lon = float(coords[0]), float(coords[1])
                except Exception:
                    pass
            if lat is not None and lon is not None:
                self._map_loaded = True
                self.map_view.setHtml(
                    _make_map_html(lat, lon, self.internet_available),
                    QUrl("http://localhost/")
                )
        # map already loaded — nothing more to do for message detail view


# ── Dialog: Internet Delivery Failure popup ──────────────────────────────

class InternetDeliveryFailureDialog(QDialog):
    """Styled error popup shown when commsrvr returns an ERR:: reply or times out."""

    def __init__(self, message: str, parent=None):
        super().__init__(parent)
        apply_standard_dialog_chrome(self, "Internet Delivery Failure")
        self.setModal(True)
        self.setFixedWidth(420)

        self.setStyleSheet("QDialog { background-color:#f5f5f5; }")

        main = QVBoxLayout(self)
        main.setContentsMargins(15, 15, 15, 15)
        main.setSpacing(10)

        title = QLabel("Internet Delivery Failure")
        title.setAlignment(Qt.AlignCenter)
        title.setFont(QFont("Roboto Slab", -1, QFont.Black))
        title.setStyleSheet(
            f"QLabel {{ background-color: {_PROG_BG}; color: {_PROG_FG};"
            " font-size: 16px; padding-top: 9px; padding-bottom: 9px; }"
        )
        main.addWidget(title)

        body = QLabel(message)
        body.setWordWrap(True)
        body.setAlignment(Qt.AlignCenter)
        body.setStyleSheet(
            "QLabel { color:#333333; font-family:Roboto; font-size:15px;"
            " font-weight:bold; padding: 12px 8px; }"
        )
        main.addWidget(body)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn = _btn("Close", _COL_CANCEL)
        btn.clicked.connect(self.accept)
        btn_row.addWidget(btn)
        main.addLayout(btn_row)


# ── Dialog: Delivery Confirmation popup (commsrvr ::DELIVERED::) ──────────

class DeliveryConfirmationDialog(QDialog):
    """Two-column popup shown when the commsrvr confirms a message delivery.

    Patterned after the QRZ Lookup dialog:
      - Title bar (program colors): "DELIVERY CONFIRMATION"
      - Column 1: QRZ data for the recipient (no QRZ profile URL)
      - Column 2: QRZ profile photo at fixed 120 px height; the dialog
                  expands horizontally to accommodate wider images
      - Below: read-only text box containing the delivered message
    """

    _PHOTO_H = 140
    _PHOTO_MAX_W = 440      # cap photo width — wide banners shrink in height to fit
    _PHOTO_DEFAULT_W = 460  # column-2 budget at default dialog width; grow only if exceeded

    # Subclasses override these to repurpose the dialog for related states.
    _WINDOW_TITLE       = "Delivery Confirmation"
    _BANNER_TEXT        = "Delivery Confirmation"
    _BANNER_BG_OVERRIDE: Optional[str] = None   # None → use program_background
    _BANNER_FG_OVERRIDE: Optional[str] = None
    _FOOTER_TEXT        = "This Message Was Delivered Successfully"
    _FOOTER_COLOR_OVERRIDE: Optional[str] = None  # None → module_foreground
    _FOOTER_AS_BANNER   = False                   # True → render footer with banner font/size (program colors by default)
    _FOOTER_BANNER_BG_OVERRIDE: Optional[str] = None  # When _FOOTER_AS_BANNER: override bg (use "transparent" for no fill)
    _FOOTER_BANNER_FG_OVERRIDE: Optional[str] = None  # When _FOOTER_AS_BANNER: override text color
    _MSG_BG_OVERRIDE: Optional[str] = None        # Message box background — None → #e9ecef
    _MSG_FG_OVERRIDE: Optional[str] = None        # Message box text color — None → #333333
    _MSG_BOLD           = False                   # When True, message text rendered bold

    def __init__(self, callsign: str, message: str,
                 module_background: str = "#f5f5f5",
                 module_foreground: str = "#333333",
                 program_background: str = "",
                 program_foreground: str = "",
                 parent=None):
        super().__init__(parent)
        apply_standard_dialog_chrome(self, self._WINDOW_TITLE)
        self.setModal(True)
        self.resize(510, 440)
        self.setMinimumSize(510, 440)
        self._callsign = (callsign or "").strip().upper()
        self._message = (message or "").replace("||", "\n")
        self._module_bg = module_background
        self._module_fg = module_foreground
        self._program_bg = program_background or _PROG_BG
        self._program_fg = program_foreground or _PROG_FG
        self._img_loader: Optional[_ImageLoader] = None
        self._gif_movie: Optional[QMovie] = None
        self._qrz_thread: Optional[_QRZThread] = None
        self._setup_ui()
        self._populate_qrz()

    def _setup_ui(self) -> None:
        msg_bg = self._MSG_BG_OVERRIDE or "#e9ecef"
        msg_fg = self._MSG_FG_OVERRIDE or "#333333"
        msg_weight = "font-weight:bold;" if self._MSG_BOLD else ""
        self.setStyleSheet(
            f"QDialog {{ background-color:{self._module_bg}; }}"
            f"QLabel {{ color:{self._module_fg}; background-color: transparent; font-size: 13px; }}"
            f"QPlainTextEdit {{ background-color:{msg_bg}; color:{msg_fg};"
            f" border:1px solid {COLOR_INPUT_BORDER}; border-radius:4px; padding:4px 8px;"
            f" font-family:'Kode Mono'; font-size:13px; {msg_weight} }}"
        )

        main = QVBoxLayout(self)
        main.setContentsMargins(15, 15, 15, 15)
        main.setSpacing(10)

        # Title bar (program colors, or override for special-state subclasses)
        banner_bg = self._BANNER_BG_OVERRIDE or self._program_bg
        banner_fg = self._BANNER_FG_OVERRIDE or self._program_fg
        title = QLabel(self._BANNER_TEXT)
        title.setAlignment(Qt.AlignCenter)
        title.setFont(QFont("Roboto Slab", -1, QFont.Black))
        title.setStyleSheet(
            f"QLabel {{ background-color: {banner_bg}; color: {banner_fg};"
            " font-size: 16px; padding-top: 9px; padding-bottom: 9px; }"
        )
        main.addWidget(title)

        # ── Two-column row ───────────────────────────────────────────────
        cols = QHBoxLayout()
        cols.setSpacing(60)

        # Column 1: QRZ data (without URL)
        self._grid = QGridLayout()
        self._grid.setSpacing(2)
        self._grid.setColumnStretch(0, 0)

        self.lbl_call    = QLabel(); self.lbl_call.setFont(_mono_font())
        self.lbl_name    = QLabel(); self.lbl_name.setFont(_mono_font())
        self.lbl_addr1   = QLabel(); self.lbl_addr1.setFont(_mono_font())
        self.lbl_addr2   = QLabel(); self.lbl_addr2.setFont(_mono_font())
        self.lbl_grid    = QLabel(); self.lbl_grid.setFont(_mono_font())
        self.lbl_county  = QLabel(); self.lbl_county.setFont(_mono_font())
        self.lbl_country = QLabel(); self.lbl_country.setFont(_mono_font())

        for row, w in enumerate((
            self.lbl_call, self.lbl_name, self.lbl_addr1, self.lbl_addr2,
            self.lbl_grid, self.lbl_county, self.lbl_country,
        )):
            self._grid.addWidget(w, row, 0)
        self._grid.setRowStretch(self._grid.rowCount(), 1)
        cols.addLayout(self._grid, 0)

        # Column 2: photo
        right = QVBoxLayout()
        right.setAlignment(Qt.AlignTop | Qt.AlignRight)
        right.setSpacing(0)
        self.lbl_image = QLabel()
        self.lbl_image.setAlignment(Qt.AlignTop | Qt.AlignRight)
        self.lbl_image.setFixedHeight(self._PHOTO_H)
        self.lbl_image.setStyleSheet("QLabel { border:none; padding:0px; }")
        right.addWidget(self.lbl_image)
        right.addStretch()
        cols.addLayout(right, 1)

        main.addLayout(cols)

        # ── Read-only message text box ───────────────────────────────────
        self.msg_view = QPlainTextEdit()
        self.msg_view.setFont(_mono_font())
        self.msg_view.setReadOnly(True)
        self.msg_view.setPlainText(self._message)
        from PyQt5.QtGui import QFontMetrics
        _fm = QFontMetrics(self.msg_view.font())
        self.msg_view.setFixedHeight(_fm.lineSpacing() * 4 + 14 + 40)
        main.addWidget(self.msg_view)

        confirm_lbl = QLabel(self._FOOTER_TEXT)
        confirm_lbl.setAlignment(Qt.AlignCenter)
        if self._FOOTER_AS_BANNER:
            footer_banner_bg = self._FOOTER_BANNER_BG_OVERRIDE or banner_bg
            footer_banner_fg = self._FOOTER_BANNER_FG_OVERRIDE or banner_fg
            confirm_lbl.setFont(QFont("Roboto Slab", -1, QFont.Black))
            confirm_lbl.setStyleSheet(
                f"QLabel {{ background-color: {footer_banner_bg}; color: {footer_banner_fg};"
                " font-size: 16px; padding-top: 9px; padding-bottom: 9px; }"
            )
        else:
            footer_color = self._FOOTER_COLOR_OVERRIDE or self._module_fg
            confirm_lbl.setFont(QFont("Roboto", -1, QFont.Bold))
            confirm_lbl.setStyleSheet(
                f"QLabel {{ color:{footer_color}; background-color: transparent;"
                " font-family:Roboto; font-size:13px; font-weight:bold; padding-top:6px; }"
            )
        main.addWidget(confirm_lbl)
        main.addStretch()

        # ── Close button ─────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch()
        self.btn_close = _btn("Close", _COL_CANCEL)
        self.btn_close.clicked.connect(self.accept)
        btn_row.addWidget(self.btn_close)
        main.addLayout(btn_row)

    # ── QRZ population ────────────────────────────────────────────────────

    def _populate_qrz(self) -> None:
        """Show cached QRZ data immediately; fall back to API lookup when missing."""
        is_active, username, password = load_qrz_config()
        cached = (
            get_qrz_cached(self._callsign)
            or get_qrz_cached(self._callsign, include_stale=True)
        )
        if cached:
            self._update_data(cached)
            return

        self._show_placeholder()
        if is_active and username:
            self._qrz_thread = _QRZThread(self._callsign, username, password)
            self._qrz_thread.result_ready.connect(self._on_qrz_result)
            self._qrz_thread.start()

    def _on_qrz_result(self, result) -> None:
        if result:
            self._update_data(result)

    def _update_data(self, data: dict) -> None:
        d = _normalize_qrz(data)
        _k = "font-family:Roboto; font-weight:bold; font-size:13px;"

        self.lbl_call.setText(f"<span style='{_k}'>Callsign:</span> {d['call']}")
        self.lbl_name.setText(f"<b>{d['name']}</b>" if d["name"] else "")
        self.lbl_addr1.setText(d["addr1"])
        city_state = ", ".join(x for x in (d["addr2"], d["state"]) if x)
        if d["zip"]:
            city_state = (city_state + " " + d["zip"]).strip()
        self.lbl_addr2.setText(city_state)

        self.lbl_grid.setText(
            f'<span style="{_k}">Grid:</span> {d["grid"]}' if d["grid"] else ""
        )
        self.lbl_county.setText(
            f'<span style="{_k}">County:</span> {d["county"]}' if d["county"] else ""
        )
        self.lbl_country.setText(
            f'<span style="{_k}">Country:</span> {d["country"]}' if d["country"] else ""
        )

        if d["image"]:
            self._img_loader = _ImageLoader(
                d["image"], max_size=(self._PHOTO_MAX_W, self._PHOTO_H)
            )
            self._img_loader.image_loaded.connect(self._on_image_loaded)
            self._img_loader.gif_loaded.connect(self._on_gif_loaded)
            self._img_loader.start()
        else:
            self._load_default_image()

    def _show_placeholder(self) -> None:
        _k = "font-family:Roboto; font-weight:bold; font-size:13px;"
        self.lbl_call.setText(f"<span style='{_k}'>Callsign:</span> {self._callsign}")
        self.lbl_grid.setText(f"<span style='{_k}'>Grid:</span>")
        self.lbl_county.setText(f"<span style='{_k}'>County:</span>")
        self.lbl_country.setText(f"<span style='{_k}'>Country:</span>")
        self._load_default_image()

    # ── Image handling ────────────────────────────────────────────────────

    def _load_default_image(self) -> None:
        px = QPixmap("00-qrz-default.png")
        if not px.isNull():
            scaled = px.scaled(
                self._PHOTO_MAX_W, self._PHOTO_H,
                Qt.KeepAspectRatio, Qt.SmoothTransformation,
            )
            self.lbl_image.setPixmap(scaled)
        else:
            self.lbl_image.clear()

    def _on_image_loaded(self, px: QPixmap) -> None:
        self.lbl_image.setPixmap(px)
        # Defer the dialog-grow check so the layout has resolved column widths.
        from PyQt5.QtCore import QTimer
        QTimer.singleShot(0, lambda w=px.width(): self._adjust_for_image_width(w))

    def _on_gif_loaded(self, data: bytes) -> None:
        px_probe = QPixmap()
        px_probe.loadFromData(data)
        if not px_probe.isNull():
            scaled_size = px_probe.scaled(
                self._PHOTO_MAX_W, self._PHOTO_H,
                Qt.KeepAspectRatio, Qt.SmoothTransformation,
            ).size()
        else:
            scaled_size = None
        buf = QBuffer()
        buf.setData(QByteArray(data))
        buf.open(QBuffer.ReadOnly)
        self._gif_movie = QMovie()
        self._gif_movie.setDevice(buf)
        self._gif_movie._buf = buf
        if scaled_size is not None:
            self._gif_movie.setScaledSize(scaled_size)
        self.lbl_image.setMovie(self._gif_movie)
        self._gif_movie.start()
        if scaled_size is not None:
            from PyQt5.QtCore import QTimer
            w = scaled_size.width()
            QTimer.singleShot(0, lambda: self._adjust_for_image_width(w))

    def _adjust_for_image_width(self, img_width: int) -> None:
        """Grow the dialog horizontally when the photo is wider than column 2.

        Column 2's actual usable width depends on the resolved width of the
        text grid in column 1; we measure it from the live layout instead of
        relying on a fixed budget.
        """
        avail = self.lbl_image.width()
        if avail > 0 and img_width > avail:
            deficit = img_width - avail
            self.resize(self.width() + deficit + 4, self.height())


# ── Dialog: Message Expired popup (commsrvr ::EXPIRED::) ──────────────────

class MessageExpiredDialog(DeliveryConfirmationDialog):
    """Variant of DeliveryConfirmationDialog shown when a message's delivery
    window expires before the recipient retrieves it.

    Uses program-colored banners top and bottom — the wording itself ("This
    Message Was Not Delivered" / "Recipient Did not retrieve it") signals
    non-delivery and keeps the popup visually distinct from the delivered
    dialog (which has a small text footer, not a banner).
    """

    _WINDOW_TITLE     = "Message Expired"
    _BANNER_TEXT      = "This Message Was Not Delivered"
    _FOOTER_TEXT      = "Recipient Did Not Retrieve It"
    _FOOTER_AS_BANNER = True


# ── Dialog: New Message notification popup ─────────────────────────────────

class NewMessagePopupDialog(QDialog):
    """Notification popup shown when a message arrives addressed to one of
    our own callsigns. Sized to the 00-message.png background image, with
    a banner reading "{CALLSIGN} Sent You a Message" across the middle."""

    _BG_IMAGE = "00-message.png"
    _FALLBACK_SIZE = (460, 190)
    Opened = 2  # QDialog.exec_() result code when "Open" is clicked

    def __init__(self, callsign: str, parent=None):
        super().__init__(parent)
        apply_standard_dialog_chrome(self, "New Message")
        self.setModal(True)
        self._callsign = (callsign or "").strip().upper()
        self._setup_ui()

    def _setup_ui(self) -> None:
        bg_pixmap = QPixmap(self._BG_IMAGE)
        if bg_pixmap.isNull():
            w, h = self._FALLBACK_SIZE
        else:
            w, h = bg_pixmap.width(), bg_pixmap.height()
        self.setFixedSize(w, h)

        bg_label = QLabel(self)
        bg_label.setGeometry(0, 0, w, h)
        bg_label.setStyleSheet("QLabel { background: transparent; border: none; }")
        if not bg_pixmap.isNull():
            bg_label.setPixmap(bg_pixmap)
        bg_label.setScaledContents(True)

        banner_h = 50
        banner = QLabel(f"{self._callsign} Sent You a Message", self)
        banner.setAlignment(Qt.AlignCenter)
        banner.setFont(QFont("Roboto Slab", -1, QFont.Black))
        banner.setStyleSheet(
            "QLabel { background-color: rgba(0, 0, 0, 170); color: #FFFFFF;"
            " font-size: 18px; }"
        )
        banner_w = w // 2
        banner.setGeometry((w - banner_w) // 2, (h - banner_h) // 2, banner_w, banner_h)
        banner.raise_()

        self.btn_close = _btn("Close", _COL_CANCEL)
        self.btn_close.setParent(self)
        self.btn_close.clicked.connect(self.accept)
        self.btn_close.adjustSize()
        self.btn_close.move(w - self.btn_close.width() - 12, h - self.btn_close.height() - 12)
        self.btn_close.raise_()

        self.btn_open = _btn("Open Message", COLOR_BTN_GREEN)
        self.btn_open.setParent(self)
        self.btn_open.clicked.connect(lambda: self.done(self.Opened))
        self.btn_open.adjustSize()
        self.btn_open.move(12, h - self.btn_open.height() - 12)
        self.btn_open.raise_()
