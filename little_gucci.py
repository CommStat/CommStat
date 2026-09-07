# Copyright (c) 2025, 2026 Manuel Ochoa
# This file is part of CommStat.
# Licensed under the GNU General Public License v3.0.
# AI Assistance: Claude (Anthropic), ChatGPT (OpenAI)
#
# In the beginning was the word.
# and the word was with God.
# and the word was God.
# And the Word was made flesh, and dwelt among us, and we beheld his glory.
#
# The truth is not hard to find,
# it's hard to embrace.


"""
A PyQt5 application for monitoring JS8Call communications,
displaying status reports, messages, and live data feeds.
"""

import sys
import os
import io
import re
import csv
import base64
import json
import faulthandler
import socket
import sqlite3
import threading
import subprocess
import urllib.request
import urllib.parse
import ssl

# Print a C-level traceback on segfaults so silent Qt crashes (especially on
# Linux) leave something actionable in the terminal instead of vanishing.
faulthandler.enable()


def _enable_windows_vt_mode() -> None:
    # Classic Windows conhost ignores ANSI escapes by default, so ConsoleColors
    # leak as raw "←[92m...←[0m" sequences. Enabling VT processing on the
    # console stdout/stderr handles makes the escapes render as color.
    if sys.platform != "win32":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        ENABLE_VT = 0x0004
        for std_handle in (-11, -12):  # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
            handle = kernel32.GetStdHandle(std_handle)
            if not handle or handle == ctypes.c_void_p(-1).value:
                continue
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | ENABLE_VT)
    except Exception:
        pass


_enable_windows_vt_mode()
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from configparser import ConfigParser
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple, Set

import folium
import maidenhead as mh

# Optional: PyEnchant for smart title case (acronym detection)
try:
    import enchant
    ENCHANT_AVAILABLE = True
except ImportError:
    ENCHANT_AVAILABLE = False

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtGui import QColor, QDesktopServices
from PyQt5.QtWidgets import QTableWidgetItem
from PyQt5.QtCore import QBuffer, QIODevice, QTimer, QDateTime, Qt, QUrl
from PyQt5.QtWidgets import qApp
from PyQt5.QtWebEngineWidgets import QWebEngineView, QWebEnginePage, QWebEngineProfile, QWebEngineSettings
from PyQt5.QtWebEngineCore import QWebEngineUrlSchemeHandler, QWebEngineUrlScheme, QWebEngineUrlRequestJob
from PyQt5.QtMultimedia import QSoundEffect
from connector_manager import ConnectorManager
from js8_tcp_client import TCPConnectionPool
from id_utils import generate_time_based_id
from constants import *
import netguard


# =============================================================================
# Constants
# =============================================================================

# Commsrvr server for remote announcements and slideshow images
# This allows the developer to push messages/images to all CommStat users
_COMMSRVR = base64.b64decode("aHR0cHM6Ly9jb21tc3RhdC5hcHA=").decode()
_PING = _COMMSRVR + "/heartbeat-808585.php"
_DELIVERY_CONFIRM = _COMMSRVR + "/delivery-confirmation-808585.php"

# Default hyperlink blue - used to style the StatRep table's clickable
# From/ID columns like links (color + underline, always on, not just hover).
_LINK_COLOR = "#0000CC"

# Backoff interval after repeated commsrvr heartbeat failures.
HEARTBEAT_BACKOFF_MS = 30 * 60 * 1000  # 30 minutes

# Pixels of mouse-wheel scroll required to advance one Leaflet zoom level
# on the bottom-left map. Leaflet default is 60; raised to dampen sensitivity.
# Paired with zoomSnap=0.25 in the folium.Map() call so fractional zoom is
# allowed — otherwise every wheel tick rounds up to a full zoom level.
MAP_WHEEL_PX_PER_ZOOM = 360

# Bottom-left map pin sizing. Pin COLOR conveys status (green/orange/red);
# pin RADIUS conveys the StatRep's scope (how local the report is). Values are
# CircleMarker pixel radii, so on-screen diameter is 2x these numbers.
# SCOPE_RADIUS / SCOPE_RADIUS_DEFAULT come from constants.py (imported above
# via `from constants import *`) and understand both the old and future scope
# label sets.

# Contacts capture (Direct Message Part 1):
#  - The sender of the RX.DIRECTED (from_call) is the RELAY — the station we
#    directly heard. The callsign parsed out of the body is the TARGET —
#    the station the relay reported hearing.
#  - _CONTACTS_PATTERN matches '<TARGET> <KEYWORD(S)> <+/-SNR>' at the start
#    of an RX.DIRECTED body. We keep only SNR-style replies (see allow-list)
#    so time-coordination chatter like 'YES +13 (NOW)' does not pollute the roster.
#  - _CONTACTS_BASE_CS_PATTERN validates a base callsign AFTER stripping any
#    '/' suffix (KO4BIA/P -> KO4BIA, K2DHS/10 -> K2DHS).
_CONTACTS_PATTERN = re.compile(
    r"^([A-Z0-9/]{3,12})\s+((?:[A-Z]+\s+){0,2}[A-Z]+)\s+([+\-]\d{1,3})\b",
    re.IGNORECASE,
)
_CONTACTS_ALLOWED_KEYWORDS = {"SNR", "HEARTBEAT SNR"}
_CONTACTS_BASE_CS_PATTERN = re.compile(r"^[A-Z0-9]{3,8}$")

# Hearing-report path (third contacts-capture path):
#  - body matches '<ADDRESSEE_CS> HEARING <CS1> <CS2> ...'
#  - The sender (from_call) is the relay; each listed callsign is a target
#    the relay reports hearing. The leading addressee is who the report was
#    directed at and is discarded. SNR is unknown for both ends of the link,
#    so a fixed placeholder is written into both SNR columns.
_CONTACTS_HEARING_PATTERN = re.compile(
    r"^[@A-Z0-9/]{3,12}\s+HEARING\s+(.+)$",
    re.IGNORECASE,
)
_CONTACTS_HEARING_DEFAULT_SNR = -99

# Group target that is always accepted for alerts and messages, whether or not
# the operator has it in their groups table — it's the network-wide channel
# every CommStat station is expected to receive. Videos are NOT exempt; they
# keep their own membership + "Save all Videos" rule.
_ALWAYS_SAVE_GROUP = "COMMSTAT"

# The map-pane view modes that show an actual region map. New alerts/videos are
# only allowed to take the pane while one of these is current — see
# MainWindow._map_pane_is_free().
_MAP_REGION_MODES = ("us", "eu", "mideast", "seasia", "world")

# Solar/radio image dialogs: (menu_label, image_url, link_html, loading_text, error_prefix)
SOLAR_IMAGE_DIALOGS = [
    ("Band Conditions", "https://www.hamqsl.com/solar101pic.php",
     '<a href="https://www.hamqsl.com/solar.html">Solar-Terrestrial Data provided by N0NBH</a>',
     "Loading band conditions...", "Failed to load band conditions"),
    ("Solar Flux", "https://www.hamqsl.com/marston.php",
     '<a href="https://www.hamqsl.com/solar.html">Solar-Terrestrial Data provided by N0NBH</a>',
     "Loading solar flux data...", "Failed to load solar flux data"),
    ("World Map", "https://www.hamqsl.com/solarmuf.php",
     '<a href="https://www.hamqsl.com/solar.html">View more at hamqsl.com</a>',
     "Loading solar conditions...", "Failed to load solar data"),
]

# qrz columns left out of Tools > Export CSVs: internal bookkeeping or not
# useful outside the app.
QRZ_EXPORT_EXCLUDED_COLUMNS = {
    "active", "effdate", "expdate", "image", "areacode", "memo", "moddate", "grid_override",
}

# Live weather map links opened in the user's browser from the Tools menu.
WEATHER_MAP_LINKS = [
    ("Nat'l Weather Service", "https://www.weather.gov/"),
    ("Ventusky", "https://www.ventusky.com/"),
    ("Windy.com", "https://www.windy.com/"),
    ("Zoom Earth", "https://zoom.earth/"),
]


def hz_to_mhz(freq_hz: float, offset: float = 0) -> float:
    """Convert frequency in Hz to MHz, optionally subtracting an offset first."""
    return (freq_hz - offset) / 1000000 if freq_hz else 0.0


def check_internet() -> bool:
    """
    Check internet connectivity by attempting to connect to DNS servers.

    Returns:
        True if internet is available, False otherwise.
    """
    # Off-Grid Mode forced by the user: don't even probe. This is the single
    # gate that everything else (RSS, map tiles, commsrvr heartbeat/auto-
    # update, INTERNET-rig option) derives from via self._internet_available.
    if not netguard.get_user_enabled():
        return False

    # Why: this runs synchronously from __init__ before the UI paints, so a
    # slow-DNS day blocked startup for up to 9s. 1s per probe keeps the
    # worst case at 3s while still tolerating typical network latency.
    for host, port in (("www.google.com", 80), ("www.cloudflare.com", 443), ("8.8.8.8", 443)):
        try:
            sock = socket.create_connection((host, port), timeout=1)
            sock.close()
            netguard.set_reachable(True)
            return True
        except (socket.timeout, socket.error):
            continue
    netguard.set_reachable(False)
    return False


def expand_abbreviations(text: str, abbreviations: Dict[str, str] = None) -> str:
    """Expand common JS8Call abbreviations to full words.

    Args:
        text: Input text with possible abbreviations.
        abbreviations: Dictionary mapping abbreviations to expansions.
                      If None, no expansion is performed.

    Returns:
        Text with abbreviations expanded.
    """
    if not text or not abbreviations:
        return text

    words = text.split()
    result = []

    for word in words:
        # Preserve punctuation
        prefix = ""
        suffix = ""
        clean_word = word

        # Extract leading punctuation
        while clean_word and not clean_word[0].isalnum():
            prefix += clean_word[0]
            clean_word = clean_word[1:]

        # Extract trailing punctuation
        while clean_word and not clean_word[-1].isalnum():
            suffix = clean_word[-1] + suffix
            clean_word = clean_word[:-1]

        # Check for abbreviation (case-insensitive)
        upper_word = clean_word.upper()
        if upper_word in abbreviations:
            expanded = abbreviations[upper_word]
            result.append(prefix + expanded + suffix)
        else:
            result.append(word)

    return ' '.join(result)


def smart_title_case(text: str, abbreviations: Dict[str, str] = None, apply_normalization: bool = True) -> str:
    """Convert text to smart title case with acronym detection.

    First expands JS8Call abbreviations, then applies title case rules:
    - Words 1-2 chars stay lowercase (e.g., "a", "of", "to"), EXCEPT at sentence start
    - Dictionary words get title case
    - Non-dictionary words become ALL CAPS (treated as acronyms)
    - First word of each sentence is always capitalized
    - All-caps words from abbreviation expansion are preserved (e.g., SC, NY, TX)

    Args:
        text: Input text to format.
        abbreviations: Dictionary mapping abbreviations to expansions.
        apply_normalization: If False, returns text unchanged. Defaults to True.

    Returns:
        Formatted text with abbreviations expanded and smart title case (if enabled).
    """
    # If normalization is disabled, return text as-is
    if not apply_normalization:
        return text

    # First expand abbreviations
    text = expand_abbreviations(text, abbreviations)
    if not text:
        return text

    # Identify words that should be preserved as all-caps
    # These are words that:
    # 1. Are in the abbreviations dictionary
    # 2. Expand to an all-caps form (e.g., state codes: TX, NY, SC)
    preserved_caps = set()
    if abbreviations:
        for word in text.split():
            clean = ''.join(c for c in word if c.isalnum())
            upper_clean = clean.upper()
            # Check if this word is in abbreviations and expands to all-caps
            if upper_clean in abbreviations:
                expansion = abbreviations[upper_clean]
                # If expansion is all uppercase, preserve it
                if expansion.isupper():
                    preserved_caps.add(expansion.upper())

    # Initialize dictionary if available
    dictionary = None
    if ENCHANT_AVAILABLE:
        try:
            dictionary = enchant.Dict("en_US")
        except Exception:
            pass

    words = text.lower().split()
    result = []

    for i, word in enumerate(words):
        # Strip punctuation for checking, preserve for output
        clean_word = ''.join(c for c in word if c.isalnum())

        # Skip empty words (punctuation only)
        if not clean_word:
            result.append(word)
            continue

        # Check if this word should be preserved as all-caps
        if clean_word.upper() in preserved_caps:
            # Reconstruct word with original punctuation but uppercase letters
            rebuilt = ""
            for c in word:
                rebuilt += c.upper() if c.isalnum() else c
            result.append(rebuilt)
            continue

        # Check if this is the start of a sentence
        is_sentence_start = (i == 0)  # First word
        if i > 0:
            # Check if previous word ends with sentence-ending punctuation
            prev_word = result[-1]
            if prev_word.rstrip().endswith(('.', '!', '?')):
                is_sentence_start = True

        if is_sentence_start:
            # Always capitalize first letter of sentence, even if 1-2 chars
            if dictionary and not dictionary.check(clean_word) and not dictionary.check(clean_word.capitalize()):
                # Not in dictionary - treat as acronym
                result.append(word.upper())
            else:
                # Regular word or short word at sentence start - capitalize
                result.append(word.capitalize())
        elif len(clean_word) <= 2:
            # Short words stay lowercase (mid-sentence)
            result.append(word)
        elif dictionary and not dictionary.check(clean_word) and not dictionary.check(clean_word.capitalize()):
            # Not in dictionary - treat as acronym
            result.append(word.upper())
        else:
            # Regular word - title case
            result.append(word.capitalize())

    return ' '.join(result)


# =============================================================================
# STATREP Parsing Helper Functions
# =============================================================================

def strip_duplicate_callsign(value: str, from_call: str) -> str:
    """
    Remove duplicate callsign from message value if present.

    JS8Call bug causes: "W8APP: W8APP: @GROUP ..." instead of "W8APP: @GROUP ..."
    Must handle both formats since most users still have the buggy version.

    Args:
        value: Message text from JS8Call TCP stream
        from_call: Sender callsign from JSON params (may include /P suffix)

    Returns:
        Cleaned message text with duplicate removed
    """
    # Extract base callsign (remove /P, /M suffixes)
    base_call = from_call.split("/")[0] if from_call else ""
    if not base_call:
        return value

    # Pattern: "CALLSIGN: CALLSIGN: remainder"
    # Use word boundary to avoid partial matches
    pattern = rf'\b{re.escape(base_call)}\s*:\s*{re.escape(base_call)}\s*:\s*'
    if re.match(pattern, value, re.IGNORECASE):
        # Remove first occurrence, keep second
        value = re.sub(pattern, f'{base_call}: ', value, count=1, flags=re.IGNORECASE)

    return value


def sanitize_ascii(text: str) -> str:
    """
    Remove non-ASCII characters (keep only printable ASCII 32-126).

    Args:
        text: Input text

    Returns:
        Sanitized text with only printable ASCII characters
    """
    return re.sub(r'[^ -~]+', '', text).strip()


def parse_message_datetime(utc: str) -> tuple:
    """
    Parse UTC timestamp and generate time-based ID.

    Args:
        utc: UTC timestamp string (format: "YYYY-MM-DD   HH:MM:SS" or "YYYY-MM-DD HH:MM:SS")

    Returns:
        (date_only_str, time_based_id)
    """
    dt_str = utc.replace("   ", " ").strip()
    dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    date_only = utc.split()[0] if utc else ""
    msg_id = generate_time_based_id(dt)

    return (date_only, msg_id)


def expand_plus_shorthand(srcode: str) -> str:
    """Expand '+' shorthand to '111111111111' (all green status)."""
    return "111111111111" if srcode == "+" else srcode


def calculate_f304_status(digits: str, grid_found: bool) -> str:
    """
    Calculate map status for F!304/F!301 based on digit score.

    Rules:
    - Count 4 as 1, all others as face value
    - Score > 12: Red (3)
    - Score > 10: Yellow (2)
    - Grid found: Green (1)
    - Else: Unknown (4)
    """
    digit_score = sum(1 if int(d) == 4 else int(d) for d in digits)

    if digit_score > 12:
        return "3"
    elif digit_score > 10:
        return "2"
    elif grid_found:
        return "1"
    else:
        return "4"


def map_f304_digits_to_fields(digits: str) -> dict:
    """
    Map 8-digit F!304 format to database fields.

    Digit positions:
    [0]=Landline, [1]=Telecom, [2]=AM/FM/TV, [3]=Internet,
    [4]=Water, [5]=Power, [6]=Nat Gas, [7]=NOAA

    Returns dict with: power, water, telecom, internet, and comment parts
    """
    # Direct mappings
    commpw = digits[5]  # Commercial power
    pubwtr = digits[4]  # Public water
    net = digits[3]     # Internet

    # Telecom mapping (position 1)
    ota_digit = int(digits[1])
    if ota_digit == 1:
        ota = "1"
    elif ota_digit in [2, 3]:
        ota = "2"
    elif ota_digit == 4:
        ota = "3"
    else:
        ota = "4"

    # Comment additions (fields not in 12-digit format)
    YESNO_MAP = {1: "Yes", 2: "Limited", 3: "No", 4: "Unknown"}
    landline = YESNO_MAP.get(int(digits[0]), "Unknown")
    amfmtv = YESNO_MAP.get(int(digits[2]), "Unknown")
    natgas = YESNO_MAP.get(int(digits[6]), "Unknown")
    noaa = YESNO_MAP.get(int(digits[7]), "Unknown")

    return {
        'power': commpw,
        'water': pubwtr,
        'telecom': ota,
        'internet': net,
        'comment_parts': [
            f"Landline = {landline}",
            f"AM/FM/TV = {amfmtv}",
            f"Nat Gas = {natgas}",
            f"NOAA = {noaa}"
        ]
    }


def map_f301_digits_to_fields(digits: str) -> dict:
    """
    Map 9-digit F!301 format to database fields.

    First digit = scope (1-5), remaining 8 follow F!304 rules.

    Returns dict with: scope, power, water, telecom, internet, and comment parts
    """
    # Scope mapping (DB-persisted text, not the UI display text)
    scope = scope_db_text_for_code(digits[0])

    # Remaining 8 digits follow F!304 format
    f304_fields = map_f304_digits_to_fields(digits[1:])

    # F!301 doesn't include Landline in comments
    f304_fields['comment_parts'] = f304_fields['comment_parts'][1:]  # Skip Landline
    f304_fields['scope'] = scope

    return f304_fields


def _strip_cs_suffix(callsign: str) -> str:
    """Return the base callsign: everything before the first '/', uppercased."""
    if not callsign:
        return ""
    return callsign.split("/", 1)[0].upper()


def base_callsign(callsign: str) -> str:
    """
    Return the longest '/'-delimited token, uppercased — used for duplicate matching.

    N0DDK/P -> N0DDK, KH6/N0DDK -> N0DDK. The stored from_callsign keeps its suffix
    (that's what the operator transmitted), so duplicate detection has to compare the
    base form: the same record can arrive as N0DDK/P over RF and N0DDK from the
    commsrvr, and both must resolve to one row.

    Matches the convention already used by qrz_client.QRZClient.lookup. Kept separate
    from _strip_cs_suffix(), which the contacts-capture path relies on for its
    everything-before-the-first-slash behavior.
    """
    if not callsign:
        return ""
    return max(callsign.upper().split("/"), key=len)


def parse_contacts_observation(value: str) -> Optional[Tuple[str, int]]:
    """
    Parse an RX.DIRECTED body for a target-SNR observation reported by the relay.

    Matches '<TARGET_CS> SNR|HEARTBEAT SNR <SIGNED_NUMBER>' at the start of the
    body. The target callsign is suffix-stripped and validated against
    _CONTACTS_BASE_CS_PATTERN; the keyword must be in _CONTACTS_ALLOWED_KEYWORDS.

    Args:
        value: Raw message body (params['value']).

    Returns:
        (target_base_cs, target_snr_int) if the body is a valid SNR observation,
        otherwise None.
    """
    if not value:
        return None
    match = _CONTACTS_PATTERN.match(value.strip())
    if not match:
        return None
    raw_cs, raw_kw, raw_snr = match.group(1), match.group(2), match.group(3)
    keyword = " ".join(raw_kw.upper().split())
    if keyword not in _CONTACTS_ALLOWED_KEYWORDS:
        return None
    base_cs = _strip_cs_suffix(raw_cs)
    if not _CONTACTS_BASE_CS_PATTERN.match(base_cs):
        return None
    try:
        return base_cs, int(raw_snr)
    except (TypeError, ValueError):
        return None


def parse_hearing_observation(value: str) -> Optional[List[str]]:
    """
    Parse a directed-message body for a HEARING report.

    Matches '<ADDRESSEE_CS> HEARING <CS1> <CS2> ...' at the start of the body.
    The addressee is discarded — it's just who the report was directed at.
    Each listed callsign is suffix-stripped, validated against
    _CONTACTS_BASE_CS_PATTERN, and deduplicated in order so JS8 decode garbage
    (e.g., '……M4TWA') is dropped silently.

    Returns the ordered list of validated heard callsigns, or None if the
    body does not match the pattern or yields no valid callsigns.
    """
    if not value:
        return None
    match = _CONTACTS_HEARING_PATTERN.match(value.strip())
    if not match:
        return None
    tail = match.group(1).rstrip(" \t♢").strip()
    if not tail:
        return None
    seen: Set[str] = set()
    heard: List[str] = []
    for token in tail.split():
        base = _strip_cs_suffix(token)
        if not _CONTACTS_BASE_CS_PATTERN.match(base):
            continue
        if base in seen:
            continue
        seen.add(base)
        heard.append(base)
    return heard if heard else None


# =============================================================================
# Helper Functionsn
# =============================================================================

def create_insecure_ssl_context():
    """Create SSL context that bypasses certificate verification.

    Some ham radio sites and RSS feeds have certificate issues,
    so we need to disable verification for those requests.
    """
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    return ssl_context


def create_verified_ssl_context():
    """Create a cert-VERIFYING SSL context for the commsrvr heartbeat channel.

    Unlike RSS (create_insecure_ssl_context), the heartbeat reply can drive
    _handle_db_update (runs server-supplied SQL) and _handle_program_update
    (downloads + installs a zip), so verification must stay ON to prevent MITM.

    We prefer certifi's bundle when available so a stale OS/Python trust store
    isn't a silent failure point (commstat.app uses a Let's Encrypt cert whose
    ISRG root may be missing on un-updated machines).
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


# =============================================================================
# Tile Scheme Handler for Map
# =============================================================================

class TileSchemeHandler(QWebEngineUrlSchemeHandler):
    """Serves map tiles from tilesPNG2 via the tiles:// custom URL scheme."""

    # Why: cap the in-flight buffer set so a missed `job.destroyed` signal
    # (e.g. when the renderer crashes mid-load) can't grow the set without
    # bound. 256 covers a full screen of tiles at our largest zoom with
    # headroom. Oldest entries are evicted FIFO.
    _MAX_LIVE_BUFS = 256

    def __init__(self, tile_dir: str, parent=None):
        super().__init__(parent)
        self._tile_dir = tile_dir
        # deque-as-FIFO so we can bound it; set lookups not needed.
        from collections import deque
        self._live_bufs: deque = deque(maxlen=self._MAX_LIVE_BUFS)

    def requestStarted(self, job: QWebEngineUrlRequestJob) -> None:
        path = job.requestUrl().path().lstrip('/')
        tile_path = os.path.join(self._tile_dir, path)
        if os.path.exists(tile_path):
            try:
                with open(tile_path, 'rb') as f:
                    data = f.read()
                buf = QBuffer()
                buf.setData(data)
                buf.open(QIODevice.ReadOnly)
                # Hold a reference until the job is destroyed OR the buffer is
                # closed, whichever fires first. The deque's maxlen evicts the
                # oldest reference if neither signal arrives.
                self._live_bufs.append(buf)

                def _drop(*_):
                    try:
                        self._live_bufs.remove(buf)
                    except ValueError:
                        pass

                job.destroyed.connect(_drop)
                buf.aboutToClose.connect(_drop)
                job.reply(b'image/png', buf)
            except Exception:
                job.fail(QWebEngineUrlRequestJob.RequestFailed)
        else:
            job.fail(QWebEngineUrlRequestJob.UrlNotFound)


# =============================================================================
# Clickable Label for Slideshow
# =============================================================================

class ClickableLabel(QtWidgets.QLabel):
    """A QLabel that emits a clicked signal when clicked."""
    clicked = QtCore.pyqtSignal()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class UpperCaseLineEdit(QtWidgets.QLineEdit):
    """QLineEdit that auto-uppercases typed and pasted text.

    Uppercasing happens in keyPressEvent (typing) and insertFromMimeData
    (paste / drag-drop) — the widget-level hooks that run before any
    text-modified signal is emitted. Not a QValidator and not a
    textChanged slot: rewriting text from inside a signal slot re-enters
    Qt's dispatch loop, which corrupts the C++ iterator on Linux/Qt5.
    """

    def keyPressEvent(self, event):
        text = event.text()
        if text and text != text.upper():
            self.insert(text.upper())
            event.accept()
            return
        super().keyPressEvent(event)

    def insertFromMimeData(self, source):
        if source is not None and source.hasText():
            mime = QtCore.QMimeData()
            mime.setText(source.text().upper())
            super().insertFromMimeData(mime)


class _FilterHeader(QtWidgets.QHeaderView):
    """Horizontal table header with an optional per-column filter row pinned
    directly beneath the column labels.

    The filter boxes used to be cell widgets in row 0 of the table, so they
    scrolled out of view with the data. Here they're children of the header
    instead: while the filter is shown the header grows by strip_h, the
    labels keep painting in the top label_h band, and the strip below is
    painted like a table row (data background + gridlines) with a QLineEdit
    laid over each filterable column. Because the header height is what
    QTableView uses to place the viewport and size the scrollbars, Qt's own
    geometry bookkeeping stays consistent and the boxes never scroll away.
    """

    def __init__(self, parent: QtWidgets.QWidget, label_h: int, strip_h: int,
                 strip_bg: str, grid_color: str) -> None:
        super().__init__(Qt.Horizontal, parent)
        self.label_h = label_h
        self.strip_h = strip_h
        self._strip_bg = QColor(strip_bg)
        self._grid_color = QColor(grid_color)
        self._filter_on = False
        self.edits: Dict[int, QtWidgets.QLineEdit] = {}
        self.setFixedHeight(label_h)
        # Column drags and the one-time resizeColumnsToContents() both land
        # here; geometriesChanged also covers stretch-last-section refits.
        self.sectionResized.connect(lambda *_: self._layout_edits())
        self.geometriesChanged.connect(self._layout_edits)
        # Horizontal scrolling shifts the header's offset without any of the
        # signals above (setOffset isn't virtual either), so follow the
        # table's scrollbar directly - matters for the wide contacts table.
        hbar = parent.horizontalScrollBar() if hasattr(parent, 'horizontalScrollBar') else None
        if hbar is not None:
            hbar.valueChanged.connect(lambda *_: self._layout_edits())

    def add_filter_edit(self, col: int, edit: QtWidgets.QLineEdit) -> None:
        """Adopt `edit` as the filter box for logical column `col`."""
        edit.setParent(self.viewport())
        edit.setVisible(self._filter_on)
        self.edits[col] = edit
        self._layout_edits()

    def set_filter_visible(self, on: bool) -> None:
        if on == self._filter_on:
            return
        self._filter_on = on
        self.setFixedHeight(self.label_h + (self.strip_h if on else 0))
        for edit in self.edits.values():
            edit.setVisible(on)
        self._layout_edits()
        # QTableView connects this signal to its updateGeometries() slot,
        # which re-reads our height for the viewport margin and scrollbars.
        self.geometriesChanged.emit()
        self.viewport().update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._layout_edits()

    def _layout_edits(self) -> None:
        if not self._filter_on:
            return
        for col, edit in self.edits.items():
            x = self.sectionViewportPosition(col)
            w = self.sectionSize(col)
            # Leave the strip's right/bottom gridline visible around the box.
            edit.setGeometry(x, self.label_h, max(0, w - 1), self.strip_h - 1)

    def paintSection(self, painter, rect, logicalIndex) -> None:
        if not self._filter_on:
            super().paintSection(painter, rect, logicalIndex)
            return
        label_rect = QtCore.QRect(rect.x(), rect.y(), rect.width(), self.label_h)
        super().paintSection(painter, label_rect, logicalIndex)
        strip = QtCore.QRect(rect.x(), rect.y() + self.label_h,
                             rect.width(), rect.height() - self.label_h)
        painter.fillRect(strip, self._strip_bg)
        painter.setPen(self._grid_color)
        painter.drawLine(strip.right(), strip.top(), strip.right(), strip.bottom())
        painter.drawLine(strip.left(), strip.bottom(), strip.right(), strip.bottom())


# =============================================================================
# Custom Web Engine Page for Map Links
# =============================================================================

_YOUTUBE_ID_RE = re.compile(
    r'(?:youtu\.be/|youtube(?:-nocookie)?\.com/(?:watch\?v=|embed/|shorts/))'
    r'([A-Za-z0-9_-]{11})'
)


def _extract_youtube_id(url: str) -> Optional[str]:
    """Pull the 11-char video id out of a YouTube watch/share/embed URL."""
    match = _YOUTUBE_ID_RE.search(url or "")
    return match.group(1) if match else None


# Video player page loaded into the map QWebEngineView. Uses the YouTube
# IFrame Player API; end-of-video navigates to commstat://video-ended (map
# buttons are the way back to the map, so there's no on-screen exit control),
# Prev/Next navigate to commstat://video-prev / commstat://video-next,
# YouTube navigates to commstat://video-youtube/{video_id} (opens the video
# in the system browser), and Delete navigates to commstat://video-delete.
# CustomWebEnginePage routes these to _on_video_ended / _on_video_prev /
# _on_video_next / opening the browser / _on_video_delete.
# No autoplay: a transparent overlay (#tap) captures
# clicks anywhere on the video and toggles play/pause. Prev/Next (bottom
# corners) and YouTube (bottom
# center) stay hidden until the mouse
# is over the pane (body:hover) so they don't clutter the video.
# The pane isn't always 16:9 (operator can resize/stretch the window), so
# layoutVideoBox() computes a true 16:9 box that fits inside the pane
# (letterboxed/pillarboxed with black bars, never cropped) and publishes it
# as CSS vars (--vb-*); it reruns on window resize. #player and #poster are
# sized off those vars instead of the pane's raw 100%/100vw so the full
# video frame is always visible. The iframe box is additionally 190px taller
# than the 16:9 box and shifted up 95px within it so YouTube's edge-anchored
# chrome (paused-state share / "More videos" / "Watch on YouTube" row at the
# bottom, title bar at the top) sits outside the visible 16:9 area.
_VIDEO_PLAYER_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
html,body{margin:0;padding:0;width:100%;height:100%;background:#000;overflow:hidden;}
:root{--vb-w:100vw;--vb-h:100vh;--vb-l:0px;--vb-t:0px;}
#player{position:absolute;top:calc(var(--vb-t) - 95px);left:var(--vb-l);
width:var(--vb-w);height:calc(var(--vb-h) + 190px);border:0;}
/* #poster is a separate element (not #player itself, which the iframe_api
   script replaces with the real iframe) so YouTube's static thumbnail (no
   API key needed) stays visible above the iframe's own black paused frame
   (controls:0 suppresses YouTube's native thumbnail) until playback
   actually starts. Sized to the real visible 16:9 box (not #player's
   oversized/shifted box, which exists only to crop YouTube's iframe chrome
   off the video) since hqdefault.jpg is already 16:9 and needs no cropping. */
#poster{position:absolute;top:var(--vb-t);left:var(--vb-l);width:var(--vb-w);height:var(--vb-h);
background-image:url('https://img.youtube.com/vi/__VIDEO_ID__/hqdefault.jpg');
background-size:cover;background-position:center;background-color:#000;
z-index:3;pointer-events:none;}
#tap{position:absolute;top:0;left:0;width:100%;height:100%;z-index:5;cursor:pointer;}
#prev,#next{position:absolute;bottom:8px;z-index:10;
opacity:0;pointer-events:none;transition:opacity .15s ease;
background:rgba(0,0,0,0.65);color:#fff;border:1px solid #999;
border-radius:4px;padding:4px 12px;font-family:sans-serif;font-size:13px;cursor:pointer;}
#prev{left:8px;}
#next{right:8px;}
#prev:hover,#next:hover{background:rgba(40,167,69,0.85);}
#bottomCenter{position:absolute;bottom:8px;left:50%;transform:translateX(-50%);z-index:10;
display:flex;opacity:0;pointer-events:none;transition:opacity .15s ease;}
#youtube{background:rgba(0,0,0,0.65);color:#fff;border:1px solid #999;
border-radius:4px;padding:4px 12px;font-family:sans-serif;font-size:13px;cursor:pointer;
margin-right:8px;}
#youtube:hover{background:rgba(40,167,69,0.85);}
#delete{background:rgba(220,53,69,0.85);color:#fff;font-family:sans-serif;
font-size:13px;font-weight:bold;border:none;border-radius:4px;padding:4px 12px;cursor:pointer;}
#delete:hover{background:rgba(200,35,51,0.9);}
#delete:active{background:rgba(189,33,48,0.95);}
body:hover #prev,body:hover #next,body:hover #bottomCenter{opacity:1;pointer-events:auto;}
</style></head>
<body>
<div id="player"></div>
<div id="poster"></div>
<div id="tap"></div>
<button id="prev" onclick="window.location='commstat://video-prev';">&#9664; Prev</button>
<button id="next" onclick="window.location='commstat://video-next';">Next &#9654;</button>
<div id="bottomCenter">
<button id="youtube" onclick="window.location='commstat://video-youtube/__VIDEO_ID__';">YouTube</button>
<button id="delete" onclick="window.location='commstat://video-delete';">Delete</button>
</div>
<script>
function layoutVideoBox() {
    var w = window.innerWidth, h = window.innerHeight, aspect = 16 / 9;
    var boxW = w, boxH = Math.round(w / aspect);
    if (boxH > h) { boxH = h; boxW = Math.round(h * aspect); }
    var root = document.documentElement.style;
    root.setProperty('--vb-w', boxW + 'px');
    root.setProperty('--vb-h', boxH + 'px');
    root.setProperty('--vb-l', Math.round((w - boxW) / 2) + 'px');
    root.setProperty('--vb-t', Math.round((h - boxH) / 2) + 'px');
}
layoutVideoBox();
window.addEventListener('resize', layoutVideoBox);
var tag = document.createElement('script');
tag.src = "https://www.youtube.com/iframe_api";
document.head.appendChild(tag);
var player;
function onYouTubeIframeAPIReady() {
    player = new YT.Player('player', {
        videoId: '__VIDEO_ID__',
        playerVars: {autoplay: 0, rel: 0, controls: 0, disablekb: 1},
        events: {
            'onStateChange': function(e) {
                var playing = (e.data === YT.PlayerState.PLAYING);
                document.getElementById('poster').style.display = playing ? 'none' : 'block';
                if (e.data === YT.PlayerState.ENDED) {
                    window.location = 'commstat://video-ended';
                }
            }
        }
    });
}
document.getElementById('tap').onclick = function() {
    if (!player || typeof player.getPlayerState !== 'function') { return; }
    if (player.getPlayerState() === YT.PlayerState.PLAYING) {
        player.pauseVideo();
    } else {
        player.playVideo();
    }
};
</script>
</body></html>"""

# TEMP POC: Instagram reel player. Loads Instagram's /embed/ page in a
# centered vertical iframe (reels are 9:16). Instagram's embed handles its
# own click-to-play chrome, so there is no tap overlay here; Skip is the
# only exit (no end-of-video event is available from the embed).
_INSTAGRAM_PLAYER_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
html,body{margin:0;padding:0;width:100%;height:100%;background:#000;overflow:hidden;}
#wrap{position:absolute;top:0;left:50%;transform:translateX(-50%);
height:100%;aspect-ratio:9/16;}
#player{width:100%;height:100%;border:0;}
#skip{position:absolute;top:8px;right:8px;z-index:10;background:rgba(0,0,0,0.65);
color:#fff;border:1px solid #999;border-radius:4px;padding:4px 12px;
font-family:sans-serif;font-size:13px;cursor:pointer;}
#skip:hover{background:rgba(40,167,69,0.85);}
</style></head>
<body>
<div id="wrap"><iframe id="player" src="__IG_EMBED_URL__"
allow="autoplay; encrypted-media" allowfullscreen></iframe></div>
<button id="skip" onclick="window.location='commstat://video-ended';">Skip &#9654;</button>
</body></html>"""


class CustomWebEnginePage(QWebEnginePage):
    """Handles navigation requests from the map and video player."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent_widget = parent

    def javaScriptConsoleMessage(self, level, message, line, source):
        if 'webkitStorageInfo' in message:
            return
        if 'SameSite' in message:
            return
        if 'Unrecognized feature' in message:
            return
        super().javaScriptConsoleMessage(level, message, line, source)

    def acceptNavigationRequest(self, url, navigation_type, is_main_frame):
        """Intercept custom URL schemes for statrep links and video events."""
        url_str = url.toString()

        # Handle video-ended event
        if url_str == "commstat://video-ended":
            if hasattr(self.parent_widget, '_on_video_ended'):
                self.parent_widget._on_video_ended()
            return False  # Prevent navigation

        # Handle video Prev/Next navigation
        if url_str == "commstat://video-prev":
            if hasattr(self.parent_widget, '_on_video_prev'):
                self.parent_widget._on_video_prev()
            return False
        if url_str == "commstat://video-next":
            if hasattr(self.parent_widget, '_on_video_next'):
                self.parent_widget._on_video_next()
            return False

        # Handle video YouTube button: open the video in the system browser
        if url.host() == "video-youtube":
            video_id = url.path().lstrip("/")
            if video_id:
                youtube_url = f"https://www.youtube.com/watch?v={video_id}"
                if hasattr(self.parent_widget, '_open_external_link'):
                    self.parent_widget._open_external_link(youtube_url, "YouTube video")
                else:
                    QDesktopServices.openUrl(QUrl(youtube_url))
            return False

        # Handle video Delete
        if url_str == "commstat://video-delete":
            if hasattr(self.parent_widget, '_on_video_delete'):
                self.parent_widget._on_video_delete()
            return False

        # Handle the Map Filter dropdown's Help entry
        if url_str == "commstat://map-filter-help":
            if hasattr(self.parent_widget, '_on_map_filter_help'):
                self.parent_widget._on_map_filter_help()
            return False

        # Handle Map Filter state selection: commstat://map-filter-set/{off|map|custom}
        if url.host() == "map-filter-set":
            state = url.path().lstrip("/")
            if hasattr(self.parent_widget, '_on_map_filter_set'):
                self.parent_widget._on_map_filter_set(state)
            return False

        # Handle watchlist pin clicks: commstat://watchlist-pin/{callsign}
        if url.host() == "watchlist-pin":
            callsign = url.path().lstrip("/")
            if hasattr(self.parent_widget, '_on_watchlist_pin_clicked'):
                self.parent_widget._on_watchlist_pin_clicked(callsign)
            return False

        # Open USGS earthquake event links in the user's browser.
        if url.scheme() in ("http", "https") and "earthquake.usgs.gov" in url.host():
            if hasattr(self.parent_widget, '_open_external_link'):
                self.parent_widget._open_external_link(url.toString(), "USGS earthquake link")
            else:
                QDesktopServices.openUrl(url)
            return False

        # Handle statrep links from map popups: /statrep/{id}/{callsign}
        if url.path().startswith("/statrep/"):
            parts = url.path().strip("/").split("/")
            if len(parts) >= 3:
                sr_id = parts[1]
                callsign = parts[2]
                mw = self.parent_widget
                if sr_id and callsign and mw:
                    def _open_dialog(sr_id=sr_id, callsign=callsign, mw=mw):
                        try:
                            from qrz_lookup import StatRepDetailDialog
                            _FC = 3
                            table = mw.statrep_table
                            def build_record_list():
                                items = []
                                for r in range(table.rowCount()):
                                    ci = table.item(r, _FC)
                                    if ci:
                                        rid = ci.data(QtCore.Qt.UserRole)
                                        if rid is not None:
                                            items.append((rid, ci.text().strip()))
                                return items
                            dlg = StatRepDetailDialog(
                                sr_id, callsign, mw._internet_available,
                                commsrvr_url=_COMMSRVR,
                                module_background=mw.config.get_color('module_background'),
                                module_foreground=mw.config.get_color('module_foreground'),
                                title_bar_background=mw.config.get_color('title_bar_background'),
                                title_bar_foreground=mw.config.get_color('title_bar_foreground'),
                                data_background=mw.config.get_color('data_background'),
                                program_background=mw.config.get_color('program_background'),
                                program_foreground=mw.config.get_color('program_foreground'),
                                condition_green=mw.config.get_color('condition_green'),
                                condition_yellow=mw.config.get_color('condition_yellow'),
                                condition_red=mw.config.get_color('condition_red'),
                                condition_gray=mw.config.get_color('condition_gray'),
                                condition_purple=mw.config.get_color('condition_purple'),
                                tcp_pool=mw.tcp_pool,
                                connector_manager=mw.connector_manager,
                                record_list_provider=build_record_list,
                                parent=mw
                            )
                            dlg.pin_changed.connect(
                                lambda _: mw._save_map_position(callback=mw._load_map)
                            )
                            dlg.record_deleted.connect(mw._load_statrep_data)
                            dlg.record_deleted.connect(
                                lambda: mw._save_map_position(callback=mw._load_map)
                            )
                            dlg.exec_()
                        except Exception as e:
                            print(f"[Map popup] Failed to open StatRepDetailDialog: {e}")
                    QTimer.singleShot(0, _open_dialog)
            return False  # Prevent navigation
        return super().acceptNavigationRequest(url, navigation_type, is_main_frame)


# =============================================================================
# ConfigManager - Handles all configuration loading
# =============================================================================

class ConfigManager:
    """Manages application configuration from config.ini."""

    def __init__(self, config_path: str = CONFIG_FILE):
        """
        Initialize ConfigManager.

        Args:
            config_path: Path to the configuration file
        """
        self.config_path = Path(config_path)
        self.colors = DEFAULT_COLORS.copy()
        self.directed_config: Dict[str, str] = {}
        self.filter_settings: Dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        """Load configuration from file."""
        # Initialize filter settings (always reset on startup)
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        self.filter_settings = {
            'start': today,
            'end': ''
        }

        # Load toggle settings from config if it exists
        default_feed = list(DEFAULT_RSS_FEEDS.keys())[0]
        sound_defaults = self._default_sound_files()

        def _legacy_default(legacy_val: Optional[bool]) -> bool:
            # If the old single-toggle `notification_sounds` key is still in the
            # config file, use its value to seed all three per-event enable flags
            # once. Otherwise default to True.
            return True if legacy_val is None else legacy_val

        if not self.config_path.exists():
            self.directed_config = {
                'hide_heartbeat': False, 'show_every_group': True,
                'hide_map': False, 'show_alerts': False, 'show_contacts': False,
                'hide_internet_feed': False, 'hide_live_feed': False,
                'save_all_alerts': False, 'save_all_messages': False, 'save_all_videos': False,
                'selected_rss_feed': default_feed, 'apply_text_normalization': False,
                'unchecked_groups': '',
                'sound_alert_enabled':   True, 'sound_alert_file':   sound_defaults['alert'],
                'sound_message_enabled': True, 'sound_message_file': sound_defaults['message'],
                'sound_statrep_enabled': True, 'sound_statrep_file': sound_defaults['statrep'],
                'weather_radar': False,
                'weather_radar_refresh': 5,
                'show_radar_timestamp': True,
                'earthquake_layer': False,
                'earthquake_min_mag': 2.5,
                'earthquake_region': 'Worldwide',
                'earthquake_refresh': 10,
                'wildfire_layer': False,
                'wildfire_min_acres': 10.0,
                'wildfire_refresh': 30,
                'shown_watchlists': '',
            }
            return

        config = ConfigParser()
        config.read(self.config_path)

        legacy_master = None
        if config.has_section("DIRECTEDCONFIG") and config.has_option("DIRECTEDCONFIG", "notification_sounds"):
            legacy_master = config.getboolean("DIRECTEDCONFIG", "notification_sounds")
        seed = _legacy_default(legacy_master)

        if config.has_section("DIRECTEDCONFIG"):
            self.directed_config = {
                'hide_heartbeat': config.getboolean("DIRECTEDCONFIG", "hide_heartbeat", fallback=False),
                'show_every_group': config.getboolean("DIRECTEDCONFIG", "show_every_group", fallback=False),
                'hide_map': config.getboolean("DIRECTEDCONFIG", "hide_map", fallback=False),
                'show_alerts': config.getboolean("DIRECTEDCONFIG", "show_alerts", fallback=False),
                'show_contacts': config.getboolean("DIRECTEDCONFIG", "show_contacts", fallback=False),
                'hide_internet_feed': config.getboolean("DIRECTEDCONFIG", "hide_internet_feed", fallback=False),
                'hide_live_feed': config.getboolean("DIRECTEDCONFIG", "hide_live_feed", fallback=False),
                'save_all_alerts': config.getboolean("DIRECTEDCONFIG", "save_all_alerts", fallback=False),
                'save_all_messages': config.getboolean("DIRECTEDCONFIG", "save_all_messages", fallback=False),
                'save_all_videos': config.getboolean("DIRECTEDCONFIG", "save_all_videos", fallback=False),
                'selected_rss_feed': config.get("DIRECTEDCONFIG", "selected_rss_feed", fallback=default_feed),
                'apply_text_normalization': config.getboolean("DIRECTEDCONFIG", "apply_text_normalization", fallback=True),
                'unchecked_groups': config.get("DIRECTEDCONFIG", "unchecked_groups", fallback=""),
                'sound_alert_enabled':   config.getboolean("DIRECTEDCONFIG", "sound_alert_enabled",   fallback=seed),
                'sound_alert_file':      config.get("DIRECTEDCONFIG", "sound_alert_file",      fallback=sound_defaults['alert']),
                'sound_message_enabled': config.getboolean("DIRECTEDCONFIG", "sound_message_enabled", fallback=seed),
                'sound_message_file':    config.get("DIRECTEDCONFIG", "sound_message_file",    fallback=sound_defaults['message']),
                'sound_statrep_enabled': config.getboolean("DIRECTEDCONFIG", "sound_statrep_enabled", fallback=seed),
                'sound_statrep_file':    config.get("DIRECTEDCONFIG", "sound_statrep_file",    fallback=sound_defaults['statrep']),
                'weather_radar':          config.getboolean("DIRECTEDCONFIG", "weather_radar",          fallback=False),
                'weather_radar_refresh':  config.getint("DIRECTEDCONFIG", "weather_radar_refresh",  fallback=5),
                'show_radar_timestamp':   config.getboolean("DIRECTEDCONFIG", "show_radar_timestamp",   fallback=True),
                'earthquake_layer':       config.getboolean("DIRECTEDCONFIG", "earthquake_layer",       fallback=False),
                'earthquake_min_mag':     config.getfloat("DIRECTEDCONFIG", "earthquake_min_mag",     fallback=2.5),
                'earthquake_region':      config.get("DIRECTEDCONFIG", "earthquake_region",      fallback="Worldwide"),
                'earthquake_refresh':     config.getint("DIRECTEDCONFIG", "earthquake_refresh",     fallback=10),
                'wildfire_layer':         config.getboolean("DIRECTEDCONFIG", "wildfire_layer",         fallback=False),
                'wildfire_min_acres':     config.getfloat("DIRECTEDCONFIG", "wildfire_min_acres",     fallback=10.0),
                'wildfire_refresh':       config.getint("DIRECTEDCONFIG", "wildfire_refresh",       fallback=30),
                'shown_watchlists':       config.get("DIRECTEDCONFIG", "shown_watchlists",       fallback=""),
                'map_theme':              config.get("DIRECTEDCONFIG", "map_theme",              fallback='dark'),
                'carto_api_key':          config.get("DIRECTEDCONFIG", "carto_api_key",          fallback=""),
            }
        else:
            self.directed_config = {
                'hide_heartbeat': False, 'show_every_group': True,
                'hide_map': False, 'show_alerts': False, 'show_contacts': False,
                'hide_internet_feed': False, 'hide_live_feed': False,
                'save_all_alerts': False, 'save_all_messages': False, 'save_all_videos': False,
                'selected_rss_feed': default_feed, 'apply_text_normalization': False,
                'unchecked_groups': '',
                'sound_alert_enabled':   True, 'sound_alert_file':   sound_defaults['alert'],
                'sound_message_enabled': True, 'sound_message_file': sound_defaults['message'],
                'sound_statrep_enabled': True, 'sound_statrep_file': sound_defaults['statrep'],
                'weather_radar': False,
                'weather_radar_refresh': 5,
                'show_radar_timestamp': True,
                'earthquake_layer': False,
                'earthquake_min_mag': 2.5,
                'earthquake_region': 'Worldwide',
                'earthquake_refresh': 10,
                'wildfire_layer': False,
                'wildfire_min_acres': 10.0,
                'wildfire_refresh': 30,
                'shown_watchlists': '',
                'map_theme': 'dark',
                'carto_api_key': '',
            }

        # Load user-edited UI colors from config.ini [COLORS].
        # Values here override DEFAULT_COLORS without touching constants.py.
        if config.has_section("COLORS"):
            for key, value in config.items("COLORS"):
                if key in self.colors and value:
                    self.colors[key] = value.strip()


    @staticmethod
    def _default_sound_files() -> Dict[str, str]:
        # First-run defaults: scan SOUNDS_DIR and pick a file per event by
        # keyword. Falls back to the first .wav alphabetically, or "" if none.
        try:
            files = sorted(f for f in os.listdir(SOUNDS_DIR) if f.lower().endswith('.wav'))
        except OSError:
            files = []
        first = files[0] if files else ""

        def pick(*keywords: str) -> str:
            for kw in keywords:
                for f in files:
                    if kw in f.lower():
                        return f
            return first

        return {
            'alert':   pick('alarm', 'alert'),
            'message': pick('bell', 'ding'),
            'statrep': pick('ping', 'triple', 'dit'),
        }

    def get_color(self, key: str) -> str:
        """Get a color value by key."""
        return self.colors.get(key, '#FFFFFF')

    def set_color(self, key: str, value: str) -> None:
        """Save a UI color override to config.ini."""
        if key not in self.colors:
            return
        self.colors[key] = value
        config = ConfigParser()
        config.read(self.config_path)
        if not config.has_section("COLORS"):
            config.add_section("COLORS")
        config.set("COLORS", key, value)
        with open(self.config_path, 'w') as f:
            config.write(f)

    def _save_setting(self, key: str, value) -> None:
        """Save a setting to both memory and config file."""
        self.directed_config[key] = value
        config = ConfigParser()
        config.read(self.config_path)
        if not config.has_section("DIRECTEDCONFIG"):
            config.add_section("DIRECTEDCONFIG")
        config.set("DIRECTEDCONFIG", key, str(value))
        with open(self.config_path, 'w') as f:
            config.write(f)

    def get_hide_heartbeat(self) -> bool:
        return self.directed_config.get('hide_heartbeat', False)

    def set_hide_heartbeat(self, value: bool) -> None:
        self._save_setting('hide_heartbeat', value)

    def get_hide_internet_feed(self) -> bool:
        return self.directed_config.get('hide_internet_feed', False)

    def set_hide_internet_feed(self, value: bool) -> None:
        self._save_setting('hide_internet_feed', value)

    def get_hide_live_feed(self) -> bool:
        return self.directed_config.get('hide_live_feed', False)

    def set_hide_live_feed(self, value: bool) -> None:
        self._save_setting('hide_live_feed', value)

    def get_hide_map(self) -> bool:
        return self.directed_config.get('hide_map', False)

    def set_hide_map(self, value: bool) -> None:
        self._save_setting('hide_map', value)

    def get_show_every_group(self) -> bool:
        return self.directed_config.get('show_every_group', False)

    def set_show_every_group(self, value: bool) -> None:
        self._save_setting('show_every_group', value)

    def get_unchecked_groups(self) -> List[str]:
        raw = self.directed_config.get('unchecked_groups', '')
        return [g.strip() for g in raw.split(',') if g.strip()]

    def set_unchecked_groups(self, groups: List[str]) -> None:
        self._save_setting('unchecked_groups', ','.join(groups))

    def get_show_alerts(self) -> bool:
        return self.directed_config.get('show_alerts', False)

    def set_show_alerts(self, value: bool) -> None:
        self._save_setting('show_alerts', value)

    def get_save_all_alerts(self) -> bool:
        return self.directed_config.get('save_all_alerts', False)

    def set_save_all_alerts(self, value: bool) -> None:
        self._save_setting('save_all_alerts', value)

    def get_save_all_messages(self) -> bool:
        return self.directed_config.get('save_all_messages', False)

    def set_save_all_messages(self, value: bool) -> None:
        self._save_setting('save_all_messages', value)

    def get_save_all_videos(self) -> bool:
        return self.directed_config.get('save_all_videos', False)

    def set_save_all_videos(self, value: bool) -> None:
        self._save_setting('save_all_videos', value)

    def get_sound_enabled(self, event: str) -> bool:
        return self.directed_config.get(f'sound_{event}_enabled', True)

    def set_sound_enabled(self, event: str, value: bool) -> None:
        self._save_setting(f'sound_{event}_enabled', value)

    def get_sound_file(self, event: str) -> str:
        return self.directed_config.get(f'sound_{event}_file', '')

    def set_sound_file(self, event: str, filename: str) -> None:
        self._save_setting(f'sound_{event}_file', filename)

    def get_show_contacts(self) -> bool:
        return self.directed_config.get('show_contacts', False)

    def set_show_contacts(self, value: bool) -> None:
        self._save_setting('show_contacts', value)

    def get_apply_text_normalization(self) -> bool:
        return self.directed_config.get('apply_text_normalization', True)

    def set_apply_text_normalization(self, value: bool) -> None:
        self._save_setting('apply_text_normalization', value)


    def get_weather_radar(self) -> bool:
        return self.directed_config.get('weather_radar', False)

    def set_weather_radar(self, value: bool) -> None:
        self._save_setting('weather_radar', value)

    def get_weather_radar_refresh(self) -> int:
        return int(self.directed_config.get('weather_radar_refresh', 5))

    def set_weather_radar_refresh(self, value: int) -> None:
        self._save_setting('weather_radar_refresh', value)

    def get_show_radar_timestamp(self) -> bool:
        return bool(self.directed_config.get('show_radar_timestamp', True))

    def set_show_radar_timestamp(self, value: bool) -> None:
        self._save_setting('show_radar_timestamp', value)


    def get_map_theme(self) -> str:
        return self.directed_config.get('map_theme', 'dark')

    def set_map_theme(self, value: str) -> None:
        self._save_setting('map_theme', value)

    def get_carto_api_key(self) -> str:
        return self.directed_config.get('carto_api_key', '')

    def set_carto_api_key(self, value: str) -> None:
        self._save_setting('carto_api_key', value)

    def get_earthquake_layer(self) -> bool:
        return bool(self.directed_config.get('earthquake_layer', False))

    def set_earthquake_layer(self, value: bool) -> None:
        self._save_setting('earthquake_layer', value)

    def get_earthquake_min_mag(self) -> float:
        try:
            return float(self.directed_config.get('earthquake_min_mag', 2.5))
        except Exception:
            return 2.5

    def set_earthquake_min_mag(self, value: float) -> None:
        self._save_setting('earthquake_min_mag', value)

    def get_shown_watchlists(self) -> List[str]:
        """Watchlist names whose member pins are shown on the map."""
        raw = self.directed_config.get('shown_watchlists', '')
        return [w for w in raw.split(',') if w]

    def set_shown_watchlists(self, watchlists: List[str]) -> None:
        self._save_setting('shown_watchlists', ','.join(watchlists))

    def get_earthquake_region(self) -> str:
        return self.directed_config.get('earthquake_region', 'Worldwide')

    def set_earthquake_region(self, value: str) -> None:
        self._save_setting('earthquake_region', value)

    def get_earthquake_refresh(self) -> int:
        try:
            return int(self.directed_config.get('earthquake_refresh', 10))
        except Exception:
            return 10

    def set_earthquake_refresh(self, value: int) -> None:
        self._save_setting('earthquake_refresh', value)

    def get_wildfire_layer(self) -> bool:
        return bool(self.directed_config.get('wildfire_layer', False))

    def set_wildfire_layer(self, value: bool) -> None:
        self._save_setting('wildfire_layer', value)

    def get_wildfire_min_acres(self) -> float:
        try:
            return float(self.directed_config.get('wildfire_min_acres', 10.0))
        except Exception:
            return 10.0

    def set_wildfire_min_acres(self, value: float) -> None:
        self._save_setting('wildfire_min_acres', value)

    def get_wildfire_refresh(self) -> int:
        try:
            return int(self.directed_config.get('wildfire_refresh', 30))
        except Exception:
            return 30

    def set_wildfire_refresh(self, value: int) -> None:
        self._save_setting('wildfire_refresh', value)

    def get_selected_rss_feed(self) -> str:
        return self.directed_config.get('selected_rss_feed', list(DEFAULT_RSS_FEEDS.keys())[0])

    def set_selected_rss_feed(self, feed_name: str) -> None:
        self._save_setting('selected_rss_feed', feed_name)


# =============================================================================
# RSSFetcher - Fetches and parses RSS news feeds
# =============================================================================

class RSSFetcher:
    """Fetches and caches RSS news headlines."""

    def __init__(self):
        """Initialize the RSS fetcher with empty cache."""
        self._headlines: List[str] = []
        self._cache_time: Optional[datetime] = None
        self._cache_duration = timedelta(minutes=5)
        self._current_url: str = ""
        self._fetching = False

    def get_headlines(self, feed_url: str, force_refresh: bool = False) -> List[str]:
        """
        Get headlines from the specified RSS feed.

        Args:
            feed_url: URL of the RSS feed
            force_refresh: If True, bypass cache and fetch fresh data

        Returns:
            List of headline strings
        """
        # Check if we need to refresh
        now = datetime.now()
        cache_valid = (
            self._cache_time is not None
            and self._current_url == feed_url
            and (now - self._cache_time) < self._cache_duration
            and not force_refresh
        )

        if cache_valid and self._headlines:
            return self._headlines

        # Return cached data if currently fetching
        if self._fetching:
            return self._headlines

        # Fetch new data
        self._current_url = feed_url
        self._fetch_feed(feed_url)
        return self._headlines

    def _fetch_feed(self, feed_url: str) -> None:
        """Fetch and parse the RSS feed."""
        self._fetching = True
        try:
            request = urllib.request.Request(feed_url)

            with urllib.request.urlopen(request, timeout=10, context=create_insecure_ssl_context()) as response:
                content = response.read().decode('utf-8', errors='replace')

            # Parse RSS XML
            root = ET.fromstring(content)
            articles = []  # List of (title, pubdate_datetime) tuples
            now = datetime.now(timezone.utc)
            cutoff_time = now - timedelta(hours=6)

            # Try RSS 2.0 format first (most common)
            for item in root.findall('.//item'):
                title_elem = item.find('title')
                pubdate_elem = item.find('pubDate')

                if title_elem is not None and title_elem.text:
                    title = title_elem.text.strip()
                    pub_date = None

                    # Parse publication date
                    if pubdate_elem is not None and pubdate_elem.text:
                        try:
                            pub_date = parsedate_to_datetime(pubdate_elem.text)
                            # Filter out articles older than 6 hours
                            if pub_date < cutoff_time:
                                continue
                        except Exception:
                            # If date parsing fails, include the article anyway
                            pub_date = None

                    articles.append((title, pub_date))

            # Try Atom format if no RSS items found
            if not articles:
                # Atom uses namespace
                ns = {'atom': 'http://www.w3.org/2005/Atom'}
                for entry in root.findall('.//atom:entry', ns):
                    title_elem = entry.find('atom:title', ns)
                    published_elem = entry.find('atom:published', ns) or entry.find('atom:updated', ns)

                    if title_elem is not None and title_elem.text:
                        title = title_elem.text.strip()
                        pub_date = None

                        # Parse publication date
                        if published_elem is not None and published_elem.text:
                            try:
                                # Atom uses ISO 8601 format
                                pub_date = datetime.fromisoformat(published_elem.text.replace('Z', '+00:00'))
                                # Filter out articles older than 6 hours
                                if pub_date < cutoff_time:
                                    continue
                            except Exception:
                                pub_date = None

                        articles.append((title, pub_date))

                # Also try without namespace
                if not articles:
                    for entry in root.findall('.//entry'):
                        title_elem = entry.find('title')
                        published_elem = entry.find('published') or entry.find('updated')

                        if title_elem is not None and title_elem.text:
                            title = title_elem.text.strip()
                            pub_date = None

                            if published_elem is not None and published_elem.text:
                                try:
                                    pub_date = datetime.fromisoformat(published_elem.text.replace('Z', '+00:00'))
                                    if pub_date < cutoff_time:
                                        continue
                                except Exception:
                                    pub_date = None

                            articles.append((title, pub_date))

            # Sort by date (newest first), articles without dates go to end
            articles.sort(key=lambda x: x[1] if x[1] else datetime.min.replace(tzinfo=timezone.utc), reverse=True)

            # Format headlines with timestamps
            headlines = []
            for title, pub_date in articles[:20]:  # Limit to 20 headlines
                if pub_date:
                    # Convert to UTC for display
                    utc_time = pub_date.astimezone(timezone.utc)
                    time_str = utc_time.strftime('%H:%M UTC')
                    headlines.append(f"{title} - {time_str}")
                else:
                    headlines.append(title)

            self._headlines = headlines
            self._cache_time = datetime.now()

        except Exception as e:
            print(f"Error fetching RSS feed: {e}")
            # Keep old headlines if fetch fails
            if not self._headlines:
                self._headlines = ["Unable to fetch news - check internet connection"]

        finally:
            self._fetching = False

    def fetch_async(self, feed_url: str, callback=None) -> None:
        """
        Fetch RSS feed in background thread.

        Args:
            feed_url: URL of the RSS feed
            callback: Optional callback function to call when done
        """
        def fetch_thread():
            self._fetch_feed(feed_url)
            if callback:
                callback()

        thread = threading.Thread(target=fetch_thread, daemon=True)
        thread.start()

    def clear_cache(self) -> None:
        """Clear the cached headlines."""
        self._headlines = []
        self._cache_time = None
        self._current_url = ""


# =============================================================================
# DatabaseManager - Handles all database operations
# =============================================================================

class DatabaseManager:
    """Manages SQLite database operations."""

    def __init__(self, db_path: str = DATABASE_FILE):
        """
        Initialize DatabaseManager.

        Args:
            db_path: Path to the SQLite database file
        """
        self.db_path = db_path

    def _execute(self, operation, default=None):
        """Execute a database operation with error handling.

        Args:
            operation: Callable that takes (cursor, connection) and returns result
            default: Value to return on error

        Returns:
            Result of operation, or default on error
        """
        try:
            with sqlite3.connect(self.db_path, timeout=10) as connection:
                cursor = connection.cursor()
                return operation(cursor, connection)
        except sqlite3.Error as error:
            print(f"Database error: {error}")
            return default

    def get_statrep_data(
        self,
        groups: List[str],
        start: str,
        end: str = '',
        show_all: bool = False,
        exclude_groups: List[str] = None,
        user_callsign: str = ""
    ) -> List[Tuple]:
        """
        Fetch StatRep data from database.

        Args:
            groups: List of active group names (empty list returns no data unless show_all)
            start: Start date filter (required)
            end: End date filter (optional, empty string means no upper limit)
            show_all: If True, return all statreps (subject to exclude_groups)
            exclude_groups: When show_all=True, exclude records from these groups
            user_callsign: User's own callsign; also returns statreps addressed directly to it

        Returns:
            List of tuples containing StatRep records
        """
        # If no active groups and not showing all, return empty list
        if not groups and not show_all and not user_callsign:
            return []

        try:
            with sqlite3.connect(self.db_path, timeout=10) as connection:
                cursor = connection.cursor()

                # Build date condition based on whether end date is provided
                if end:
                    date_condition = "datetime BETWEEN ? AND ?"
                    date_params = [start, end]
                else:
                    date_condition = "datetime >= ?"
                    date_params = [start]

                # Build query based on whether we're showing all or filtering by groups
                if show_all:
                    if exclude_groups:
                        excl_with_at = ["@" + g for g in exclude_groups]
                        placeholders = ",".join("?" * len(excl_with_at))
                        query = f"""
                            SELECT db, datetime, freq, from_callsign, target, sr_id, grid, scope, map,
                                   power, water, med, telecom, travel, internet,
                                   fuel, food, crime, civil, political, comments, source, id, global_id
                            FROM statrep
                            WHERE target NOT IN ({placeholders})
                              AND ({date_condition} OR pinned = 1)
                            ORDER BY datetime DESC
                        """
                        params = excl_with_at + date_params
                    else:
                        query = f"""
                            SELECT db, datetime, freq, from_callsign, target, sr_id, grid, scope, map,
                                   power, water, med, telecom, travel, internet,
                                   fuel, food, crime, civil, political, comments, source, id, global_id
                            FROM statrep
                            WHERE {date_condition} OR pinned = 1
                            ORDER BY datetime DESC
                        """
                        params = date_params
                else:
                    # Build group filter; also include statreps addressed to the user's callsign
                    groups_with_at = ["@" + g for g in groups]
                    target_list = groups_with_at[:]
                    if user_callsign:
                        target_list.append(user_callsign.upper())
                    if not target_list:
                        return []
                    placeholders = ",".join("?" * len(target_list))
                    query = f"""
                        SELECT db, datetime, freq, from_callsign, target, sr_id, grid, scope, map,
                               power, water, med, telecom, travel, internet,
                               fuel, food, crime, civil, political, comments, source, id, global_id
                        FROM statrep
                        WHERE target IN ({placeholders}) AND ({date_condition} OR pinned = 1)
                        ORDER BY datetime DESC
                    """
                    params = target_list + date_params

                cursor.execute(query, params)
                return cursor.fetchall()
        except sqlite3.Error as error:
            print(f"Database error: {error}")
            return []

    def get_message_data(
        self,
        groups: List[str],
        start: str,
        end: str = '',
        show_all: bool = False
    ) -> List[Tuple]:
        """
        Fetch message data from database.

        Args:
            groups: List of active group names
            start: Start date filter (required)
            end: End date filter (optional, empty string means no upper limit)
            show_all: If True, return all messages regardless of group

        Returns:
            List of tuples containing message records
        """
        try:
            with sqlite3.connect(self.db_path, timeout=10) as connection:
                cursor = connection.cursor()

                # Build date condition based on whether end date is provided
                if end:
                    date_condition = "datetime BETWEEN ? AND ?"
                    date_params = [start, end]
                else:
                    date_condition = "datetime >= ?"
                    date_params = [start]

                if show_all:
                    # Show all messages regardless of group
                    query = f"""SELECT db, datetime, freq, from_callsign, target, msg_id, message, source, delivered, global_id
                               FROM messages
                               WHERE {date_condition}
                               ORDER BY datetime DESC"""
                    params = date_params
                elif groups:
                    # Filter by active groups (add @ prefix for matching)
                    groups_with_at = ["@" + g for g in groups]
                    placeholders = ",".join("?" * len(groups_with_at))
                    query = f"""SELECT db, datetime, freq, from_callsign, target, msg_id, message, source, delivered, global_id
                               FROM messages
                               WHERE target IN ({placeholders}) AND {date_condition}
                               ORDER BY datetime DESC"""
                    params = groups_with_at + date_params
                else:
                    # No groups and not show_all - return empty
                    return []

                cursor.execute(query, params)
                return cursor.fetchall()
        except sqlite3.Error as error:
            print(f"Database error: {error}")
            return []

    def get_all_groups(self) -> List[str]:
        """Get all group names."""
        def op(cursor, conn):
            cursor.execute("SELECT name FROM groups ORDER BY name")
            return [row[0] for row in cursor.fetchall()]
        return self._execute(op, [])

    def add_group(self, group_name: str, comment: str = "", url1: str = "", url2: str = "") -> bool:
        """Add a new group with optional fields. Returns True if successful."""
        name = group_name.strip().upper()[:MAX_GROUP_NAME_LENGTH]
        if not name:
            return False
        try:
            with sqlite3.connect(self.db_path, timeout=10) as connection:
                cursor = connection.cursor()
                today = datetime.now().strftime("%Y-%m-%d")
                cursor.execute(
                    "INSERT INTO groups (name, comment, url1, url2, date_added) VALUES (?, ?, ?, ?, ?)",
                    (name, comment.strip(), url1.strip(), url2.strip(), today)
                )
                connection.commit()
                return True
        except sqlite3.IntegrityError:
            # Duplicate name
            return False
        except sqlite3.Error as error:
            print(f"Database error: {error}")
            return False

    def update_group(self, group_name: str, comment: str = "", url1: str = "", url2: str = "") -> bool:
        """Update an existing group's fields. Returns True if successful."""
        def op(cursor, conn):
            cursor.execute(
                "UPDATE groups SET comment = ?, url1 = ?, url2 = ? WHERE name = ?",
                (comment.strip(), url1.strip(), url2.strip(), group_name.upper())
            )
            conn.commit()
            return cursor.rowcount > 0
        return self._execute(op, False)

    def update_group_full(self, old_name: str, new_name: str, comment: str = "") -> bool:
        """Rename a group and update its comment. Returns True if successful."""
        def op(cursor, conn):
            cursor.execute(
                "UPDATE groups SET name = ?, comment = ? WHERE name = ?",
                (new_name.strip().upper(), comment.strip(), old_name.strip().upper())
            )
            conn.commit()
            return cursor.rowcount > 0
        return self._execute(op, False)

    def upsert_contacts_pair(
        self,
        relay_cs: str,
        relay_snr: int,
        target_cs: str,
        target_snr: int,
        freq_mhz: float,
    ) -> bool:
        """
        Upsert two contacts rows from a single RX.DIRECTED SNR observation.

        relay_cs  = the station we directly heard (sender of the message)
        relay_snr = our local SNR reading of that station
        target_cs = the station the relay reported hearing (body callsign)
        target_snr = the SNR the relay reported for that station

        Entry 1 (relay observation):  pair = (target_cs, relay_cs)
        Entry 2 (relay self-presence): pair = (relay_cs, relay_cs), snr = relay_snr

        Both rows use UNIQUE(target_cs, relay_cs) to refresh freq, SNR, and
        insert_date on every new observation instead of growing the table.
        """
        if not target_cs or not relay_cs:
            return False
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        sql = (
            "INSERT INTO contacts "
            "(freq, relay_snr, relay_cs, target_cs, target_snr, insert_date) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(target_cs, relay_cs) DO UPDATE SET "
            "freq        = excluded.freq, "
            "relay_snr   = excluded.relay_snr, "
            "target_snr  = excluded.target_snr, "
            "insert_date = excluded.insert_date"
        )
        try:
            with sqlite3.connect(self.db_path, timeout=10) as connection:
                cursor = connection.cursor()
                cursor.execute(
                    sql,
                    (freq_mhz, relay_snr, relay_cs, target_cs, target_snr, now_utc),
                )
                cursor.execute(
                    sql,
                    (freq_mhz, relay_snr, relay_cs, relay_cs, relay_snr, now_utc),
                )
                connection.commit()
                return True
        except sqlite3.OperationalError as error:
            # Schema not yet applied — fail soft so the live feed keeps running.
            print(f"contacts upsert skipped (schema not ready?): {error}")
            return False
        except sqlite3.Error as error:
            print(f"Database error: {error}")
            return False

    def upsert_contacts_hearing(
        self,
        relay_cs: str,
        heard_list: List[str],
        freq_mhz: float,
        snr: int = _CONTACTS_HEARING_DEFAULT_SNR,
    ) -> bool:
        """
        Upsert contacts rows from a HEARING report.

        relay_cs   = the station that issued the HEARING report (from_call)
        heard_list = ordered base callsigns the relay reports hearing
        freq_mhz   = current dial frequency (MHz, 3-decimal)
        snr        = placeholder written to both SNR columns of every row
                     (defaults to _CONTACTS_HEARING_DEFAULT_SNR since the
                     report carries no SNR data for either end of the link)

        One row per heard callsign as (relay_cs, target_cs=heard) plus one
        self-presence row (relay_cs, target_cs=relay_cs). UNIQUE(target_cs,
        relay_cs) collapses repeat observations in place.
        """
        if not relay_cs or not heard_list:
            return False
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        sql = (
            "INSERT INTO contacts "
            "(freq, relay_snr, relay_cs, target_cs, target_snr, insert_date) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(target_cs, relay_cs) DO UPDATE SET "
            "freq        = excluded.freq, "
            "relay_snr   = excluded.relay_snr, "
            "target_snr  = excluded.target_snr, "
            "insert_date = excluded.insert_date"
        )
        try:
            with sqlite3.connect(self.db_path, timeout=10) as connection:
                cursor = connection.cursor()
                for heard_cs in heard_list:
                    cursor.execute(sql, (freq_mhz, snr, relay_cs, heard_cs, snr, now_utc))
                cursor.execute(sql, (freq_mhz, snr, relay_cs, relay_cs, snr, now_utc))
                connection.commit()
                return True
        except sqlite3.OperationalError as error:
            print(f"contacts upsert skipped (schema not ready?): {error}")
            return False
        except sqlite3.Error as error:
            print(f"Database error: {error}")
            return False

    def purge_old_contacts(self, hours: int) -> int:
        """
        Delete contacts rows whose insert_date is older than `hours`.

        insert_date is stored as 'YYYY-MM-DD HH:MM:SS UTC'; lexicographic
        comparison against a same-format threshold sorts correctly so no
        SQL date parsing is needed.

        Returns the number of rows deleted. Returns 0 if hours <= 0 (which
        disables the purge — useful for letting an operator opt out via a
        constant of 0) or on DB error.
        """
        if hours <= 0:
            return 0
        threshold = (
            datetime.now(timezone.utc) - timedelta(hours=hours)
        ).strftime("%Y-%m-%d %H:%M:%S UTC")

        def op(cursor, conn):
            cursor.execute("DELETE FROM contacts WHERE insert_date < ?", (threshold,))
            conn.commit()
            return cursor.rowcount

        return self._execute(op, 0)

    def upsert_contact_self(
        self,
        cs: str,
        snr: int,
        freq_mhz: float,
    ) -> bool:
        """
        Upsert a single self-presence row for an RX.DIRECTED whose body did
        not parse as an SNR observation (e.g., a group post, a relayed
        message, free-form text). Records that `cs` was on the air at `snr`.

        Both relay_cs and target_cs are set to `cs`; both SNR columns are set
        to `snr`. UNIQUE(target_cs, relay_cs) keeps this collapsed to one
        roster row per callsign.
        """
        if not cs:
            return False
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        sql = (
            "INSERT INTO contacts "
            "(freq, relay_snr, relay_cs, target_cs, target_snr, insert_date) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(target_cs, relay_cs) DO UPDATE SET "
            "freq        = excluded.freq, "
            "relay_snr   = excluded.relay_snr, "
            "target_snr  = excluded.target_snr, "
            "insert_date = excluded.insert_date"
        )
        try:
            with sqlite3.connect(self.db_path, timeout=10) as connection:
                cursor = connection.cursor()
                cursor.execute(sql, (freq_mhz, snr, cs, cs, snr, now_utc))
                connection.commit()
                return True
        except sqlite3.OperationalError as error:
            print(f"contacts upsert skipped (schema not ready?): {error}")
            return False
        except sqlite3.Error as error:
            print(f"Database error: {error}")
            return False

    def get_group_details(self, group_name: str) -> Optional[Dict]:
        """Get full details of a group."""
        def op(cursor, conn):
            cursor.execute(
                "SELECT name, comment, url1, url2, date_added FROM groups WHERE name = ?",
                (group_name.upper(),)
            )
            row = cursor.fetchone()
            if row:
                return {
                    "name": row[0],
                    "comment": row[1] or "",
                    "url1": row[2] or "",
                    "url2": row[3] or "",
                    "date_added": row[4] or "",
                }
            return None
        return self._execute(op, None)

    def get_all_groups_details(self) -> List[Dict]:
        """Get full details of all groups, sorted by name."""
        def op(cursor, conn):
            cursor.execute(
                "SELECT name, comment, url1, url2, date_added FROM groups ORDER BY name"
            )
            return [
                {
                    "name": row[0],
                    "comment": row[1] or "",
                    "url1": row[2] or "",
                    "url2": row[3] or "",
                    "date_added": row[4] or "",
                }
                for row in cursor.fetchall()
            ]
        return self._execute(op, [])

    def remove_group(self, group_name: str) -> bool:
        """Remove a group. Returns True if successful."""
        def op(cursor, conn):
            cursor.execute("DELETE FROM groups WHERE name = ?", (group_name.upper(),))
            conn.commit()
            return cursor.rowcount > 0
        return self._execute(op, False)

    def get_group_count(self) -> int:
        """Get the number of groups."""
        def op(cursor, conn):
            cursor.execute("SELECT COUNT(*) FROM groups")
            return cursor.fetchone()[0]
        return self._execute(op, 0)

    # ── Watchlists ─────────────────────────────────────────────────────────
    # A watchlist is a named list of qrz contacts drawn on the map as
    # persistent halo pins. Independent of the JS8 @GROUPS used for message
    # routing. Methods that read the watchlist tables degrade gracefully
    # (empty/False) on a DB that predates them.

    def get_all_watchlists_details(self) -> List[Dict]:
        """Get full details of all watchlists, sorted by name."""
        def op(cursor, conn):
            cursor.execute(
                "SELECT name, comment, object_color, object_shape FROM watchlists ORDER BY name"
            )
            return [
                {
                    "name": row[0],
                    "comment": row[1] or "",
                    "object_color": row[2] or DEFAULT_OBJECT_COLOR,
                    "object_shape": row[3] or DEFAULT_OBJECT_SHAPE,
                }
                for row in cursor.fetchall()
            ]
        return self._execute(op, [])

    def get_all_watchlists(self) -> List[str]:
        """Get all watchlist names, sorted."""
        def op(cursor, conn):
            cursor.execute("SELECT name FROM watchlists ORDER BY name")
            return [row[0] for row in cursor.fetchall()]
        return self._execute(op, [])

    def add_watchlist(self, name: str, comment: str = "",
                      object_color: str = DEFAULT_OBJECT_COLOR,
                      object_shape: str = DEFAULT_OBJECT_SHAPE) -> bool:
        """Add a new watchlist. Returns False on duplicate name."""
        name = name.strip().upper()
        if not name:
            return False
        try:
            with sqlite3.connect(self.db_path, timeout=10) as connection:
                cursor = connection.cursor()
                cursor.execute(
                    "INSERT INTO watchlists (name, comment, object_color, object_shape, date_added) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (name, comment.strip(), object_color, object_shape,
                     datetime.now().strftime("%Y-%m-%d"))
                )
                connection.commit()
                return True
        except sqlite3.IntegrityError:
            # Duplicate name
            return False
        except sqlite3.Error as error:
            print(f"Database error: {error}")
            return False

    def update_watchlist(self, old_name: str, new_name: str, comment: str = "",
                         object_color: str = DEFAULT_OBJECT_COLOR,
                         object_shape: str = DEFAULT_OBJECT_SHAPE) -> bool:
        """Rename a watchlist and update its comment and object style."""
        def op(cursor, conn):
            cursor.execute(
                "UPDATE watchlists SET name = ?, comment = ?, object_color = ?, object_shape = ? "
                "WHERE name = ?",
                (new_name.strip().upper(), comment.strip(), object_color, object_shape,
                 old_name.strip().upper())
            )
            conn.commit()
            return cursor.rowcount > 0
        return self._execute(op, False)

    def remove_watchlist(self, name: str) -> bool:
        """Remove a watchlist and its member list. qrz contacts are untouched."""
        def op(cursor, conn):
            cursor.execute(
                "DELETE FROM watchlistMembers WHERE watchlist_id = "
                "(SELECT id FROM watchlists WHERE name = ?)",
                (name.strip().upper(),)
            )
            cursor.execute("DELETE FROM watchlists WHERE name = ?", (name.strip().upper(),))
            conn.commit()
            return cursor.rowcount > 0
        return self._execute(op, False)

    def get_watchlist_id(self, name: str) -> Optional[int]:
        """Get a watchlist's id by name, or None if not found."""
        def op(cursor, conn):
            cursor.execute("SELECT id FROM watchlists WHERE name = ?", (name.strip().upper(),))
            row = cursor.fetchone()
            return row[0] if row else None
        return self._execute(op, None)

    def get_watchlist_member_counts(self) -> Dict[str, int]:
        """Get member counts keyed by watchlist name. Empty watchlists count 0."""
        def op(cursor, conn):
            cursor.execute(
                "SELECT w.name, COUNT(wm.id) FROM watchlists w "
                "LEFT JOIN watchlistMembers wm ON wm.watchlist_id = w.id "
                "GROUP BY w.id"
            )
            return {row[0]: row[1] for row in cursor.fetchall()}
        return self._execute(op, {})

    def get_watchlist_active_member_counts(self) -> Dict[str, int]:
        """Get active-member counts keyed by watchlist name. Active reflects
        the qrz contact's own flag (qrz.active), shared across every
        watchlist a contact belongs to. Empty or fully-inactive watchlists
        count 0."""
        def op(cursor, conn):
            cursor.execute(
                "SELECT w.name, COUNT(q.id) FROM watchlists w "
                "LEFT JOIN watchlistMembers wm ON wm.watchlist_id = w.id "
                "LEFT JOIN qrz q ON q.id = wm.member_id AND q.active = 1 "
                "GROUP BY w.id"
            )
            return {row[0]: row[1] for row in cursor.fetchall()}
        return self._execute(op, {})

    def get_watchlist_members(self, watchlist_id: int) -> List[Dict]:
        """Get a watchlist's members joined with their qrz contact details.
        The grid column shows the operator's grid_override when one is set.
        Active is the qrz contact's own flag (qrz.active) — shared across
        every watchlist the contact belongs to, not per-membership."""
        def op(cursor, conn):
            cursor.execute(
                "SELECT q.id, q.callsign, q.name, q.city, q.state, "
                "COALESCE(NULLIF(q.grid_override, ''), q.grid), q.active "
                "FROM watchlistMembers wm JOIN qrz q ON q.id = wm.member_id "
                "WHERE wm.watchlist_id = ? ORDER BY q.callsign",
                (watchlist_id,)
            )
            return [
                {
                    "id": row[0],
                    "callsign": row[1] or "",
                    "name": row[2] or "",
                    "city": row[3] or "",
                    "state": row[4] or "",
                    "grid": row[5] or "",
                    "active": bool(row[6]),
                }
                for row in cursor.fetchall()
            ]
        return self._execute(op, [])

    def add_watchlist_member(self, watchlist_id: int, member_id: int) -> bool:
        """Add a qrz contact to a watchlist. Returns False if already a member."""
        try:
            with sqlite3.connect(self.db_path, timeout=10) as connection:
                cursor = connection.cursor()
                cursor.execute(
                    "INSERT INTO watchlistMembers (watchlist_id, member_id) VALUES (?, ?)",
                    (watchlist_id, member_id)
                )
                connection.commit()
                return True
        except sqlite3.IntegrityError:
            # Already a member
            return False
        except sqlite3.Error as error:
            print(f"Database error: {error}")
            return False

    def remove_watchlist_member(self, watchlist_id: int, member_id: int) -> bool:
        """Remove a qrz contact from a watchlist. The qrz row itself is untouched."""
        def op(cursor, conn):
            cursor.execute(
                "DELETE FROM watchlistMembers WHERE watchlist_id = ? AND member_id = ?",
                (watchlist_id, member_id)
            )
            conn.commit()
            return cursor.rowcount > 0
        return self._execute(op, False)

    def set_watchlist_member_active(self, member_id: int, active: bool) -> bool:
        """Set a qrz contact's Active flag — controls whether they show on the
        map when any watchlist they belong to is checked under Watchlist
        Overlay. Lives on the qrz row (member_id == qrz.id), not per
        watchlist membership, so it's shared across every watchlist the
        contact is on."""
        def op(cursor, conn):
            cursor.execute(
                "UPDATE qrz SET active = ? WHERE id = ?",
                (1 if active else 0, member_id)
            )
            conn.commit()
            return cursor.rowcount > 0
        return self._execute(op, False)

    def set_qrz_grid_override(self, callsign: str, grid: str) -> bool:
        """Set (or clear, with '') a contact's grid override. Lives in its own
        qrz column so QRZ.com API refreshes never overwrite it — same
        mechanism as the memo column."""
        def op(cursor, conn):
            cursor.execute(
                "UPDATE qrz SET grid_override = ? WHERE callsign = ?",
                (grid.strip(), callsign.strip().upper())
            )
            conn.commit()
            return cursor.rowcount > 0
        return self._execute(op, False)

    def get_qrz_id(self, callsign: str) -> Optional[int]:
        """Get a qrz row id by callsign, or None if not cached."""
        def op(cursor, conn):
            cursor.execute("SELECT id FROM qrz WHERE callsign = ?", (callsign.strip().upper(),))
            row = cursor.fetchone()
            return row[0] if row else None
        return self._execute(op, None)

    def qrz_type_exists(self, qrz_type: int) -> bool:
        """True if any qrz contact carries the given type. Each 1-click
        preset dataset marks its rows with a distinct type, so this detects
        an installed preset even after its watchlist is renamed."""
        def op(cursor, conn):
            cursor.execute("SELECT COUNT(*) FROM qrz WHERE type = ?", (qrz_type,))
            return cursor.fetchone()[0] > 0
        return self._execute(op, False)

    def add_qrz_manual(self, callsign: str, name: str = "", city: str = "",
                       state: str = "", grid: str = "") -> Optional[int]:
        """Insert a manually entered contact into the qrz table.

        Normalization matches QRZClient._save_to_cache. Returns the row id —
        the existing row's id if the callsign is already cached.
        """
        cs = callsign.strip().upper()
        if not cs:
            return None
        def op(cursor, conn):
            try:
                cursor.execute(
                    "INSERT INTO qrz (callsign, active, name, city, state, grid) VALUES (?, 1, ?, ?, ?, ?)",
                    (cs, name.strip().title(), city.strip().title(),
                     state.strip().upper(), grid.strip())
                )
                conn.commit()
                return cursor.lastrowid
            except sqlite3.IntegrityError:
                cursor.execute("SELECT id FROM qrz WHERE callsign = ?", (cs,))
                row = cursor.fetchone()
                return row[0] if row else None
        return self._execute(op, None)

    def get_watchlist_pins(self, watchlist_names: List[str]) -> List[Tuple]:
        """Get (callsign, lat, lon, grid, grid_override, object_color, object_shape)
        for the active members of the named watchlists. A callsign on two shown
        watchlists with different object styles returns one row per style. The
        joins drop members whose watchlist or qrz contact was deleted, and
        inactive contacts (q.active = 0, a per-contact flag shared across every
        watchlist they belong to) are excluded from the overlay."""
        if not watchlist_names:
            return []
        def op(cursor, conn):
            placeholders = ",".join("?" * len(watchlist_names))
            cursor.execute(
                "SELECT DISTINCT q.callsign, q.lat, q.lon, q.grid, q.grid_override, "
                "w.object_color, w.object_shape "
                "FROM watchlistMembers wm "
                "JOIN qrz q ON q.id = wm.member_id "
                f"JOIN watchlists w ON w.id = wm.watchlist_id AND w.name IN ({placeholders}) "
                "WHERE q.active = 1",
                watchlist_names
            )
            return cursor.fetchall()
        return self._execute(op, [])

    def get_abbreviations(self) -> Dict[str, str]:
        """Get all abbreviations from database as a dictionary."""
        def op(cursor, conn):
            cursor.execute("SELECT abbrev, expansion FROM abbreviations ORDER BY abbrev")
            return {row[0]: row[1] for row in cursor.fetchall()}
        return self._execute(op, {})

    def add_abbreviation(self, abbrev: str, expansion: str) -> bool:
        """Add or update an abbreviation. Returns True if successful."""
        abbrev = abbrev.strip().upper()
        expansion = expansion.strip()
        if not abbrev or not expansion:
            return False
        def op(cursor, conn):
            cursor.execute(
                "INSERT OR REPLACE INTO abbreviations (abbrev, expansion) VALUES (?, ?)",
                (abbrev, expansion)
            )
            conn.commit()
            return cursor.rowcount > 0
        return self._execute(op, False)

    def remove_abbreviation(self, abbrev: str) -> bool:
        """Remove an abbreviation. Returns True if successful."""
        def op(cursor, conn):
            cursor.execute("DELETE FROM abbreviations WHERE abbrev = ?", (abbrev.upper(),))
            conn.commit()
            return cursor.rowcount > 0
        return self._execute(op, False)

    def get_qrz_settings(self) -> Tuple[str, str, bool]:
        """Get QRZ settings from database. Returns (username, password, is_active)."""
        def op(cursor, conn):
            cursor.execute("SELECT username, password, is_active FROM qrz_settings WHERE id = 1")
            result = cursor.fetchone()
            if result:
                return (result[0] or "", result[1] or "", bool(result[2]))
            return ("", "", False)
        return self._execute(op, ("", "", False))

    def set_qrz_settings(self, username: str, password: str, is_active: bool) -> bool:
        """Save QRZ settings to database."""
        def op(cursor, conn):
            cursor.execute(
                "UPDATE qrz_settings SET username = ?, password = ?, is_active = ? WHERE id = 1",
                (username, password, 1 if is_active else 0)
            )
            if cursor.rowcount == 0:
                cursor.execute(
                    "INSERT INTO qrz_settings (id, username, password, is_active) VALUES (1, ?, ?, ?)",
                    (username, password, 1 if is_active else 0)
                )
            conn.commit()
            return True
        return self._execute(op, False)

    def get_user_settings(self) -> Tuple[str, str, str]:
        """Get user callsign, grid square, and state from controls table."""
        def op(cursor, conn):
            cursor.execute("SELECT callsign, gridsquare, state FROM controls WHERE id = 1")
            row = cursor.fetchone()
            return (row[0] or "", row[1] or "", row[2] or "") if row else ("", "", "")
        return self._execute(op, ("", "", ""))

    def get_qrz_callsigns(self) -> set:
        """Return set of all callsigns cached in the qrz table."""
        def op(cursor, conn):
            cursor.execute("SELECT callsign FROM qrz")
            return {row[0].upper() for row in cursor.fetchall()}
        return self._execute(op, set())

    def get_qrz_contacts(self) -> list:
        """Return all QRZ cached contacts ordered by most recent first."""
        def op(cursor, conn):
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT callsign, name, address, city, state, zip, country, grid, "
                "class, email, image, memo, insert_date FROM qrz WHERE type = 1 "
                "ORDER BY insert_date DESC"
            )
            return cursor.fetchall()
        return self._execute(op, [])

    def get_statrep_export_data(self) -> Tuple[List[str], List[Tuple]]:
        """Return (column_names, rows) for every statrep row, for CSV export.
        Excludes the memo column."""
        def op(cursor, conn):
            cursor.execute("SELECT * FROM statrep ORDER BY datetime DESC")
            columns = [d[0] for d in cursor.description]
            keep_indices = [i for i, c in enumerate(columns) if c != "memo"]
            kept_columns = [columns[i] for i in keep_indices]
            rows = [tuple(row[i] for i in keep_indices) for row in cursor.fetchall()]
            return kept_columns, rows
        return self._execute(op, ([], []))

    def get_qrz_export_data(self) -> Tuple[List[str], List[Tuple]]:
        """Return (column_names, rows) for every qrz row, for CSV export.

        Excludes QRZ_EXPORT_EXCLUDED_COLUMNS. Only includes type = 1 rows
        with a non-null lat and lon.
        """
        def op(cursor, conn):
            cursor.execute("SELECT * FROM qrz WHERE type = 1 AND lat IS NOT NULL AND lon IS NOT NULL")
            columns = [d[0] for d in cursor.description]
            keep_indices = [i for i, c in enumerate(columns) if c not in QRZ_EXPORT_EXCLUDED_COLUMNS]
            kept_columns = [columns[i] for i in keep_indices]
            rows = [tuple(row[i] for i in keep_indices) for row in cursor.fetchall()]
            return kept_columns, rows
        return self._execute(op, ([], []))

    def get_watchlist_members_export_data(self) -> Tuple[List[str], List[Tuple]]:
        """Return (column_names, rows) for every watchlist member, for CSV export.

        Same qrz columns as get_qrz_export_data (also dropping id), prefixed
        with the owning watchlist's name and a Yes/No flag from qrz.active
        (per-contact, shared across every watchlist). The grid column uses
        grid_override when set, falling back to grid otherwise — same
        precedence as the map overlay. Only includes watchlists where
        preset = 0.
        """
        excluded = QRZ_EXPORT_EXCLUDED_COLUMNS | {"id"}

        def op(cursor, conn):
            cursor.execute("SELECT * FROM qrz LIMIT 0")
            qrz_columns = [d[0] for d in cursor.description]
            kept_qrz_columns = [c for c in qrz_columns if c not in excluded]
            select_cols = ", ".join(
                "COALESCE(NULLIF(q.grid_override, ''), q.grid) AS grid" if c == "grid" else f"q.{c}"
                for c in kept_qrz_columns
            )
            cursor.execute(
                f"SELECT w.name, q.active, {select_cols} "
                "FROM watchlistMembers wm "
                "JOIN watchlists w ON w.id = wm.watchlist_id "
                "JOIN qrz q ON q.id = wm.member_id "
                "WHERE w.preset = 0 "
                "ORDER BY w.name, q.callsign"
            )
            columns = ["Watchlist", "Active"] + kept_qrz_columns
            rows = [
                (row[0], "Yes" if row[1] else "No") + tuple(row[2:])
                for row in cursor.fetchall()
            ]
            return columns, rows
        return self._execute(op, ([], []))

    def get_watchlist_callsigns_export_data(self) -> List[Tuple[str, List[str]]]:
        """Return [(watchlist_name, [callsigns])] for every watchlist that has
        members, for the Watchlist Callsigns bulk-import export. Only
        includes watchlists where preset = 0."""
        def op(cursor, conn):
            cursor.execute(
                "SELECT w.name, q.callsign "
                "FROM watchlistMembers wm "
                "JOIN watchlists w ON w.id = wm.watchlist_id "
                "JOIN qrz q ON q.id = wm.member_id "
                "WHERE w.preset = 0 "
                "ORDER BY w.name, q.callsign"
            )
            grouped: Dict[str, List[str]] = {}
            order: List[str] = []
            for name, callsign in cursor.fetchall():
                if name not in grouped:
                    grouped[name] = []
                    order.append(name)
                grouped[name].append(callsign)
            return [(name, grouped[name]) for name in order]
        return self._execute(op, [])

    def delete_qrz_contact(self, callsign: str) -> bool:
        """Delete a single QRZ cached contact by callsign."""
        def op(cursor, conn):
            try:
                cursor.execute(
                    "DELETE FROM watchlistMembers WHERE member_id = "
                    "(SELECT id FROM qrz WHERE callsign = ?)",
                    (callsign,)
                )
            except sqlite3.Error:
                # watchlistMembers table may not exist yet; the contact delete
                # must keep working regardless.
                pass
            cursor.execute("DELETE FROM qrz WHERE callsign = ?", (callsign,))
            conn.commit()
            return cursor.rowcount > 0
        return self._execute(op, False)

    def set_user_settings(self, callsign: str, grid: str, state: str) -> bool:
        """Save user callsign, grid square, and state to controls table."""
        def op(cursor, conn):
            cursor.execute(
                "UPDATE controls SET callsign = ?, gridsquare = ?, state = ? WHERE id = 1",
                (callsign, grid, state)
            )
            conn.commit()
            return cursor.rowcount > 0
        return self._execute(op, False)

    def get_default_map(self) -> str:
        """Get user's preferred startup map region; falls back to 'us'."""
        def op(cursor, conn):
            try:
                cursor.execute("SELECT default_map FROM controls WHERE id = 1")
                row = cursor.fetchone()
                return (row[0] or "us") if row else "us"
            except sqlite3.OperationalError:
                return "us"
        return self._execute(op, "us")

    def set_default_map(self, region: str) -> bool:
        """Save user's preferred startup map region."""
        def op(cursor, conn):
            try:
                cursor.execute(
                    "UPDATE controls SET default_map = ? WHERE id = 1",
                    (region,)
                )
                conn.commit()
                return cursor.rowcount > 0
            except sqlite3.OperationalError:
                return False
        return self._execute(op, False)

    def get_notify_enabled(self) -> bool:
        """Get whether message notifications (new-message, delivery-
        confirmation, and expired popups) are enabled; defaults to True."""
        def op(cursor, conn):
            try:
                cursor.execute("SELECT mssgNotify FROM controls WHERE id = 1")
                row = cursor.fetchone()
                return bool(row[0]) if row and row[0] is not None else True
            except sqlite3.OperationalError:
                return True
        return self._execute(op, True)

    def set_notify_enabled(self, enabled: bool) -> bool:
        """Save whether message notifications (new-message, delivery-
        confirmation, and expired popups) are enabled."""
        def op(cursor, conn):
            try:
                cursor.execute(
                    "UPDATE controls SET mssgNotify = ? WHERE id = 1",
                    (1 if enabled else 0,)
                )
                conn.commit()
                return cursor.rowcount > 0
            except sqlite3.OperationalError:
                return False
        return self._execute(op, False)

    def get_map_filters(self) -> List[Tuple[str, str, str, str, str, str]]:
        """All saved Custom Filtering criteria sets, ordered by name.

        Returns (name, from_call, to_call, grid, remarks, mode) tuples. The
        OperationalError guard keeps the filter bar usable on a database that
        predates the map_filters table - same approach as get_default_map.
        """
        def op(cursor, conn):
            try:
                cursor.execute(
                    "SELECT name, from_call, to_call, grid, remarks, mode "
                    "FROM map_filters ORDER BY name COLLATE NOCASE"
                )
                return [
                    (r[0], r[1] or "", r[2] or "", r[3] or "", r[4] or "", r[5] or "or")
                    for r in cursor.fetchall()
                ]
            except sqlite3.OperationalError:
                return []
        return self._execute(op, [])

    def save_map_filter(self, name: str, from_call: str, to_call: str,
                        grid: str, remarks: str, mode: str) -> bool:
        """Insert or replace a saved Custom Filtering criteria set by name."""
        def op(cursor, conn):
            try:
                cursor.execute(
                    "UPDATE map_filters SET from_call = ?, to_call = ?,"
                    " grid = ?, remarks = ?, mode = ? WHERE name = ?",
                    (from_call, to_call, grid, remarks, mode, name)
                )
                if cursor.rowcount == 0:
                    cursor.execute(
                        "INSERT INTO map_filters"
                        " (name, from_call, to_call, grid, remarks, mode, date_added)"
                        " VALUES (?, ?, ?, ?, ?, ?, datetime('now'))",
                        (name, from_call, to_call, grid, remarks, mode)
                    )
                conn.commit()
                return True
            except sqlite3.OperationalError:
                return False
        return self._execute(op, False)

    def delete_map_filter(self, name: str) -> bool:
        """Delete a saved Custom Filtering criteria set by name."""
        def op(cursor, conn):
            try:
                cursor.execute("DELETE FROM map_filters WHERE name = ?", (name,))
                conn.commit()
                return cursor.rowcount > 0
            except sqlite3.OperationalError:
                return False
        return self._execute(op, False)

    def set_qrz_active(self, is_active: bool) -> bool:
        """Toggle QRZ active status."""
        def op(cursor, conn):
            cursor.execute(
                "UPDATE qrz_settings SET is_active = ? WHERE id = 1",
                (1 if is_active else 0,)
            )
            conn.commit()
            return cursor.rowcount > 0
        return self._execute(op, False)

    def get_alert_count(self) -> int:
        """Get the total number of alerts in the database."""
        def op(cursor, conn):
            cursor.execute("SELECT COUNT(*) FROM alerts")
            result = cursor.fetchone()
            return result[0] if result else 0
        return self._execute(op, 0)

    def delete_alert_at_offset(self, offset: int) -> bool:
        """Delete the alert at the specified offset from most recent."""
        def op(cursor, conn):
            cursor.execute(
                "DELETE FROM alerts WHERE id = ("
                "SELECT id FROM alerts ORDER BY datetime DESC LIMIT 1 OFFSET ?)",
                (offset,)
            )
            conn.commit()
            return cursor.rowcount > 0
        return self._execute(op, False)

    def get_alert_at_offset(self, offset: int) -> Optional[Tuple[str, str, int, str, str, str]]:
        """Get an alert at the specified offset from most recent.

        Args:
            offset: 0 for most recent, 1 for second most recent, etc.

        Returns:
            Tuple of (title, message, color, datetime, from_callsign, group) or None.
        """
        def op(cursor, conn):
            cursor.execute(
                "SELECT title, message, color, datetime, from_callsign, target "
                "FROM alerts ORDER BY datetime DESC LIMIT 1 OFFSET ?",
                (offset,)
            )
            result = cursor.fetchone()
            if result:
                return (result[0], result[1], result[2], result[3], result[4] or "", result[5] or "")
            return None
        return self._execute(op, None)

    def get_video_count(self) -> int:
        """Get the total number of rows in the videos table."""
        def op(cursor, conn):
            cursor.execute("SELECT COUNT(*) FROM videos")
            result = cursor.fetchone()
            return result[0] if result else 0
        return self._execute(op, 0)

    def get_video_at_offset(self, offset: int) -> Optional[Tuple[int, str, str, str, str]]:
        """Get (id, url, title, datetime, from_callsign) of the video row at
        the given offset from most recent (0 = latest), or None."""
        def op(cursor, conn):
            cursor.execute(
                "SELECT id, url, title, datetime, from_callsign "
                "FROM videos ORDER BY datetime DESC LIMIT 1 OFFSET ?",
                (offset,)
            )
            result = cursor.fetchone()
            if result:
                return (result[0], result[1], result[2] or "", result[3] or "", result[4] or "")
            return None
        return self._execute(op, None)

    def get_all_videos(self) -> List[Tuple[int, str, str, str, str, str]]:
        """Get all video rows (id, title, from_callsign, target, date, url),
        oldest first."""
        def op(cursor, conn):
            cursor.execute(
                "SELECT id, title, from_callsign, target, date, url "
                "FROM videos ORDER BY datetime ASC"
            )
            return [(r[0], r[1] or "", r[2] or "", r[3] or "", r[4] or "", r[5] or "")
                    for r in cursor.fetchall()]
        return self._execute(op, [])

    def delete_video(self, video_id: int) -> None:
        """Delete a video row by id."""
        def op(cursor, conn):
            cursor.execute("DELETE FROM videos WHERE id = ?", (video_id,))
            conn.commit()
        self._execute(op)

    def mark_video_played(self, video_id: int) -> None:
        """Mark a video row as played."""
        def op(cursor, conn):
            cursor.execute("UPDATE videos SET played = 1 WHERE id = ?", (video_id,))
            conn.commit()
        self._execute(op)

    def has_unplayed_video(self) -> bool:
        """True if any video row has played = 0."""
        def op(cursor, conn):
            cursor.execute("SELECT 1 FROM videos WHERE played = 0 LIMIT 1")
            return cursor.fetchone() is not None
        return self._execute(op, False)

    _RECORD_TABLES = {"alerts", "messages", "statrep", "videos"}

    def get_table_stats(self, table: str) -> Tuple[int, str]:
        """Return (row_count, oldest_datetime_or_empty) for one of the four
        CommStat record tables (alerts, messages, statrep, videos)."""
        if table not in self._RECORD_TABLES:
            return (0, "")
        def op(cursor, conn):
            cursor.execute(f"SELECT COUNT(*), MIN(datetime) FROM {table}")
            count, oldest = cursor.fetchone()
            return (count or 0, oldest or "")
        return self._execute(op, (0, ""))

    def delete_rows_older_than(self, table: str, days: int) -> int:
        """Delete rows in one of the four CommStat record tables whose
        datetime is older than `days` days (UTC). days=0 deletes all rows.
        Returns rows deleted."""
        if table not in self._RECORD_TABLES or days < 0:
            return 0
        threshold = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        def op(cursor, conn):
            cursor.execute(f"DELETE FROM {table} WHERE datetime < ?", (threshold,))
            conn.commit()
            return cursor.rowcount
        return self._execute(op, 0)


# =============================================================================
# MainWindow - Main application window
class _MenuBarMenu(QtWidgets.QMenu):
    """QMenu anchored to the bottom edge of its parent QMenuBar.

    On macOS/Windows Qt already places the popup just below the menu bar,
    so this is a no-op. On some Linux setups Qt places the popup overlapping
    the menu bar — only then do we shift it down to the menu bar's bottom.
    """

    def showEvent(self, event):
        parent = self.parent()
        if isinstance(parent, QtWidgets.QMenuBar):
            target_y = parent.mapToGlobal(QtCore.QPoint(0, parent.height())).y()
            if self.y() < target_y:
                self.move(self.x(), target_y)
        super().showEvent(event)


class _PersistentCheckMenu(_MenuBarMenu):
    """Menu bar dropdown variant that stays open when a checkable item (group
    filters, Hide Green Pins, Weather Radar, Watchlist Overlay entries, etc.)
    is clicked, so several can be toggled without reopening the menu each
    time. Non-checkable actions (e.g. the date-range presets) still close the
    menu as normal. Used by the Filter, Map, and Settings menus."""

    def mouseReleaseEvent(self, event):
        action = self.activeAction()
        if action is not None and action.isCheckable() and action.isEnabled():
            action.trigger()
            event.accept()
            return
        super().mouseReleaseEvent(event)


# =============================================================================

class SoundPlayer:
    """
    Plays short notification sounds for inbound StatRep / Message / Alert events.

    Per-event filename and enable flag live in ConfigManager; the dialog can
    call reload(event) to swap files at runtime without restarting the app.
    QSoundEffect instances are held on the instance so Qt's async playback
    doesn't get GC'd mid-play. Missing files are tolerated silently.
    """

    EVENTS = ("statrep", "message", "alert")
    VOLUME = 0.8

    def __init__(self, config: "ConfigManager", sounds_dir: str = SOUNDS_DIR):
        self.config = config
        self.sounds_dir = Path(sounds_dir)
        self._effects: Dict[str, QSoundEffect] = {}
        self._preview: Optional[QSoundEffect] = None
        self._preview_proc: Optional[subprocess.Popen] = None
        self.reload_all()

    def reload(self, event: str) -> None:
        if not self.config.get_sound_enabled(event):
            self._effects.pop(event, None)
            return
        filename = self.config.get_sound_file(event)
        if not filename:
            self._effects.pop(event, None)
            return
        path = self.sounds_dir / filename
        if not path.exists():
            self._effects.pop(event, None)
            return
        effect = QSoundEffect()
        effect.setSource(QUrl.fromLocalFile(str(path.resolve())))
        effect.setVolume(self.VOLUME)
        self._effects[event] = effect

    def reload_all(self) -> None:
        for event in self.EVENTS:
            self.reload(event)

    def play(self, msg_type: str) -> None:
        if not self.config.get_sound_enabled(msg_type):
            return
        effect = self._effects.get(msg_type)
        if effect is not None:
            effect.play()

    def preview(self, filename: str) -> None:
        # Ignores the per-event enabled flag — used by the Sound Settings dialog
        # Play button so the user can audition a clip before turning it on.
        if not filename:
            return
        path = self.sounds_dir / filename
        if not path.exists():
            return
        abs_path = str(path.resolve())
        if sys.platform == "darwin":
            # QSoundEffect's CoreAudioOutput backend silently drops play()
            # calls made from a statusChanged handler (re-entrancy issue on
            # macOS). afplay is simpler and confirmed reliable.
            if self._preview_proc is not None:
                self._preview_proc.terminate()
                self._preview_proc = None
            self._preview_proc = subprocess.Popen(
                ["/usr/bin/afplay", abs_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            effect = QSoundEffect()
            effect.setVolume(self.VOLUME)
            self._preview = effect
            effect.statusChanged.connect(
                lambda: effect.play() if effect.status() == QSoundEffect.Ready else None
            )
            effect.setSource(QUrl.fromLocalFile(abs_path))


class MainWindow(QtWidgets.QMainWindow):
    """Main application window for CommStat."""

    # Collapse a burst of inbound events into a single notification sound.
    _SOUND_DEBOUNCE_MS = 300

    def __init__(self, config: ConfigManager, db: DatabaseManager):
        """
        Initialize the main window.

        Args:
            config: ConfigManager instance with loaded settings
            db: DatabaseManager instance for database operations
        """
        super().__init__()
        self.config = config
        self.db = db

        self._sound_player = SoundPlayer(config)
        self._pending_sound_type: Optional[str] = None
        self._sound_debounce_timer = QTimer(self)
        self._sound_debounce_timer.setSingleShot(True)
        self._sound_debounce_timer.timeout.connect(self._play_pending_sound)

        # Internet connectivity state. Probed in the background once the
        # window is visible (see _load_initial_data/_start_internet_check)
        # — the previous synchronous check here blocked the window from
        # appearing for up to 3s on a slow-DNS day.
        self._internet_available = False

        # Initialize JS8Call connector manager and TCP connection pool
        self.connector_manager = ConnectorManager()
        self.connector_manager.init_connectors_table()
        self.tcp_pool = TCPConnectionPool(self.connector_manager, self)
        self.tcp_pool.any_message_received.connect(self._handle_tcp_message)
        self.tcp_pool.any_connection_changed.connect(self._handle_connection_changed)
        self.tcp_pool.any_status_message.connect(self._handle_status_message)
        self.tcp_pool.any_callsign_received.connect(self._handle_callsign_received)
        self.tcp_pool.any_grid_received.connect(self._handle_grid_received)

        # Store station info by rig name (persists even if connection is lost)
        self.rig_callsigns: Dict[str, str] = {}
        self.rig_grids: Dict[str, str] = {}
        self.rig_states: Dict[str, str] = {}
        self.rig_status_logged: Set[str] = set()  # Track which rigs have logged initial status

        # Live feed message buffer (stores messages from all TCP connections)
        self.feed_messages: List[str] = []
        self.max_feed_messages = 500  # Limit buffer size
        self._hide_live_feed: bool = self.config.get_hide_live_feed()
        self._hide_internet_statrep: bool = self.config.get_hide_internet_feed()
        self._hide_green_pins: bool = False         # Session-only; resets on restart
        self._hide_all_pins: bool = False           # Session-only; resets on restart
        self._map_filter_state: str = "off"         # Session-only; resets on restart. One of: off, map, custom
        self._map_bounds: Optional[Tuple[float, float, float, float]] = None  # (south, west, north, east); only set while _map_filter_state == "map"
        self._custom_filter_mode: str = "or"         # Session-only; joins the Custom Filtering boxes. One of: and, or
        self._message_filter_active: bool = False    # Session-only; message table filter row toggle
        self._message_filter_values: Dict[int, str] = {}  # Session-only; per-column filter text for message_table

        # Run startup status checks and initiate TCP connections.
        # Order: User Settings -> Groups -> JS8 Connectors -> QRZ Settings.
        self._log_startup_status()

        # Map state
        self.map_loaded = False
        self._last_map_region = self.db.get_default_map()
        self._current_view_mode: str = ""
        self._region_pin_counts: Dict[str, int] = {"us": 0, "eu": 0, "mideast": 0, "seasia": 0, "world": 0}

        self._setup_window()
        self._setup_ui()
        self._sync_weather_radar_action()
        self._setup_radar_refresh_timer()
        QTimer.singleShot(0, self._apply_restored_view)
        # Scrollbar visibility (which the FILTER labels' x position accounts
        # for) isn't settled until the window is actually shown and laid
        # out, so do one final reposition once that's happened.
        QTimer.singleShot(0, self._reposition_message_filter_btn)
        QTimer.singleShot(0, self._reposition_statrep_filter_btn)

    def _setup_window(self) -> None:
        """Configure window properties (size, title, icon)."""
        self.setObjectName("MainWindow")
        self.setWindowTitle(WINDOW_TITLE)
        self.resize(*WINDOW_SIZE)

        # Restore window geometry from config
        self._restore_window_position()

        # Cmd+Q on macOS may bypass closeEvent — save on aboutToQuit too.
        qApp.aboutToQuit.connect(self._save_window_position)

        # Set window icon
        icon_path = Path(ICON_FILE)
        if icon_path.exists():
            icon = QtGui.QIcon()
            icon.addPixmap(QtGui.QPixmap(str(icon_path)), QtGui.QIcon.Normal, QtGui.QIcon.Off)
            self.setWindowIcon(icon)

    def _restore_window_position(self) -> None:
        """Restore window geometry from config.ini."""
        config = ConfigParser()
        if not os.path.exists(CONFIG_FILE):
            return

        config.read(CONFIG_FILE)
        if not config.has_section("WINDOW"):
            return

        # Stashed for _apply_restored_view(), which runs once the map/message/
        # contacts/video widgets that _set_map_view_mode() depends on actually
        # exist - none of them are built yet at this point in __init__.
        self._pending_last_view = config.get("WINDOW", "last_view", fallback="") or None
        try:
            self._pending_pane_height = config.getint("WINDOW", "map_pane_height", fallback=None)
            self._pending_pane_width = config.getint("WINDOW", "map_pane_width", fallback=None)
        except (ValueError, TypeError):
            self._pending_pane_height = None
            self._pending_pane_width = None

        try:
            x = config.getint("WINDOW", "x", fallback=None)
            y = config.getint("WINDOW", "y", fallback=None)
            width = config.getint("WINDOW", "width", fallback=None)
            height = config.getint("WINDOW", "height", fallback=None)
        except (ValueError, TypeError):
            return

        if width is not None and height is not None:
            self.resize(width, height)
        if x is None or y is None:
            return

        # move(x, y) before show() places the visible frame's top-left corner
        # at exactly (x, y) on Windows/DWM, and pos() reports that same point
        # while the native window is alive - so saving pos() and restoring it
        # with move() is a stable fixed point with no frame-margin fudge.
        # (The old margin-subtracting compensation here was over-correcting;
        # the real drift came from _save_window_position reading pos() after
        # the native window was already destroyed - see closeEvent.)
        self.move(x, y)

        # Some window managers ignore a pre-show move; re-apply once the
        # window is up if it didn't land.
        target = QtCore.QPoint(x, y)

        def _verify() -> None:
            if self.isVisible() and self.pos() != target:
                self.move(target)

        QTimer.singleShot(0, _verify)

    def closeEvent(self, event) -> None:
        """Clean up resources and save window position before closing."""
        # Stop all timers
        if hasattr(self, 'clock_timer'):
            self.clock_timer.stop()
        if hasattr(self, 'slideshow_timer'):
            self.slideshow_timer.stop()
        if hasattr(self, 'internet_timer'):
            self.internet_timer.stop()
        if hasattr(self, 'commsrvr_timer'):
            self.commsrvr_timer.stop()

        # Disconnect all TCP connections gracefully
        if hasattr(self, 'tcp_pool'):
            print("Closing TCP connections...")
            self.tcp_pool.disconnect_all()

        # Save window position
        self._save_window_position()
        event.accept()

    def _save_window_position(self) -> None:
        """Save window geometry to config.ini."""
        # closeEvent already saved, and by the time aboutToQuit fires after a
        # native close the platform window is gone: pos() then reports the
        # client-area origin instead of the frame origin (one title bar lower),
        # which is what made the position creep between launches.
        if getattr(self, "_window_position_saved", False):
            return
        self._window_position_saved = True

        config = ConfigParser()
        if os.path.exists(CONFIG_FILE):
            config.read(CONFIG_FILE)

        if not config.has_section("WINDOW"):
            config.add_section("WINDOW")

        # Drop any stale geometry blob from older versions.
        config.remove_option("WINDOW", "geometry")

        # Save pos(). _restore_window_position re-moves the window with the
        # frame-margin offset subtracted, so this value is a stable fixed
        # point across restarts.
        pos = self.pos()
        size = self.size()
        config.set("WINDOW", "x", str(pos.x()))
        config.set("WINDOW", "y", str(pos.y()))
        config.set("WINDOW", "width", str(size.width()))
        config.set("WINDOW", "height", str(size.height()))

        # Save which view was showing (map region / images / videos / alerts /
        # contacts) and the map pane's size, so relaunch can restore both.
        last_view = getattr(self, "_current_view_mode", "") or "us"
        config.set("WINDOW", "last_view", last_view)

        if hasattr(self, "bottom_splitter") and not self.bottom_splitter.isHidden():
            pane_height = self.bottom_splitter.height()
            pane_width = self.bottom_splitter.sizes()[0] if self.bottom_splitter.sizes() else 0
        else:
            # Contacts view hides bottom_splitter entirely; fall back to the
            # last known size (see _set_map_view_mode's "contacts" branch).
            pane_height = getattr(self, "_saved_map_pane_height", 0)
            pane_width = getattr(self, "_saved_map_pane_width", 0)
        if pane_height:
            config.set("WINDOW", "map_pane_height", str(pane_height))
        if pane_width:
            config.set("WINDOW", "map_pane_width", str(pane_width))

        try:
            with open(CONFIG_FILE, 'w') as f:
                config.write(f)
        except IOError as e:
            print(f"Warning: Could not save window position: {e}")

    def _setup_ui(self) -> None:
        """Build the user interface."""
        # Create central widget with background color
        self.central_widget = QtWidgets.QWidget(self)
        self.central_widget.setStyleSheet(
            f"background-color: {self.config.get_color('program_background')};"
        )
        self.setCentralWidget(self.central_widget)

        # Main layout
        self.main_layout = QtWidgets.QGridLayout(self.central_widget)
        self.main_layout.setObjectName("mainLayout")

        # Main grid: header at top, resizable content splitter below
        self.main_layout.setRowStretch(0, 0)  # Unused (was menu bar)
        self.main_layout.setRowStretch(1, 0)  # Header
        self.main_layout.setRowStretch(2, 1)  # Resizable content area
        self.main_layout.setColumnStretch(0, 1)
        self.main_layout.setColumnStretch(1, 1)

        # Setup components
        self._setup_menu()
        self._setup_header()
        self._setup_resizable_panes()
        self._setup_statrep_table()
        self._setup_live_feed()
        self._setup_map_widget()
        self._setup_message_table()
        self._setup_contacts_widget()
        self._setup_timers()

        # Single Ctrl+C shortcut for all tables — dispatches by focused viewport
        QtWidgets.QShortcut(QtGui.QKeySequence("Ctrl+C"), self).activated.connect(
            self._handle_copy_shortcut
        )

        # Populate the filter menu group checkboxes
        self._populate_filter_groups_menu()
        self._populate_watchlist_pins_menu()

        # Load initial data once the window is actually visible (see
        # _load_initial_data) instead of blocking __init__/show() on the
        # connectivity probe, DB queries, and full map render.
        QTimer.singleShot(0, self._load_initial_data)

    def _load_initial_data(self) -> None:
        """Run once the window is visible and the event loop has started:
        kick off the background connectivity probe, wire the QRZ cache
        notifier, and load StatRep/map/live-feed/message data.

        Deferred out of __init__ via the QTimer.singleShot(0, ...) at the
        end of _setup_ui() so the window shell appears immediately instead
        of waiting on a blocking socket probe and several DB queries plus a
        full folium map render.
        """
        self._start_internet_check()

        # Live-bold StatRep/Message rows when a QRZ API lookup writes to the
        # qrz table. Deferred here (unlike every other lazy dialog import,
        # which goes through _resolve_dialog_class on first use) only
        # because nothing needs it before the window is shown.
        from qrz_lookup import qrz_cache_notifier
        qrz_cache_notifier.record_written.connect(self._on_qrz_record_written)

        self._load_statrep_data()
        self._load_map()
        self._load_live_feed()
        self._load_message_data()

    def _setup_resizable_panes(self) -> None:
        """Create splitters so the operator can freely resize the main panes."""
        self.content_splitter = QtWidgets.QSplitter(Qt.Vertical, self.central_widget)
        self.content_splitter.setObjectName("contentSplitter")
        self.content_splitter.setChildrenCollapsible(False)
        self.content_splitter.setHandleWidth(8)

        self.bottom_splitter = QtWidgets.QSplitter(Qt.Horizontal, self.central_widget)
        self.bottom_splitter.setObjectName("bottomSplitter")
        self.bottom_splitter.setChildrenCollapsible(False)
        self.bottom_splitter.setHandleWidth(8)

        # Left side of the bottom splitter can show Map, Images, or Alerts.
        self.map_stack = QtWidgets.QStackedWidget(self.central_widget)
        self.map_stack.setObjectName("mapStack")
        self.map_stack.setMinimumSize(320, 180)

        # Refit images/alert text after the pane is resized. Debounced via
        # single-shot QTimer to avoid rescaling on every pixel of a drag.
        self._map_pane_resize_timer = QTimer(self)
        self._map_pane_resize_timer.setSingleShot(True)
        self._map_pane_resize_timer.timeout.connect(self._on_map_pane_resized)
        self.map_stack.installEventFilter(self)

        self.bottom_splitter.addWidget(self.map_stack)
        self.content_splitter.addWidget(self.bottom_splitter)

        self.main_layout.addWidget(self.content_splitter, 2, 0, 1, 2)

        # Widgets are created later by _setup_statrep_table(),
        # _setup_live_feed(), _setup_map_widget(), and _setup_message_table().
        # Do not load data here; those widgets do not exist yet.

        # Commsrvr check will start automatically after 30 seconds via timer

    def _start_internet_check(self) -> None:
        """Probe connectivity on a background thread so the up-to-3s worst
        case (see check_internet()'s own comment) never blocks the UI
        thread. Polls for completion via QTimer rather than a cross-thread
        signal, matching the pattern already used for the image-fetch
        dialog and _check_commsrvr_content_async — safe because the poll
        timer itself is always scheduled from the main thread.

        Called from _load_initial_data(), after _setup_ui() has already
        built the timers/menu actions _on_internet_check_complete() may
        need to touch on a False->True transition.
        """
        result: Dict[str, bool] = {}

        def worker() -> None:
            result['available'] = check_internet()

        threading.Thread(target=worker, daemon=True).start()

        def poll() -> None:
            if 'available' in result:
                self._on_internet_check_complete(result['available'])
            else:
                QTimer.singleShot(100, poll)

        QTimer.singleShot(100, poll)

    def _on_internet_check_complete(self, is_available: bool) -> None:
        """Handle the startup connectivity probe's result."""
        self._internet_available = is_available
        if is_available:
            print("Internet connectivity: Available")
            if hasattr(self, 'internet_timer'):
                self.internet_timer.stop()
            self._on_internet_became_available()
            # _load_map() may already have rendered offline-only if it ran
            # before this background probe resolved — refresh it once so
            # the online tile layer (and any enabled overlays) picks up
            # now that connectivity is confirmed. Unconditional here
            # (unlike _retry_internet_check's map refresh) because this
            # race is the common case at startup, not a rare
            # comes-back-online-30-minutes-later edge case.
            if self.map_loaded:
                self._save_map_position(callback=self._load_map)
        else:
            print("Internet connectivity: Not available (will retry in 30 minutes)")

    def _open_external_link(self, url: str, label: str = "This link") -> None:
        """Open an external URL in the OS browser, unless Off-Grid Mode is on."""
        if not netguard.guard(f'"{label}" link'):
            QtWidgets.QMessageBox.information(
                self, "Off-Grid Mode",
                f"\"{label}\" opens an external website and is disabled while Off-Grid Mode is on.\n\n"
                "Switch back to ONLINE in the header to use it."
            )
            return
        QDesktopServices.openUrl(QUrl(url))

    def _refresh_network_toggle_ui(self) -> None:
        """Sync the header switch's checked-state, label, and color to netguard's
        effective state (user preference AND real reachability)."""
        online = netguard.is_network_enabled()
        self.network_toggle_btn.blockSignals(True)
        self.network_toggle_btn.setChecked(online)
        self.network_toggle_btn.blockSignals(False)
        if online:
            self.network_toggle_btn.setText("\U0001F310  ONLINE")
            self.network_toggle_btn.setStyleSheet(
                "QPushButton { background-color: #28a745; color: #FFFFFF;"
                " border: none; border-radius: 4px; font-weight: bold; }"
                " QPushButton:hover { background-color: #218838; }"
            )
        elif netguard.get_user_enabled():
            # User hasn't forced Off-Grid — this is real connectivity loss.
            self.network_toggle_btn.setText("\U0001F4F5  OFF-GRID")
            self.network_toggle_btn.setStyleSheet(
                "QPushButton { background-color: #a03030; color: #FFFFFF;"
                " border: none; border-radius: 4px; font-weight: bold; }"
                " QPushButton:hover { background-color: #8a2a2a; }"
            )
        else:
            self.network_toggle_btn.setText("\U0001F4F5  OFF-GRID")
            self.network_toggle_btn.setStyleSheet(
                "QPushButton { background-color: #555555; color: #FFFFFF;"
                " border: none; border-radius: 4px; font-weight: bold; }"
                " QPushButton:hover { background-color: #444444; }"
            )

    def _on_netguard_state_changed(self, online: bool) -> None:
        """netguard listener: keep the header switch in sync whenever the
        effective state changes from anywhere — a manual click (handled
        directly by _set_network_mode too) or a real-connectivity probe
        finding the connection came back or dropped on its own."""
        self._refresh_network_toggle_ui()

    def _on_network_toggle_clicked(self, checked: bool) -> None:
        """Handle a click on the header Off-Grid/Online switch."""
        self._set_network_mode(checked)

    def _set_network_mode(self, online_enabled: bool) -> None:
        """
        Master switch: sets the user's intent in netguard, then immediately
        applies the resulting effective mode rather than waiting for the next
        timer tick. netguard's listener callback keeps the button's visual
        state in sync automatically, including cases where the user asks to
        go Online but real connectivity isn't there yet.
        """
        netguard.set_user_enabled(online_enabled)
        # Always resync explicitly: Qt already flipped the checkable button's
        # internal checked-state as part of delivering this very click, and
        # netguard's listener only fires on an actual effective-state change
        # (e.g. clicking Online while there's still no real internet leaves
        # the effective state at Off-Grid, so no listener notification would
        # otherwise fire to correct the button back).
        self._refresh_network_toggle_ui()

        if not online_enabled:
            # Going Off-Grid: force offline immediately and stop all
            # background network activity, even if it's mid-cycle.
            self._internet_available = False
            if hasattr(self, 'internet_timer'):
                self.internet_timer.stop()
            if hasattr(self, 'commsrvr_timer'):
                self.commsrvr_timer.stop()
            self.newsfeed_timer.stop() if hasattr(self, 'newsfeed_timer') else None
            self.newsfeed_pause_timer.stop() if hasattr(self, 'newsfeed_pause_timer') else None
            if self.config.get_selected_rss_feed() == "Disable":
                self.newsfeed_label.setText("      +++  News Feed Disabled  +++")
            else:
                self.newsfeed_label.setText("      +++  Off-Grid Mode — News Feed Disabled  +++")
            print("[Off-Grid Mode] Enabled — all network features disabled (heartbeat, RSS, map tiles, QRZ, INTERNET rig).")
            # Deliberately not touching the map here — losing the online
            # tile layer doesn't need a full regenerate/reload, and existing
            # pins stay visible fine as-is.
            return

        # Coming back Online: re-probe real connectivity and restart
        # everything that depends on it.
        self._internet_available = check_internet()
        if hasattr(self, 'internet_timer'):
            self.internet_timer.stop()
        if self._internet_available:
            print("Internet connectivity: Available")
            feed_name = self.config.get_selected_rss_feed()
            if feed_name == "Disable":
                self.newsfeed_label.setText("      +++  News Feed Disabled  +++")
            else:
                self._start_rss_fetch()

            def start_commsrvr_heartbeat():
                self._check_commsrvr()
                self.commsrvr_timer.start(HEARTBEAT_INTERVAL_MS)
            QTimer.singleShot(HEARTBEAT_DELAY_MS, start_commsrvr_heartbeat)

            # Give the WebEngine a moment before touching it again after
            # being idle through the off-grid period, rather than hitting
            # it immediately in this same click-handler call stack.
            QTimer.singleShot(1500, lambda: self._save_map_position(callback=self._load_map))
        else:
            print("Internet connectivity: Not available (will retry in 30 minutes)")
            self.newsfeed_label.setText("  No internet connection")
            if hasattr(self, 'internet_timer'):
                self.internet_timer.start(INTERNET_CHECK_INTERVAL)

    def _log_startup(self, line: str) -> None:
        """Log a startup status line to console (no timestamp) and live feed (timestamped)."""
        print(line)
        utc_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        self._add_to_feed(f"{utc_str}\t{line}", "")

    def _log_startup_status(self) -> None:
        """Emit startup status messages and initiate TCP connections.

        Order: User Settings, Groups, JS8 Connectors (and connection attempts),
        QRZ Settings.
        """
        # User settings (controls table)
        callsign, grid, state = self.db.get_user_settings()
        if callsign:
            self._log_startup(f"[User Settings Found] {callsign}, {grid}, {state}")
        else:
            self._log_startup("[User Settings NOT Found] Go to Menu -> Config -> User Settings")

        # Groups (stored with @ prefix in DB; displayed without)
        group_names = [g.lstrip('@') for g in self.db.get_all_groups()]
        if not group_names:
            self._log_startup("[Groups NOT Found] Go to Menu -> Config -> Manage Groups")
        elif len(group_names) == 1:
            self._log_startup(f"[Group Found] {group_names[0]}")
        else:
            self._log_startup(f"[Groups Found] {', '.join(group_names)}")

        # QRZ settings
        qrz_username, _, _ = self.db.get_qrz_settings()
        if qrz_username:
            self._log_startup("[QRZ Settings Found]")
        else:
            self._log_startup("[QRZ Settings NOT Found] Go to Menu -> Config -> QRZ Settings")

        # JS8 connectors — only log NOT Found if none configured.
        # Existing TCP "Attempting to connect" / "Connected" messages cover the rest.
        if not self.connector_manager.get_all_connectors(enabled_only=False):
            self._log_startup("[JS8 Connectors NOT Found] Go to Menu -> Config -> JS8 Connectors")
        # Initiate TCP connections (status messages emitted via status_message signal).
        # Each auto_connect=1 row is re-enabled and reconnected; auto_connect=0 rows
        # stay quiescent until the user clicks Reconnect.
        self.tcp_pool.connect_all()

    def _on_internet_became_available(self) -> None:
        """Restart everything gated on connectivity once it's confirmed
        available. Shared by the startup probe
        (_on_internet_check_complete) and the periodic 30-minute retry
        (_retry_internet_check) below.

        Re-enables the internet-gated map overlay menu items, which were
        disabled at startup/menu-build time because there was no connection
        yet — without this they stay grayed out for the rest of the session
        even after connectivity returns.
        """
        # Send first heartbeat after the HEARTBEAT_DELAY_MS delay, then start timer
        def start_commsrvr_heartbeat():
            self._check_commsrvr()  # Send first heartbeat immediately
            self.commsrvr_timer.start(HEARTBEAT_INTERVAL_MS)
        QTimer.singleShot(HEARTBEAT_DELAY_MS, start_commsrvr_heartbeat)

        self._sync_weather_radar_action()
        self._sync_earthquake_action()
        self._sync_wildfire_action()
        self._restart_radar_refresh_timer()

    def _retry_internet_check(self) -> None:
        """Retry internet connectivity check (called by timer)."""
        was_available = self._internet_available
        self._internet_available = check_internet()

        if self._internet_available and not was_available:
            # Internet just became available
            print("Internet connectivity: Now available")
            self.internet_timer.stop()
            self._on_internet_became_available()
            if self.config.get_weather_radar() or self.config.get_earthquake_layer() or self.config.get_wildfire_layer():
                self._save_map_position(callback=self._load_map)
        elif not self._internet_available:
            print("Internet connectivity: Still not available (will retry in 30 minutes)")

    def _menubar_qss(self) -> str:
        """Shared stylesheet for the menu bar and all of its menus/submenus.

        The QMenu::item background-color and the QMenu::indicator rules are
        load-bearing: they force Qt's stylesheet engine to draw menu items
        itself on every platform instead of delegating to the native style
        (QMacStyle reserves a menu-wide check column and draws its own large
        checkmark, which broke layout on macOS).

        Item indentation differs by platform because it has to: on macOS,
        QMenu::indicator ignores padding/margin entirely for its own
        position (only border-left shifts it), and — confirmed empirically,
        not documented — a QMenu that contains at least one checkable action
        silently adds ~14px of extra left inset to every item's text in that
        menu (checkable or not), on top of whatever padding is set here.
        That's what "padding-left: 0" below is leaning on: it lets the
        automatic inset alone align plain-text rows with the checkbox's own
        left edge in menus that mix checkable and non-checkable items (see
        _fix_plain_menu_indent for menus with no checkable items at all,
        which get none of that automatic inset and need an explicit one).

        Windows has no such automatic inset: plain items honor the item
        padding directly, but QMenu::indicator is laid out from the item's
        left edge and ignores that padding, so checkboxes render flush
        against the frame while text sits 12px in. Giving the indicator a
        matching left margin fixes it — margin (unlike 'left', which only
        offsets the drawn box) also reserves the space, so the checkable
        rows' own text still clears the checkbox instead of colliding
        with it.
        """
        menu_bg = self.config.get_color('menu_background')
        menu_fg = self.config.get_color('menu_foreground')
        panel_bg = self.config.get_color('module_background')
        panel_fg = self.config.get_color('module_foreground')
        # Extra declarations folded into the shared QMenu::indicator rule.
        indicator_rule = ""
        if sys.platform == "win32":
            item_rule = """
            QMenu::item {
                font-family: Roboto;
                font-size: 13px;
                padding: 3px 12px;
                background-color: transparent;
            }
            """
            # Match QMenu::item's 12px left padding (see docstring).
            indicator_rule = "margin-left: 12px;"
        else:
            item_rule = """
            QMenu::item {
                font-family: Roboto;
                font-size: 13px;
                padding: 3px 12px 3px 0px;
                background-color: transparent;
            }
            QMenu::item:unchecked, QMenu::item:checked {
                padding-left: 10px;
                border-left: 14px solid transparent;
            }
            """
        return f"""
            QMenuBar {{
                background-color: {menu_bg};
                color: {menu_fg};
                font-family: Roboto;
                font-size: 13px;
                font-weight: bold;
            }}
            QMenuBar::item {{
                padding: 6px 8px;
            }}
            QMenuBar::item:selected {{
                background-color: {menu_bg};
            }}
            QMenu {{
                background-color: {panel_bg};
                color: {panel_fg};
                font-family: Roboto;
                font-size: 13px;
            }}
            {item_rule}
            QMenu::item:selected {{
                background-color: {menu_bg};
                color: {menu_fg};
            }}
            QMenu::item:disabled {{
                background-color: {menu_bg};
                color: {menu_fg};
                font-weight: bold;
            }}
            QMenu::separator {{
                height: 1px;
                background: {panel_fg};
                margin: 4px 8px;
            }}
            QMenu::indicator {{
                width: 12px;
                height: 12px;
                {indicator_rule}
                background-color: white;
                border: 1px solid #7f7f7f;
                border-radius: 2px;
            }}
            QMenu::indicator:checked {{
                background-color: {menu_bg};
                border: 1px solid {menu_bg};
            }}
            QMenu::indicator:exclusive {{
                border-radius: 6px;
            }}
        """

    def _fix_plain_menu_indent(self, menu: QtWidgets.QMenu) -> None:
        """Give a checkbox-free menu the same left inset that mixed menus
        get for free from macOS/Linux's automatic checkbox-gutter reservation
        (see _menubar_qss) — without this, menus like Transmit/Tools render
        flush against the left edge since Qt never reserves that gutter
        unless the menu has at least one checkable action. No-op on Windows,
        which never relied on that automatic reservation to begin with.
        """
        if sys.platform == "win32":
            return
        if any(a.isCheckable() for a in menu.actions()):
            return
        menu.setStyleSheet("QMenu::item { padding: 3px 12px 3px 14px; }")

    def _setup_menu(self) -> None:
        """Create the menu bar with all actions."""
        self.menubar = QtWidgets.QMenuBar(self)
        self.menubar.setNativeMenuBar(False)  # Use Qt menu bar, not native (fixes Linux)
        self.setMenuBar(self.menubar)  # Explicitly set as main window's menu bar
        self.menubar.setVisible(True)
        self.menubar.setFixedHeight(30)
        # Clear corner widgets that may interfere with menu layout on Linux
        self.menubar.setCornerWidget(None, Qt.TopLeftCorner)
        self.menubar.setCornerWidget(None, Qt.TopRightCorner)
        # Section headers throughout the menus are disabled QActions; the shared
        # stylesheet's QMenu::item:disabled rule styles them bold. In each menu
        # the disabled items are exclusively section titles.
        self.menubar.setStyleSheet(self._menubar_qss())

        # Create the main menu
        self.menu = _PersistentCheckMenu("Settings", self.menubar)
        self.menubar.addMenu(self.menu)

        # Define menu actions: (name, text, handler)
        menu_items = [
            ("user_settings",     "User Settings",     self._on_user_settings),
            ("manage_groups",     "Manage Groups",     self._on_manage_groups),
            ("js8_connectors",    "JS8 Connectors",    self._on_js8_connectors),
            ("qrz_enable",     "QRZ Settings",    self._on_qrz_enable),
            ("sound_settings", "Sound Settings",  self._on_sound_settings),
            ("manage_watchlists", "Manage Watchlists", self._on_manage_watchlists),
        ]

        configuration_label = QtWidgets.QAction("Configuration", self)
        configuration_label.setEnabled(False)  # Disabled as a section title
        self.menu.addAction(configuration_label)

        # Create actions for dropdown menu
        self.actions: Dict[str, QtWidgets.QAction] = {}
        for item in menu_items:
            name, text, handler = item
            action = QtWidgets.QAction(text, self)
            action.triggered.connect(handler)
            self.menu.addAction(action)
            self.actions[name] = action

        self.groups_menu = self.menu

        # ALERTS, MESSAGES & VIDEOS section (moved here from the Filter menu)
        alerts_messages_label = QtWidgets.QAction("Alerts, Msgs && Videos", self)
        alerts_messages_label.setEnabled(False)  # Disabled as a section title
        self.menu.addAction(alerts_messages_label)

        self.save_all_alerts_action = QtWidgets.QAction("Save all Alerts", self)
        self.save_all_alerts_action.setCheckable(True)
        self.save_all_alerts_action.setChecked(self.config.get_save_all_alerts())
        self.save_all_alerts_action.triggered.connect(self._on_toggle_save_all_alerts)
        self.menu.addAction(self.save_all_alerts_action)

        self.save_all_messages_action = QtWidgets.QAction("Save all Messages", self)
        self.save_all_messages_action.setCheckable(True)
        self.save_all_messages_action.setChecked(self.config.get_save_all_messages())
        self.save_all_messages_action.triggered.connect(self._on_toggle_save_all_messages)
        self.menu.addAction(self.save_all_messages_action)

        self.save_all_videos_action = QtWidgets.QAction("Save all Videos", self)
        self.save_all_videos_action.setCheckable(True)
        self.save_all_videos_action.setChecked(self.config.get_save_all_videos())
        self.save_all_videos_action.triggered.connect(self._on_toggle_save_all_videos)
        self.menu.addAction(self.save_all_videos_action)

        alerts_messages_help = QtWidgets.QAction("Help", self)
        alerts_messages_help.triggered.connect(self._on_alerts_messages_help)
        self.menu.addAction(alerts_messages_help)

        # Populate group checkboxes (will be called after menu setup)
        # Deferred to after db initialization in __init__

        # Create the Transmit menu
        self.transmit_menu = _MenuBarMenu("Transmit", self.menubar)
        self.menubar.addMenu(self.transmit_menu)

        hybrid_lbl = QtWidgets.QAction("Hybrid Tools", self)
        hybrid_lbl.setEnabled(False)
        self.transmit_menu.addAction(hybrid_lbl)

        for name, text, handler in [
            ("statrep",      "Status Report",        self._on_statrep),
            ("send_message", "Message",               self._on_send_message),
            ("group_alert",  "Alert",                 self._on_group_alert),
            ("group_event",  "Event",                 self._on_group_event),
        ]:
            action = QtWidgets.QAction(text, self)
            action.triggered.connect(handler)
            self.transmit_menu.addAction(action)
            self.actions[name] = action

        internet_lbl = QtWidgets.QAction("Internet Tools", self)
        internet_lbl.setEnabled(False)
        self.transmit_menu.addAction(internet_lbl)

        inet_msg_action = QtWidgets.QAction("Direct Message", self)
        inet_msg_action.triggered.connect(self._on_qrz_lookup)
        self.transmit_menu.addAction(inet_msg_action)
        self.actions["internet_message"] = inet_msg_action

        share_video_action = QtWidgets.QAction("Share YouTube Video", self)
        share_video_action.triggered.connect(self._on_share_video)
        self.transmit_menu.addAction(share_video_action)
        self.actions["share_video"] = share_video_action

        section_lbl = QtWidgets.QAction("Grid Down Tools", self)
        section_lbl.setEnabled(False)
        self.transmit_menu.addAction(section_lbl)

        for name, text, handler in [
            ("js8_direct_message", "JS8 Direct Message", self._on_js8_direct_message),
            ("js8email", "JS8 Email", self._on_js8email),
            ("js8sms",   "JS8 SMS",   self._on_js8sms),
        ]:
            action = QtWidgets.QAction(text, self)
            action.triggered.connect(handler)
            self.transmit_menu.addAction(action)
            self.actions[name] = action

        self._fix_plain_menu_indent(self.transmit_menu)

        # Create the Filter menu
        self.filter_menu = _PersistentCheckMenu("Filter", self.menubar)
        self.menubar.addMenu(self.filter_menu)

        # Helper to create styled menu checkboxes

        # DATE FILTERING section
        date_filter_label = QtWidgets.QAction("Date Filtering", self)
        date_filter_label.setEnabled(False)  # Disabled as a section title
        self.filter_menu.addAction(date_filter_label)

        for label, days in [
            ("Reset to Midnight", 0), ("Reset to 1 day ago", 1),
            ("Reset to 2 days ago", 2), ("Reset to 3 days ago", 3),
            ("Reset to 1 week ago", 7),
        ]:
            action = QtWidgets.QAction(label, self)
            action.triggered.connect(lambda checked, d=days: self._reset_filter_date(d))
            self.filter_menu.addAction(action)


        # LIVE FEED section
        live_feed_label = QtWidgets.QAction("Live Feed", self)
        live_feed_label.setEnabled(False)  # Disabled as a section title
        self.filter_menu.addAction(live_feed_label)

        self.hide_heartbeat_action = QtWidgets.QAction("Hide CQ & Heartbeat", self)
        self.hide_heartbeat_action.setCheckable(True)
        self.hide_heartbeat_action.setChecked(self.config.get_hide_heartbeat())
        self.hide_heartbeat_action.triggered.connect(self._on_toggle_heartbeat)
        self.filter_menu.addAction(self.hide_heartbeat_action)

        self.hide_live_feed_action = QtWidgets.QAction("Hide Live Feed", self)
        self.hide_live_feed_action.setCheckable(True)
        self.hide_live_feed_action.setChecked(self.config.get_hide_live_feed())
        self.hide_live_feed_action.triggered.connect(self._on_toggle_hide_live_feed)
        self.filter_menu.addAction(self.hide_live_feed_action)

        # STATREP & MESSAGES section
        statrep_messages_label = QtWidgets.QAction("Status Reports", self)
        statrep_messages_label.setEnabled(False)  # Disabled as a section title
        self.filter_menu.addAction(statrep_messages_label)

        self.hide_internet_statrep_action = QtWidgets.QAction("Hide Internet Feed", self)
        self.hide_internet_statrep_action.setCheckable(True)
        self.hide_internet_statrep_action.setChecked(self.config.get_hide_internet_feed())
        self.hide_internet_statrep_action.triggered.connect(self._on_toggle_hide_internet_statrep)
        self.filter_menu.addAction(self.hide_internet_statrep_action)

        self.hide_green_pins_action = QtWidgets.QAction("Hide Green Pins", self)
        self.hide_green_pins_action.setCheckable(True)
        self.hide_green_pins_action.setChecked(False)
        self.hide_green_pins_action.triggered.connect(self._on_toggle_hide_green_pins)
        self.filter_menu.addAction(self.hide_green_pins_action)

        self.hide_all_pins_action = QtWidgets.QAction("Hide All Pins", self)
        self.hide_all_pins_action.setCheckable(True)
        self.hide_all_pins_action.setChecked(False)
        self.hide_all_pins_action.triggered.connect(self._on_toggle_hide_all_pins)
        self.filter_menu.addAction(self.hide_all_pins_action)

        # Per-group checkboxes are inserted here dynamically after DB is ready
        self.filter_group_actions: Dict[str, QtWidgets.QAction] = {}

        self.show_every_group_action = QtWidgets.QAction("Show Other Groups", self)
        self.show_every_group_action.setCheckable(True)
        self.show_every_group_action.setChecked(self.config.get_show_every_group())
        self.show_every_group_action.triggered.connect(self._on_toggle_show_every_group)
        self.filter_menu.addAction(self.show_every_group_action)

        # Map theme menu
        self.map_theme_menu = _PersistentCheckMenu("Map", self.menubar)
        self.menubar.addMenu(self.map_theme_menu)

        map_overlay_label = QtWidgets.QAction("Map Overlay Options", self)
        map_overlay_label.setEnabled(False)
        self.map_theme_menu.addAction(map_overlay_label)

        self.map_radar_action = QtWidgets.QAction("Weather Radar", self)
        self.map_radar_action.setCheckable(True)
        self.map_radar_action.setChecked(self.config.get_weather_radar() and self._internet_available)
        self.map_radar_action.setEnabled(self._internet_available)
        self.map_radar_action.triggered.connect(self._set_weather_radar)
        self.map_theme_menu.addAction(self.map_radar_action)

        self.map_radar_refresh_menu = self.map_theme_menu.addMenu("Radar Refresh")
        self.map_radar_refresh_actions = {}
        for minutes, label in [(0, "Off"), (2, "2 Minutes"), (5, "5 Minutes"), (10, "10 Minutes")]:
            action = QtWidgets.QAction(label, self)
            action.setCheckable(True)
            action.setChecked(self.config.get_weather_radar_refresh() == minutes)
            action.triggered.connect(lambda checked=False, m=minutes: self._set_weather_radar_refresh(m))
            self.map_radar_refresh_menu.addAction(action)
            self.map_radar_refresh_actions[minutes] = action

        self.map_radar_timestamp_action = QtWidgets.QAction("Show Radar Timestamp", self)
        self.map_radar_timestamp_action.setCheckable(True)
        self.map_radar_timestamp_action.setChecked(self.config.get_show_radar_timestamp())
        self.map_radar_timestamp_action.triggered.connect(self._set_show_radar_timestamp)
        self.map_theme_menu.addAction(self.map_radar_timestamp_action)

        self.map_theme_menu.addSeparator()

        self.map_earthquake_action = QtWidgets.QAction("Earthquakes (USGS)", self)
        self.map_earthquake_action.setCheckable(True)
        self.map_earthquake_action.setChecked(self.config.get_earthquake_layer())
        self.map_earthquake_action.setEnabled(self._internet_available)
        self.map_earthquake_action.triggered.connect(self._set_earthquake_layer)
        self.map_theme_menu.addAction(self.map_earthquake_action)

        self.map_eq_region_menu = self.map_theme_menu.addMenu("Earthquake Region")
        self.map_eq_region_actions = {}
        for region in ["USA", "North America", "Worldwide"]:
            action = QtWidgets.QAction(region, self)
            action.setCheckable(True)
            action.setChecked(self.config.get_earthquake_region() == region)
            action.triggered.connect(lambda checked=False, r=region: self._set_earthquake_region(r))
            self.map_eq_region_menu.addAction(action)
            self.map_eq_region_actions[region] = action

        self.map_eq_mag_menu = self.map_theme_menu.addMenu("Earthquake Min Magnitude")
        self.map_eq_mag_actions = {}
        for mag_value, label in [(0.0, "All"), (2.5, "2.5+"), (4.5, "4.5+"), (5.5, "5.5+")]:
            action = QtWidgets.QAction(label, self)
            action.setCheckable(True)
            action.setChecked(abs(self.config.get_earthquake_min_mag() - mag_value) < 0.01)
            action.triggered.connect(lambda checked=False, m=mag_value: self._set_earthquake_min_mag(m))
            self.map_eq_mag_menu.addAction(action)
            self.map_eq_mag_actions[mag_value] = action

        self.map_eq_refresh_menu = self.map_theme_menu.addMenu("Earthquake Refresh")
        self.map_eq_refresh_actions = {}
        for minutes, label in [(5, "5 Minutes"), (10, "10 Minutes"), (30, "30 Minutes")]:
            action = QtWidgets.QAction(label, self)
            action.setCheckable(True)
            action.setChecked(self.config.get_earthquake_refresh() == minutes)
            action.triggered.connect(lambda checked=False, m=minutes: self._set_earthquake_refresh(m))
            self.map_eq_refresh_menu.addAction(action)
            self.map_eq_refresh_actions[minutes] = action

        self.map_theme_menu.addSeparator()

        self.map_wildfire_action = QtWidgets.QAction("Wildfires (NIFC)", self)
        self.map_wildfire_action.setCheckable(True)
        self.map_wildfire_action.setChecked(self.config.get_wildfire_layer())
        self.map_wildfire_action.setEnabled(self._internet_available)
        self.map_wildfire_action.triggered.connect(self._set_wildfire_layer)
        self.map_theme_menu.addAction(self.map_wildfire_action)

        self.map_wildfire_acres_menu = self.map_theme_menu.addMenu("Wildfire Min Size")
        self.map_wildfire_acres_actions = {}
        for acres_value, label in [(0.0, "All"), (10.0, "10+ Acres"), (100.0, "100+ Acres"), (1000.0, "1,000+ Acres")]:
            action = QtWidgets.QAction(label, self)
            action.setCheckable(True)
            action.setChecked(abs(self.config.get_wildfire_min_acres() - acres_value) < 0.01)
            action.triggered.connect(lambda checked=False, a=acres_value: self._set_wildfire_min_acres(a))
            self.map_wildfire_acres_menu.addAction(action)
            self.map_wildfire_acres_actions[acres_value] = action

        self.map_wildfire_refresh_menu = self.map_theme_menu.addMenu("Wildfire Refresh")
        self.map_wildfire_refresh_actions = {}
        for minutes, label in [(10, "10 Minutes"), (30, "30 Minutes"), (60, "60 Minutes")]:
            action = QtWidgets.QAction(label, self)
            action.setCheckable(True)
            action.setChecked(self.config.get_wildfire_refresh() == minutes)
            action.triggered.connect(lambda checked=False, m=minutes: self._set_wildfire_refresh(m))
            self.map_wildfire_refresh_menu.addAction(action)
            self.map_wildfire_refresh_actions[minutes] = action

        # WATCHLIST OVERLAY section — one checkbox per watchlist that has members,
        # inserted dynamically by _populate_watchlist_pins_menu above the next
        # section's label. (Help now lives on the Manage Watchlists dialog.)
        watchlist_pins_label = QtWidgets.QAction("Watchlist Overlay", self)
        watchlist_pins_label.setEnabled(False)  # Disabled as a section title
        self.map_theme_menu.addAction(watchlist_pins_label)

        self.watchlist_pin_actions: Dict[str, QtWidgets.QAction] = {}

        self.map_theme_label = QtWidgets.QAction("Map Theme Options", self)
        self.map_theme_label.setEnabled(False)
        self.map_theme_menu.addAction(self.map_theme_label)

        self.map_theme_group = QtWidgets.QActionGroup(self)
        self.map_theme_group.setExclusive(True)

        self.map_dark_action = QtWidgets.QAction("Dark Map", self)
        self.map_dark_action.setCheckable(True)
        self.map_dark_action.setChecked(self.config.get_map_theme() == "dark")
        self.map_dark_action.triggered.connect(lambda: self._set_map_theme("dark"))
        self.map_theme_group.addAction(self.map_dark_action)
        self.map_theme_menu.addAction(self.map_dark_action)

        self.map_light_action = QtWidgets.QAction("Light Map", self)
        self.map_light_action.setCheckable(True)
        self.map_light_action.setChecked(self.config.get_map_theme() == "light")
        self.map_light_action.triggered.connect(lambda: self._set_map_theme("light"))
        self.map_theme_group.addAction(self.map_light_action)
        self.map_theme_menu.addAction(self.map_light_action)

        # Create Tools dropdown menu
        self.tools_menu = _MenuBarMenu("Tools", self.menubar)
        self.menubar.addMenu(self.tools_menu)

        # Helper to create menu actions
        def create_action(menu, label, key, handler):
            action = QtWidgets.QAction(label, self)
            action.triggered.connect(handler)
            menu.addAction(action)
            self.actions[key] = action

        # Helper to create a non-clickable bold section header in a menu.
        def add_section_header(menu, text):
            header = QtWidgets.QAction(text, self)
            header.setEnabled(False)
            font = header.font()
            font.setBold(True)
            header.setFont(font)
            menu.addAction(header)

        # Export section - dump database tables to CSV
        add_section_header(self.tools_menu, "Export")
        create_action(self.tools_menu, "QRZ Contacts", "export_contacts", self._on_export_contacts)
        create_action(self.tools_menu, "Status Reports", "export_statreps", self._on_export_statreps)
        create_action(self.tools_menu, "Watchlist Callsigns", "export_watchlist_callsigns", self._on_export_watchlist_callsigns)
        create_action(self.tools_menu, "Watchlist Members", "export_watchlist_members", self._on_export_watchlist_members)

        # HAMSQL Tools section - solar/radio image dialogs
        add_section_header(self.tools_menu, "HAMSQL Tools")
        for menu_label, url, link, load_text, err_prefix in SOLAR_IMAGE_DIALOGS:
            create_action(
                self.tools_menu, menu_label, menu_label.lower().replace(" ", "_"),
                lambda checked=False, t=menu_label, u=url, l=link, lt=load_text, ep=err_prefix:
                    self._show_image_dialog(title=t, image_url=u, link_html=l, loading_text=lt, error_prefix=ep)
            )

        add_section_header(self.tools_menu, "CommStat Utilities")
        create_action(self.tools_menu, "Brevity", "brevity", self._on_brevity_generator)
        create_action(self.tools_menu, "Grid Finder", "grid_finder", self._on_grid_finder)
        create_action(self.tools_menu, "Data Manager", "data_manager", self._on_data_manager)
        create_action(self.tools_menu, "Video Manager", "video_manager", self._on_video_manager)
        create_action(self.tools_menu, "Maintenance", "maintenance", self._on_maintenance)

        self._fix_plain_menu_indent(self.tools_menu)

        # Menubar items
        create_action(self.menubar, "QRZ", "qrz_lookup", self._on_qrz_lookup)

        # Create Websites dropdown menu - browser links formerly under Tools
        self.websites_menu = _MenuBarMenu("Websites", self.menubar)
        self.menubar.addMenu(self.websites_menu)

        add_section_header(self.websites_menu, "Open Source Intel")
        for label, url in [
            ("Osiris",     "https://www.osirisai.live"),
            ("Provenance", "https://www.provenance.website"),
        ]:
            create_action(
                self.websites_menu, label, "osint_" + label.lower(),
                lambda checked=False, u=url, lbl=label: self._open_external_link(u, lbl)
            )

        add_section_header(self.websites_menu, "Weather Maps")
        for label, url in WEATHER_MAP_LINKS:
            create_action(
                self.websites_menu, label, "weather_" + label.lower().replace(" ", "_").replace(".", "_"),
                lambda checked=False, u=url, lbl=label: self._open_external_link(u, lbl)
            )

        add_section_header(self.websites_menu, "Internet Websites")
        create_action(
            self.websites_menu, "Apocalypse Early Warning", "apocalypse_early_warning",
            lambda checked=False: self._open_external_link("https://ews.kylemcdonald.net/", "Apocalypse Early Warning")
        )
        create_action(self.websites_menu, "Flock Camera Map", "flock_camera_map", self._on_flock_camera_map)
        create_action(self.websites_menu, "Live Radiation Map", "live_radiation_map", self._on_live_radiation_map)
        for label, key, url in [
            ("Real-Time Lightning",    "lightning_map",     "https://www.lightningmaps.org/"),
            ("US Power Outages",       "power_outages",     "https://poweroutage.us/"),
            ("USGS Earthquakes",       "usgs_earthquakes",  "https://earthquake.usgs.gov/earthquakes/map/"),
            ("Wildfire Map",           "wildfire_map",      "https://wildfiretrackers.com/"),
            ("World Internet Outages", "internet_outages",  "https://radar.cloudflare.com/outage-center"),
        ]:
            create_action(
                self.websites_menu, label, key,
                lambda checked=False, u=url, lbl=label: self._open_external_link(u, lbl)
            )

        self._fix_plain_menu_indent(self.websites_menu)

        create_action(self.menubar, "Help", "help", self._on_help)
        # close() rather than qApp.quit() so Exit runs the same closeEvent path
        # as the title-bar X: timers stopped, TCP pool disconnected, position
        # saved while the window is still alive.
        create_action(self.menubar, "Exit" + " " * 10, "exit", self.close)
        create_action(self.menubar, "What's New" + " " * 10, "whats_new", self._on_whats_new)
        create_action(self.menubar, "Live Better", "live_better", self._on_live_better)

        # Add status bar
        self.statusbar = QtWidgets.QStatusBar(self)
        self.setStatusBar(self.statusbar)
        self.statusbar.setStyleSheet(
            "QStatusBar, QStatusBar QLabel, QStatusBar QPushButton {"
            " font-family: Roboto; font-size: 12px; font-weight: normal; }"
        )

        # Map view toggle buttons (left side of status bar)
        for label, mode, width in [
            ("US", "us", 58),
            ("EU", "eu", 58),
            ("Mid-East", "mideast", 68),
            ("SE Asia", "seasia", 68),
            ("World", "world", 68),
            ("Images", "images", 68),
            ("Videos", "videos", 68),
            ("Alerts", "alerts", 68),
            ("Contacts", "contacts", 68),
        ]:
            btn = QtWidgets.QPushButton(label)
            btn.setFixedHeight(18)
            btn.setFixedWidth(width)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda checked, m=mode: self._set_map_view_mode(m))
            self.statusbar.addWidget(btn)
            setattr(self, f"_btn_{mode}", btn)

        # Quick-toggle buttons after the divider: cyan when off, green when
        # on, shortcuts for the same Map-menu overlay toggles.
        quick_toggle_style = (
            f"QPushButton {{ background-color: {COLOR_BTN_CYAN}; color: #FFFFFF;"
            f" border: none; border-radius: 4px; padding: 2px 10px; }}"
            f"QPushButton:checked {{ background-color: {COLOR_BTN_GREEN}; color: #FFFFFF; }}"
            f"QPushButton:disabled {{ background-color: {COLOR_BTN_GRAY}; color: #333333; }}"
        )
        for label, getter, setter_name, attr_name in [
            ("Weather",  self.config.get_weather_radar,    "_set_weather_radar",   "quick_weather_btn"),
            ("Quakes",   self.config.get_earthquake_layer, "_set_earthquake_layer", "quick_earthquake_btn"),
            ("Wildfire", self.config.get_wildfire_layer,   "_set_wildfire_layer",  "quick_wildfire_btn"),
        ]:
            btn = QtWidgets.QPushButton(label)
            btn.setFixedHeight(18)
            btn.setFixedWidth(74)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setCheckable(True)
            btn.setChecked(getter())
            btn.setEnabled(self._internet_available)
            btn.setStyleSheet(quick_toggle_style)
            btn.clicked.connect(getattr(self, setter_name))
            self.statusbar.addWidget(btn)
            setattr(self, attr_name, btn)

        # Add "Rig Status:" label (no sunken effect, permanent on right)
        rig_status_header = QtWidgets.QLabel(" Rig Status: ")
        self.statusbar.addPermanentWidget(rig_status_header)

        # Dictionary to hold status widgets for each rig
        self.rig_status_widgets: Dict[str, Tuple[QtWidgets.QLabel, QtWidgets.QLabel]] = {}

    def _setup_header(self) -> None:
        """Create the header row with News Feed and Time."""
        # Header container widget with horizontal layout
        self.header_widget = QtWidgets.QWidget(self.central_widget)
        self.header_widget.setFixedHeight(38)
        self.header_layout = QtWidgets.QHBoxLayout(self.header_widget)
        self.header_layout.setContentsMargins(0, 0, 0, 0)

        fg_color = self.config.get_color('program_foreground')
        menu_bg = self.config.get_color('menu_background')
        menu_fg = self.config.get_color('menu_foreground')
        font = QtGui.QFont("Roboto", -1, QtGui.QFont.Bold)
        font.setPixelSize(15)

        # News label
        self.label_newsfeed = QtWidgets.QLabel(self.header_widget)
        self.label_newsfeed.setStyleSheet(f"color: {fg_color};")
        self.label_newsfeed.setText("News:")
        self.label_newsfeed.setFont(font)
        self.header_layout.addWidget(self.label_newsfeed)

        # RSS Feed selector dropdown
        self.feed_combo = QtWidgets.QComboBox(self.header_widget)
        self.feed_combo.setFixedSize(180, 28)
        self.feed_combo.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed)
        _combo_font = QtGui.QFont("Roboto", -1)
        _combo_font.setPixelSize(15)
        self.feed_combo.setFont(_combo_font)
        self.feed_combo.setStyleSheet(f"""
            QComboBox {{
                background-color: {menu_bg};
                color: {menu_fg};
                border: 1px solid {menu_fg};
                padding: 2px 5px;
            }}
            QComboBox::drop-down {{
                border: none;
            }}
        """)
        # Style the dropdown list view directly
        combo_view = QtWidgets.QListView()
        _combo_view_font = QtGui.QFont("Roboto", -1)
        _combo_view_font.setPixelSize(15)
        combo_view.setFont(_combo_view_font)
        combo_view.setStyleSheet(f"""
            QListView {{
                background-color: {menu_bg};
                color: {menu_fg};
                outline: none;
            }}
            QListView::item {{
                background-color: {menu_bg};
                color: {menu_fg};
                padding: 4px;
            }}
        """)
        self.feed_combo.setView(combo_view)
        # Populate with feed names
        for feed_name in DEFAULT_RSS_FEEDS.keys():
            self.feed_combo.addItem(feed_name)
        self.feed_combo.addItem("Disable")
        # Set to saved selection
        saved_feed = self.config.get_selected_rss_feed()
        index = self.feed_combo.findText(saved_feed)
        if index >= 0:
            self.feed_combo.setCurrentIndex(index)
        # Connect signal
        self.feed_combo.currentTextChanged.connect(self._on_feed_changed)
        self.header_layout.addWidget(self.feed_combo)

        # News ticker (scrolling text)
        self.newsfeed_label = QtWidgets.QLabel(self.header_widget)
        self.newsfeed_label.setFixedHeight(32)
        self.newsfeed_label.setMinimumWidth(0)
        self.newsfeed_label.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
        _ticker_font = QtGui.QFont("Kode Mono", -1)
        _ticker_font.setPixelSize(15)
        self.newsfeed_label.setFont(_ticker_font)
        self.newsfeed_label.setStyleSheet(
            f"background-color: {self.config.get_color('newsfeed_background')};"
            f"color: {self.config.get_color('newsfeed_foreground')};"
        )
        self.header_layout.addWidget(self.newsfeed_label, 7)
        # Recompute ticker length when the label is resized so the scrolling
        # text fills the new width. Debounced via single-shot QTimer to avoid
        # restarting on every pixel during a window drag.
        self._newsfeed_resize_timer = QTimer(self)
        self._newsfeed_resize_timer.setSingleShot(True)
        self._newsfeed_resize_timer.timeout.connect(self._refresh_newsfeed_for_resize)
        self.newsfeed_label.installEventFilter(self)

        # Last 20 button - shows last 20 news headlines
        self.last20_button = QtWidgets.QPushButton("Last 20", self.header_widget)
        self.last20_button.setFixedSize(80, 28)
        _btn_font = QtGui.QFont("Roboto", -1)
        _btn_font.setPixelSize(15)
        self.last20_button.setFont(_btn_font)
        self.last20_button.setStyleSheet(f"""
            QPushButton {{
                background-color: {menu_bg};
                color: {menu_fg};
                border: 1px solid {menu_fg};
                padding: 2px 5px;
            }}
            QPushButton:hover {{
                background-color: {menu_fg};
                color: {menu_bg};
            }}
        """)
        self.last20_button.clicked.connect(self._on_last20_clicked)
        self.header_layout.addWidget(self.last20_button)

        self.header_layout.addSpacing(22)

        # Off-Grid / Online master switch — gates every network call in the
        # app (heartbeat/auto-update, RSS, map tiles, QRZ, INTERNET rig).
        self.network_toggle_btn = QtWidgets.QPushButton(self.header_widget)
        self.network_toggle_btn.setCheckable(True)
        self.network_toggle_btn.setFixedSize(118, 28)
        self.network_toggle_btn.setFont(_btn_font)
        self.network_toggle_btn.clicked.connect(self._on_network_toggle_clicked)
        self.header_layout.addWidget(self.network_toggle_btn)
        netguard.add_listener(self._on_netguard_state_changed)
        self._refresh_network_toggle_ui()

        self.header_layout.addSpacing(22)

        # Time label
        self.label_time_prefix = QtWidgets.QLabel(self.header_widget)
        self.label_time_prefix.setStyleSheet(f"color: {fg_color};")
        self.label_time_prefix.setText("Time:")
        self.label_time_prefix.setFont(font)
        self.header_layout.addWidget(self.label_time_prefix)

        # Time display
        self.time_label = QtWidgets.QLabel(self.header_widget)
        self.time_label.setFixedSize(120, 32)
        _time_font = QtGui.QFont("Kode Mono", -1)
        _time_font.setPixelSize(16)
        self.time_label.setFont(_time_font)
        self.time_label.setStyleSheet(
            f"background-color: {self.config.get_color('time_background')};"
            f"color: {self.config.get_color('time_foreground')};"
        )
        self.time_label.setAlignment(QtCore.Qt.AlignCenter)
        self.header_layout.addWidget(self.time_label)

        # Add header to main layout (row 1, spans all columns)
        self.main_layout.addWidget(self.header_widget, 1, 0, 1, 2)

    def _setup_table_widget(self, table: QtWidgets.QTableWidget, headers: list) -> None:
        """Apply common styling and header configuration to a table widget."""
        title_bg = self.config.get_color('title_bar_background')
        title_fg = self.config.get_color('title_bar_foreground')
        data_bg = self.config.get_color('data_background')
        data_fg = self.config.get_color('data_foreground')

        table.setStyleSheet(f"""
            QTableWidget {{
                background-color: {data_bg};
                color: {data_fg};
                font-family: Roboto;
                font-size: 13px;
                gridline-color: #D2D0CF;
                border: 1px solid #D2D0CF;
            }}
            QTableWidget QHeaderView::section {{
                background-color: {title_bg};
                color: {title_fg};
                font-family: Roboto;
                font-weight: bold;
                padding: 4px;
                border: 1px solid {title_bg};
            }}
            QTableWidget::item:selected {{
                background-color: #cce5ff;
                color: #000000;
            }}
            QToolTip {{
                background-color: #FFFFE1;
                color: black;
                border: 1px solid black;
            }}
        """)
        table.setShowGrid(True)

        table.horizontalHeader().setStyleSheet(f"""
            QHeaderView::section {{
                background-color: {title_bg};
                color: {title_fg};
                font-family: Roboto;
                font-weight: bold;
                font-size: 13px;
                padding: 4px;
            }}
        """)

        table.setHorizontalHeaderLabels(headers)

        header = table.horizontalHeader()
        header.setMinimumSectionSize(10)
        # Interactive (not ResizeToContents) so the user can drag columns
        # narrower/wider after the initial content-based fit — see the
        # one-time resizeColumnsToContents() call in _populate_table.
        header.setSectionResizeMode(QtWidgets.QHeaderView.Interactive)
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.Fixed)
        header.resizeSection(0, 10)
        # Explicitly mark the last column Stretch rather than relying on
        # setStretchLastSection() to override its resize mode — on Linux/X11
        # that combo can misattribute the extra width to an earlier column
        # instead of the last one.
        header.setSectionResizeMode(len(headers) - 1, QtWidgets.QHeaderView.Stretch)
        header.setStretchLastSection(True)
        table.verticalHeader().setVisible(False)

    def _setup_statrep_table(self) -> None:
        """Create the StatRep data table."""
        self.statrep_table = QtWidgets.QTableWidget(self.central_widget)
        self.statrep_table.setObjectName("statrepTable")
        self.statrep_table.setColumnCount(21)
        self.statrep_table.setRowCount(0)

        self._setup_table_widget(self.statrep_table, STATREP_HEADERS)
        self.statrep_table.horizontalHeader().setFixedHeight(30)
        # Blank the built-in "Remarks" header text - it's drawn instead as a
        # floating label (below) so it can be centered in section 2 of a
        # 5-section split of the gap between the "Pol" column and the
        # FILTER link, matching the record counter centered in section 4.
        remarks_header_item = self.statrep_table.horizontalHeaderItem(len(STATREP_HEADERS) - 1)
        if remarks_header_item:
            remarks_header_item.setText("")

        self.statrep_table.itemClicked.connect(self._on_statrep_click)
        self.statrep_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.statrep_table.customContextMenuRequested.connect(
            lambda pos: self._show_table_copy_menu(self.statrep_table, pos)
        )
        # The Custom Filtering bar sits directly above the table's column
        # headers, so both live in a container that takes the splitter slot.
        self.statrep_container = QtWidgets.QWidget(self.central_widget)
        _v = QtWidgets.QVBoxLayout(self.statrep_container)
        _v.setContentsMargins(0, 0, 0, 0)
        _v.setSpacing(0)
        self._setup_custom_filter_bar()
        _v.addWidget(self.custom_filter_bar)
        _v.addWidget(self.statrep_table)

        self.content_splitter.insertWidget(0, self.statrep_container)

        # FILTER toggle - same underlined-word style/position as the message
        # table's, floating over this table's title bar far right edge.
        # Toggles the Map Filter's Custom state (shared with the map's own
        # Filter dropdown, so both stay in sync).
        title_fg = self.config.get_color('title_bar_foreground')
        self.statrep_filter_btn = ClickableLabel("FILTER", self.statrep_table)
        self.statrep_filter_btn.setStyleSheet(
            "QLabel { background: transparent; color: %s;"
            " font-family: Roboto; font-size: 13px; font-weight: bold;"
            " text-decoration: underline; }" % title_fg
        )
        self.statrep_filter_btn.setCursor(Qt.PointingHandCursor)
        self.statrep_filter_btn.clicked.connect(self._toggle_statrep_custom_filter)
        self.statrep_filter_btn.adjustSize()

        # "Remarks" label and record counter sit in sections 2 and 4 of a
        # 5-section split of the gap between the "Pol" column and the
        # FILTER link; see _reposition_statrep_filter_btn(). The counter's
        # text is updated in _load_statrep_data() to track the current
        # filter.
        self.statrep_remarks_label = QtWidgets.QLabel("Remarks", self.statrep_table)
        self.statrep_remarks_label.setStyleSheet(
            "QLabel { background: transparent; color: %s;"
            " font-family: Roboto; font-size: 13px; font-weight: bold; }" % title_fg
        )
        self.statrep_remarks_label.adjustSize()

        self.statrep_count_label = QtWidgets.QLabel("", self.statrep_table)
        self.statrep_count_label.setStyleSheet(
            "QLabel { background: transparent; color: %s;"
            " font-family: Roboto; font-size: 13px; font-weight: bold; }" % title_fg
        )
        self.statrep_count_label.adjustSize()

        self.statrep_table.installEventFilter(self)
        # See the matching comment in _setup_message_table: scrollbar
        # show/hide doesn't fire a table Resize event on its own.
        self.statrep_table.verticalScrollBar().rangeChanged.connect(
            lambda *_: QTimer.singleShot(0, self._reposition_statrep_filter_btn)
        )
        self._reposition_statrep_filter_btn()

    def _setup_custom_filter_bar(self) -> None:
        """Build the Custom Filtering row shown above the StatRep headers
        while Map Filter is "custom". Four comma-separated criteria boxes
        joined by an AND/OR toggle; hidden in every other filter state.
        A dropdown on the left recalls criteria sets saved to map_filters."""
        from ui_helpers import connect_single

        self.custom_filter_bar = QtWidgets.QWidget(self.central_widget)
        self.custom_filter_bar.setObjectName("customFilterBar")
        self.custom_filter_bar.setFixedHeight(34)

        row = QtWidgets.QHBoxLayout(self.custom_filter_bar)
        row.setContentsMargins(10, 0, 10, 0)
        row.setSpacing(6)

        # Saved-filter picker leads the bar. Item 0 is the placeholder (no
        # saved filter selected); every other item carries its name as itemData.
        self._cf_saved: Dict[str, Tuple[str, str, str, str, str]] = {}
        self._cf_saved_combo = QtWidgets.QComboBox()
        self._cf_saved_combo.setFixedSize(170, 22)
        self._cf_saved_combo.setCursor(Qt.PointingHandCursor)
        self._cf_saved_combo.currentIndexChanged.connect(self._on_saved_filter_selected)
        row.addWidget(self._cf_saved_combo)
        row.addSpacing(10)

        # Four criteria boxes with the AND/OR conjunction labels between them.
        # Each box names itself with its placeholder rather than carrying a
        # separate label - the bar is already near the window's width, and the
        # placeholder disappears exactly when the typed value makes it redundant.
        # Boxes are wide (140px) because each holds a comma- or space-
        # separated list; longer lists scroll horizontally within the box.
        # No tooltips here - the rules are documented in the map's Filter > Help.
        _FIELDS = [("from", "From"), ("to", "To"), ("grid", "Grid"), ("remarks", "Remarks")]
        self._cf_inputs: Dict[str, QtWidgets.QLineEdit] = {}
        self._cf_conj_labels: List[QtWidgets.QLabel] = []
        for idx, (key, label_text) in enumerate(_FIELDS):
            if idx:
                conj = QtWidgets.QLabel(self._custom_filter_mode.upper())
                conj.setAlignment(Qt.AlignCenter)
                conj.setFixedWidth(34)
                self._cf_conj_labels.append(conj)
                row.addWidget(conj)

            # Remarks is free text (not a callsign/grid), so it keeps whatever
            # case the operator typed; matching itself is already
            # case-insensitive (see _custom_filter_match).
            edit = QtWidgets.QLineEdit() if key == "remarks" else UpperCaseLineEdit()
            edit.setFixedSize(140, 22)
            edit.setPlaceholderText(label_text)
            edit.textChanged.connect(lambda _t: self._custom_filter_timer.start())
            self._cf_inputs[key] = edit
            row.addWidget(edit)

        # The AND/OR picker sits right after the last box so the bar still reads
        # as a sentence, with Save/Delete for the saved-filter list beside it.
        row.addSpacing(14)
        self._cf_mode_combo = QtWidgets.QComboBox()
        self._cf_mode_combo.setFixedSize(76, 22)
        self._cf_mode_combo.setCursor(Qt.PointingHandCursor)
        for mode in ("and", "or"):
            self._cf_mode_combo.addItem(mode.upper(), mode)
        self._cf_mode_combo.currentIndexChanged.connect(
            lambda _i: self._on_custom_filter_mode(self._cf_mode_combo.currentData())
        )
        row.addWidget(self._cf_mode_combo)

        # Clear empties the four criteria boxes without touching any saved
        # filter, so it sits before Save/Delete rather than with them.
        self._cf_clear_btn = QtWidgets.QPushButton("Clear")
        row.addWidget(self._cf_clear_btn)

        # Save / Delete act on the saved-filter dropdown, so they sit with the
        # criteria rather than with the bar's Help / Close chrome.
        self._cf_save_btn = QtWidgets.QPushButton("Save")
        self._cf_delete_btn = QtWidgets.QPushButton("Delete")
        row.addWidget(self._cf_save_btn)
        row.addWidget(self._cf_delete_btn)

        row.addStretch(1)

        # Help / Close pinned far right, using the app's standard dialog button
        # colors (pink Help, gray Close - see ui_helpers.make_button callers).
        self._cf_help_btn = QtWidgets.QPushButton("Help")
        self._cf_close_btn = QtWidgets.QPushButton("Close")
        row.addWidget(self._cf_help_btn)
        row.addWidget(self._cf_close_btn)

        self._cf_action_buttons = [
            (self._cf_clear_btn, COLOR_BTN_RED),
            (self._cf_save_btn, COLOR_BTN_GREEN),
            (self._cf_delete_btn, COLOR_BTN_RED),
            (self._cf_help_btn, "#e83e8c"),
            (self._cf_close_btn, "#555555"),
        ]
        # connect_single: a second click while the name prompt is up would
        # otherwise stack two dialogs.
        self._cf_clear_btn.clicked.connect(self._on_clear_custom_filter)
        connect_single(self._cf_save_btn, self._on_save_custom_filter)
        connect_single(self._cf_delete_btn, self._on_delete_custom_filter)
        self._cf_help_btn.clicked.connect(lambda _c: self._on_map_filter_help())
        self._cf_close_btn.clicked.connect(lambda _c: self._on_map_filter_set("off"))
        for btn, _color in self._cf_action_buttons:
            btn.setFixedSize(60, 22)
            btn.setCursor(Qt.PointingHandCursor)

        # Re-filtering reloads the map (a full re-render), so coalesce
        # keystrokes rather than firing on every one.
        self._custom_filter_timer = QTimer(self)
        self._custom_filter_timer.setSingleShot(True)
        self._custom_filter_timer.setInterval(350)
        self._custom_filter_timer.timeout.connect(self._apply_custom_filter)

        self._reload_saved_filters()
        self._style_custom_filter_bar()
        self.custom_filter_bar.setVisible(False)

    def _style_custom_filter_bar(self) -> None:
        """Apply theme colors to the Custom Filtering bar."""
        # Bar takes the live feed's color pair, not the table header's - kept
        # as a pair so the text stays legible if the feed colors are rethemed.
        bar_bg = self.config.get_color('feed_background')
        bar_fg = self.config.get_color('feed_foreground')

        # Scoped to the objectName so it doesn't cascade into the children.
        self.custom_filter_bar.setStyleSheet(
            f"#customFilterBar {{ background-color: {bar_bg};"
            " border: 1px solid #000000; }"
        )
        # Every label must declare a transparent background: central_widget
        # carries a selector-less "background-color: {program_background}"
        # stylesheet, which cascades into all descendants and would otherwise
        # paint each label the program color instead of the bar's.
        for lbl in self._cf_conj_labels:
            lbl.setStyleSheet(
                f"background: transparent; color: {bar_fg};"
                " font-family: Roboto; font-weight: bold; font-size: 12px;"
            )
        # The placeholder names the box now that the labels are gone, so it is
        # darkened from Qt's default near-invisible gray - but kept clearly
        # lighter than typed text so an empty box still reads as empty. It has
        # to be a palette role: Qt 5's stylesheet parser has no
        # placeholder-text-color property and warns "Unknown property" for it.
        _ph_palette = QtGui.QPalette()
        _ph_palette.setColor(QtGui.QPalette.PlaceholderText, QtGui.QColor("#8A8A8A"))
        for edit in self._cf_inputs.values():
            edit.setStyleSheet(
                "QLineEdit { background-color: #FFFFFF; color: #333333;"
                " border: 1px solid rgba(0,0,0,0.25); border-radius: 3px;"
                " padding: 1px 5px; font-family: Roboto; font-size: 12px; }"
                "QLineEdit:focus { border: 1px solid #28a745; }"
            )
            edit.setPalette(_ph_palette)
        # Both dropdowns match the criteria boxes. Two rules that look harmless
        # are deliberately absent: styling ::drop-down erases Qt's built-in
        # arrow (leaving something that reads as a text box), and the popup list
        # is a separate top-level widget, so QAbstractItemView must be styled
        # here or the list falls back to the platform palette.
        _combo_qss = (
            "QComboBox { background-color: #FFFFFF; color: #333333;"
            " border: 1px solid rgba(0,0,0,0.25); border-radius: 3px;"
            " padding: 1px 5px; font-family: Roboto; font-size: 12px; }"
            "QComboBox:focus { border: 1px solid #28a745; }"
            "QComboBox QAbstractItemView { background-color: #FFFFFF; color: #333333;"
            " selection-background-color: #cce5ff; selection-color: #000000;"
            " font-family: Roboto; font-size: 12px; }"
        )
        self._cf_saved_combo.setStyleSheet(_combo_qss)
        self._cf_mode_combo.setStyleSheet(_combo_qss)
        # Fixed brand colors, scaled down to the bar's 22px button height.
        for btn, color in self._cf_action_buttons:
            btn.setStyleSheet(
                f"QPushButton {{ background-color: {color}; color: #FFFFFF;"
                " border: none; border-radius: 4px;"
                " font-family: Roboto; font-weight: bold; font-size: 12px; }"
            )
        self._refresh_custom_filter_mode_ui()

    def _refresh_custom_filter_mode_ui(self) -> None:
        """Sync the AND/OR dropdown and the conjunction labels to
        self._custom_filter_mode."""
        idx = self._cf_mode_combo.findData(self._custom_filter_mode)
        if idx >= 0 and idx != self._cf_mode_combo.currentIndex():
            # Blocked: setting the index would re-enter _on_custom_filter_mode.
            self._cf_mode_combo.blockSignals(True)
            self._cf_mode_combo.setCurrentIndex(idx)
            self._cf_mode_combo.blockSignals(False)
        for lbl in self._cf_conj_labels:
            lbl.setText(self._custom_filter_mode.upper())

    def _on_custom_filter_mode(self, mode: str) -> None:
        """AND/OR picked - reflow the labels and re-filter immediately
        (a single selection needs no debounce)."""
        if mode not in ("and", "or") or mode == self._custom_filter_mode:
            return
        self._custom_filter_mode = mode
        self._refresh_custom_filter_mode_ui()
        self._apply_custom_filter()

    _CF_PLACEHOLDER = "-- SAVED FILTERS --"

    def _reload_saved_filters(self, select: str = None) -> None:
        """Refill the saved-filter dropdown from map_filters.

        select: name to leave selected; defaults to the placeholder. Signals are
        blocked while rebuilding so clearing the list never fires a selection.
        """
        rows = self.db.get_map_filters()
        self._cf_saved = {r[0]: r[1:] for r in rows}

        self._cf_saved_combo.blockSignals(True)
        self._cf_saved_combo.clear()
        self._cf_saved_combo.addItem(self._CF_PLACEHOLDER, None)
        for name, *_rest in rows:
            self._cf_saved_combo.addItem(name, name)
        idx = self._cf_saved_combo.findData(select) if select else 0
        self._cf_saved_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._cf_saved_combo.blockSignals(False)

    def _on_saved_filter_selected(self, _idx: int) -> None:
        """Load a saved criteria set into the boxes and re-filter once."""
        name = self._cf_saved_combo.currentData()
        if not name:
            return                      # Placeholder: leave what is typed alone.
        saved = self._cf_saved.get(name)
        if not saved:
            return
        from_call, to_call, grid, remarks, mode = saved
        # Signals blocked: four textChanged firings would each restart the
        # debounce timer, and the single _apply_custom_filter below covers all.
        for key, value in (("from", from_call), ("to", to_call),
                           ("grid", grid), ("remarks", remarks)):
            edit = self._cf_inputs[key]
            edit.blockSignals(True)
            edit.setText(value or "")
            edit.blockSignals(False)
        self._custom_filter_mode = mode if mode in ("and", "or") else "or"
        self._refresh_custom_filter_mode_ui()
        self._apply_custom_filter()

    def _on_clear_custom_filter(self) -> None:
        """Empty all four criteria boxes and re-filter once.

        Leaves the saved-filter dropdown and AND/OR mode alone - this only
        clears what is typed, not any saved filter."""
        for edit in self._cf_inputs.values():
            edit.blockSignals(True)
            edit.clear()
            edit.blockSignals(False)
        self._apply_custom_filter()

    def _on_save_custom_filter(self) -> None:
        """Name the current criteria and store them in map_filters."""
        from ui_helpers import confirm, prompt_text

        values = {key: edit.text().strip() for key, edit in self._cf_inputs.items()}
        if not any(values.values()):
            QtWidgets.QMessageBox.warning(
                self, "Custom Filtering",
                "Enter at least one criteria before saving."
            )
            return

        name = prompt_text(self, "Save Filter", "Filter name:",
                           **self._help_theme_colors())
        if not name:
            return                      # Cancelled or left empty.

        if name.upper() in {n.upper() for n in self._cf_saved}:
            if not confirm(
                self, "Replace Filter",
                f'A filter named "{name}" already exists.\n\nReplace it?'
            ):
                return

        if not self.db.save_map_filter(
            name, values["from"], values["to"], values["grid"], values["remarks"],
            self._custom_filter_mode
        ):
            QtWidgets.QMessageBox.critical(
                self, "Custom Filtering", "Could not save the filter."
            )
            return
        self._reload_saved_filters(select=name)

    def _on_delete_custom_filter(self) -> None:
        """Delete the saved filter currently shown in the dropdown."""
        from ui_helpers import confirm

        name = self._cf_saved_combo.currentData()
        if not name:
            QtWidgets.QMessageBox.warning(
                self, "Custom Filtering",
                "Select a saved filter to delete."
            )
            return
        if not confirm(self, "Delete Filter", f'Delete the saved filter "{name}"?'):
            return
        if not self.db.delete_map_filter(name):
            QtWidgets.QMessageBox.critical(
                self, "Custom Filtering", "Could not delete the filter."
            )
        # Back to the placeholder; the criteria boxes keep filtering as they are.
        self._reload_saved_filters()

    def _custom_filter_match(self, row) -> bool:
        """True if a statrep row passes the Custom Filtering bar's criteria.

        Within one box, comma- or space-separated terms are always OR'd
        ("N0DDK, W1ABC" or "N0DDK W1ABC" matches either). Across boxes, the
        bar's AND/OR toggle decides. Empty boxes impose no constraint; an
        entirely empty bar matches everything. All matching is
        case-insensitive substring."""
        if self._map_filter_state != "custom":
            return True
        pairs = [
            (self._cf_inputs["from"].text(),    row[3]),
            (self._cf_inputs["to"].text(),      row[4]),
            (self._cf_inputs["grid"].text(),    row[6]),
            (self._cf_inputs["remarks"].text(), row[20]),
        ]
        results = []
        for raw, field in pairs:
            terms = [t for t in re.split(r"[,\s]+", raw.strip().upper()) if t]
            if not terms:
                continue          # empty (or comma-only) box = no constraint
            hay = str(field or "").upper()
            results.append(any(t in hay for t in terms))
        if not results:
            return True
        return any(results) if self._custom_filter_mode == "or" else all(results)

    def _apply_custom_filter(self) -> None:
        """Re-filter the table and map from the Custom Filtering bar."""
        if self._map_filter_state != "custom":
            return
        self._load_statrep_data()
        self._save_map_position(callback=self._load_map)

    def _setup_map_widget(self) -> None:
        """Create the map widget using QWebEngineView."""
        self.map_widget = QWebEngineView(self.central_widget)
        self.map_widget.setObjectName("mapWidget")
        self.map_widget.setMinimumSize(320, 180)
        self.map_widget.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Preferred)

        # Set custom page to handle statrep links
        custom_page = CustomWebEnginePage(self)
        self.map_widget.setPage(custom_page)

        # QWebEngineView paints white for an instant on setHtml() while the new
        # document loads (its own background isn't parsed/painted yet), which
        # reads as a white flash on every map refresh in dark mode. Matching the
        # page's idle background to the current map theme up front removes it.
        self._update_map_background_color()

        # Add to resizable map stack
        self.map_widget.setMinimumSize(320, 180)
        self.map_widget.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Preferred)
        self.map_stack.addWidget(self.map_widget)

        # Polls the live Leaflet map's viewport bounds while Map Filter is
        # "map" (see _poll_map_bounds); not started until that state is set.
        self._map_bounds_timer = QtCore.QTimer(self)
        self._map_bounds_timer.setInterval(750)
        self._map_bounds_timer.timeout.connect(self._poll_map_bounds)

        # Setup map disabled label (hidden by default)
        self._setup_map_disabled_label()

        # Setup view-toggle buttons below map
        self._setup_map_view_buttons()

    def _update_map_background_color(self) -> None:
        """Set map_widget's idle page background to match the current map
        theme, so the moment of white shown while a new setHtml() document
        loads reads as the theme color instead of a white flash."""
        dark = self.config.get_map_theme() == "dark"
        self.map_widget.page().setBackgroundColor(QColor("#101510" if dark else "#FFFFFF"))

    def _setup_map_disabled_label(self) -> None:
        """Create the label/image display shown when map is hidden."""
        self.map_disabled_label = ClickableLabel(self.central_widget)
        self.map_disabled_label.setMinimumSize(320, 180)
        self.map_disabled_label.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Expanding
        )
        self.map_disabled_label.setAlignment(Qt.AlignCenter)
        self.map_disabled_label.setCursor(QtGui.QCursor(Qt.PointingHandCursor))

        # Use feed colors for background
        bg_color = self.config.get_color('feed_background')
        fg_color = self.config.get_color('feed_foreground')
        self.map_disabled_label.setStyleSheet(
            f"background-color: {bg_color}; color: {fg_color}; font-size: 18px; font-weight: bold;"
        )

        # Prev/Next step through the slideshow (alphabetical order); only
        # visible while the mouse is over the frame - same overlay style
        # and hover behavior as the video player and Alerts nav buttons.
        _NAV_BTN_QSS = (
            "QPushButton { background-color: rgba(0,0,0,0.65); color: white;"
            " font-family: Roboto; font-size: 13px; border: 1px solid #999;"
            " border-radius: 4px; padding: 4px 12px; }"
            "QPushButton:hover { background-color: rgba(40,167,69,0.85); }"
        )
        self.image_prev_btn = QtWidgets.QPushButton("◀ Prev", self.map_disabled_label)
        self.image_prev_btn.setStyleSheet(_NAV_BTN_QSS)
        self.image_prev_btn.clicked.connect(lambda: self._image_navigate(-1))

        self.image_next_btn = QtWidgets.QPushButton("Next ▶", self.map_disabled_label)
        self.image_next_btn.setStyleSheet(_NAV_BTN_QSS)
        self.image_next_btn.clicked.connect(lambda: self._image_navigate(1))

        self.map_disabled_label.clicked.connect(self._on_image_label_clicked)

        self._image_hovering = False
        self.image_prev_btn.hide()
        self.image_next_btn.hide()

        self.map_disabled_label.installEventFilter(self)

        self.map_stack.addWidget(self.map_disabled_label)

        # Image slideshow state
        self.slideshow_items: List[str] = []
        self.slideshow_index: int = 0
        self._slideshow_source_pixmap: Optional[QtGui.QPixmap] = None

        # Timer for slideshow
        self.slideshow_timer = QtCore.QTimer(self)
        self.slideshow_timer.timeout.connect(self._show_next_image)
        self.slideshow_timer.setInterval(SLIDESHOW_INTERVAL * 60000)  # Convert minutes to ms

        # Setup alert display widget
        self._setup_alert_display()

    def _setup_alert_display(self) -> None:
        """Create the alert display widget shown when Show Alerts is enabled."""
        self.alert_display = QtWidgets.QWidget(self.central_widget)
        self.alert_display.setMinimumSize(320, 180)
        self.alert_display.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Expanding
        )

        # Track current alert index (0 = most recent)
        self.alert_index = 0

        # Use vertical layout
        alert_layout = QtWidgets.QVBoxLayout(self.alert_display)
        alert_layout.setAlignment(Qt.AlignTop)

        # Add small spacing at top
        alert_layout.addSpacing(2)

        # Title label (first line)
        self.alert_title_label = QtWidgets.QLabel()
        self.alert_title_label.setAlignment(Qt.AlignCenter)
        self.alert_title_label.setTextFormat(Qt.RichText)  # Enable HTML formatting
        # Title uses Roboto Slab with Black weight (heaviest/900)
        title_font = QtGui.QFont("Roboto Slab", 24, QtGui.QFont.Black)
        self.alert_title_label.setFont(title_font)
        alert_layout.addWidget(self.alert_title_label)

        # Message label (second line)
        self.alert_message_label = QtWidgets.QLabel()
        self.alert_message_label.setAlignment(Qt.AlignCenter)
        self.alert_message_label.setWordWrap(True)
        self.alert_message_label.setTextFormat(Qt.RichText)
        self.alert_message_label.setOpenExternalLinks(True)
        # Message uses Roboto (clean sans-serif for readability)
        message_font = QtGui.QFont("Roboto", 18)
        self.alert_message_label.setFont(message_font)
        alert_layout.addWidget(self.alert_message_label)

        # Spacer between message and date
        alert_layout.addStretch(1)

        # Date received label (at bottom)
        self.alert_date_label = QtWidgets.QLabel()
        self.alert_date_label.setAlignment(Qt.AlignCenter)
        self.alert_date_label.setTextFormat(Qt.RichText)  # Enable HTML formatting
        # Date uses Roboto (clean sans-serif for readability)
        date_font = QtGui.QFont("Roboto", -1)
        date_font.setPixelSize(19)
        self.alert_date_label.setFont(date_font)
        alert_layout.addWidget(self.alert_date_label)
        alert_layout.addSpacing(46)

        # Prev/Next/Delete float directly on alert_display (not in
        # alert_layout) so they can be pinned to the corners/bottom-center —
        # mirrors the video player's overlay Prev/Next/Delete buttons.
        # _reposition_alert_nav_buttons keeps them pinned on resize.
        # Same size/style as the video player's Prev/Next overlay buttons
        # (rgba(0,0,0,0.65), 1px #999 border, 4px radius, green hover) —
        # auto-sized to content like their HTML/CSS counterparts, not fixed.
        _NAV_BTN_QSS = (
            "QPushButton { background-color: rgba(0,0,0,0.65); color: white;"
            " font-family: Roboto; font-size: 13px; border: 1px solid #999;"
            " border-radius: 4px; padding: 4px 12px; }"
            "QPushButton:hover { background-color: rgba(40,167,69,0.85); }"
        )
        self.alert_prev_btn = QtWidgets.QPushButton("◀ Prev", self.alert_display)
        self.alert_prev_btn.setStyleSheet(_NAV_BTN_QSS)
        self.alert_prev_btn.clicked.connect(lambda: self._alert_navigate(1))

        self.alert_next_btn = QtWidgets.QPushButton("Next ▶", self.alert_display)
        self.alert_next_btn.setStyleSheet(_NAV_BTN_QSS)
        self.alert_next_btn.clicked.connect(lambda: self._alert_navigate(-1))

        self.alert_delete_btn = QtWidgets.QPushButton("Delete", self.alert_display)
        self.alert_delete_btn.setStyleSheet(
            "QPushButton { background-color: rgba(220,53,69,0.85); color: white; font-family: Roboto; font-size: 13px; font-weight: bold; border: none; border-radius: 4px; padding: 4px 12px; }"
            "QPushButton:hover { background-color: rgba(200,35,51,0.9); }"
            "QPushButton:pressed { background-color: rgba(189,33,48,0.95); }"
        )
        self.alert_delete_btn.clicked.connect(self._alert_delete)

        # Prev/Next/Delete only show while the mouse is over the frame -
        # mirrors the video player's body:hover behavior.
        self._alert_hovering = False
        self._alert_has_alert = False
        self.alert_prev_btn.hide()
        self.alert_next_btn.hide()
        self.alert_delete_btn.hide()

        self.alert_display.installEventFilter(self)

        # Default styling (will be updated when alert is displayed)
        self.alert_display.setStyleSheet("background-color: #333333;")
        self.alert_title_label.setStyleSheet("color: #ffffff;")
        self.alert_message_label.setStyleSheet("color: #ffffff; font-family: Roboto;")
        self.alert_date_label.setStyleSheet("color: #ffffff; font-family: Roboto;")

        self.map_stack.addWidget(self.alert_display)

    def _reposition_alert_nav_buttons(self) -> None:
        """Pin Prev (bottom-left), Next (bottom-right), and Delete
        (bottom-center) over the alert display's current size — matches the
        video player's bottom-row Prev/Next/Delete layout."""
        w = self.alert_display.width()
        h = self.alert_display.height()
        margin = 8
        self.alert_prev_btn.resize(self.alert_prev_btn.sizeHint())
        self.alert_next_btn.resize(self.alert_next_btn.sizeHint())
        self.alert_prev_btn.move(margin, h - self.alert_prev_btn.height() - margin)
        self.alert_next_btn.move(w - self.alert_next_btn.width() - margin,
                                  h - self.alert_next_btn.height() - margin)
        del_size = self.alert_delete_btn.sizeHint()
        self.alert_delete_btn.resize(del_size)
        self.alert_delete_btn.move((w - del_size.width()) // 2, h - del_size.height() - margin)
        for btn in (self.alert_prev_btn, self.alert_next_btn, self.alert_delete_btn):
            btn.raise_()

    def _update_alert_nav_visibility(self) -> None:
        """Show Prev/Next/Delete only while the mouse is over the alert
        frame - mirrors the video player's body:hover behavior."""
        hovering = self._alert_hovering
        self.alert_prev_btn.setVisible(hovering)
        self.alert_next_btn.setVisible(hovering)
        self.alert_delete_btn.setVisible(hovering and self._alert_has_alert)

    def _reposition_message_filter_btn(self) -> None:
        """Pin the Filter toggle to the message table's title bar, all the
        way to the right (left of the vertical scrollbar if visible). The
        record counter is centered in the single gap between the "ID"
        column and the FILTER link."""
        if not hasattr(self, 'message_filter_btn'):
            return
        btn = self.message_filter_btn
        table = self.message_table
        margin = 23  # yields a 40px gap to the table edge (23 + 17px scrollbar)
        scrollbar = table.verticalScrollBar()
        sb_w = scrollbar.width() if scrollbar and scrollbar.isVisible() else 0
        # Center within the label band only - the header is taller while the
        # pinned filter strip is shown, and the link belongs with the labels.
        header_h = table.horizontalHeader().label_h
        x = table.width() - btn.width() - sb_w - margin
        y = max(0, (header_h - btn.height()) // 2)
        btn.move(max(0, x), y)
        btn.raise_()

        if hasattr(self, 'message_count_label'):
            count_label = self.message_count_label
            last_col = table.columnCount() - 1
            left_edge = table.horizontalHeader().sectionViewportPosition(last_col)
            right_edge = x  # left edge of the FILTER link
            center = left_edge + (right_edge - left_edge) / 2

            cx = center - count_label.width() / 2
            cy = max(0, (header_h - count_label.height()) // 2)
            count_label.move(max(0, int(cx)), cy)
            count_label.raise_()

    def _reposition_statrep_filter_btn(self) -> None:
        """Pin the Filter toggle to the StatRep table's title bar, all the
        way to the right (left of the vertical scrollbar if visible). The
        gap between the "Pol" column and the FILTER link is split into 5
        equal sections: 1 empty, 2 "Remarks", 3 empty, 4 the record
        counter, 5 empty."""
        if not hasattr(self, 'statrep_filter_btn'):
            return
        btn = self.statrep_filter_btn
        table = self.statrep_table
        margin = 23  # matches message_filter_btn: 40px gap to the table edge
        scrollbar = table.verticalScrollBar()
        sb_w = scrollbar.width() if scrollbar and scrollbar.isVisible() else 0
        header_h = table.horizontalHeader().height()
        x = table.width() - btn.width() - sb_w - margin
        y = max(0, (header_h - btn.height()) // 2)
        btn.move(max(0, x), y)
        btn.raise_()

        if hasattr(self, 'statrep_remarks_label') and hasattr(self, 'statrep_count_label'):
            remarks_label = self.statrep_remarks_label
            count_label = self.statrep_count_label
            remarks_col = len(STATREP_HEADERS) - 1
            left_edge = table.horizontalHeader().sectionViewportPosition(remarks_col)
            right_edge = x  # left edge of the FILTER link
            fifth = max(0, right_edge - left_edge) / 5

            rx = left_edge + fifth * 1.5 - remarks_label.width() / 2
            ry = max(0, (header_h - remarks_label.height()) // 2)
            remarks_label.move(max(0, int(rx)), ry)
            remarks_label.raise_()

            cx = left_edge + fifth * 3.5 - count_label.width() / 2
            cy = max(0, (header_h - count_label.height()) // 2)
            count_label.move(max(0, int(cx)), cy)
            count_label.raise_()

    def _toggle_statrep_custom_filter(self) -> None:
        """FILTER label above the StatRep table - toggles the same Custom
        Filtering state as the Map Filter dropdown's Custom option, so the
        map and table stay in sync with whichever control was used."""
        new_state = "off" if self._map_filter_state == "custom" else "custom"
        self._on_map_filter_set(new_state)

    def _setup_map_view_buttons(self) -> None:
        """Apply the default map region as a baseline (buttons live in the
        status bar). Widgets built later in _setup_ui() (message_table,
        contacts_widget, video data) aren't ready yet, so this always starts
        on the map; _apply_restored_view() switches to the last-visible view
        once construction finishes, if one was saved."""
        default = self.db.get_default_map()
        if default not in ("us", "eu", "mideast", "seasia", "world"):
            default = "us"
        self._set_map_view_mode(default)

    def _apply_restored_view(self) -> None:
        """Switch to the view (map region / images / videos / alerts /
        contacts) and map pane size that were active when the app last
        closed, as read into self._pending_* by _restore_window_position().
        Runs once via QTimer.singleShot(0, ...) after __init__ completes, so
        every widget _set_map_view_mode() touches actually exists."""
        valid_modes = ("us", "eu", "mideast", "seasia", "world",
                       "images", "videos", "alerts", "contacts")
        pending_view = getattr(self, "_pending_last_view", None)
        pending_height = getattr(self, "_pending_pane_height", None)
        pending_width = getattr(self, "_pending_pane_width", None)

        if pending_height:
            self._saved_map_pane_height = pending_height
        if pending_width:
            self._saved_map_pane_width = pending_width

        if pending_view in valid_modes and pending_view != self._current_view_mode:
            self._set_map_view_mode(pending_view)

        if pending_height and hasattr(self, "bottom_splitter") and not self.bottom_splitter.isHidden():
            total = self.content_splitter.height()
            feed_h = self.feed_text.height() if hasattr(self, "feed_text") else 100
            handle_px = self.content_splitter.handleWidth() * 2
            statrep_h = max(100, total - feed_h - pending_height - handle_px)
            self.content_splitter.setSizes([statrep_h, feed_h, pending_height])
            if pending_width:
                remaining = max(100, self.bottom_splitter.width() - pending_width)
                self.bottom_splitter.setSizes([pending_width, remaining])

    def _set_map_view_mode(self, mode: str) -> None:
        """Switch the map panel between region maps, Images, Alerts, and Contacts views."""
        INACTIVE = "background-color: #DDDDDD; color: #000000; border: none; border-radius: 4px; padding: 2px 10px;"
        ACTIVE   = "background-color: #28a745; color: white; border: none; border-radius: 4px; padding: 2px 10px;"

        # Region presets: (center_lat, center_lng), zoom
        REGION_VIEWS = {
            "us":      ((38.8199286, -96.7782551), 4),
            "eu":      ((45.8150, 15.9819),        4),
            "mideast": ((31.7683, 35.2137),        4),
            "seasia":  ((22.4778, 101.1718),       4),
            "world":   ((33.7948, -83.7132),       1),
        }

        self._current_view_mode = mode

        # Non-region, non-Videos buttons are styled directly; region buttons
        # go through _update_region_button_pin_indicators and Videos through
        # _update_video_button_indicator so they can show orange when
        # inactive (pins on the map / unplayed video, respectively).
        for m in ("images", "alerts", "contacts"):
            btn = getattr(self, f"_btn_{m}", None)
            if btn:
                btn.setStyleSheet(ACTIVE if m == mode else INACTIVE)
        self._update_video_button_indicator()
        self._update_region_button_pin_indicators()

        if mode in REGION_VIEWS:
            center, zoom = REGION_VIEWS[mode]
            self.map_center = center
            self.map_zoom = zoom
            self._last_map_region = mode
            self._stop_slideshow()
            self._show_bottom_section()
            self.map_stack.setCurrentWidget(self.map_widget)
            self._unlock_map_pane()
            if hasattr(self, 'contacts_widget'):
                self.contacts_widget.hide()
            if hasattr(self, 'message_table'):
                self.message_table.show()
            self.config.set_hide_map(False)
            self.config.set_show_alerts(False)
            self.config.set_show_contacts(False)
            self._load_map()
        elif mode == "images":
            self._stop_slideshow()
            self._show_bottom_section()
            self.map_stack.setCurrentWidget(self.map_disabled_label)
            self._reposition_image_nav_buttons()
            self._unlock_map_pane()
            if hasattr(self, 'contacts_widget'):
                self.contacts_widget.hide()
            if hasattr(self, 'message_table'):
                self.message_table.show()
            self._start_slideshow()
            self.config.set_hide_map(True)
            self.config.set_show_alerts(False)
            self.config.set_show_contacts(False)
        elif mode == "videos":
            self._stop_slideshow()
            self._show_bottom_section()
            self.map_stack.setCurrentWidget(self.map_widget)
            self._unlock_map_pane()
            if hasattr(self, 'contacts_widget'):
                self.contacts_widget.hide()
            if hasattr(self, 'message_table'):
                self.message_table.show()
            self.config.set_hide_map(False)
            self.config.set_show_alerts(False)
            self.config.set_show_contacts(False)
            self._video_index = 0
            self._play_video_at_index()
        elif mode == "alerts":
            self._stop_slideshow()
            self._show_bottom_section()
            self._unlock_map_pane()
            if hasattr(self, 'contacts_widget'):
                self.contacts_widget.hide()
            if hasattr(self, 'message_table'):
                self.message_table.show()
            self.config.set_hide_map(True)
            self.config.set_show_alerts(True)
            self.config.set_show_contacts(False)
            self.alert_index = 0
            self._show_alert_display()
        elif mode == "contacts":
            self._stop_slideshow()
            # Remember the pane height/width so they can be restored when leaving Contacts
            if not self.bottom_splitter.isHidden():
                self._saved_map_pane_height = self.bottom_splitter.height()
                sizes = self.bottom_splitter.sizes()
                if sizes:
                    self._saved_map_pane_width = sizes[0]
            self.bottom_splitter.hide()
            self.config.set_hide_map(True)
            self.config.set_show_alerts(False)
            self.config.set_show_contacts(True)
            if hasattr(self, 'contacts_widget'):
                self.contacts_widget.show()
                # Give the contacts view the same height the pane had
                pane_h = getattr(self, '_saved_map_pane_height', 0) or 340
                total = self.content_splitter.height()
                feed_h = self.feed_text.height() if hasattr(self, 'feed_text') else 100
                handle_px = self.content_splitter.handleWidth() * 3
                statrep_h = max(100, total - feed_h - pane_h - handle_px)
                self.content_splitter.setSizes([statrep_h, feed_h, 0, pane_h])
                self._load_contacts_data()
                # The pinned filter boxes keep their text across view
                # switches; honor it (and its Records count) on the reload.
                self._apply_contacts_filter()
                self.contacts_table.viewport().setFocus()
            else:
                QTimer.singleShot(0, lambda: self._set_map_view_mode("contacts"))

    def _unlock_map_pane(self, min_w: int = 604, min_h: int = 340) -> None:
        """Make the lower-left pane splitter-resizable with the given minimum."""
        self.map_stack.setMinimumSize(min_w, min_h)
        self.map_stack.setMaximumSize(16777215, 16777215)
        self.map_stack.setSizePolicy(
            QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Preferred)
        self.bottom_splitter.setMinimumHeight(min_h)
        self.bottom_splitter.setMaximumHeight(16777215)

    def _show_bottom_section(self) -> None:
        """Show the bottom splitter, restoring the operator's pane height when
        returning from the Contacts view (which hides the whole section).
        Splitter sizes are otherwise left alone so a user resize survives
        switching between map, images, alerts, and video."""
        returning = self.bottom_splitter.isHidden()
        self.bottom_splitter.show()
        saved_h = getattr(self, '_saved_map_pane_height', 0)
        if returning and hasattr(self, 'contacts_widget') and self.contacts_widget.isVisible():
            # Carry over any resize the user made while in the Contacts view
            saved_h = self.contacts_widget.height() or saved_h
            self._saved_map_pane_height = saved_h
        if returning and saved_h:
            total = self.content_splitter.height()
            feed_h = self.feed_text.height() if hasattr(self, 'feed_text') else 100
            handle_px = self.content_splitter.handleWidth() * 2
            statrep_h = max(100, total - feed_h - saved_h - handle_px)
            self.content_splitter.setSizes([statrep_h, feed_h, saved_h])

    def _update_region_button_pin_indicators(self) -> None:
        """
        Style the four region buttons from current state:
          - selected region  → green
          - has pins on it   → orange (#F07800)
          - otherwise        → gray
        Pin counts come from self._region_pin_counts (filled by _load_map);
        selected region comes from self._current_view_mode.
        """
        INACTIVE = "background-color: #DDDDDD; color: #000000; border: none; border-radius: 4px; padding: 2px 10px;"
        ACTIVE   = "background-color: #28a745; color: white;   border: none; border-radius: 4px; padding: 2px 10px;"
        HASPINS  = "background-color: #F07800; color: white;   border: none; border-radius: 4px; padding: 2px 10px;"

        counts  = getattr(self, "_region_pin_counts", {}) or {}
        current = getattr(self, "_current_view_mode", "")

        for region in ("us", "eu", "mideast", "seasia", "world"):
            btn = getattr(self, f"_btn_{region}", None)
            if not btn:
                continue
            if region == current:
                btn.setStyleSheet(ACTIVE)
            elif counts.get(region, 0) > 0:
                btn.setStyleSheet(HASPINS)
            else:
                btn.setStyleSheet(INACTIVE)

    def _update_video_button_indicator(self) -> None:
        """
        Style the Videos button from current state:
          - active view        → green
          - has unplayed video → orange (#F07800)
          - otherwise          → gray
        Mirrors _update_region_button_pin_indicators for the region buttons.
        """
        btn = getattr(self, '_btn_videos', None)
        if not btn:
            return
        INACTIVE = "background-color: #DDDDDD; color: #000000; border: none; border-radius: 4px; padding: 2px 10px;"
        ACTIVE   = "background-color: #28a745; color: white;   border: none; border-radius: 4px; padding: 2px 10px;"
        HASPINS  = "background-color: #F07800; color: white;   border: none; border-radius: 4px; padding: 2px 10px;"

        if getattr(self, "_current_view_mode", "") == "videos":
            btn.setStyleSheet(ACTIVE)
        elif self.db.has_unplayed_video():
            btn.setStyleSheet(HASPINS)
        else:
            btn.setStyleSheet(INACTIVE)

    def _alert_font_scale(self) -> float:
        """Font scale proportional to the pane size, relative to the default
        604 x 340 pane. Measures map_stack (not alert_display) because a stack
        page that isn't current yet still has stale geometry."""
        w, h = self.map_stack.width(), self.map_stack.height()
        if w < 50 or h < 50:
            # Not laid out yet
            return 1.0
        return max(0.55, min(2.5, min(w / MAP_WIDTH, h / MAP_HEIGHT)))

    def _fit_alert_message(self, base_pt: float) -> None:
        """Shrink the message font until the wrapped text fits the space left
        over after the title and date, keeping clear of the floating Delete
        button pinned to the bottom edge."""
        label = self.alert_message_label
        avail = (self.map_stack.height()
                 - self.alert_title_label.sizeHint().height()
                 - self.alert_date_label.sizeHint().height()
                 - 30    # clearance for the floating Delete button
                 - 50)   # layout margins/spacing
        if avail <= 0:
            return
        # Pane width minus the alert layout's side margins (map_stack, not the
        # label, because the label's geometry is stale before the page shows)
        width = max(self.map_stack.width() - 22, 100)
        font = label.font()
        pt = base_pt
        while pt > 8:
            font.setPointSizeF(pt)
            label.setFont(font)
            # heightForWidth lays out the wrapped rich text at the given width
            if label.heightForWidth(width) <= avail:
                break
            pt -= 1

    def _show_alert_display(self) -> None:
        """Show the alert display with the current alert from database."""
        # Get total alert count and fetch alert at current index
        alert_count = self.db.get_alert_count()
        alert = self.db.get_alert_at_offset(self.alert_index)
        scale = self._alert_font_scale()

        # Update navigation button states
        self.alert_prev_btn.setEnabled(self.alert_index < alert_count - 1)
        self.alert_next_btn.setEnabled(self.alert_index > 0)

        if alert:
            title, message, color, date_received, from_callsign, group = alert

            # Set colors based on alert color - all alerts use red
            color_map = {
                1: ("#333333", "#ffffff"),  # was #dc3545 (formerly Yellow)
                2: ("#333333", "#ffffff"),  # was #dc3545 (formerly Orange)
                3: ("#333333", "#ffffff"),  # was #dc3545
                4: ("#333333", "#ffffff"),  # was #dc3545 (formerly Black)
            }
            bg_color, text_color = color_map.get(color, ("#dc3545", "#ffffff"))

            # Format date to remove seconds (e.g., "2026-01-15 11:00:00" -> "2026-01-15 11:00")
            date_formatted = date_received[:16] if len(date_received) > 16 else date_received

            # Build date/callsign line with bold labels (use Roboto font)
            date_line = f'<span style="font-family: Roboto;"><b>Date Sent:</b> {date_formatted}'
            if from_callsign:
                date_line += f"&nbsp;&nbsp;&nbsp;<b>Sent By:</b> {from_callsign}"
            date_line += "</span>"

            # Format alert display:
            # Top: group - ALERT (smaller font)
            # Middle: title (bold, bigger than message)
            # Bottom: message (normal)
            if group:
                # Show group + ALERT at top, then title in bold below (strip @ symbol)
                group_display = group.lstrip('@')
                formatted_title = f'<div style="font-family: \'Kode Mono\'; font-size: {round(22 * scale)}px; font-weight: bold; margin-top: {round(-6 * scale)}px;">@{group_display} - ALERT</div>'
                if title:
                    formatted_title += f'<div style="font-family: \'Roboto Slab\'; font-size: {round(30 * scale)}px; font-weight: 900; margin-top: {round(18 * scale)}px;">{title}</div>'
            else:
                # No group, just show title in bold
                formatted_title = f'<div style="font-family: \'Roboto Slab\'; font-size: {round(26 * scale)}px; font-weight: 900;">{title if title else ""}</div>'

            self.alert_display.setStyleSheet(f"background-color: {bg_color};")
            self.alert_title_label.setStyleSheet(f"color: {text_color};")
            self.alert_message_label.setStyleSheet(f"color: {text_color}; font-family: Roboto;")
            self.alert_date_label.setStyleSheet(f"color: {text_color}; font-family: Roboto;")
            self.alert_title_label.setText(formatted_title)
            message_font = self.alert_message_label.font()
            message_font.setPointSizeF(18 * scale)
            self.alert_message_label.setFont(message_font)
            date_font = self.alert_date_label.font()
            date_font.setPixelSize(max(10, round(19 * scale)))
            self.alert_date_label.setFont(date_font)
            # JS8Call transmits/decodes radio traffic in all-caps, so alert
            # text often arrives as "HTTPS://..." — match case-insensitively
            # or those links would never get highlighted.
            _parts = re.split(r'(https?://\S+)', message, flags=re.IGNORECASE)
            _msg_html = "".join(
                f'<a href="{p.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")}"'
                f' style="color:#00FF00;">'
                f'{p.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")}</a>'
                if i % 2 else
                p.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                for i, p in enumerate(_parts)
            )
            self.alert_message_label.setText(_msg_html)
            self.alert_date_label.setText(date_line)
            self._fit_alert_message(18 * scale)
        else:
            # No alerts - show placeholder
            self.alert_display.setStyleSheet("background-color: #333333;")
            self.alert_title_label.setStyleSheet("color: #ffffff;")
            self.alert_message_label.setStyleSheet("color: #ffffff; font-family: Roboto;")
            self.alert_date_label.setStyleSheet("color: #ffffff; font-family: Roboto;")
            title_font = self.alert_title_label.font()
            title_font.setPointSizeF(24 * scale)
            self.alert_title_label.setFont(title_font)
            self.alert_title_label.setText("No Alerts")
            self.alert_message_label.setText("")
            self.alert_date_label.setText("")

        self._alert_has_alert = alert is not None
        self._update_alert_nav_visibility()
        self.map_stack.setCurrentWidget(self.alert_display)
        self._reposition_alert_nav_buttons()

    def _alert_navigate(self, direction: int) -> None:
        """Navigate alerts by direction (-1 for newer, +1 for older)."""
        new_index = self.alert_index + direction
        if direction < 0 and new_index >= 0:
            self.alert_index = new_index
            self._show_alert_display()
        elif direction > 0 and new_index < self.db.get_alert_count():
            self.alert_index = new_index
            self._show_alert_display()

    def _alert_delete(self) -> None:
        """Delete the currently displayed alert and refresh the view."""
        self.db.delete_alert_at_offset(self.alert_index)
        count = self.db.get_alert_count()
        if self.alert_index >= count:
            self.alert_index = max(0, count - 1)
        self._show_alert_display()

    def _fetch_commsrvr_content(self) -> Optional[str]:
        """Fetch and extract content from commsrvr server.

        Returns:
            Extracted content string, or None on error.
        """
        try:
            # Get callsign: prefer first active JS8 connector callsign, fall back to user settings
            callsign = next((cs for cs in self.rig_callsigns.values() if cs), None)
            if not callsign:
                callsign, _, __ = self.db.get_user_settings()
            if not callsign:
                callsign = "UNKNOWN"

            # Get db_version, build_number, data_id, and qrz_id from controls table
            db_version = 0
            build_number = 500  # Default fallback
            data_id = 0  # Default fallback
            qrz_id = 0  # Default fallback
            try:
                with sqlite3.connect(DATABASE_FILE, timeout=10) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT db_version, build_number, data_id, qrz_id FROM controls WHERE id = 1")
                    result = cursor.fetchone()
                    if result:
                        db_version = result[0]
                        build_number = result[1] if len(result) > 1 else 500
                        data_id = result[2] if len(result) > 2 else 0
                        qrz_id = result[3] if len(result) > 3 and result[3] is not None else 0
            except sqlite3.Error:
                pass  # Use default values if query fails

            # Only report qrz_id when at least one JS8 connector is in "Connected" status
            qrz_value = qrz_id if self.tcp_pool.get_connected_rig_names() else 0

            # Build heartbeat URL with callsign, data_id, qrz_id, db_version, and build_number parameters
            heartbeat_url = f"{_PING}?cs={callsign}&id={data_id}&qrz={qrz_value}&db={db_version}&build={build_number}&version={VERSION}"

            request = urllib.request.Request(heartbeat_url)
            with urllib.request.urlopen(request, timeout=10, context=create_verified_ssl_context()) as response:
                content = response.read().decode('utf-8')

            return content.strip() or None
        except Exception as e:
            # Never silent: this path was previously a black hole, making field
            # diagnosis (e.g. TLS cert-verification failures) impossible.
            print(f"Error contacting commsrvr heartbeat: {type(e).__name__}: {e}")
            return None

    def _handle_db_update(self, content: str) -> bool:
        """Handle database update from commsrvr server.

        Expected format:
        db_update
        db: 3
        sql:
        CREATE TABLE ... );
        INSERT INTO ... );

        Each SQL statement ends with };

        Args:
            content: The db_update response content

        Returns:
            True if update was successful, False otherwise
        """
        try:
            lines = content.split('\n')

            if not lines or lines[0].strip() != 'db_update':
                return False

            new_db_version = None
            sql_section = None

            # Find db version and sql section
            for i, line in enumerate(lines):
                if line.strip().startswith('db:'):
                    try:
                        new_db_version = int(line.split(':', 1)[1].strip())
                    except (ValueError, IndexError):
                        return False
                elif line.strip().startswith('sql:'):
                    # SQL may start on this line or the next
                    sql_start = line.split(':', 1)[1].strip()
                    if sql_start:
                        # SQL starts on same line
                        sql_section = sql_start + '\n' + '\n'.join(lines[i+1:])
                    else:
                        # SQL starts on next line
                        sql_section = '\n'.join(lines[i+1:])
                    break

            if new_db_version is None or sql_section is None:
                return False

            # Split SQL statements by semicolon
            sql_statements = []
            raw_statements = sql_section.split(';')

            for stmt in raw_statements:
                stmt = stmt.strip()
                if stmt:  # Skip empty statements
                    sql_statements.append(stmt)

            if not sql_statements:
                return False

            # Execute SQL statements
            try:
                with sqlite3.connect(DATABASE_FILE, timeout=10) as conn:
                    cursor = conn.cursor()
                    for sql in sql_statements:
                        cursor.execute(sql)
                    cursor.execute("UPDATE controls SET db_version = ? WHERE id = 1", (new_db_version,))
                    conn.commit()
                print(f"Database updated successfully to version {new_db_version}")
                return True
            except sqlite3.Error as e:
                print(f"Database update failed: {e}")
                return False

        except Exception as e:
            print(f"Error handling db_update: {e}")
            return False

    def _handle_qrz_update(self, content: str) -> bool:
        """Handle a qrz_update push from commsrvr server.

        Expected format:
        qrz_update
        sql: BEGIN TRANSACTION;
        {SQL statements}
        COMMIT;
        UPDATE controls SET qrz_id = 1119;

        Unlike db_update, this format carries no `db:` line and does not
        bump controls.db_version — it's purely a qrz_id sync notification.

        Args:
            content: The qrz_update response content

        Returns:
            True if update was successful, False otherwise
        """
        try:
            lines = content.split('\n')

            if not lines or lines[0].strip() != 'qrz_update':
                return False

            sql_section = None

            # Find sql section
            for i, line in enumerate(lines):
                if line.strip().startswith('sql:'):
                    # SQL may start on this line or the next
                    sql_start = line.split(':', 1)[1].strip()
                    if sql_start:
                        # SQL starts on same line
                        sql_section = sql_start + '\n' + '\n'.join(lines[i+1:])
                    else:
                        # SQL starts on next line
                        sql_section = '\n'.join(lines[i+1:])
                    break

            if sql_section is None:
                return False

            # Split SQL statements by semicolon
            sql_statements = []
            raw_statements = sql_section.split(';')

            for stmt in raw_statements:
                stmt = stmt.strip()
                if stmt:  # Skip empty statements
                    sql_statements.append(stmt)

            if not sql_statements:
                return False

            qrz_id_pattern = re.compile(r'UPDATE\s+controls\s+SET\s+qrz_id\s*=\s*(\d+)', re.IGNORECASE)
            qrz_id_value = None

            # Execute SQL statements
            try:
                with sqlite3.connect(DATABASE_FILE, timeout=10) as conn:
                    cursor = conn.cursor()
                    for sql in sql_statements:
                        cursor.execute(sql)
                        match = qrz_id_pattern.search(sql)
                        if match:
                            qrz_id_value = match.group(1)
                    conn.commit()
                if qrz_id_value is not None:
                    print(f"Updated qrz_id to {qrz_id_value} in controls table.")
                else:
                    print("qrz_update applied successfully.")
                return True
            except sqlite3.Error as e:
                print(f"Database update failed: {e}")
                return False

        except Exception as e:
            print(f"Error handling qrz_update: {e}")
            return False

    def _handle_program_update(self, content: str) -> bool:
        """Handle program update from commsrvr server.

        Expected format:
        program_update
        build: 501
        url: https://commstat.com/downloads/update.zip

        Args:
            content: The program_update response content

        Returns:
            True if download was successful, False otherwise
        """
        try:
            lines = [line.strip() for line in content.split('\n') if line.strip()]
            if not lines or lines[0] != 'program_update':
                return False

            new_build = None
            download_url = None

            # Parse the update content
            for line in lines[1:]:
                if line.startswith('build:'):
                    try:
                        new_build = int(line.split(':', 1)[1].strip())
                    except (ValueError, IndexError):
                        print(f"Invalid build number format: {line}")
                        return False
                elif line.startswith('url:') or line.startswith('URL:'):
                    download_url = line.split(':', 1)[1].strip()
                    # Handle URLs that might have multiple colons (https://)
                    if '://' in line:
                        download_url = line.split(None, 1)[1].strip()

            if new_build is None or not download_url:
                print("Missing build number or URL in program_update")
                return False

            # Create updates directory if it doesn't exist
            import os
            updates_dir = os.path.join(os.path.dirname(__file__), 'updates')
            os.makedirs(updates_dir, exist_ok=True)

            update_file = os.path.join(updates_dir, 'update.zip')

            # Download the update
            print(f"Downloading update build {new_build} from {download_url}")

            try:
                with urllib.request.urlopen(download_url, timeout=30, context=create_verified_ssl_context()) as response:
                    with open(update_file, 'wb') as f:
                        f.write(response.read())

                print(f"Update downloaded successfully to {update_file}")

                # Update build_number in database
                try:
                    with sqlite3.connect(DATABASE_FILE, timeout=10) as conn:
                        conn.execute("UPDATE controls SET build_number = ? WHERE id = 1", (new_build,))
                        conn.commit()
                    print(f"Build number updated to {new_build} in database")
                except sqlite3.Error as e:
                    print(f"Warning: Failed to update build number in database: {e}")
                    # Continue anyway - the update file is downloaded

                # Show restart prompt to user
                QtCore.QMetaObject.invokeMethod(
                    self, "_show_program_update_notification",
                    QtCore.Qt.QueuedConnection,
                    QtCore.Q_ARG(int, new_build)
                )

                return True

            except Exception as e:
                print(f"Failed to download update: {e}")
                # Clean up partial download
                if os.path.exists(update_file):
                    os.remove(update_file)
                return False

        except Exception as e:
            print(f"Error handling program_update: {e}")
            return False

    def _is_valid_grid(self, grid: str) -> bool:
        """Check if a string looks like a valid Maidenhead grid square.

        Args:
            grid: String to validate

        Returns:
            True if it looks like a valid grid square (e.g., EM83CV, FN20, etc.)
        """
        if not grid:
            return False
        grid = grid.strip().upper()
        # Grid squares are 2 letters + 2 digits + optional 2 letters/digits
        # Examples: EM83, EM83CV, FN20XS
        if len(grid) < 4 or len(grid) > 8:
            return False
        # First 2 chars must be letters A-R
        if not (grid[0].isalpha() and grid[1].isalpha()):
            return False
        if not (grid[0] in 'ABCDEFGHIJKLMNOPQR' and grid[1] in 'ABCDEFGHIJKLMNOPQR'):
            return False
        # Next 2 must be digits
        if not (grid[2].isdigit() and grid[3].isdigit()):
            return False
        return True

    def _lookup_grid_for_callsign(self, callsign: str) -> Optional[str]:
        """Look up grid square for a callsign using QRZ cache/API.

        Args:
            callsign: Callsign to lookup

        Returns:
            Grid square or None if not found
        """
        try:
            from qrz_client import QRZClient, load_qrz_config

            # Local qrz table first, always — lookup() serves a cached row
            # (any age) when QRZ is disabled, unconfigured, or the account
            # has no XML subscription, and only calls the API for a
            # stale/missing row on a subscriber account.
            _active, username, password = load_qrz_config()
            client = QRZClient(username, password)
            result = client.lookup(callsign, use_cache=True)

            if result and result.get('grid'):
                grid = result['grid']
                print(f"[QRZ] Found grid {grid} for {callsign}")
                return grid

            return None
        except Exception as e:
            print(f"[QRZ] Error looking up {callsign}: {e}")
            return None

    def _resolve_grid(
        self,
        rig_name: str,
        grid: str,
        callsign: str,
        fallback_grid: str = "",
        msg_format: str = ""
    ) -> str:
        """
        Resolve grid square with QRZ fallback if needed.

        Args:
            rig_name: Rig identifier for logging
            grid: Primary grid square (may be empty/invalid)
            callsign: Callsign to lookup if grid is missing
            fallback_grid: Grid to use if QRZ lookup fails
            msg_format: Message format for logging (e.g., "STATREP", "F!304")

        Returns:
            Valid grid square or fallback
        """
        prefix = f"[{rig_name}] {msg_format}: " if msg_format else f"[{rig_name}] "

        # Case 1: Already have a precise grid (5+ chars) - use directly
        if grid and len(grid) > 4:
            return grid

        # Case 2: Have a 4-char grid - try to upgrade via QRZ
        if grid and len(grid) == 4:
            qrz_grid = self._lookup_grid_for_callsign(callsign)
            if qrz_grid and len(qrz_grid) > 4 and qrz_grid[:4].upper() == grid.upper():
                # Format as mixed case: first 4 upper + rest lower (e.g., EM83cv)
                # This makes QRZ-upgraded grids visually distinguishable
                qrz_grid = qrz_grid[:4].upper() + qrz_grid[4:].lower()
                print(f"{prefix}Upgraded grid {grid} -> {qrz_grid} via QRZ for {callsign}")
                return qrz_grid
            return grid

        # Try QRZ lookup for missing/invalid grid
        print(f"{prefix}Missing/invalid grid, attempting QRZ lookup for {callsign}")

        qrz_grid = self._lookup_grid_for_callsign(callsign)
        if qrz_grid:
            print(f"{prefix}Found grid {qrz_grid} via QRZ for {callsign}")
            return qrz_grid

        print(f"{prefix}QRZ lookup failed, using fallback grid")
        return fallback_grid if fallback_grid else ""

    def _find_callsign_duplicate(
        self,
        table: str,
        id_field: str,
        date_only: str,
        id_value: str,
        from_callsign: str
    ) -> Optional[tuple]:
        """
        Find an existing row for the same record sent by the same station.

        Looks up on (date, id_field) — the leading columns of the table's UNIQUE
        index — then compares base callsigns in Python so N0DDK and N0DDK/P match.

        Args:
            table: Table name (messages, alerts, statrep)
            id_field: Name of the record's ID column (msg_id, alert_id, sr_id)
            date_only: Date portion "YYYY-MM-DD"
            id_value: The record ID to match
            from_callsign: Incoming sender callsign (suffix included)

        Returns:
            (row_id, stored_from_callsign) of the matching row, or None.
        """
        incoming_base = base_callsign(from_callsign)
        if not incoming_base:
            return None
        try:
            with sqlite3.connect(DATABASE_FILE, timeout=10) as conn:
                rows = conn.execute(
                    f"SELECT id, from_callsign FROM {table} WHERE date = ? AND {id_field} = ?",
                    (date_only, id_value)
                ).fetchall()
        except sqlite3.Error:
            return None

        for row_id, stored_call in rows:
            if base_callsign(stored_call or "") == incoming_base:
                return (row_id, stored_call or "")
        return None

    def _upgrade_duplicate_row(
        self,
        table: str,
        row_id: int,
        stored_callsign: str,
        incoming_callsign: str,
        incoming_global_id: int = 0
    ) -> None:
        """
        Carry the richer values from a discarded duplicate onto the row we keep.

        Prefers the suffixed callsign: if the stored row has the bare base (N0DDK)
        and the duplicate carries the suffix the operator transmitted (N0DDK/P),
        promote the stored row so the fuller callsign survives no matter which copy
        arrived first. Also backfills global_id when the stored row lacks one.

        Args:
            table: Table name
            row_id: Primary key of the row being kept
            stored_callsign: from_callsign currently on that row
            incoming_callsign: from_callsign of the duplicate being discarded
            incoming_global_id: global_id from the duplicate (0 when absent/unsupported)
        """
        if "/" in (incoming_callsign or "") and "/" not in (stored_callsign or ""):
            try:
                with sqlite3.connect(DATABASE_FILE, timeout=10) as conn:
                    conn.execute(
                        f"UPDATE {table} SET from_callsign = ? WHERE id = ?",
                        (incoming_callsign, row_id)
                    )
                    conn.commit()
            except sqlite3.IntegrityError:
                # A suffixed row already occupies that UNIQUE key — leave this one as is.
                pass
            except sqlite3.Error:
                pass

        if incoming_global_id:
            try:
                with sqlite3.connect(DATABASE_FILE, timeout=10) as conn:
                    conn.execute(
                        f"UPDATE {table} SET global_id = ? "
                        f"WHERE id = ? AND (global_id IS NULL OR global_id = 0)",
                        (incoming_global_id, row_id)
                    )
                    conn.commit()
            except sqlite3.Error:
                pass

    def _insert_message_data(
        self,
        rig_name: str,
        table: str,
        data: dict,
        id_field: str,
        msg_type: str,
        from_callsign: str,
        extra_info: str = "",
        raw_line: str = ""
    ) -> str:
        """
        Generic database insert with standardized error handling.

        Args:
            rig_name: Rig identifier for logging
            table: Database table name
            data: Dict of column_name: value pairs
            id_field: Name of the ID field for duplicate detection
            msg_type: Return value on success (e.g., "statrep", "message")
            from_callsign: Sender callsign for logging
            extra_info: Optional extra info for success message (e.g., " (FORWARDED)")
            raw_line: Data line as received (verbatim for commsrvr traffic, an
                equivalent built from JS8Call params for TCP traffic). When set
                and this insert is a direct message to one of our callsigns,
                it's echoed back to the server as a delivery confirmation
                alongside the new-message popup.

        Returns:
            msg_type on success, empty string on failure
        """
        # Suffix-insensitive duplicate check. The UNIQUE indexes key on the literal
        # from_callsign, so a record saved locally as N0DDK/P and returned by the
        # commsrvr as N0DDK does not collide and would be written twice. Compare the
        # base callsigns here, before the insert, to catch that.
        # Skipped for videos (id_field='global_id' — their key is the server ID, not
        # the callsign) and for any table whose data dict lacks the columns.
        if id_field != 'global_id' and 'date' in data and 'from_callsign' in data:
            existing = self._find_callsign_duplicate(
                table, id_field, data['date'], data[id_field], data['from_callsign']
            )
            if existing:
                _ex_id, _ex_call = existing
                incoming_global_id = data.get('global_id', 0)
                print(f"[{rig_name}] Skipping duplicate {msg_type.upper()} {data.get(id_field, '')} from {from_callsign} — already received (Global ID: {incoming_global_id})")
                self._upgrade_duplicate_row(
                    table, _ex_id, _ex_call, data['from_callsign'],
                    incoming_global_id if 'global_id' in data else 0
                )
                return ""

        columns = ", ".join(data.keys())
        placeholders = ", ".join(["?" for _ in data])
        query = f"INSERT INTO {table} ({columns}) VALUES({placeholders})"
        try:
            with sqlite3.connect(DATABASE_FILE, timeout=10) as conn:
                conn.execute(query, tuple(data.values()))
                conn.commit()
            print(f"{ConsoleColors.SUCCESS}[{rig_name}] Added {msg_type.upper()} {data.get(id_field, '')}{extra_info} from: {from_callsign} (Global ID: {data.get('global_id', 0)}){ConsoleColors.RESET}")
            QtCore.QMetaObject.invokeMethod(
                self, "_queue_notification_sound",
                QtCore.Qt.QueuedConnection,
                QtCore.Q_ARG(str, msg_type),
            )
            # A "message" row whose target isn't an @GROUP is always a
            # direct message to one of our own callsigns (every insert path
            # gates it that way before calling in here) — pop the banner.
            if msg_type == "message" and not str(data.get('target', '')).startswith('@'):
                QtCore.QMetaObject.invokeMethod(
                    self, "_show_new_message_popup",
                    QtCore.Qt.QueuedConnection,
                    QtCore.Q_ARG(str, from_callsign),
                    QtCore.Q_ARG(str, str(data.get('msg_id', ''))),
                    QtCore.Q_ARG(str, str(data.get('message', ''))),
                    QtCore.Q_ARG(str, str(data.get('global_id', 0))),
                )
                if raw_line:
                    self._send_delivery_confirmation(raw_line)
            return msg_type
        except sqlite3.IntegrityError as e:
            if id_field in str(e) or "UNIQUE" in str(e):
                id_val = data.get(id_field, "unknown")
                incoming_global_id = data.get('global_id', 0)
                print(f"[{rig_name}] Skipping duplicate {msg_type.upper()} {data.get(id_field, '')} from {from_callsign} — already received (Global ID: {incoming_global_id})")
                if incoming_global_id:
                    try:
                        with sqlite3.connect(DATABASE_FILE, timeout=10) as conn:
                            conn.execute(
                                f"UPDATE {table} SET global_id = ? WHERE {id_field} = ? AND (global_id IS NULL OR global_id = 0)",
                                (incoming_global_id, id_val)
                            )
                            conn.commit()
                    except sqlite3.Error:
                        pass
            else:
                print(f"{ConsoleColors.WARNING}[{rig_name}] WARNING: Database constraint violation: {e}{ConsoleColors.RESET}")
        except sqlite3.Error as e:
            print(f"{ConsoleColors.ERROR}[{rig_name}] ERROR: {msg_type.capitalize()} database insert failed for {from_callsign}: {e}{ConsoleColors.RESET}")
        return ""

    def _process_fcode_statrep(
        self,
        rig_name: str,
        value: str,
        from_callsign: str,
        target: str,
        grid: str,
        freq: int,
        snr: int,
        utc: str,
        format_code: str,  # "F!304" or "F!301"
        source: int = 1,  # 1=Radio (TCP), 2=Internet (commsrvr)
        global_id: int = 0
    ) -> str:
        """
        Process F!304 or F!301 STATREP format messages.

        Args:
            format_code: "F!304" (8 digits) or "F!301" (9 digits)

        Returns:
            "statrep" on success, empty string on failure
        """
        # Determine pattern based on format
        digit_count = 8 if format_code == "F!304" else 9
        pattern = rf'{format_code}\s+(\d{{{digit_count}}})\s*(.*?)(?:>])?$'

        match = re.search(pattern, value, re.IGNORECASE)
        if not match:
            return ""

        digits = match.group(1)
        remainder = match.group(2)

        # Map digits to fields
        if format_code == "F!304":
            field_map = map_f304_digits_to_fields(digits)
            scope = scope_db_text_for_code("1")  # F!304 carries no scope digit; always the reporter's own location
            status_digits = digits
        else:  # F!301
            field_map = map_f301_digits_to_fields(digits)
            scope = field_map['scope']
            status_digits = digits[1:]  # Skip scope digit

        # F!304/F!301 messages don't contain grid data — resolve via callsign lookup
        # _lookup_grid_for_callsign checks qrz table first, then QRZ API (caches result)
        fcode_grid = self._lookup_grid_for_callsign(from_callsign) or ""
        grid_found = bool(fcode_grid and len(fcode_grid) >= 4)

        # Build comments
        comment_parts = [format_code] + field_map['comment_parts']
        comments = ", ".join(comment_parts)
        if remainder.strip():
            comments += f" - {remainder.strip()}"
        comments = sanitize_ascii(comments)

        # Calculate status
        fcode_status = calculate_f304_status(status_digits, grid_found)

        # Generate ID and extract date
        date_only, srid = parse_message_datetime(utc)

        # Default group
        fcode_group = target if target else "@ALL"

        # Build data dict for insertion
        data = {
            'datetime': utc,
            'date': date_only,
            'freq': freq,
            'db': snr,
            'source': source,
            'sr_id': srid,
            'from_callsign': from_callsign,
            'target': fcode_group,
            'grid': fcode_grid,
            'scope': scope,
            'map': fcode_status,
            'power': field_map['power'],
            'water': field_map['water'],
            'med': "4",
            'telecom': field_map['telecom'],
            'travel': "4",
            'internet': field_map['internet'],
            'fuel': "4",
            'food': "4",
            'crime': "4",
            'civil': "4",
            'political': "4",
            'comments': comments,
            'global_id': global_id
        }

        return self._insert_message_data(
            rig_name, "statrep", data, "sr_id", "statrep", from_callsign
        )

    def _handle_commsrvr_data_messages(self, content: str) -> bool:
        """Handle commsrvr server data messages with ID prefixes.

        Expected format (one or more lines):
        113:  2026-02-06 18:32:32    14118000    0    30    N0DDK: @MAGNET ,EM83CV,3,T31,321311111331,GA,{&%}
        114:  2026-02-06 18:35:10    14118000    0    30    W1ABC: @ALL LRT ,1,Test Alert,This is a test,{%%}

        Format per line:
        ID: date time freq_hz unused(0) snr callsign: message_data

        Args:
            content: The commsrvr response content with ID-prefixed messages

        Returns:
            True if at least one message was processed, False otherwise
        """
        import re
        from datetime import datetime, timezone
        from typing import Optional

        try:
            lines = content.split('\n')
            processed_count = 0
            last_data_id = 0
            data_types_processed = set()  # Track which data types were added

            # Process each line that starts with an ID
            for line in lines:
                line = line.strip()
                if not line:
                    continue

                # Catch ::DELIVERED:: / ::EXPIRED:: lines regardless of whether they carry an ID prefix
                _directive_slot = None
                if "::DELIVERED::" in line:
                    _directive_slot = ("::DELIVERED::", "_show_delivered_popup", "Delivered")
                elif "::EXPIRED::" in line:
                    _directive_slot = ("::EXPIRED::", "_show_expired_popup", "Expired")
                if _directive_slot is not None:
                    _tag, _slot, _label = _directive_slot
                    print(f"[COMMSRVR] {_tag} raw line: {line!r}")
                    # Strip optional leading ID prefix before the directive
                    raw_payload = re.sub(r'^\d+:\s*', '', line)
                    raw_payload = raw_payload[len(_tag):]
                    if "," in raw_payload:
                        callsign, msg_text = raw_payload.split(",", 1)
                        callsign = callsign.strip()

                        # ::DELIVERED:: now arrives as CALLSIGN,DATE,MSG_ID,message
                        # (older format was just CALLSIGN,message). When the date
                        # and msg_id are present, flag the matching sent message as
                        # delivered and strip them from the popup text.
                        if _tag == "::DELIVERED::":
                            _deliv = re.match(
                                r'^(\d{4}-\d{2}-\d{2}),([^,]+),(.*)$', msg_text, re.DOTALL
                            )
                            if _deliv:
                                _date, _msgid, msg_text = (
                                    _deliv.group(1).strip(),
                                    _deliv.group(2).strip(),
                                    _deliv.group(3),
                                )
                                QtCore.QMetaObject.invokeMethod(
                                    self, "_mark_message_delivered",
                                    QtCore.Qt.QueuedConnection,
                                    QtCore.Q_ARG(str, callsign),
                                    QtCore.Q_ARG(str, _date),
                                    QtCore.Q_ARG(str, _msgid),
                                )

                        print(f"[COMMSRVR] {_label} — callsign={callsign!r}  msg={msg_text.strip()!r}")
                        QtCore.QMetaObject.invokeMethod(
                            self, _slot,
                            QtCore.Qt.QueuedConnection,
                            QtCore.Q_ARG(str, callsign),
                            QtCore.Q_ARG(str, msg_text.strip())
                        )
                    else:
                        print(f"[COMMSRVR] {_tag} payload missing comma, skipping: {raw_payload!r}")
                    continue

                # Check if line starts with a number followed by colon
                id_match = re.match(r'^(\d+):\s*(.+)$', line)
                if not id_match:
                    continue

                data_id = int(id_match.group(1))
                data = id_match.group(2).strip()
                raw_data_line = data

                # Track the highest ID we've seen
                if data_id > last_data_id:
                    last_data_id = data_id

                # Handle delete directive
                delete_match = re.match(r'^::STATREP-DELETE::(\d+)$', data)
                if delete_match:
                    gid = int(delete_match.group(1))
                    self.db._execute(
                        lambda cursor, conn, g=gid: (
                            cursor.execute("DELETE FROM statrep WHERE global_id = ?", (g,)),
                            conn.commit()
                        ),
                        None
                    )
                    try:
                        with sqlite3.connect(DATABASE_FILE, timeout=10) as conn:
                            conn.execute("UPDATE controls SET data_id = ? WHERE id = 1", (data_id,))
                            conn.commit()
                        print(f"Updated data_id to {data_id} in controls table (delete)")
                    except sqlite3.Error as e:
                        print(f"Warning: Failed to update data_id in controls table (delete): {e}")
                    continue

                # Parse the data line: date time freq_hz unused snr callsign: message
                # Example: 2026-02-06 18:32:32    14118000    0    30    N0DDK: @MAGNET ,EM83CV,3,T31,321311111331,GA,{&%}
                # Fields: date(0) time(1) freq_hz(2) unused/0(3) snr/db(4) callsign:message(5)
                if data.startswith("DM:"):
                    data = data[3:]
                parts = data.split(None, 5)  # Split on whitespace, max 6 parts
                if len(parts) < 6:
                    print(f"Skipping malformed data line (ID {data_id}): insufficient fields")
                    continue

                try:
                    utc_date = parts[0]  # YYYY-MM-DD
                    utc_time = parts[1]  # HH:MM:SS
                    utc = f"{utc_date} {utc_time}"
                    freq = int(parts[2])  # Frequency in Hz
                    # parts[3] is unknown/unused (always 0)
                    db = int(parts[4])  # SNR in dB
                    message_part = parts[5]  # callsign: message_data

                    # Split callsign from message
                    if ':' not in message_part:
                        print(f"Skipping malformed message (ID {data_id}): no callsign separator")
                        continue

                    callsign_and_msg = message_part.split(':', 1)
                    from_callsign = callsign_and_msg[0].strip()
                    message_value = message_part  # Keep full message with sender prefix for consistent parsing

                    # Extract target group from message if present
                    target = ""
                    target_match = re.search(r'(@[A-Z0-9]+)', message_value, re.IGNORECASE)
                    if target_match:
                        target = target_match.group(1).upper()
                    else:
                        # Check if statrep is addressed directly to the user's callsign
                        _my_call = next((cs for cs in self.rig_callsigns.values() if cs), None)
                        if not _my_call:
                            _my_call, _, __ = self.db.get_user_settings()
                        if _my_call:
                            _direct = re.match(r'^\w+:\s+(\w+)\s+,', message_value, re.IGNORECASE)
                            if _direct and _direct.group(1).upper() == _my_call.upper():
                                target = _my_call.upper()

                    # Preprocess message value
                    message_value = self._preprocess_message_value(message_value, from_callsign)

                    # Parse using unified parser (source=2 for Internet)
                    msg_type, _ = self._parse_commstat_message(
                        "COMMSRVR", from_callsign, message_value, target, "", freq, db, utc, source=2, global_id=data_id,
                        raw_line=raw_data_line
                    )

                    if msg_type:
                        processed_count += 1
                        data_types_processed.add(msg_type)

                except Exception as e:
                    print(f"Error parsing data line (ID {data_id}): {e}")
                    import traceback
                    traceback.print_exc()
                    continue

            # Update data_id in controls table if we processed any messages
            if last_data_id > 0:
                try:
                    with sqlite3.connect(DATABASE_FILE, timeout=10) as conn:
                        conn.execute("UPDATE controls SET data_id = ? WHERE id = 1", (last_data_id,))
                        conn.commit()
                    print(f"Updated data_id to {last_data_id} in controls table")
                except sqlite3.Error as e:
                    print(f"Warning: Failed to update data_id in controls table: {e}")

            # Trigger UI refresh for processed data types (on main thread)
            if data_types_processed:
                QtCore.QMetaObject.invokeMethod(
                    self, "_refresh_commsrvr_data",
                    QtCore.Qt.QueuedConnection,
                    QtCore.Q_ARG(set, data_types_processed)
                )

            return processed_count > 0

        except Exception as e:
            print(f"Error handling commsrvr data messages: {e}")
            import traceback
            traceback.print_exc()
            return False

    @QtCore.pyqtSlot(str, str, str)
    def _mark_message_delivered(self, callsign: str, date: str, msg_id: str) -> None:
        """Flag a sent message as delivered and refresh the message table.

        Fired when the commsrvr returns ::DELIVERED::CALLSIGN,DATE,MSG_ID,...
        The row we sent has the recipient in the target column, so match on
        target/date/msg_id. Runs on the GUI thread (queued) so the table
        refresh — which touches Qt widgets — is safe.
        """
        try:
            with sqlite3.connect(DATABASE_FILE, timeout=10) as conn:
                conn.execute(
                    "UPDATE messages SET delivered = 1 "
                    "WHERE target = ? AND date = ? AND msg_id = ?",
                    (callsign, date, msg_id)
                )
                conn.commit()
        except sqlite3.Error as e:
            print(f"[DELIVERED] Failed to mark message delivered: {e}")

        self._load_message_data()

    @QtCore.pyqtSlot(str, str)
    def _show_delivered_popup(self, callsign: str, message: str) -> None:
        """Show a delivery confirmation popup when the commsrvr confirms a message was delivered,
        unless the user has disabled it in User Settings (controls.mssgNotify)."""
        if not self.db.get_notify_enabled():
            return
        print(f"[DELIVERED] Showing popup — callsign={callsign!r}  message={message!r}")
        from qrz_lookup import DeliveryConfirmationDialog
        dlg = DeliveryConfirmationDialog(
            callsign=callsign,
            message=message,
            module_background=self.config.get_color('module_background'),
            module_foreground=self.config.get_color('module_foreground'),
            program_background=self.config.get_color('program_background'),
            program_foreground=self.config.get_color('program_foreground'),
            parent=self,
        )
        dlg.exec_()

    @QtCore.pyqtSlot(str, str)
    def _show_expired_popup(self, callsign: str, message: str) -> None:
        """Show an expiry popup when the commsrvr reports a message expired before retrieval,
        unless the user has disabled it in User Settings (controls.mssgNotify)."""
        if not self.db.get_notify_enabled():
            return
        print(f"[EXPIRED] Showing popup — callsign={callsign!r}  message={message!r}")
        from qrz_lookup import MessageExpiredDialog
        dlg = MessageExpiredDialog(
            callsign=callsign,
            message=message,
            module_background=self.config.get_color('module_background'),
            module_foreground=self.config.get_color('module_foreground'),
            program_background=self.config.get_color('program_background'),
            program_foreground=self.config.get_color('program_foreground'),
            parent=self,
        )
        dlg.exec_()

    @QtCore.pyqtSlot(str, str, str, str)
    def _show_new_message_popup(self, callsign: str, msg_id: str, message_text: str, global_id: str) -> None:
        """Show a notification popup when a message arrives addressed to
        one of our own callsigns, unless the user has disabled it in
        User Settings (controls.mssgNotify). "Open" closes the popup and
        opens the message detail dialog, mirroring _on_message_click."""
        print(f"[MESSAGE] Received message from {callsign!r} — Global ID: {global_id}")
        if not self.db.get_notify_enabled():
            return
        from qrz_lookup import NewMessagePopupDialog
        dlg = NewMessagePopupDialog(callsign=callsign, parent=self)
        if dlg.exec_() == NewMessagePopupDialog.Opened:
            from qrz_lookup import MessageDetailDialog
            detail = MessageDetailDialog(
                callsign, message_text, self._internet_available,
                commsrvr_url=_COMMSRVR,
                module_background=self.config.get_color('module_background'),
                module_foreground=self.config.get_color('module_foreground'),
                data_background=self.config.get_color('data_background'),
                program_background=self.config.get_color('program_background'),
                program_foreground=self.config.get_color('program_foreground'),
                msg_id=msg_id,
                tcp_pool=self.tcp_pool,
                connector_manager=self.connector_manager,
                refresh_callback=self._load_message_data,
                parent=self
            )
            detail.record_deleted.connect(self._load_message_data)
            if detail.exec_() == 1:
                self._load_message_data()

    def _send_delivery_confirmation(self, raw_line: str) -> None:
        """POST the exact data line back to the server as delivery confirmation
        for a direct message addressed to one of our callsigns — commsrvr
        (Internet) traffic passes the raw server line verbatim; TCP (Radio)
        traffic passes an equivalent line built from the JS8Call params.

        Callers include the main/UI thread (TCP messages arrive via a Qt
        socket signal, see _handle_tcp_message/_process_directed_message) as
        well as the background thread already used for commsrvr polling, so
        the actual network call always runs on its own daemon thread to
        never block the UI. Never raises — a failure here only logs and does
        not affect the message insert or popup that triggered it.
        """
        if not netguard.guard("Delivery confirmation"):
            return

        def _send():
            try:
                post_data = urllib.parse.urlencode({'data': raw_line}).encode('utf-8')
                print(f"[Delivery Confirmation] POST data: {post_data}")
                req = urllib.request.Request(_DELIVERY_CONFIRM, data=post_data, method='POST')
                with urllib.request.urlopen(req, timeout=10, context=create_verified_ssl_context()) as response:
                    result = response.read().decode('utf-8').strip()
                print(f"[Delivery Confirmation] Server response: {result!r}")
            except Exception as e:
                print(f"[Delivery Confirmation] Failed to send: {type(e).__name__}: {e}")

        threading.Thread(target=_send, daemon=True).start()

    @QtCore.pyqtSlot(set)
    def _refresh_commsrvr_data(self, data_types: set) -> None:
        """Refresh UI for data received from commsrvr server (called from main thread).

        Args:
            data_types: Set of data types to refresh ('statrep', 'alert', 'message', 'video')
        """
        # Only one thing can own the map pane. Alerts outrank videos: when both
        # arrive on the same heartbeat the pane goes to Alerts and the video is
        # left to the orange Videos button.
        if 'alert' in data_types:
            take_pane = self._trigger_show_alerts
        elif 'video' in data_types:
            take_pane = self._trigger_show_videos
        else:
            take_pane = None

        if 'statrep' in data_types:
            self._load_statrep_data()
            # Let the map finish regenerating before anything takes the pane.
            self._save_map_position(callback=lambda: self._load_map(callback=take_pane))

        if 'message' in data_types:
            self._load_message_data()

        if 'alert' in data_types:
            if 'statrep' not in data_types:
                self._trigger_show_alerts()
            self._load_live_feed()

        if 'video' in data_types:
            self._update_video_button_indicator()
            if 'alert' not in data_types and 'statrep' not in data_types:
                self._trigger_show_videos()

    @QtCore.pyqtSlot(int)
    def _show_program_update_notification(self, new_build: int) -> None:
        """Show notification prompting user to restart (called from main thread)."""
        from ui_helpers import confirm
        if confirm(
            self,
            "Update Available",
            f"CommStat build {new_build} has been downloaded.\n\n"
            f"Please close the application to install the update.\n\n"
            f"Close CommStat now?",
            default_yes=True,
        ):
            # Close the application gracefully - this triggers closeEvent() which
            # disconnects TCP connections and saves state
            # commstat.py will apply the update on next launch
            self.close()

    def _load_slideshow_images(self) -> None:
        """Load images with priority: my_images > images > 00-default.png."""
        self.slideshow_items = []
        self.slideshow_index = 0
        valid_extensions = ('.png', '.jpg', '.jpeg', '.gif', '.bmp')

        # Priority 1: Check my_images folder
        my_images_folder = os.path.join(os.getcwd(), "my_images")
        if os.path.isdir(my_images_folder):
            files = sorted(os.listdir(my_images_folder))
            for filename in files:
                if filename.lower().endswith(valid_extensions):
                    image_path = os.path.join(my_images_folder, filename)
                    self.slideshow_items.append(image_path)

        if self.slideshow_items:
            return

        # Priority 2: Check images folder
        images_folder = os.path.join(os.getcwd(), "images")
        if os.path.isdir(images_folder):
            files = sorted(os.listdir(images_folder))
            for filename in files:
                if filename.lower().endswith(valid_extensions):
                    image_path = os.path.join(images_folder, filename)
                    self.slideshow_items.append(image_path)

        if self.slideshow_items:
            return

        # Priority 3: Use default image
        default_image = os.path.join(os.getcwd(), "00-default.png")
        if os.path.isfile(default_image):
            self.slideshow_items.append(default_image)

    def _start_slideshow(self) -> None:
        """Start the image slideshow."""
        self._load_slideshow_images()
        if self.slideshow_items:
            self._show_current_image()
            self.slideshow_timer.start()
        else:
            self._slideshow_source_pixmap = None
            self.map_disabled_label.setPixmap(QtGui.QPixmap())
            self.map_disabled_label.setText("Map Disabled")

    def _stop_slideshow(self) -> None:
        """Stop the image slideshow."""
        self.slideshow_timer.stop()

    def _show_current_image(self) -> None:
        """Display the current slideshow image."""
        if not self.slideshow_items:
            return

        image_path = self.slideshow_items[self.slideshow_index]
        self._slideshow_source_pixmap = QtGui.QPixmap(image_path)
        self._rescale_slideshow_image()

    def _rescale_slideshow_image(self) -> None:
        """Scale the current slideshow image to the label's current size."""
        pixmap = self._slideshow_source_pixmap
        if pixmap is None or pixmap.isNull():
            return
        # Measure the pane, not the label - the label's geometry is stale if
        # its stack page wasn't showing when the pane was resized
        target = self.map_stack.size()
        if target.width() < 50 or target.height() < 50:
            # Not laid out yet - fall back to the default pane size
            target = QtCore.QSize(MAP_WIDTH, MAP_HEIGHT)
        scaled_pixmap = pixmap.scaled(
            target,
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation
        )
        self.map_disabled_label.setPixmap(scaled_pixmap)
        self.map_disabled_label.setText("")

    def _on_image_label_clicked(self) -> None:
        """Open the currently displayed slideshow image in the system
        default browser (mirrors the video player's YouTube button)."""
        if not self.slideshow_items:
            return
        image_path = self.slideshow_items[self.slideshow_index]
        QDesktopServices.openUrl(QUrl.fromLocalFile(image_path))

    def _show_next_image(self) -> None:
        """Advance to the next image in the slideshow (auto-advance timer
        callback - does not reset the interval)."""
        self._image_navigate(1, restart_timer=False)

    def _image_navigate(self, direction: int, restart_timer: bool = True) -> None:
        """Step the slideshow by one image, alphabetically, wrapping around
        at either end. Manual Prev/Next clicks restart the auto-advance
        timer so a click isn't immediately followed by an auto-advance."""
        if not self.slideshow_items:
            return
        self.slideshow_index = (self.slideshow_index + direction) % len(self.slideshow_items)
        self._show_current_image()
        if restart_timer and self.slideshow_timer.isActive():
            self.slideshow_timer.start()

    def _reposition_image_nav_buttons(self) -> None:
        """Pin Prev (bottom-left) and Next (bottom-right) over the image
        display's current size - matches the Alerts/video nav layout."""
        w = self.map_disabled_label.width()
        h = self.map_disabled_label.height()
        margin = 8
        self.image_prev_btn.resize(self.image_prev_btn.sizeHint())
        self.image_next_btn.resize(self.image_next_btn.sizeHint())
        self.image_prev_btn.move(margin, h - self.image_prev_btn.height() - margin)
        self.image_next_btn.move(w - self.image_next_btn.width() - margin,
                                  h - self.image_next_btn.height() - margin)
        for btn in (self.image_prev_btn, self.image_next_btn):
            btn.raise_()

    def _update_image_nav_visibility(self) -> None:
        """Show Prev/Next only while the mouse is over the image frame -
        mirrors the video player's body:hover behavior."""
        hovering = self._image_hovering
        self.image_prev_btn.setVisible(hovering)
        self.image_next_btn.setVisible(hovering)

    def _check_commsrvr_content_async(self) -> None:
        """Background thread to check commsrvr for updates."""
        try:
            content = self._fetch_commsrvr_content()
            if not content:
                return

            if content.strip() == '1':
                return

            # Check if server returns "0"
            if content.strip() == '0':
                print("Commsrvr server reply = 0")
                return
            content_stripped = content.strip()

            if content_stripped.startswith('db_update'):
                self._handle_db_update(content_stripped)
                return
            elif content_stripped.startswith('program_update'):
                self._handle_program_update(content_stripped)
                return
            elif content_stripped.startswith('qrz_update'):
                self._handle_qrz_update(content_stripped)
                return

            if "::DELIVERED::" in content_stripped:
                print(f"[COMMSRVR] ::DELIVERED:: detected in content: {content_stripped!r}")
            if "::EXPIRED::" in content_stripped:
                print(f"[COMMSRVR] ::EXPIRED:: detected in content: {content_stripped!r}")

            if (re.search(r'^\d+:\s+\d{4}-\d{2}-\d{2}', content_stripped, re.MULTILINE) or
                    re.search(r'^\d+:\s+::STATREP-DELETE::', content_stripped, re.MULTILINE) or
                    re.search(r'::DELIVERED::', content_stripped) or
                    re.search(r'::EXPIRED::', content_stripped)):
                self._handle_commsrvr_data_messages(content_stripped)

        except Exception:
            pass

    def _setup_live_feed(self) -> None:
        """Create the live feed text area."""
        # Feed text area
        self.feed_text = QtWidgets.QPlainTextEdit(self.central_widget)
        self.feed_text.setObjectName("feedText")
        mono_font = QtGui.QFont(FONT_MONO, -1, QtGui.QFont.Medium)
        mono_font.setPixelSize(13)
        self.feed_text.setFont(mono_font)
        self.feed_text.setStyleSheet(
            f"background-color: {self.config.get_color('feed_background')};"
            f"color: {self.config.get_color('feed_foreground')};"
        )
        self.feed_text.setReadOnly(True)

        # No word wrap, always show scrollbars
        self.feed_text.setWordWrapMode(QtGui.QTextOption.NoWrap)
        self.feed_text.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.feed_text.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        # Add to resizable vertical splitter
        self.feed_text.setMinimumHeight(106)
        self.content_splitter.insertWidget(1, self.feed_text)
        self.content_splitter.setSizes([300, 146, 340])
        self.content_splitter.setStretchFactor(0, 1)  # statrep absorbs all vertical resize
        self.content_splitter.setStretchFactor(1, 0)  # live feed stays fixed
        self.content_splitter.setStretchFactor(2, 0)  # bottom section stays fixed

        if self._hide_live_feed:
            self.feed_text.hide()

    def _load_live_feed(self) -> None:
        """Initialize the live feed display from buffer."""
        self._update_feed_display()

    def _update_feed_display(self) -> None:
        """Update the live feed display from the message buffer."""
        # Safety check - feed_text may not exist during startup
        if not hasattr(self, 'feed_text'):
            return

        if not self.feed_messages:
            self.feed_text.setPlainText("Waiting for JS8Call traffic...")
            return

        # Filter messages based on settings
        messages = self.feed_messages
        if self.config.get_hide_heartbeat():
            messages = [
                msg for msg in messages
                if 'HEARTBEAT' not in msg.upper()
                and '@ALLCALL CQ' not in msg.upper()
            ]

        # Join messages (already in newest-first order)
        self.feed_text.setPlainText('\n'.join(messages))

    def _setup_message_table(self) -> None:
        """Create the message data table."""
        self.message_table = QtWidgets.QTableWidget(self.central_widget)
        self.message_table.setObjectName("messageTable")
        self.message_table.setColumnCount(7)
        self.message_table.setRowCount(0)

        # Custom header so the FILTER row can be pinned beneath the column
        # labels instead of scrolling away as table row 0. Installed before
        # _setup_table_widget so its header styling/resize modes apply to it.
        header = _FilterHeader(
            self.message_table, label_h=30, strip_h=30,
            strip_bg=self.config.get_color('data_background'),
            grid_color="#D2D0CF",
        )
        self.message_table.setHorizontalHeader(header)

        self._setup_table_widget(self.message_table, [
            "", "Date Time", "Freq", "From", "To", "ID", ""
        ])

        # Per-column filter boxes, mirroring the contacts table's filter row.
        # No box for Date Time, Freq, or ID (not useful to free-text filter),
        # nor for the blank SNR-color column (col 0, which never has text).
        # Created once and kept for the life of the table; the header shows
        # and hides them with the filter toggle.
        _EXCLUDED_COLS = {0, 1, 2, 5}  # "", Date Time, Freq, ID
        filter_font = QtGui.QFont("Kode Mono", -1)
        filter_font.setPixelSize(13)
        filter_style = (
            "QLineEdit { background-color: white; color: #333333; "
            "border: none; padding: 1px 3px; }"
        )
        for col in range(self.message_table.columnCount()):
            if col in _EXCLUDED_COLS:
                continue
            edit = QtWidgets.QLineEdit()
            edit.setFont(filter_font)
            edit.setStyleSheet(filter_style)
            edit.textChanged.connect(lambda text, c=col: self._on_message_filter_changed(c, text))
            header.add_filter_edit(col, edit)

        self.message_table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.message_table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.message_table.setMinimumSize(320, 180)
        self.message_table.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Preferred)

        self.message_table.itemClicked.connect(self._on_message_click)
        self.message_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.message_table.customContextMenuRequested.connect(
            lambda pos: self._show_table_copy_menu(self.message_table, pos)
        )

        # Filter toggle - an underlined "Filter" word (same font/size as the
        # table's title-bar column headers) floating over the title bar's
        # far right edge; green like Map Filter's active state once toggled.
        # Positioned in _reposition_message_filter_btn via the table's
        # Resize events (see eventFilter).
        title_fg = self.config.get_color('title_bar_foreground')
        self._message_filter_label_style = (
            "QLabel {{ background: transparent; color: {color};"
            " font-family: Roboto; font-size: 13px; font-weight: bold;"
            " text-decoration: underline; }}"
        )
        self.message_filter_btn = ClickableLabel("FILTER", self.message_table)
        self.message_filter_btn.setStyleSheet(
            self._message_filter_label_style.format(color=title_fg)
        )
        self.message_filter_btn.setCursor(Qt.PointingHandCursor)
        self.message_filter_btn.clicked.connect(self._toggle_message_filter)
        self.message_filter_btn.adjustSize()

        # Record counter - floating label centered in the single gap between
        # the "ID" column and the FILTER link (mirrors the StatRep table's
        # counter). Text is updated in _populate_table() to track the
        # current filter; position tracked in _reposition_message_filter_btn().
        self.message_count_label = QtWidgets.QLabel("0 Messages", self.message_table)
        self.message_count_label.setStyleSheet(
            "QLabel { background: transparent; color: %s;"
            " font-family: Roboto; font-size: 13px; font-weight: bold; }" % title_fg
        )
        self.message_count_label.adjustSize()

        self.message_table.installEventFilter(self)
        # The vertical scrollbar's own show/hide (as rows load) doesn't fire
        # a table Resize event, so the button's sb_w-dependent x would go
        # stale until some unrelated resize happened to trigger it. Its
        # rangeChanged fires whenever the row count changes the scroll
        # range, which is exactly when visibility can flip.
        self.message_table.verticalScrollBar().rangeChanged.connect(
            lambda *_: QTimer.singleShot(0, self._reposition_message_filter_btn)
        )
        self._reposition_message_filter_btn()

        self.bottom_splitter.addWidget(self.message_table)
        self.bottom_splitter.setSizes([MAP_WIDTH, max(400, self.width() - MAP_WIDTH)])
        self.bottom_splitter.setStretchFactor(0, 0)  # map stays fixed on window resize
        self.bottom_splitter.setStretchFactor(1, 1)  # messages absorb all horizontal resize

    def _setup_contacts_widget(self) -> None:
        """Create the QRZ contacts table widget spanning both map and message columns."""
        _CONTACTS_HEADERS = [
            "Callsign", "Name", "Address", "City", "State",
            "Zip", "Country", "Grid", "Class", "Email", "Image", "Memo", "Date Added",
            "Delete"
        ]
        _MEMO_COL = _CONTACTS_HEADERS.index("Memo")
        _DELETE_COL = len(_CONTACTS_HEADERS) - 1

        self.contacts_widget = QtWidgets.QWidget(self.central_widget)
        outer = QtWidgets.QVBoxLayout(self.contacts_widget)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.contacts_table = QtWidgets.QTableWidget(self.central_widget)
        self.contacts_table.setObjectName("contactsTable")
        self.contacts_table.setColumnCount(len(_CONTACTS_HEADERS))
        self.contacts_table.setRowCount(0)

        # Same pinned-filter header as the message table, so the filter
        # boxes stay put beneath the column labels instead of scrolling
        # away with the data. Contacts filtering is always on, so the strip
        # is shown permanently rather than toggled.
        header = _FilterHeader(
            self.contacts_table, label_h=30, strip_h=30,
            strip_bg=self.config.get_color('data_background'),
            grid_color="#D2D0CF",
        )
        self.contacts_table.setHorizontalHeader(header)

        self._setup_table_widget(self.contacts_table, _CONTACTS_HEADERS)
        self.contacts_table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.contacts_table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)

        # Interactive mode — resizeColumnsToContents() is called once after data loads
        header = self.contacts_table.horizontalHeader()
        header.setStretchLastSection(False)
        for col in range(len(_CONTACTS_HEADERS)):
            header.setSectionResizeMode(col, QtWidgets.QHeaderView.Interactive)
        header.setSectionResizeMode(_MEMO_COL, QtWidgets.QHeaderView.Fixed)
        self.contacts_table.setColumnWidth(_MEMO_COL, 160)

        # One filter QLineEdit per column, pinned in the header strip.
        filter_font = QtGui.QFont("Kode Mono", -1)
        filter_font.setPixelSize(13)
        filter_style = (
            "QLineEdit { background-color: white; color: #333333; "
            "border: none; padding: 1px 3px; }"
        )
        self._contacts_filters: List[Optional[QtWidgets.QLineEdit]] = []
        for col in range(len(_CONTACTS_HEADERS)):
            if col == _DELETE_COL:
                # No filter box under the action column.
                self._contacts_filters.append(None)
                continue
            edit = QtWidgets.QLineEdit()
            edit.setFont(filter_font)
            edit.setStyleSheet(filter_style)
            edit.textChanged.connect(self._apply_contacts_filter)
            header.add_filter_edit(col, edit)
            self._contacts_filters.append(edit)
        header.set_filter_visible(True)

        self.contacts_table.itemClicked.connect(self._on_contacts_item_clicked)
        self.contacts_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.contacts_table.customContextMenuRequested.connect(self._on_contacts_context_menu)


        outer.addWidget(self.contacts_table)

        self.contacts_widget.setMinimumSize(320, 180)
        self.contacts_widget.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Preferred)
        self.content_splitter.addWidget(self.contacts_widget)
        self.contacts_widget.hide()

    def _load_contacts_data(self) -> None:
        """Populate the contacts table from the QRZ cache."""
        _IMAGE_COL = 10
        _MEMO_COL = 11
        _DATE_COL = 12
        _DELETE_COL = 13

        data_fg = self.config.get_color('data_foreground')
        kode_font = QtGui.QFont("Kode Mono", -1)
        kode_font.setPixelSize(13)
        kode_bold = QtGui.QFont("Kode Mono", -1)
        kode_bold.setPixelSize(13)
        kode_bold.setBold(True)

        rows = self.db.get_qrz_contacts()

        col_keys = ["callsign", "name", "address", "city", "state",
                    "zip", "country", "grid", "class", "email", "image", "memo",
                    "insert_date"]

        self.contacts_table.setUpdatesEnabled(False)
        self.contacts_table.setRowCount(len(rows))

        for table_row, row in enumerate(rows):
            for col, key in enumerate(col_keys):
                raw = row[key] if row[key] is not None else ""

                if col == _IMAGE_COL:
                    if raw:
                        item = QTableWidgetItem("View")
                        item.setForeground(QColor("#0078d7"))
                        item.setData(Qt.UserRole, raw)
                    else:
                        item = QTableWidgetItem("—")
                        item.setForeground(QColor(data_fg))
                        item.setData(Qt.UserRole, None)
                elif col == _DATE_COL:
                    try:
                        dt = datetime.fromisoformat(str(raw))
                        formatted = dt.strftime("%b %d, %Y")
                    except (ValueError, TypeError):
                        formatted = str(raw)
                    item = QTableWidgetItem(formatted)
                elif col == _MEMO_COL:
                    item = QTableWidgetItem(str(raw))
                    if raw:
                        item.setToolTip(str(raw))
                else:
                    item = QTableWidgetItem(str(raw))

                if col == 0:
                    item.setFont(kode_bold)
                    item.setTextAlignment(Qt.AlignCenter)
                else:
                    item.setFont(kode_font)
                item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                self.contacts_table.setItem(table_row, col, item)

            # Far-right "Delete" action link.
            del_item = QTableWidgetItem("Delete")
            del_item.setFont(kode_font)
            del_item.setForeground(QColor("#cc0000"))
            del_item.setTextAlignment(Qt.AlignCenter)
            del_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            self.contacts_table.setItem(table_row, _DELETE_COL, del_item)

        self.contacts_table.setUpdatesEnabled(True)
        self.contacts_table.resizeColumnsToContents()
        self.contacts_table.setColumnWidth(_MEMO_COL, 160)  # Fixed column; content-resize ignored

        # Cap narrow columns that ResizeToContents makes too wide
        _MAX_WIDTHS = {4: 55, 5: 70, 7: 70, 8: 60, 10: 55}
        header = self.contacts_table.horizontalHeader()
        for col, max_w in _MAX_WIDTHS.items():
            if header.sectionSize(col) > max_w:
                header.resizeSection(col, max_w)

        # Fresh rows are all visible; callers that need the filter boxes
        # honored call _apply_contacts_filter() next, which recounts.
        self._update_contacts_count_label()

    def _apply_contacts_filter(self) -> None:
        """Show/hide data rows based on the pinned per-column filter inputs."""
        filters = [edit.text().strip().upper() if edit else "" for edit in self._contacts_filters]
        for row in range(self.contacts_table.rowCount()):
            match = True
            for col, f in enumerate(filters):
                if not f:
                    continue
                item = self.contacts_table.item(row, col)
                cell = item.text().upper() if item else ""
                if f not in cell:
                    match = False
                    break
            self.contacts_table.setRowHidden(row, not match)
        self._update_contacts_count_label()

    def _update_contacts_count_label(self) -> None:
        """Show the number of rows currently visible in the Name column
        header, so the count tracks the filter boxes rather than the whole
        QRZ cache."""
        table = self.contacts_table
        count = sum(1 for row in range(table.rowCount()) if not table.isRowHidden(row))
        table.setHorizontalHeaderItem(
            1, QTableWidgetItem(f"Name   ({count} Records)")
        )

    def _on_contacts_item_clicked(self, item: QTableWidgetItem) -> None:
        """Callsign opens QRZ lookup; image opens URL; all others just select."""
        col = item.column()

        if col == 0:
            callsign = item.text().strip()
            if callsign:
                from qrz_lookup import QRZLookupDialog
                dlg = QRZLookupDialog(
                    module_background=self.config.get_color('module_background'),
                    module_foreground=self.config.get_color('module_foreground'),
                    program_background=self.config.get_color('program_background'),
                    program_foreground=self.config.get_color('program_foreground'),
                    refresh_callback=self._load_message_data,
                    parent=self
                )
                dlg.cs_edit.setText(callsign)
                dlg._search()
                dlg.msg_edit.setFocus()
                dlg.exec_()
                self.contacts_table.viewport().setFocus()
        elif col == 10:
            url = item.data(Qt.UserRole)
            if url:
                self._open_external_link(url, "Contact link")
        elif col == 12:
            cs_item = self.contacts_table.item(item.row(), 0)
            callsign = cs_item.text().strip() if cs_item else ""
            if callsign and self._confirm_delete_contact(callsign):
                self.db.delete_qrz_contact(callsign)
                self._load_contacts_data()
                self._apply_contacts_filter()
        else:
            text = item.text().strip()
            if text and text != "—":
                QtWidgets.QApplication.clipboard().setText(text)

    def _confirm_delete_contact(self, callsign: str) -> bool:
        """Yes/No confirmation prompt before deleting a contact. Returns True on Yes."""
        panel_bg = self.config.get_color('module_background')
        panel_fg = self.config.get_color('module_foreground')
        prog_bg = self.config.get_color('program_background')
        prog_fg = self.config.get_color('program_foreground')

        box = QtWidgets.QMessageBox(self)
        box.setIcon(QtWidgets.QMessageBox.Question)
        box.setWindowTitle("Delete Contact")
        box.setText(f'Are you sure you want to delete "{callsign}" ?')
        box.setStandardButtons(
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No
        )
        box.setDefaultButton(QtWidgets.QMessageBox.No)
        box.setStyleSheet(
            f"QMessageBox {{ background-color:{panel_bg}; }}"
            f"QMessageBox QLabel {{ color:{panel_fg}; font-family:'Roboto'; "
            f"font-size:13px; }}"
            f"QPushButton {{ background-color:{prog_bg}; color:{prog_fg}; "
            f"border:none; padding:5px 18px; min-width:60px; "
            f"font-family:'Roboto'; font-size:13px; }}"
            f"QPushButton:hover {{ background-color:{prog_fg}; color:{prog_bg}; }}"
        )
        return box.exec_() == QtWidgets.QMessageBox.Yes

    def _on_contacts_context_menu(self, pos) -> None:
        """Show right-click context menu with Copy option for contacts table."""
        item = self.contacts_table.itemAt(pos)
        if not item or item.text().strip() == "—":
            return
        self._show_table_copy_menu(self.contacts_table, pos)

    def _copy_contacts_current_cell(self) -> None:
        """Copy the currently selected contacts cell text via Ctrl+C."""
        item = self.contacts_table.currentItem()
        if item:
            text = item.text().strip()
            if text and text != "—":
                QtWidgets.QApplication.clipboard().setText(text)

    def _handle_copy_shortcut(self) -> None:
        """Single Ctrl+C handler — dispatches to whichever table viewport has focus."""
        focused = QtWidgets.QApplication.focusWidget()
        # Text inputs (Custom Filtering bar, contacts filter row) copy their
        # own selection; without this the window-level shortcut swallows Ctrl+C.
        if isinstance(focused, QtWidgets.QLineEdit):
            focused.copy()
            return
        if hasattr(self, 'contacts_table') and focused in (
            self.contacts_table, self.contacts_table.viewport()
        ):
            self._copy_contacts_current_cell()
        elif focused in (self.statrep_table, self.statrep_table.viewport()):
            self._copy_table_current_cell(self.statrep_table)
        elif focused in (self.message_table, self.message_table.viewport()):
            self._copy_table_current_cell(self.message_table)

    def _show_table_copy_menu(self, table: QtWidgets.QTableWidget, pos) -> None:
        """Show a right-click Copy context menu for any table cell."""
        item = table.itemAt(pos)
        if not item:
            return
        text = item.text().strip()
        if not text:
            return
        menu = QtWidgets.QMenu(table)
        menu.setStyleSheet(
            "QMenu { background-color: #FFFFE1; color: black; border: 1px solid black; }"
            "QMenu::item:selected { background-color: #e6e600; color: black; }"
        )
        copy_action = menu.addAction("Copy")
        action = menu.exec_(table.viewport().mapToGlobal(pos))
        if action == copy_action:
            QtWidgets.QApplication.clipboard().setText(text)

    def _copy_table_current_cell(self, table: QtWidgets.QTableWidget) -> None:
        """Copy the currently selected cell in any table via Ctrl+C."""
        item = table.currentItem()
        if item:
            text = item.text().strip()
            if text:
                QtWidgets.QApplication.clipboard().setText(text)

    def _load_message_data(self) -> None:
        """Load message data from database into the table."""
        data = self.db.get_message_data(
            groups=[],
            start='',
            end='',
            show_all=True
        )

        self._populate_table(self.message_table, data)

        # _populate_table() rebuilds every row from scratch, but the filter
        # boxes live in the header (pinned below the column labels), so they
        # survive the reload - just re-apply their text to the fresh rows.
        if self._message_filter_active:
            self._apply_message_filter()

    def _toggle_message_filter(self) -> None:
        """Toggle the message table's pinned per-column filter row on/off."""
        self._message_filter_active = not self._message_filter_active
        header = self.message_table.horizontalHeader()
        if not self._message_filter_active:
            self._message_filter_values = {}
            for edit in header.edits.values():
                edit.blockSignals(True)
                edit.clear()
                edit.blockSignals(False)
        header.set_filter_visible(self._message_filter_active)
        self._apply_message_filter()
        # The header just changed height; the FILTER link and counter are
        # centered in its label band, so keep them there.
        self._reposition_message_filter_btn()

    def _on_message_filter_changed(self, col: int, text: str) -> None:
        self._message_filter_values[col] = text
        self._apply_message_filter()

    def _apply_message_filter(self) -> None:
        """Show/hide message rows based on the filter row's per-column text."""
        table = self.message_table
        filters = {col: val.strip().upper() for col, val in self._message_filter_values.items() if val.strip()}
        for row in range(table.rowCount()):
            match = True
            for col, f in filters.items():
                item = table.item(row, col)
                cell = item.text().upper() if item else ""
                if f not in cell:
                    match = False
                    break
            table.setRowHidden(row, not match)
        self._update_message_count_label()

    def _update_message_count_label(self) -> None:
        """Set the title-bar counter to the number of rows currently shown,
        so it tracks the FILTER boxes rather than the full table."""
        if not hasattr(self, 'message_count_label'):
            return
        table = self.message_table
        count = sum(1 for row in range(table.rowCount()) if not table.isRowHidden(row))
        label = "1 Message" if count == 1 else f"{count} Messages"
        self.message_count_label.setText(label)
        self.message_count_label.adjustSize()
        self._reposition_message_filter_btn()

    def _fetch_earthquake_events(self) -> list:
        """Fetch and cache USGS earthquake GeoJSON. Returns cached data on failure."""
        try:
            if not getattr(self, "_internet_available", False):
                return getattr(self, "_earthquake_cache", [])

            min_mag = 0.0
            if hasattr(self.config, "get_earthquake_min_mag"):
                min_mag = float(self.config.get_earthquake_min_mag())

            # Use official USGS 24-hour feeds. Lower magnitude feed has more events.
            if min_mag >= 4.5:
                url = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson"
            elif min_mag >= 2.5:
                url = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_day.geojson"
            else:
                url = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_day.geojson"

            now = time.time()
            refresh_min = self.config.get_earthquake_refresh() if hasattr(self.config, "get_earthquake_refresh") else 10
            # is not None (not truthiness) — an empty list is a legitimate "no
            # quakes matched the filter" cache and must still throttle, or a
            # quiet feed refetches (and re-stamps cache_time) on every single
            # call, which never lets the poll in _ensure_earthquake_data_async
            # settle and forces a full map reload in a near-continuous loop.
            if getattr(self, "_earthquake_cache", None) is not None and now - getattr(self, "_earthquake_cache_time", 0) < max(300, refresh_min * 60):
                return self._earthquake_cache

            request = urllib.request.Request(url, headers={"User-Agent": "CommStat/2.5"})
            with urllib.request.urlopen(request, timeout=8, context=create_verified_ssl_context()) as response:
                data = json.loads(response.read().decode("utf-8", errors="replace"))

            events = []
            region = self.config.get_earthquake_region() if hasattr(self.config, "get_earthquake_region") else "Worldwide"
            for feature in data.get("features", []):
                props = feature.get("properties", {}) or {}
                geom = feature.get("geometry", {}) or {}
                coords = geom.get("coordinates") or []
                if len(coords) < 3:
                    continue
                lon, lat, depth = coords[0], coords[1], coords[2]
                mag = props.get("mag")
                if mag is None:
                    continue
                try:
                    mag = float(mag)
                except Exception:
                    continue
                if mag < min_mag:
                    continue

                if region == "USA" and not (18 <= lat <= 72 and -170 <= lon <= -60):
                    continue
                if region == "North America" and not (5 <= lat <= 83 and -170 <= lon <= -45):
                    continue

                events.append({
                    "mag": mag,
                    "place": props.get("place", "Unknown location"),
                    "time": props.get("time"),
                    "url": props.get("url", ""),
                    "id": feature.get("id", props.get("code", "")),
                    "lat": lat,
                    "lon": lon,
                    "depth": depth,
                })

            self._earthquake_cache = events
            self._earthquake_cache_time = now
            return events
        except Exception as e:
            print(f"[Earthquake] USGS feed unavailable: {e}")
            return getattr(self, "_earthquake_cache", [])

    def _earthquake_color(self, mag: float) -> str:
        # Earthquake colors intentionally avoid station status colors.
        if mag >= 7.0:
            return "#ffffff"   # white
        if mag >= 6.0:
            return "#ff00ff"   # magenta
        if mag >= 5.0:
            return "#8a2be2"   # purple
        if mag >= 4.0:
            return "#2f7bff"   # blue
        return "#00e5ff"       # cyan

    def _ensure_earthquake_data_async(self) -> None:
        """Kick a background fetch of USGS earthquake data (if one isn't
        already running) so _add_earthquakes_to_map() never blocks the UI
        thread on the up-to-8s USGS call. The current render uses whatever
        is already cached (possibly nothing, the first time); once the
        fetch actually lands fresh data, the map reloads itself once.
        _fetch_earthquake_events() already skips the network call and
        returns instantly when its own cache is still fresh, so calling
        this on every map render is cheap."""
        if getattr(self, "_earthquake_fetch_in_progress", False):
            return
        self._earthquake_fetch_in_progress = True

        def worker() -> None:
            self._fetch_earthquake_events()
            self._earthquake_fetch_in_progress = False

        threading.Thread(target=worker, daemon=True).start()

        def poll() -> None:
            if self._earthquake_fetch_in_progress:
                QTimer.singleShot(200, poll)
            # Compare against what's actually on screen, not a snapshot taken
            # before this fetch started — an unrelated _load_map() (e.g. the
            # startup connectivity refresh) may have already rendered this
            # same fresh data while the fetch was in flight, in which case
            # reloading again here would just be a redundant extra flash.
            elif self.map_loaded and getattr(self, "_earthquake_cache_time", 0) != getattr(self, "_earthquake_rendered_cache_time", 0):
                self._save_map_position(callback=self._load_map)

        QTimer.singleShot(200, poll)

    def _add_earthquakes_to_map(self, m) -> None:
        """Add optional USGS earthquake markers to the folium map."""
        try:
            if not (hasattr(self.config, "get_earthquake_layer") and self.config.get_earthquake_layer()):
                return

            if getattr(self, "_internet_available", False):
                self._ensure_earthquake_data_async()

            fg = folium.FeatureGroup(name="USGS Earthquakes", overlay=True, control=True, show=True)
            for eq in getattr(self, "_earthquake_cache", []):
                mag = eq["mag"]
                color = self._earthquake_color(mag)
                radius = max(5, min(18, 4 + mag * 1.8))
                t = eq.get("time")
                if t:
                    try:
                        t_str = datetime.fromtimestamp(int(t) / 1000, timezone.utc).strftime("%Y-%m-%d %H:%MZ")
                    except Exception:
                        t_str = "Unknown"
                else:
                    t_str = "Unknown"

                popup_html = f"""
                <div style='font-family:Arial,sans-serif;background:rgba(20,20,20,.96);color:#f5f5f5;
                            padding:8px 10px;border:1px solid #777;border-radius:6px;min-width:210px;'>
                    <div style='font-weight:bold;font-size:14px;color:{color};'>M{mag:.1f} Earthquake</div>
                    <div><b>Location:</b> {eq.get('place','Unknown')}</div>
                    <div><b>Depth:</b> {eq.get('depth','?')} km</div>
                    <div><b>UTC:</b> {t_str}</div>
                    <div><b>USGS ID:</b> {eq.get('id','')}</div>
                    <div style='margin-top:5px;'><a href='{eq.get('url','')}' target='_blank' style='color:#8fd3ff;'>Open USGS Event</a></div>
                </div>
                """
                eq_marker = folium.CircleMarker(
                    location=[eq["lat"], eq["lon"]],
                    radius=radius,
                    color="#ff3333" if mag >= 7.0 else color,
                    fill=True,
                    fill_color=color,
                    fill_opacity=0.72,
                    opacity=0.95,
                    weight=2 if mag >= 7.0 else 0,
                    popup=folium.Popup(popup_html, max_width=280),
                    tooltip=f"M{mag:.1f} {eq.get('place','')}"
                )
                # folium.CircleMarker's path_options() whitelist silently drops
                # unrecognized kwargs (pane included) — set it directly on the
                # generated options dict instead of passing it to the constructor.
                eq_marker.options["pane"] = "eqPane"
                eq_marker.add_to(fg)
            fg.add_to(m)
            self._earthquake_rendered_cache_time = getattr(self, "_earthquake_cache_time", 0)
        except Exception as e:
            print(f"[Earthquake] map overlay failed: {e}")

    def _fetch_wildfire_events(self) -> list:
        """Fetch and cache current US wildfire incidents from NIFC's public
        WFIGS feed (no API key required). Returns cached data on failure."""
        try:
            if not getattr(self, "_internet_available", False):
                return getattr(self, "_wildfire_cache", [])

            min_acres = self.config.get_wildfire_min_acres() if hasattr(self.config, "get_wildfire_min_acres") else 10.0

            now = time.time()
            refresh_min = self.config.get_wildfire_refresh() if hasattr(self.config, "get_wildfire_refresh") else 30
            # is not None (not truthiness) — see the matching comment in
            # _fetch_earthquake_events: an empty list is a legitimate "no
            # fires matched the filter" cache and must still throttle.
            if getattr(self, "_wildfire_cache", None) is not None and now - getattr(self, "_wildfire_cache_time", 0) < max(300, refresh_min * 60):
                return self._wildfire_cache

            # WFIGS Incident Locations (Current) - NIFC's public, keyless feed of
            # open wildland fire incidents. IncidentTypeCategory='WF' excludes
            # prescribed burns (RX) and complexes (CX).
            base = ("https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/"
                    "WFIGS_Incident_Locations_Current/FeatureServer/0/query")
            params = {
                "where": f"IncidentTypeCategory='WF' AND IncidentSize>={min_acres}",
                "outFields": "IncidentName,IncidentSize,PercentContained,"
                             "FireDiscoveryDateTime,POOState,IncidentComplexityLevel",
                "orderByFields": "IncidentSize DESC",
                "resultRecordCount": "1000",
                "f": "geojson",
            }
            url = base + "?" + urllib.parse.urlencode(params)

            request = urllib.request.Request(url, headers={"User-Agent": "CommStat/2.5"})
            with urllib.request.urlopen(request, timeout=8, context=create_verified_ssl_context()) as response:
                data = json.loads(response.read().decode("utf-8", errors="replace"))

            events = []
            for feature in data.get("features", []):
                props = feature.get("properties", {}) or {}
                geom = feature.get("geometry", {}) or {}
                coords = geom.get("coordinates") or []
                if len(coords) < 2:
                    continue
                lon, lat = coords[0], coords[1]
                acres = props.get("IncidentSize")
                if acres is None:
                    continue
                try:
                    acres = float(acres)
                except Exception:
                    continue

                events.append({
                    "name": props.get("IncidentName") or "Unnamed Incident",
                    "acres": acres,
                    "contained": props.get("PercentContained"),
                    "discovered": props.get("FireDiscoveryDateTime"),
                    "state": (props.get("POOState") or "").replace("US-", ""),
                    "complexity": props.get("IncidentComplexityLevel") or "Unknown",
                    "lat": lat,
                    "lon": lon,
                })

            self._wildfire_cache = events
            self._wildfire_cache_time = now
            return events
        except Exception as e:
            print(f"[Wildfire] NIFC feed unavailable: {e}")
            return getattr(self, "_wildfire_cache", [])

    def _wildfire_color(self, acres: float) -> str:
        # Deliberately the conventional fire yellow/orange/red scale - the
        # flame-shaped icon (not a circle) already keeps it visually distinct
        # from station status pins and earthquake markers on the same map.
        if acres >= 10000:
            return "#8b0000"   # dark red - megafire
        if acres >= 1000:
            return "#ff4500"   # red-orange
        if acres >= 100:
            return "#ff8c00"   # dark orange
        return "#ffc107"       # gold

    @staticmethod
    def _wildfire_icon(color: str) -> "folium.DivIcon":
        """Build an 18x18 flame-shaped icon so wildfire markers read as fire
        at a glance, distinct in silhouette from every other circular pin
        type on the map (status reports, watchlist objects, earthquakes)."""
        html = f'''
        <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 18 18">
            <path d="M9 1 C9 1 4 6.5 4 10.5 C4 13.5 6.5 16 9 16 C11.5 16 14 13.5 14 10.5 C14 6.5 9 1 9 1 Z"
                  fill="{color}" fill-opacity="0.92" stroke="#3a1200" stroke-width="0.8"/>
        </svg>
        '''
        return folium.DivIcon(html=html, icon_size=(18, 18), icon_anchor=(9, 15))

    def _ensure_wildfire_data_async(self) -> None:
        """Background-fetch NIFC wildfire data, mirroring
        _ensure_earthquake_data_async() — see its docstring."""
        if getattr(self, "_wildfire_fetch_in_progress", False):
            return
        self._wildfire_fetch_in_progress = True

        def worker() -> None:
            self._fetch_wildfire_events()
            self._wildfire_fetch_in_progress = False

        threading.Thread(target=worker, daemon=True).start()

        def poll() -> None:
            if self._wildfire_fetch_in_progress:
                QTimer.singleShot(200, poll)
            # See _ensure_earthquake_data_async's matching comment: compare
            # against what's actually on screen, not a pre-fetch snapshot,
            # so an unrelated reload that already picked up this same fresh
            # data doesn't get followed by a redundant extra one here.
            elif self.map_loaded and getattr(self, "_wildfire_cache_time", 0) != getattr(self, "_wildfire_rendered_cache_time", 0):
                self._save_map_position(callback=self._load_map)

        QTimer.singleShot(200, poll)

    def _add_wildfires_to_map(self, m) -> None:
        """Add optional NIFC wildfire incident markers to the folium map."""
        try:
            if not (hasattr(self.config, "get_wildfire_layer") and self.config.get_wildfire_layer()):
                return

            if getattr(self, "_internet_available", False):
                self._ensure_wildfire_data_async()

            fg = folium.FeatureGroup(name="Wildfires (NIFC)", overlay=True, control=True, show=True)
            for fire in getattr(self, "_wildfire_cache", []):
                acres = fire["acres"]
                color = self._wildfire_color(acres)
                t = fire.get("discovered")
                if t:
                    try:
                        t_str = datetime.fromtimestamp(int(t) / 1000, timezone.utc).strftime("%Y-%m-%d %H:%MZ")
                    except Exception:
                        t_str = "Unknown"
                else:
                    t_str = "Unknown"

                contained = fire.get("contained")
                contained_str = f"{int(contained)}%" if contained is not None else "Unknown"

                popup_html = f"""
                <div style='font-family:Arial,sans-serif;background:rgba(20,20,20,.96);color:#f5f5f5;
                            padding:8px 10px;border:1px solid #777;border-radius:6px;min-width:210px;'>
                    <div style='font-weight:bold;font-size:14px;color:{color};'>{fire['name']}</div>
                    <div><b>Size:</b> {acres:,.0f} acres</div>
                    <div><b>Contained:</b> {contained_str}</div>
                    <div><b>Discovered:</b> {t_str}</div>
                    <div><b>State:</b> {fire.get('state') or 'Unknown'}</div>
                    <div><b>Complexity:</b> {fire.get('complexity')}</div>
                </div>
                """
                folium.Marker(
                    location=[fire["lat"], fire["lon"]],
                    icon=self._wildfire_icon(color),
                    popup=folium.Popup(popup_html, max_width=280),
                    tooltip=f"{fire['name']} ({acres:,.0f} ac)"
                ).add_to(fg)
            fg.add_to(m)
            self._wildfire_rendered_cache_time = getattr(self, "_wildfire_cache_time", 0)
        except Exception as e:
            print(f"[Wildfire] map overlay failed: {e}")

    @staticmethod
    def _watchlist_pin_icon(color: str, shape: str) -> "folium.DivIcon":
        """Build an 8x8 halo icon for a watchlist pin: translucent fill only,
        no outline, as inline SVG so shapes need no plugin and render offline.
        Unknown colors/shapes fall back to the pink circle."""
        hex_color = WATCHLIST_OBJECT_COLORS.get(
            (color or "").upper(), WATCHLIST_OBJECT_COLORS[DEFAULT_OBJECT_COLOR])
        shape = (shape or "").upper()
        if shape == "SQUARE":
            svg_shape = '<rect x="0" y="0" width="8" height="8"'
        elif shape == "TRIANGLE":
            svg_shape = '<polygon points="4,0 8,8 0,8"'
        else:
            svg_shape = '<circle cx="4" cy="4" r="4"'
        html = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8">'
            f'{svg_shape} fill="{hex_color}" fill-opacity="0.80"/></svg>'
        )
        return folium.DivIcon(html=html, icon_size=(8, 8), icon_anchor=(4, 4))

    @staticmethod
    def _event_pin_icon(color: str) -> "folium.DivIcon":
        """16x16 Event marker: solid 3px outline + translucent (0.40) fill.
        Stroke is centered on the SVG path, so r=6.5 (not 8) keeps the outer
        edge at exactly 16px diameter once the 3px stroke is added."""
        html = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16">'
            f'<circle cx="8" cy="8" r="6.5" fill="{color}" fill-opacity="0.40" '
            f'stroke="{color}" stroke-width="3"/></svg>'
        )
        return folium.DivIcon(html=html, icon_size=(16, 16), icon_anchor=(8, 8))

    def _add_watchlist_pins_to_map(self, m) -> None:
        """Add persistent halo pins for watchlist members to the folium map,
        in each watchlist's configured pin color and shape.

        Drawn before the StatRep pins so live pins render on top, and queried
        outside the StatRep pipeline so they ignore every date/group/hide
        filter and never appear in the status report table.

        Placement precedence: an operator grid override beats the stored
        lat/lon (the FCC-derived location is stale for the same reason the
        override exists), which beats the FCC grid."""
        self._watchlist_pin_click_js = []
        try:
            shown = self.config.get_shown_watchlists()
            if not shown:
                return
            # watchlistPane (created up front in _load_map, between eqPane
            # and weatherPane) keeps these pins below weather radar and
            # StatRep pins regardless of add order — Leaflet markers
            # otherwise default to markerPane (z-index 600), which paints
            # above everything else on this map.
            for callsign, lat, lon, grid, grid_override, object_color, object_shape in \
                    self.db.get_watchlist_pins(shown):
                try:
                    coords = None
                    if grid_override:
                        coords = self._grid_to_latlon(grid_override)
                    if coords is None and lat is not None and lon is not None:
                        coords = (lat, lon)
                    if coords is None:
                        coords = self._grid_to_latlon(grid or "")
                    if coords is None:
                        continue
                    lat, lon = coords
                    marker = folium.Marker(
                        location=[float(lat), float(lon)],
                        icon=self._watchlist_pin_icon(object_color, object_shape),
                        tooltip=callsign,
                        pane="watchlistPane",
                    )
                    marker.add_to(m)
                    # Clicking the halo opens the QRZ Lookup module, routed
                    # through the commstat:// scheme intercepted in
                    # CustomWebEnginePage.acceptNavigationRequest.
                    self._watchlist_pin_click_js.append(
                        f"{marker.get_name()}.on('click', function() {{"
                        f" window.location.href = 'commstat://watchlist-pin/{callsign}'; }});"
                    )
                except Exception as e:
                    print(f"[WatchlistPins] skipped {callsign}: {e}")
        except Exception as e:
            print(f"[WatchlistPins] map overlay failed: {e}")

    def _grid_to_latlon(self, grid: str) -> Optional[Tuple[float, float]]:
        """Convert a Maidenhead grid square to (lat, lon), or None if invalid."""
        try:
            coords = mh.to_location(grid, center=True)
            return float(coords[0]), float(coords[1])
        except Exception:
            return None

    def _load_map(self, callback=None) -> None:
        """Generate and display the folium map with StatRep pins."""
        filters = self.config.filter_settings
        # Map Filter "map"/"custom" states ignore the Filter menu's group/hide
        # selections entirely and start from every StatRep for the date range.
        if self._map_filter_state in ("map", "custom"):
            groups, exclude_groups, show_all = [], [], True
        else:
            groups, exclude_groups, show_all = self._get_filtered_groups()

        # Use saved map position or default to US center
        if not hasattr(self, 'map_center'):
            self.map_center = (38.8199286, -96.7782551)
            self.map_zoom = 4

        # zoomSnap=0.25 allows fractional zoom so wheelPxPerZoomLevel actually
        # matters — with Leaflet's default zoomSnap=1, every wheel tick rounds
        # up to a full zoom level no matter how small the per-tick delta is.
        m = folium.Map(
            tiles=None,
            zoom_start=self.map_zoom,
            location=self.map_center,
            wheelPxPerZoomLevel=MAP_WHEEL_PX_PER_ZOOM,
            zoomSnap=0.25,
        )

        # Overlay stacking order (bottom to top): earthquakes, watchlist
        # pins, weather radar, StatRep pins. All four sit in custom panes
        # created up front (Leaflet's z-index is a fixed style, not derived
        # from add order, so panes only need to exist before anything is
        # added to them — creating them all here in one place makes the
        # ordering self-evident instead of accidental). tilePane (the base
        # map, z=200) stays under all of these; markerPane (z=600, used by
        # wildfire icons and left untouched) stays above all of these.
        folium.map.CustomPane("eqPane", z_index=360, pointer_events=True).add_to(m)
        folium.map.CustomPane("watchlistPane", z_index=370, pointer_events=True).add_to(m)
        folium.map.CustomPane("weatherPane", z_index=380, pointer_events=False).add_to(m)
        folium.map.CustomPane("statrepPane", z_index=390, pointer_events=True).add_to(m)

        # Add local (offline) tile layer.
        folium.raster_layers.TileLayer(
            tiles='tiles://local/{z}/{x}/{y}.png',
            name='Local Tiles',
            attr='Local Tiles',
            max_zoom=8,
            control=False
        ).add_to(m)

        # Add online tile layer (CartoDB Dark Matter) for zoom > 8, only if internet available.
        # CARTO's raster tile service now requires an API key (unauthenticated
        # requests get served a watermarked "API KEY REQUIRED" tile instead of
        # an error). The key is never hardcoded/committed — it's delivered
        # on-demand via a Tools > Maintenance code (see maintenance.py) and
        # written to config.ini's carto_api_key, same trusted-push channel
        # Watchlist Presets uses for db_update payloads.
        carto_key = self.config.get_carto_api_key()
        carto_url = 'https://basemaps.cartocdn.com/rastertiles/dark_all/{z}/{x}/{y}{r}.png'
        if carto_key:
            carto_url += f'?key={carto_key}'
        if self._internet_available:
            folium.raster_layers.TileLayer(
                tiles=(carto_url if self.config.get_map_theme() == 'dark' else 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png'),
                name='CartoDB Dark Matter',
                attr='CartoDB, © OpenStreetMap contributors',
                min_zoom=1,
                control=False
            ).add_to(m)


        # Optional online weather radar overlay. Disabled automatically when offline.
        if self._internet_available and self.config.get_weather_radar():
            folium.raster_layers.TileLayer(
                tiles='https://mesonet.agron.iastate.edu/cache/tile.py/1.0.0/nexrad-n0r/{z}/{x}/{y}.png',
                name='Weather Radar',
                attr='NOAA NEXRAD',
                overlay=True,
                control=False,
                opacity=0.55,
                pane='weatherPane'
            ).add_to(m)

            if self.config.get_show_radar_timestamp():
                radar_timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
                radar_timestamp_html = f"""
                <div style="
                    position: fixed;
                    bottom: 12px;
                    right: 12px;
                    z-index: 9999;
                    background: rgba(0, 0, 0, 0.78);
                    color: #F0EAD6;
                    border: 1px solid #3B4B2A;
                    border-radius: 4px;
                    padding: 5px 8px;
                    font-family: Arial, sans-serif;
                    font-size: 11px;
                    font-weight: bold;
                    box-shadow: 0 0 6px rgba(0,0,0,0.6);
                ">
                    NOAA/NEXRAD Radar Updated: {radar_timestamp}
                </div>
                """
                m.get_root().html.add_child(folium.Element(radar_timestamp_html))

        # Bounding boxes used to count pins per region for status-bar indicators
        # (lat_min, lat_max, lng_min, lng_max). Boxes can overlap; a pin near a
        # boundary may light multiple buttons.
        REGION_BBOX = {
            "us":      ( 24.0,  50.0, -125.0,  -66.0),
            "eu":      ( 35.0,  72.0,  -10.0,   40.0),
            "mideast": ( 12.0,  42.0,   25.0,   65.0),
            "seasia":  (-10.0,  30.0,   90.0,  140.0),
        }
        # "world" lights up when any pin falls OUTSIDE the continental US bbox.
        US_BBOX = REGION_BBOX["us"]
        region_counts = {"us": 0, "eu": 0, "mideast": 0, "seasia": 0, "world": 0}

        # Member pins render in watchlistPane (see the pane setup at the top
        # of this method) so StatRep pins always paint on top, even sharing
        # a grid square.
        self._add_watchlist_pins_to_map(m)

        pin_registry = {}  # statrep_id -> [lat, lon] for bounce/pan; populated inside try
        # Get StatRep data for pins
        try:
            _map_callsign = next((cs for cs in self.rig_callsigns.values() if cs), "") or ""
            if not _map_callsign:
                _map_callsign, _, __ = self.db.get_user_settings()
            data = self.db.get_statrep_data(
                groups=groups,
                start=filters.get('start', DEFAULT_FILTER_START),
                end=filters.get('end', ''),
                show_all=show_all,
                exclude_groups=exclude_groups,
                user_callsign=_map_callsign
            )

            gridlist = []
            for row in data:
                callsign = row[3]   # from_callsign
                srid = row[5]       # sr_id (display only)
                grid = row[6]       # grid
                scope = row[7]      # scope (text); drives pin radius
                status = str(row[8])  # map (status)
                statrep_id = row[22]  # database primary key (unique)

                # Custom Filtering bar drops non-matching rows before any of
                # the per-pin work below (and before the duplicate-grid jitter
                # bookkeeping, so only pins that render get offset).
                if self._map_filter_state == "custom" and not self._custom_filter_match(row):
                    continue

                # Convert grid to coordinates
                try:
                    coords = self._grid_to_latlon(grid)
                    if coords is None:
                        raise ValueError(f"invalid grid: {grid}")
                    lat, lon = coords

                    # Offset duplicate grids
                    count = gridlist.count(grid)
                    if count > 0:
                        lat += count * 0.01
                        lon += count * 0.01
                    gridlist.append(grid)

                    # Create tactical quick-info popup HTML.
                    # Details link still opens the original full StatRep window.
                    sr_dt = row[1][:16] if row[1] else ""
                    freq_text = f"{hz_to_mhz(row[2]):.3f} MHz" if row[2] else ""
                    group_text = str(row[4]).lstrip("@") if row[4] else ""
                    status_label = {
                        "1": "Normal",
                        "2": "Advisory",
                        "3": "Emergency",
                        "4": "Unknown",
                        "6": "Event"
                    }.get(status, "Unknown")
                    status_color = {
                        "1": "#39d12f",
                        "2": "#ff9f1a",
                        "3": "#ff3333",
                        "4": "#c7c7c7",
                        "6": "#8000ff"
                    }.get(status, "#c7c7c7")
                    _light_map = self.config.get_map_theme() == "light"
                    _bg = ("linear-gradient(145deg,rgba(248,248,245,.97),rgba(235,235,230,.95))"
                           if _light_map else
                           "linear-gradient(145deg,rgba(18,18,18,.96),rgba(46,46,46,.94))")
                    _text = "#1a1a1a" if _light_map else "#f5f5f5"
                    _label = "#555555" if _light_map else "#d9d9d9"
                    _border = "rgba(0,0,0,.18)" if _light_map else "rgba(255,255,255,.18)"
                    _link_color = "#0055cc" if _light_map else "#ffffff"
                    html = f'''<HTML>
                        <BODY style="margin:0;background:transparent;font-family:Roboto,Arial,sans-serif;">
                            <div style="
                                width:190px;
                                min-height:130px;
                                box-sizing:border-box;
                                background:{_bg};
                                color:{_text};
                                border:1px solid {_border};
                                border-radius:10px;
                                padding:10px 12px 9px 12px;
                                box-shadow:0 0 18px rgba(0,0,0,.65);
                            ">
                                <div style="font-size:20px;font-weight:900;line-height:22px;margin-bottom:8px;">{callsign}</div>
                                <table style="width:100%;border-collapse:collapse;font-size:12px;line-height:17px;color:{_text};">
                                    <tr><td style="color:{_label};width:48px;">Scope:</td><td>{scope}</td></tr>
                                    <tr><td style="color:{_label};">Freq:</td><td>{freq_text}</td></tr>
                                    <tr><td style="color:{_label};">Group:</td><td>{group_text}</td></tr>
                                    <tr><td style="color:{_label};">Date:</td><td>{sr_dt}</td></tr>
                                </table>
                                <div style="text-align:center;margin-top:2px;">
                                    <a href="http://localhost/statrep/{statrep_id}/{callsign}"
                                       style="font-size:14px;font-weight:800;color:{_link_color};text-decoration:underline;">Details</a>
                                </div>
                            </div>
                        </BODY>
                    </HTML>'''
                    iframe = folium.IFrame(html, width=208, height=148)
                    popup = folium.Popup(iframe, min_width=208, max_width=208)

                    # Map Filter "map"/"custom" states bypass these entirely.
                    if self._map_filter_state not in ("map", "custom"):
                        # Skip every pin when Hide All Pins is active
                        if self._hide_all_pins:
                            continue

                        # Skip green pins when filter is active
                        if self._hide_green_pins and status == "1":
                            continue

                        # Skip internet-sourced statreps when filter is active
                        if self._hide_internet_statrep and row[21] != 1:
                            continue

                    # Count this pin against any region whose bounding box contains it
                    for _region, (_lat_min, _lat_max, _lng_min, _lng_max) in REGION_BBOX.items():
                        if _lat_min <= lat <= _lat_max and _lng_min <= lon <= _lng_max:
                            region_counts[_region] += 1
                    # "world" counts pins OUTSIDE the continental US bbox
                    if not (US_BBOX[0] <= lat <= US_BBOX[1] and US_BBOX[2] <= lon <= US_BBOX[3]):
                        region_counts["world"] += 1

                    if status == "6":
                        # Event record: distinct outlined/translucent marker, no halo.
                        # Renders whenever the row reached this loop at all — the
                        # SQL query already applied the date-range/pinned filter,
                        # same as every other status. Pinned just keeps it on the
                        # map past its date range, exactly like non-Event pins.
                        folium.Marker(
                            location=[lat, lon],
                            icon=self._event_pin_icon(self.config.get_color('condition_purple')),
                            popup=popup,
                            pane="statrepPane"
                        ).add_to(m)
                    else:
                        # Color conveys status; radius conveys scope.
                        if status == "1":
                            color = "green"
                        elif status == "2":
                            color = "orange"
                        elif status == "3":
                            color = "red"
                        else:
                            color = "black"

                        radius = SCOPE_RADIUS.get(scope, SCOPE_RADIUS_DEFAULT)

                        # Soft halo goes underneath the solid marker.
                        # Halo radius grows with scope; solid is always 8px (radius 4).
                        # interactive=False prevents the halo from stealing clicks from the popup marker.
                        # folium.CircleMarker's path_options() whitelist silently
                        # drops unrecognized kwargs — pane and interactive included
                        # — so both are set directly on .options below instead of
                        # being passed to the constructor.
                        halo_marker = folium.CircleMarker(
                            radius=radius,
                            fill=True,
                            color=color,
                            fill_color=color,
                            fill_opacity=0.22,
                            opacity=0,
                            weight=2,
                            location=[lat, lon]
                        )
                        halo_marker.options["interactive"] = False
                        halo_marker.options["pane"] = "statrepPane"
                        halo_marker.add_to(m)

                        # Solid status marker stays on top and remains clickable.
                        solid_marker = folium.CircleMarker(
                            radius=3,
                            fill=True,
                            color=color,
                            fill_color=color,
                            fill_opacity=1.0,
                            opacity=0,
                            weight=1,
                            location=[lat, lon],
                            popup=popup
                        )
                        solid_marker.options["pane"] = "statrepPane"
                        solid_marker.add_to(m)
                    pin_registry[str(statrep_id)] = [lat, lon]
                except Exception as e:
                    print(f"Error adding pin for grid {grid} (statrep_id {statrep_id}): {e}")

        except Exception as e:
            print(f"Error loading map data: {e}")

        # Publish per-region pin counts to status-bar indicators
        self._region_pin_counts = region_counts
        self._update_region_button_pin_indicators()

        self._add_earthquakes_to_map(m)
        self._add_wildfires_to_map(m)

        # Save map to bytes and display
        map_data = io.BytesIO()
        m.save(map_data, close_file=False)

        map_html = map_data.getvalue().decode()
        popup_css = """
<style>
.leaflet-popup-content-wrapper {
  background: transparent !important;
  box-shadow: none !important;
  border: none !important;
  padding: 0 !important;
}
.leaflet-popup-content {
  margin: 0 !important;
}
.leaflet-popup-tip {
  background: rgba(25,25,25,.96) !important;
}
</style>
"""
        try:
            map_html = map_html.replace("</head>", popup_css + "</head>", 1)
        except Exception:
            pass

        bounce_css = """
<style>
@keyframes commstatBounce {
  0%   { transform: scale(1.0); }
  15%  { transform: scale(3.2); }
  30%  { transform: scale(1.0); }
  45%  { transform: scale(2.2); }
  60%  { transform: scale(1.0); }
  72%  { transform: scale(1.6); }
  84%  { transform: scale(1.0); }
  92%  { transform: scale(1.3); }
  100% { transform: scale(1.0); }
}
/* Specificity 0-3-2 beats the pulse rule's 0-2-2 when both carry !important.
   StatRep pins (regular CircleMarkers and Event DivIcon markers alike) all
   render in the custom statrepPane -> .leaflet-statrep-pane, not the
   default overlayPane/markerPane, so the selectors target that pane. */
.leaflet-statrep-pane svg path.leaflet-interactive.commstat-bounce {
  transform-box: fill-box !important;
  transform-origin: center !important;
  animation: commstatBounce 1.1s cubic-bezier(.36,.07,.19,.97) forwards !important;
}
/* Event pins are DivIcon markers (see _event_pin_icon): the animation target
   is the inner <svg>, never the marker's own div, because Leaflet positions
   that div via a translate3d() transform - animating scale directly on it
   would clobber the marker's position instead of just bouncing it. Folium's
   DivIcon defaults to className "empty" (not "leaflet-div-icon"), so match
   on the svg alone rather than assuming a wrapper class. */
.leaflet-statrep-pane svg.commstat-bounce {
  transform-box: fill-box !important;
  transform-origin: center !important;
  animation: commstatBounce 1.1s cubic-bezier(.36,.07,.19,.97) forwards !important;
}
</style>
"""
        map_html = map_html.replace('</head>', bounce_css + '</head>')

        # Map Filter button - top-right overlay, same visual language as the
        # Videos player's Prev/Next/Delete buttons (rgba(0,0,0,0.65) glass).
        # The dropdown only offers On/Off, which drive the "map" state
        # (hidden unless the mouse is over the map, same body:hover trick as
        # the video player's Prev/Next, when off; green and always visible
        # when on) - this triggers the map-bounds filter only. The "custom"
        # state (purple, always visible) is a separate state still shown
        # here when active, but it's reached only via the FILTER labels on
        # the StatRep/Message tables, not from this dropdown.
        filter_btn_css = """
<style>
#commstatMapFilterWrap {
  position: fixed;
  top: 10px;
  right: 10px;
  z-index: 10000;
  font-family: sans-serif;
  font-size: 13px;
}
#commstatMapFilterBtn {
  display: block;
  min-width: 110px;
  text-align: center;
  background: rgba(0,0,0,0.65);
  color: #fff;
  border: 1px solid #999;
  border-radius: 4px;
  padding: 4px 12px;
  font-weight: bold;
  cursor: pointer;
  opacity: 0;
  pointer-events: none;
  transition: opacity .15s ease, background-color .15s ease;
}
body:hover #commstatMapFilterBtn {
  opacity: 1;
  pointer-events: auto;
}
/* Leaflet's default zoom (+/-) control, upper-left of the map, follows the
   same hover-to-reveal behavior as the filter button above. */
.leaflet-control-zoom {
  opacity: 0;
  pointer-events: none;
  transition: opacity .15s ease;
}
body:hover .leaflet-control-zoom {
  opacity: 1;
  pointer-events: auto;
}
#commstatMapFilterBtn.commstat-filter-map {
  background: rgba(40,167,69,0.85);
  border-color: #28a745;
  opacity: 1 !important;
  pointer-events: auto !important;
}
#commstatMapFilterBtn.commstat-filter-custom {
  background: rgba(111,66,193,0.85);
  border-color: #6f42c1;
  opacity: 1 !important;
  pointer-events: auto !important;
}
#commstatMapFilterMenu {
  display: none;
  position: absolute;
  top: 100%;
  right: 0;
  margin-top: 4px;
  background: rgba(0,0,0,0.85);
  border: 1px solid #999;
  border-radius: 4px;
  overflow: hidden;
  min-width: 110px;
}
#commstatMapFilterMenu.commstat-menu-open {
  display: block;
}
#commstatMapFilterMenu button {
  display: block;
  width: 100%;
  box-sizing: border-box;
  background: transparent;
  color: #fff;
  border: none;
  padding: 6px 12px;
  text-align: left;
  font-family: sans-serif;
  font-size: 13px;
  cursor: pointer;
}
#commstatMapFilterMenu button:hover {
  background: rgba(255,255,255,0.15);
}
/* Help isn't a filter state - a rule sets it apart from the three modes. */
#commstatMapFilterMenu button.commstat-menu-help {
  border-top: 1px solid rgba(255,255,255,0.25);
}
</style>
"""
        map_html = map_html.replace('</head>', filter_btn_css + '</head>')

        filter_state = getattr(self, '_map_filter_state', 'off')
        if filter_state not in ('off', 'map', 'custom'):
            filter_state = 'off'
        _FILTER_LABELS = {'off': 'FILTER: OFF', 'map': 'FILTER: MAP', 'custom': 'FILTER: CUSTOM'}
        filter_btn_class = f' class="commstat-filter-{filter_state}"' if filter_state != 'off' else ''
        filter_btn_html = f"""
<div id="commstatMapFilterWrap">
  <button id="commstatMapFilterBtn"{filter_btn_class}
    onclick="document.getElementById('commstatMapFilterMenu').classList.toggle('commstat-menu-open');">{_FILTER_LABELS[filter_state]}</button>
  <div id="commstatMapFilterMenu">
    <button onclick="window.location='commstat://map-filter-set/off';">Off</button>
    <button onclick="window.location='commstat://map-filter-set/map';">On</button>
    <button class="commstat-menu-help"
      onclick="document.getElementById('commstatMapFilterMenu').classList.remove('commstat-menu-open');window.location='commstat://map-filter-help';">Help</button>
  </div>
</div>
<script>
document.addEventListener('click', function(e) {{
  var wrap = document.getElementById('commstatMapFilterWrap');
  var menu = document.getElementById('commstatMapFilterMenu');
  if (wrap && menu && !wrap.contains(e.target)) {{
    menu.classList.remove('commstat-menu-open');
  }}
}});
</script>
"""
        map_html = map_html.replace('</body>', filter_btn_html + '\n</body>')

        # Circle marker popups open on click and stay until user clicks elsewhere.
        hover_js = """<script>
</script>"""
        webkit_shim = """<script>
if (window.webkitStorageInfo === undefined && navigator.webkitTemporaryStorage) {
    Object.defineProperty(window, 'webkitStorageInfo', {
        get: function() { return navigator.webkitTemporaryStorage; }
    });
}
</script>"""
        map_html = map_html.replace('</head>', webkit_shim + '\n</head>')
        map_html = map_html.replace('</body>', hover_js + '\n</body>')

        bounce_js = '<script>\nwindow._commstatPinRegistry=' + json.dumps(pin_registry) + """;
window.commstatBouncePin = function(srid) {
    var target = window._commstatPinRegistry[String(srid)];
    if (!target) return;
    Object.keys(window).forEach(function(key) {
        var obj = window[key];
        if (!obj || !obj._leaflet_id || !obj.eachLayer || !obj.panTo) return;
        obj.panTo([target[0], target[1]]);
        obj.eachLayer(function(layer) {
            if (!layer.getLatLng) return;
            // CircleMarkers animate their SVG path directly; DivIcon markers
            // (Event pins) animate their inner <svg>, not the icon div itself,
            // since the div carries Leaflet's translate3d() position transform.
            var el = layer._path || (layer._icon && layer._icon.querySelector('svg'));
            if (!el) return;
            var ll = layer.getLatLng();
            if (Math.abs(ll.lat - target[0]) < 0.0001 &&
                    Math.abs(ll.lng - target[1]) < 0.0001) {
                el.classList.remove('commstat-bounce');
                void el.getBoundingClientRect();
                el.classList.add('commstat-bounce');
                setTimeout(function() { el.classList.remove('commstat-bounce'); }, 1200);
            }
        });
    });
};
</script>"""
        map_html = map_html.replace('</body>', bounce_js + '\n</body>')

        # Watchlist pin click bindings (built in _add_watchlist_pins_to_map).
        # Folium emits the script that DEFINES the marker variables after
        # </body>, i.e. after this injected block executes — so the bindings
        # must wait for the window load event or they hit a ReferenceError.
        if getattr(self, '_watchlist_pin_click_js', None):
            watchlist_click_js = (
                '<script>\nwindow.addEventListener("load", function() {\n'
                + '\n'.join(self._watchlist_pin_click_js)
                + '\n});\n</script>'
            )
            map_html = map_html.replace('</body>', watchlist_click_js + '\n</body>')

        # Always set new HTML content (reload() only refreshes cached content).
        # Exception: while a video is playing, map_widget is showing the video
        # player HTML, not the map — pins/data still refresh into
        # _last_map_html so the map is current whenever the operator leaves
        # video mode, but we don't clobber the video with setHtml() while
        # it's on screen.
        self._last_map_html = map_html
        if getattr(self, '_current_view_mode', '') != "videos":
            self._update_map_background_color()
            self.map_widget.setHtml(self._last_map_html, QUrl("http://localhost/"))
        self.map_loaded = True

        if callback:
            callback()

    def _play_video_at_index(self) -> None:
        """Play the video row at self._video_index (0 = most recent); falls
        back to the current map region if the table is empty or the URL
        isn't YouTube. Marks the row played and refreshes the Videos button
        indicator."""
        row = self.db.get_video_at_offset(getattr(self, '_video_index', 0))
        video_row_id, url = (row[0], row[1]) if row else (None, None)
        video_id = _extract_youtube_id(url) if url else None
        if not video_id:
            self._set_map_view_mode(getattr(self, '_last_map_region', 'us'))
            return
        self._play_video(video_id)
        self.db.mark_video_played(video_row_id)
        self._update_video_button_indicator()
        # Instagram POC kept for reference — embed loads but playback fails:
        # Instagram serves H.264 only and QtWebEngine's open Chromium build
        # has no proprietary codecs (YouTube works via VP9/Opus).
        # self._play_instagram("https://www.instagram.com/reel/Da-1pOuulP4/")

    def _on_video_prev(self) -> None:
        """Prev button: step to the next-older video row, if any."""
        index = getattr(self, '_video_index', 0)
        if index + 1 < self.db.get_video_count():
            self._video_index = index + 1
            self._play_video_at_index()

    def _on_video_next(self) -> None:
        """Next button: step to the next-newer video row, if any."""
        index = getattr(self, '_video_index', 0)
        if index > 0:
            self._video_index = index - 1
            self._play_video_at_index()

    def _on_video_delete(self) -> None:
        """Delete button: remove the currently displayed video row, then
        show what's now at the same offset (or step back if it was last)."""
        index = getattr(self, '_video_index', 0)
        row = self.db.get_video_at_offset(index)
        if row:
            self.db.delete_video(row[0])
        count = self.db.get_video_count()
        if index >= count:
            self._video_index = max(0, count - 1)
        self._update_video_button_indicator()
        self._play_video_at_index()

    def _play_video(self, video_id: str) -> None:
        """Play a YouTube video in the map pane; the map returns when it ends."""
        # Make sure the web view is the visible pane in the map stack
        if self.map_stack.currentWidget() is not self.map_widget:
            self._set_map_view_mode(getattr(self, '_last_map_region', 'us'))

        html = _VIDEO_PLAYER_HTML.replace("__VIDEO_ID__", video_id)
        self.map_widget.setHtml(html, QUrl("http://localhost/"))

    def _play_instagram(self, reel_url: str) -> None:
        """TEMP POC: show an Instagram reel in the map pane via its /embed/ page."""
        if self.map_stack.currentWidget() is not self.map_widget:
            self._set_map_view_mode(getattr(self, '_last_map_region', 'us'))
        embed_url = reel_url.split("?")[0].rstrip("/") + "/embed/"
        html = _INSTAGRAM_PLAYER_HTML.replace("__IG_EMBED_URL__", embed_url)
        self.map_widget.setHtml(html, QUrl("http://localhost/"))

    def _on_video_ended(self) -> None:
        """Video playback ended (commstat://video-ended): restore the map."""
        self._set_map_view_mode(getattr(self, '_last_map_region', 'us'))

    def _save_map_position(self, callback=None) -> None:
        """Save current map center and zoom via JavaScript."""
        if not self.map_loaded:
            if callback:
                callback()
            return

        js_code = """
        (function() {
            try {
                var mapId = Object.keys(window).find(k => k.startsWith('map_'));
                if (mapId && window[mapId]) {
                    var map = window[mapId];
                    var center = map.getCenter();
                    var zoom = map.getZoom();
                    return JSON.stringify({lat: center.lat, lng: center.lng, zoom: zoom});
                }
            } catch(e) {}
            return null;
        })();
        """

        def handle_result(result):
            if result:
                try:
                    import json
                    data = json.loads(result)
                    self.map_center = (data['lat'], data['lng'])
                    self.map_zoom = data['zoom']
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    pass
            if callback:
                callback()

        self.map_widget.page().runJavaScript(js_code, handle_result)

    def _poll_map_bounds(self) -> None:
        """While Map Filter is "map", periodically read the live Leaflet
        map's viewport bounds so the StatRep table can be restricted to only
        pins currently on screen. Polling (rather than a moveend/zoomend push
        from JS) sidesteps QWebEngineView blocking script-initiated
        navigation from a data: URL without real user-gesture activation."""
        if self._map_filter_state != "map" or not self.map_loaded:
            return

        js_code = """
        (function() {
            try {
                var mapId = Object.keys(window).find(k => k.startsWith('map_'));
                if (mapId && window[mapId]) {
                    var b = window[mapId].getBounds();
                    return JSON.stringify({s: b.getSouth(), w: b.getWest(), n: b.getNorth(), e: b.getEast()});
                }
            } catch(e) {}
            return null;
        })();
        """
        self.map_widget.page().runJavaScript(js_code, self._on_map_bounds_result)

    def _on_map_bounds_result(self, result) -> None:
        if not result:
            return
        try:
            data = json.loads(result)
            bounds = (data['s'], data['w'], data['n'], data['e'])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return
        if bounds == self._map_bounds:
            return
        self._map_bounds = bounds
        self._load_statrep_data()

    def _load_statrep_data(self) -> None:
        """Load StatRep data from database into the table."""
        filters = self.config.filter_settings
        state = self._map_filter_state
        bypass_menu = state in ("map", "custom")

        # Map Filter "map"/"custom" states ignore the Filter menu's group/hide
        # selections entirely, matching what the map itself plots.
        if bypass_menu:
            groups, exclude_groups, show_all = [], [], True
            user_callsign = ""
        else:
            groups, exclude_groups, show_all = self._get_filtered_groups()
            user_callsign = next((cs for cs in self.rig_callsigns.values() if cs), "") or ""
            if not user_callsign:
                user_callsign, _, __ = self.db.get_user_settings()

        # Fetch data from database
        data = self.db.get_statrep_data(
            groups=groups,
            start=filters.get('start', DEFAULT_FILTER_START),
            end=filters.get('end', ''),
            show_all=show_all,
            exclude_groups=exclude_groups,
            user_callsign=user_callsign
        )

        if not bypass_menu:
            if self._hide_all_pins:
                data = []
            if self._hide_internet_statrep:
                data = [row for row in data if row[21] == 1]
            if self._hide_green_pins:
                data = [row for row in data if str(row[8]) != "1"]
        elif state == "map":
            if self._map_bounds:
                # Only records whose pin currently falls inside the map's
                # visible viewport - panning/zooming the map live-narrows this.
                south, west, north, east = self._map_bounds
                def _pin_in_view(row):
                    coords = self._grid_to_latlon(row[6])
                    return coords is not None and south <= coords[0] <= north and west <= coords[1] <= east
                data = [row for row in data if _pin_in_view(row)]
        elif state == "custom":
            # Same criteria the map's pin loop applies, so both stay in sync.
            data = [row for row in data if self._custom_filter_match(row)]

        # Status color mapping for values 1-4, plus 6 (Event)
        status_colors = {
            "1": "condition_green",
            "2": "condition_yellow",
            "3": "condition_red",
            "4": "condition_gray",
            "6": "condition_purple"
        }
        self._populate_table(self.statrep_table, data, status_colors)

        if hasattr(self, 'statrep_count_label'):
            count = len(data)
            self.statrep_count_label.setText(f"{count} Record" + ("" if count == 1 else "s"))
            self.statrep_count_label.adjustSize()
            self._reposition_statrep_filter_btn()

    def _on_statrep_click(self, item: QTableWidgetItem) -> None:
        """From callsign (col 3) opens detail view; ID (col 5) bounces map pin; others copy text."""
        _FROM_COL = 3
        _ID_COL   = 5
        row = item.row()

        if item.column() == _FROM_COL:
            callsign_item = self.statrep_table.item(row, _FROM_COL)
            if callsign_item:
                callsign  = callsign_item.text().strip()
                record_id = callsign_item.data(QtCore.Qt.UserRole)
                table = self.statrep_table
                def build_record_list():
                    items = []
                    for r in range(table.rowCount()):
                        ci = table.item(r, _FROM_COL)
                        if ci:
                            rid = ci.data(QtCore.Qt.UserRole)
                            if rid is not None:
                                items.append((rid, ci.text().strip()))
                    return items
                from qrz_lookup import StatRepDetailDialog
                dlg = StatRepDetailDialog(
                    record_id, callsign, self._internet_available,
                    commsrvr_url=_COMMSRVR,
                    module_background=self.config.get_color('module_background'),
                    module_foreground=self.config.get_color('module_foreground'),
                    title_bar_background=self.config.get_color('title_bar_background'),
                    title_bar_foreground=self.config.get_color('title_bar_foreground'),
                    data_background=self.config.get_color('data_background'),
                    program_background=self.config.get_color('program_background'),
                    program_foreground=self.config.get_color('program_foreground'),
                    condition_green=self.config.get_color('condition_green'),
                    condition_yellow=self.config.get_color('condition_yellow'),
                    condition_red=self.config.get_color('condition_red'),
                    condition_gray=self.config.get_color('condition_gray'),
                    condition_purple=self.config.get_color('condition_purple'),
                    tcp_pool=self.tcp_pool,
                    connector_manager=self.connector_manager,
                    record_list_provider=build_record_list,
                    refresh_callback=self._load_message_data,
                    parent=self
                )
                dlg.pin_changed.connect(
                    lambda _: self._save_map_position(callback=self._load_map)
                )
                dlg.record_deleted.connect(self._load_statrep_data)
                dlg.record_deleted.connect(
                    lambda: self._save_map_position(callback=self._load_map)
                )
                if dlg.exec_() == 1:
                    self._load_statrep_data()
        elif item.column() == _ID_COL:
            record_id = item.data(QtCore.Qt.UserRole)
            if record_id is not None and self.map_loaded:
                js = f"if(window.commstatBouncePin)window.commstatBouncePin({json.dumps(str(record_id))});"
                self.map_widget.page().runJavaScript(js)
        else:
            text = item.text().strip()
            if text:
                QtWidgets.QApplication.clipboard().setText(text)

    def _on_message_click(self, item: QTableWidgetItem) -> None:
        """From callsign (col 3) opens detail view; all other cells copy their text."""
        _FROM_COL = 3
        row = item.row()

        if item.column() == _FROM_COL:
            callsign_item = self.message_table.item(row, _FROM_COL)
            message_item  = self.message_table.item(row, 6)
            msg_id_item   = self.message_table.item(row, 5)
            if callsign_item:
                callsign = callsign_item.text().strip()
                message_text = (message_item.data(QtCore.Qt.UserRole) or message_item.text()) if message_item else ""
                msg_id = str(msg_id_item.data(QtCore.Qt.UserRole)).strip() if msg_id_item and msg_id_item.data(QtCore.Qt.UserRole) is not None else ""
                from qrz_lookup import MessageDetailDialog
                dlg = MessageDetailDialog(
                    callsign, message_text, self._internet_available,
                    commsrvr_url=_COMMSRVR,
                    module_background=self.config.get_color('module_background'),
                    module_foreground=self.config.get_color('module_foreground'),
                    data_background=self.config.get_color('data_background'),
                    program_background=self.config.get_color('program_background'),
                    program_foreground=self.config.get_color('program_foreground'),
                    msg_id=msg_id,
                    tcp_pool=self.tcp_pool,
                    connector_manager=self.connector_manager,
                    refresh_callback=self._load_message_data,
                    parent=self
                )
                dlg.record_deleted.connect(self._load_message_data)
                if dlg.exec_() == 1:
                    self._load_message_data()
        else:
            text = item.text().strip()
            if text:
                QtWidgets.QApplication.clipboard().setText(text)

    def _on_qrz_record_written(self, callsign: str) -> None:
        """Bold any matching From-callsign (col 3) cells in StatRep and Message tables."""
        cs = base_callsign(callsign)
        if not cs:
            return
        for table in (self.statrep_table, self.message_table):
            for row in range(table.rowCount()):
                item = table.item(row, 3)
                if item and base_callsign(item.text()) == cs:
                    font = item.font()
                    if not font.bold():
                        font.setBold(True)
                        item.setFont(font)
                    item.setToolTip("Exists in QRZ local cache")

    def _on_qrz_contacts_menu(self) -> None:
        """Toggle QRZ Contacts view; switch back to map if already showing."""
        if hasattr(self, 'contacts_widget') and self.contacts_widget.isVisible():
            self._set_map_view_mode(getattr(self, "_last_map_region", "us"))
        else:
            self._set_map_view_mode("contacts")

    def _export_csv(self, title: str, filename_prefix: str, data_fn) -> None:
        """Prompt for a save path and write the (columns, rows) from data_fn to CSV."""
        default_name = f"{filename_prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        downloads_dir = QtCore.QStandardPaths.writableLocation(QtCore.QStandardPaths.DownloadLocation)
        default_path = os.path.join(downloads_dir, default_name) if downloads_dir else default_name
        file_path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, title, default_path, "CSV Files (*.csv)"
        )
        if not file_path:
            return

        columns, rows = data_fn()
        try:
            with open(file_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(columns)
                writer.writerows(rows)
        except OSError as e:
            QtWidgets.QMessageBox.critical(self, "Export Failed", f"Could not write CSV file:\n{e}")
            return

        QtWidgets.QMessageBox.information(
            self, "Export Complete", f"Exported {len(rows)} record(s) to:\n{file_path}"
        )

    def _on_export_contacts(self) -> None:
        """Export all qrz table rows to a CSV file (Tools > Export menu)."""
        self._export_csv("Export Contacts", "contacts", self.db.get_qrz_export_data)

    def _on_export_statreps(self) -> None:
        """Export all statrep table rows to a CSV file (Tools > Export menu)."""
        self._export_csv("Export Status Reports", "status_reports", self.db.get_statrep_export_data)

    def _on_export_watchlist_members(self) -> None:
        """Export all watchlist members to a CSV file (Tools > Export menu)."""
        self._export_csv("Export Watchlist Members", "watchlist_members", self.db.get_watchlist_members_export_data)

    def _on_export_watchlist_callsigns(self) -> None:
        """Export watchlist callsigns to a bulk-import TXT file (Tools > Export
        menu). One line per watchlist: "Name: CALL1, CALL2, CALL3"."""
        default_name = f"watchlist_callsigns_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        downloads_dir = QtCore.QStandardPaths.writableLocation(QtCore.QStandardPaths.DownloadLocation)
        default_path = os.path.join(downloads_dir, default_name) if downloads_dir else default_name
        file_path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export Watchlist Callsigns", default_path, "Text Files (*.txt)"
        )
        if not file_path:
            return

        data = self.db.get_watchlist_callsigns_export_data()
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                for name, callsigns in data:
                    f.write(f"{name}: {', '.join(callsigns)}\n")
        except OSError as e:
            QtWidgets.QMessageBox.critical(self, "Export Failed", f"Could not write file:\n{e}")
            return

        QtWidgets.QMessageBox.information(
            self, "Export Complete", f"Exported {len(data)} watchlist(s) to:\n{file_path}"
        )

    def _on_data_manager(self) -> None:
        """Open Data Manager dialog (Tools menu)."""
        Cls = self._resolve_dialog_class("data_manager", "DataManagerDialog")
        dlg = Cls(self.db, parent=self)
        dlg.exec_()

    def _on_video_manager(self) -> None:
        """Open Video Manager dialog (Tools menu)."""
        Cls = self._resolve_dialog_class("video_manager", "VideoManagerDialog")
        dlg = Cls(self.db, parent=self)
        dlg.exec_()

    def _on_grid_finder(self) -> None:
        """Open Grid Finder as an in-process modeless window; reuse if already open."""
        existing = getattr(self, "_grid_finder_window", None)
        if existing is not None and existing.isVisible():
            existing.raise_()
            existing.activateWindow()
            return
        Cls = self._resolve_dialog_class("gridfinder", "GridFinderApp")
        win = Cls(parent=self)
        geo = self.geometry()
        ww, wh = win.width(), win.height()
        win.move(geo.x() + (geo.width() - ww) // 2, geo.y() + (geo.height() - wh) // 2)
        self._grid_finder_window = win
        win.show()

    def _on_brevity_generator(self) -> None:
        """Open the Brevity Code Generator in-process; reuse if already open."""
        existing = getattr(self, "_brevity_window", None)
        if existing is not None and existing.isVisible():
            existing.raise_()
            existing.activateWindow()
            return
        Cls = self._resolve_dialog_class("brevity", "BrevityApp")
        win = Cls(
            panel_bg=self.config.get_color('module_background'),
            panel_fg=self.config.get_color('module_foreground'),
            parent=self,
        )
        geo = self.geometry()
        ww, wh = win.width(), win.height()
        win.move(geo.x() + (geo.width() - ww) // 2, geo.y() + (geo.height() - wh) // 2)
        self._brevity_window = win
        win.show()

    def _resolve_dialog_class(self, module_name: str, class_name: str):
        import importlib
        import sys
        module = importlib.import_module(module_name)
        if DEV_RELOAD_DIALOGS and module_name in sys.modules:
            module = importlib.reload(module)
        return getattr(module, class_name)

    def _help_theme_colors(self) -> dict:
        """Live theme colors for help popups, so they track a theme change
        instead of being stuck on the DEFAULT_COLORS baked in at import."""
        return {
            'panel_bg': self.config.get_color('module_background'),
            'prog_bg': self.config.get_color('program_background'),
            'prog_fg': self.config.get_color('program_foreground'),
        }

    def _on_help(self) -> None:
        HelpDialogCls = self._resolve_dialog_class("help", "HelpDialog")
        dlg = HelpDialogCls(self)
        dlg.exec_()

    def _on_whats_new(self) -> None:
        """Open the What's New page in the user's browser."""
        self._open_external_link("https://commstat.app/new-features.php", "What's New")

    def _on_live_better(self) -> None:
        """Open the Live Better page in the user's browser."""
        self._open_external_link("https://commstat.app/how-are-you-feeling.php", "Live Better")

    def _on_flock_camera_map(self) -> None:
        """Open the Flock Camera Map (deflock.org) in the user's browser."""
        self._open_external_link("https://maps.deflock.org/?lat=39.8283&lng=-98.5795&zoom=4.00", "Flock Camera Map")

    def _on_live_radiation_map(self) -> None:
        """Open the Live Radiation Map (gmcmap.com) in the user's browser."""
        self._open_external_link("https://gmcmap.com/", "Live Radiation Map")

    def _on_qrz_lookup(self) -> None:
        """Open standalone QRZ Lookup dialog (Tools menu)."""
        QRZLookupDialogCls = self._resolve_dialog_class("qrz_lookup", "QRZLookupDialog")
        dlg = QRZLookupDialogCls(
            module_background=self.config.get_color('module_background'),
            module_foreground=self.config.get_color('module_foreground'),
            program_background=self.config.get_color('program_background'),
            program_foreground=self.config.get_color('program_foreground'),
            refresh_callback=self._load_message_data,
            parent=self
        )
        dlg.exec_()

    def _setup_timers(self) -> None:
        """Setup timers for clock, data refresh, and news feed animation."""
        # Clock timer - updates every second
        self.clock_timer = QTimer(self)
        self.clock_timer.timeout.connect(self._update_time)
        self.clock_timer.start(1000)
        self._update_time()  # Initial display
        self._update_connected_rigs_display()  # Initial connected rigs display

        # Periodic resync of the status-bar rig indicators against live socket
        # state. Why: the indicators are otherwise event-driven via
        # any_connection_changed; if a signal is missed (e.g. socket transitions
        # without `disconnected` firing) the indicators drift out of sync with
        # the JS8 Connectors dialog. A 5s poll keeps them self-correcting
        # without churning sqlite during degraded states.
        self.rig_status_timer = QTimer(self)
        self.rig_status_timer.timeout.connect(self._update_connected_rigs_display)
        self.rig_status_timer.start(5000)

        # Internet check timer - retries every 30 minutes if offline
        self.internet_timer = QTimer(self)
        self.internet_timer.timeout.connect(self._retry_internet_check)
        if not self._internet_available:
            self.internet_timer.start(INTERNET_CHECK_INTERVAL)

        # Commsrvr check timer - runs every 3 minutes, starts 30 seconds after launch
        self.commsrvr_timer = QTimer(self)
        self.commsrvr_timer.timeout.connect(self._check_commsrvr)
        if self._internet_available:
            # Delay first heartbeat by HEARTBEAT_DELAY_MS, then start timer for subsequent heartbeats
            def start_commsrvr_heartbeat():
                self._check_commsrvr()  # Send first heartbeat immediately
                self.commsrvr_timer.start(HEARTBEAT_INTERVAL_MS)
            QTimer.singleShot(HEARTBEAT_DELAY_MS, start_commsrvr_heartbeat)

        # News ticker animation timer
        self.newsfeed_timer = QTimer(self)
        self.newsfeed_timer.timeout.connect(self._tick_newsfeed)
        # Owned single-shot for the type-on→scroll-off pause; named so we can
        # cancel it when the animation is restarted from outside the natural
        # _next_headline chain (resize debounce, RSS refresh, feed change).
        self.newsfeed_pause_timer = QTimer(self)
        self.newsfeed_pause_timer.setSingleShot(True)
        self.newsfeed_pause_timer.timeout.connect(self._start_scroll_phase)
        self._newsfeed_frame = 0
        self._newsfeed_phase = 0  # 0 = type-on, 1 = scroll-off
        self._scroll_start = 0.0

        # News ticker state
        self.newsfeed_text = ""
        self.newsfeed_chars = 0
        self.rss_fetcher = RSSFetcher()
        self.headline_index = 0
        self.headlines: List[str] = []

        # RSS refresh timer - refreshes feed every 5 minutes
        self.rss_timer = QTimer(self)
        self.rss_timer.timeout.connect(self._refresh_rss_feed)
        self.rss_timer.start(300000)  # 5 minutes

        # Contacts retention timer - purge rows older than
        # CONTACTS_RETENTION_HOURS at startup, then once per hour.
        self.contacts_purge_timer = QTimer(self)
        self.contacts_purge_timer.timeout.connect(self._purge_old_contacts)
        self.contacts_purge_timer.start(3600 * 1000)  # 1 hour
        self._purge_old_contacts()

        # Initial RSS fetch
        if self.config.get_selected_rss_feed() == "Disable":
            self.newsfeed_label.setText("      +++  News Feed Disabled  +++")
        elif self._internet_available:
            self._start_rss_fetch()

    def _check_commsrvr(self) -> None:
        """Check commsrvr server for content updates (runs in background thread)."""
        if not self._internet_available:
            return
        thread = threading.Thread(target=self._check_commsrvr_content_async, daemon=True)
        thread.start()

    def _update_time(self) -> None:
        """Update the time display with current UTC time."""
        current_time = QDateTime.currentDateTimeUtc()
        self.time_label.setText(current_time.toString("hh:mm:ss"))

    def _update_connected_rigs_display(self) -> None:
        """Update the connected rigs display with currently connected rig names."""
        connected = self.tcp_pool.get_connected_rig_names()

        # Update status bar widgets for each rig
        all_rigs = self.connector_manager.get_all_connectors()

        # Remove widgets for rigs that no longer exist
        all_rig_names = [r['rig_name'] for r in all_rigs]
        for rig_name in list(self.rig_status_widgets.keys()):
            if rig_name not in all_rig_names:
                label_rig, label_status = self.rig_status_widgets[rig_name]
                self.statusbar.removeWidget(label_rig)
                self.statusbar.removeWidget(label_status)
                label_rig.deleteLater()
                label_status.deleteLater()
                del self.rig_status_widgets[rig_name]

        # Create or update widgets for each rig
        for rig in all_rigs:
            rig_name = rig['rig_name']
            is_enabled = rig.get('enabled', 1) == 1
            is_connected = rig_name in connected

            if rig_name not in self.rig_status_widgets:
                # Create new widgets for this rig
                # Rig name label with sunken effect
                label_rig = QtWidgets.QLabel(f" {rig_name} ")
                label_rig.setFrameStyle(QtWidgets.QFrame.Panel | QtWidgets.QFrame.Sunken)
                label_rig.setLineWidth(2)

                # Status label with sunken effect
                label_status = QtWidgets.QLabel()
                label_status.setFrameStyle(QtWidgets.QFrame.Panel | QtWidgets.QFrame.Sunken)
                label_status.setLineWidth(2)

                # Add to status bar (on right side)
                self.statusbar.addPermanentWidget(label_rig)
                self.statusbar.addPermanentWidget(label_status)

                # Store references
                self.rig_status_widgets[rig_name] = (label_rig, label_status)

            # Update status label
            _, label_status = self.rig_status_widgets[rig_name]
            if not is_enabled:
                label_status.setText(" Disabled ")
                label_status.setStyleSheet(
                    "background-color: #888888; color: white;"
                    " font-family: Roboto; font-size: 12px; font-weight: normal;"
                )
            elif is_connected:
                label_status.setText(" Connected ")
                label_status.setStyleSheet(
                    "background-color: #00dd00; color: black;"
                    " font-family: Roboto; font-size: 12px; font-weight: normal;"
                )
            else:
                label_status.setText(" Disconnected ")
                label_status.setStyleSheet(
                    "background-color: #dd0000; color: white;"
                    " font-family: Roboto; font-size: 12px; font-weight: normal;"
                )

    def _tick_newsfeed(self) -> None:
        """Timer-driven tick for news feed animation."""
        text = self.newsfeed_text
        visible = self.newsfeed_chars

        if self._newsfeed_phase == 0:
            # Type-on: reveal characters one at a time
            frame = self._newsfeed_frame
            self.newsfeed_label.setText(text[0:frame])
            self._newsfeed_frame += 1
            if self._newsfeed_frame >= visible:
                # Window is full — pause before scrolling
                self.newsfeed_timer.stop()
                self.newsfeed_pause_timer.start(NEWSFEED_PAUSE_MS)
        else:
            # Scroll-off: wall-clock-based so duration is accurate on Windows
            elapsed = time.monotonic() - self._scroll_start
            progress = min(1.0, elapsed / (NEWSFEED_SCROLL_DURATION_MS / 1000.0))
            scroll_steps = len(text) - visible
            offset = int(progress * scroll_steps)
            frame = visible + offset
            self.newsfeed_label.setText(text[frame - visible:frame])
            if progress >= 1.0:
                self.newsfeed_timer.stop()
                self._next_headline()

    def _start_scroll_phase(self) -> None:
        """Begin the scroll-off phase after the pause."""
        self._newsfeed_phase = 1
        self._scroll_start = time.monotonic()
        self.newsfeed_timer.start(16)  # ~60 fps; position derived from wall-clock time

    def _next_headline(self) -> None:
        """Called when news ticker animation completes - show next headline."""
        if self.headlines:
            self.headline_index = (self.headline_index + 1) % len(self.headlines)
        self._display_current_headline()

    def _start_rss_fetch(self) -> None:
        """Start fetching RSS feed in background."""
        feed_name = self.config.get_selected_rss_feed()
        feed_url = DEFAULT_RSS_FEEDS.get(feed_name, list(DEFAULT_RSS_FEEDS.values())[0])
        self.newsfeed_label.setText("  Loading news...")
        self.rss_fetcher.fetch_async(feed_url, callback=self._on_rss_fetched)

    def _on_rss_fetched(self) -> None:
        """Called when RSS fetch completes (from background thread)."""
        # Use QTimer to safely update UI from main thread
        QTimer.singleShot(0, self._update_headlines_from_fetch)

    def _update_headlines_from_fetch(self) -> None:
        """Update headlines list and start display (called on main thread)."""
        feed_name = self.config.get_selected_rss_feed()
        feed_url = DEFAULT_RSS_FEEDS.get(feed_name, list(DEFAULT_RSS_FEEDS.values())[0])
        self.headlines = self.rss_fetcher.get_headlines(feed_url)
        self.headline_index = 0
        self._display_current_headline()

    def _refresh_rss_feed(self) -> None:
        """Refresh RSS feed periodically."""
        feed_name = self.config.get_selected_rss_feed()
        if self._internet_available and feed_name != "Disable":
            feed_url = DEFAULT_RSS_FEEDS.get(feed_name, list(DEFAULT_RSS_FEEDS.values())[0])
            self.rss_fetcher.fetch_async(feed_url, callback=self._on_rss_fetched)

    def _display_current_headline(self) -> None:
        """Display the current headline with scrolling animation."""
        if not self.headlines:
            self.newsfeed_label.setText("  No news available")
            return

        try:
            headline = self.headlines[self.headline_index]

            # Set green color for news headlines
            self.newsfeed_label.setStyleSheet(
                f"background-color: {self.config.get_color('newsfeed_background')};"
                f"color: {self.config.get_color('newsfeed_foreground')};"
            )

            # Build ticker text with headline
            ticker_text = f" {headline}"

            # Calculate how many characters fit in the ticker width.
            # averageCharWidth() overestimates for typical ASCII news text because
            # it averages across all Unicode glyphs. Measure a representative
            # lowercase+space sample instead for a more accurate fit.
            fm = self.newsfeed_label.fontMetrics()
            sample = 'abcdefghijklmnopqrstuvwxyz '
            avg_char_px = fm.horizontalAdvance(sample) / len(sample)
            self.newsfeed_chars = int(self.newsfeed_label.width() / avg_char_px)

            # Add padding spaces
            padding = ' ' * self.newsfeed_chars
            self.newsfeed_text = ticker_text + padding

            # Setup and start animation — cancel any pending pause from a
            # previous cycle so it can't fire scroll-off mid-type-on.
            self.newsfeed_timer.stop()
            self.newsfeed_pause_timer.stop()
            self._newsfeed_frame = 0
            self._newsfeed_phase = 0
            self.newsfeed_timer.start(NEWSFEED_TYPE_INTERVAL_MS)
        except (IndexError, TypeError) as e:
            print(f"Error displaying headline: {e}")
            self.newsfeed_label.setText("  News feed error")

    def eventFilter(self, obj, event):
        """Watch for newsfeed_label and map pane resizes to refit content."""
        if obj is getattr(self, 'newsfeed_label', None) and event.type() == QtCore.QEvent.Resize:
            # Debounce: restart the timer so we only refresh once the drag settles.
            self._newsfeed_resize_timer.start(150)
        elif obj is getattr(self, 'map_stack', None) and event.type() == QtCore.QEvent.Resize:
            self._map_pane_resize_timer.start(150)
        elif obj is getattr(self, 'alert_display', None):
            if event.type() == QtCore.QEvent.Resize:
                self._reposition_alert_nav_buttons()
            elif event.type() == QtCore.QEvent.Enter:
                self._alert_hovering = True
                self._update_alert_nav_visibility()
            elif event.type() == QtCore.QEvent.Leave:
                self._alert_hovering = False
                self._update_alert_nav_visibility()
        elif obj is getattr(self, 'message_table', None) and event.type() == QtCore.QEvent.Resize:
            self._reposition_message_filter_btn()
        elif obj is getattr(self, 'statrep_table', None) and event.type() == QtCore.QEvent.Resize:
            self._reposition_statrep_filter_btn()
        elif obj is getattr(self, 'map_disabled_label', None):
            if event.type() == QtCore.QEvent.Resize:
                self._reposition_image_nav_buttons()
            elif event.type() == QtCore.QEvent.Enter:
                self._image_hovering = True
                self._update_image_nav_visibility()
            elif event.type() == QtCore.QEvent.Leave:
                self._image_hovering = False
                self._update_image_nav_visibility()
        return super().eventFilter(obj, event)

    def _on_map_pane_resized(self) -> None:
        """Refit the map pane's content once a resize settles."""
        mode = getattr(self, '_current_view_mode', None)
        if mode == "images":
            self._rescale_slideshow_image()
        elif mode == "alerts":
            self._show_alert_display()

    def _refresh_newsfeed_for_resize(self) -> None:
        """Recompute and restart the current headline at the new label width."""
        # Only refresh if we are actively scrolling a headline (not Disabled / no-internet states).
        feed_name = self.config.get_selected_rss_feed()
        if feed_name == "Disable" or not self._internet_available or not self.headlines:
            return
        self.newsfeed_timer.stop()
        self.newsfeed_pause_timer.stop()
        self._display_current_headline()

    def _on_feed_changed(self, feed_name: str) -> None:
        """Handle feed selection change."""
        self.config.set_selected_rss_feed(feed_name)
        self.rss_fetcher.clear_cache()
        self.headlines = []
        self.headline_index = 0
        self.newsfeed_timer.stop()
        self.newsfeed_pause_timer.stop()
        if feed_name == "Disable":
            self.newsfeed_label.setText("      +++  News Feed Disabled  +++")
        elif self._internet_available:
            self._start_rss_fetch()
        else:
            self.newsfeed_label.setText("  No internet connection")

    def _on_last20_clicked(self) -> None:
        """Show dialog with last 20 news headlines."""
        headlines = self.headlines if self.headlines else ["No headlines available"]

        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("Last 20 News Headlines")
        dialog.setMinimumSize(600, 400)

        layout = QtWidgets.QVBoxLayout(dialog)

        # Feed name label
        feed_name = self.feed_combo.currentText()
        feed_label = QtWidgets.QLabel(f"Feed: {feed_name}")
        feed_label.setFont(QtGui.QFont("Arial", 12, QtGui.QFont.Bold))
        layout.addWidget(feed_label)

        # Headlines list
        list_widget = QtWidgets.QListWidget()
        _list_font = QtGui.QFont("Roboto", -1)
        _list_font.setPixelSize(13)
        list_widget.setFont(_list_font)
        list_widget.setAlternatingRowColors(True)
        for i, headline in enumerate(headlines[:20], 1):
            list_widget.addItem(f"{i}. {headline}")
        layout.addWidget(list_widget)

        # Close button
        close_btn = QtWidgets.QPushButton("Close")
        close_btn.clicked.connect(dialog.accept)
        layout.addWidget(close_btn)

        dialog.exec_()

    # -------------------------------------------------------------------------
    # Menu Action Handlers (placeholders for now)
    # -------------------------------------------------------------------------

    def _on_js8email(self) -> None:
        """Open JS8 Email window."""
        Cls = self._resolve_dialog_class("js8mail", "JS8MailDialog")
        dialog = Cls(self.tcp_pool, self.connector_manager, self)
        dialog.exec_()

    def _on_js8sms(self) -> None:
        """Open JS8 SMS window."""
        Cls = self._resolve_dialog_class("js8sms", "JS8SMSDialog")
        dialog = Cls(self.tcp_pool, self.connector_manager, self)
        dialog.exec_()

    def _on_js8_direct_message(self) -> None:
        """Open JS8 Direct Message window."""
        Cls = self._resolve_dialog_class("js8_direct_message", "JS8DirectMessageDialog")
        dialog = Cls(self.tcp_pool, self.connector_manager, self._load_message_data, parent=self)
        dialog.exec_()

    def _on_statrep(self) -> None:
        """Open StatRep window."""
        Cls = self._resolve_dialog_class("statrep", "StatRepDialog")
        dialog = Cls(
            self.tcp_pool, self.connector_manager, self,
            module_background=self.config.get_color('module_background'),
            data_background=self.config.get_color('data_background')
        )
        dialog.exec_()

    def _on_send_message(self) -> None:
        """Open Send Message window."""
        Cls = self._resolve_dialog_class("group_message", "GroupMessageDialog")
        dialog = Cls(self.tcp_pool, self.connector_manager, self._load_message_data, parent=self)
        dialog.exec_()

    def _on_group_alert(self) -> None:
        """Open Group Alert window."""
        Cls = self._resolve_dialog_class("alert", "AlertDialog")
        dialog = Cls(self.tcp_pool, self.connector_manager, self._trigger_show_alerts, parent=self)
        dialog.exec_()

    def _on_group_event(self) -> None:
        """Open Group Event window."""
        Cls = self._resolve_dialog_class("group_event", "GroupEventDialog")
        dialog = Cls(
            self.tcp_pool, self.connector_manager, self,
            module_background=self.config.get_color('module_background'),
            data_background=self.config.get_color('data_background')
        )
        dialog.exec_()

    def _on_share_video(self) -> None:
        """Open Share Video window."""
        Cls = self._resolve_dialog_class("video", "VideoDialog")
        dialog = Cls(self._update_video_button_indicator, parent=self)
        dialog.exec_()

    def _on_filter(self) -> None:
        """Open Display Filter window."""
        Cls = self._resolve_dialog_class("filter", "FilterDialog")
        dialog = Cls(self.config.filter_settings, self)
        if dialog.exec_() == QtWidgets.QDialog.Accepted:
            # Update filter settings directly
            self.config.filter_settings = dialog.get_filters()
            # Refresh data with new filters
            self._refresh_all_data()

    def _reset_filter_date(self, days_ago: int) -> None:
        """Reset filter start date to specified days ago and apply."""
        from datetime import datetime, timedelta, timezone

        # Calculate new start date using UTC time
        if days_ago == 0:
            # For midnight, use current UTC date at 00:00:00
            utc_now = datetime.now(timezone.utc)
            new_start = utc_now.strftime("%Y-%m-%d") + " 00:00:00"
        else:
            # For days ago, calculate from current UTC time
            new_start = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")

        # Update in-memory filter settings
        self.config.filter_settings = {
            'start': new_start,
            'end': ''  # No end date
        }

        # Refresh data with new filters
        self._refresh_all_data()

        print(f"Filter reset (UTC): start={new_start}")

    def _on_toggle_heartbeat(self, checked: bool) -> None:
        """Toggle heartbeat message filtering in live feed."""
        self.config.set_hide_heartbeat(checked)
        self._load_live_feed()

    def _on_toggle_hide_internet_statrep(self, checked: bool) -> None:
        """Show only RF-sourced (source=1) statreps in table and map."""
        self._hide_internet_statrep = checked
        self.config.set_hide_internet_feed(checked)
        self._load_statrep_data()
        self._save_map_position(callback=self._load_map)

    def _on_map_filter_help(self) -> None:
        """Open the Map Filter help module from the map's dropdown."""
        show = self._resolve_dialog_class("help", "show_map_filter_help")
        show(self, **self._help_theme_colors())

    def _on_map_filter_set(self, state: str) -> None:
        """Set Map Filter to Off/Map/Custom. Both "Map" and "Custom" ignore the
        Filter menu's group/hide selections and start from every StatRep in the
        date range; "Map" then restricts the table to the map's viewport, while
        "Custom" applies the Custom Filtering bar to both table and map."""
        if state not in ("off", "map", "custom"):
            return
        self._map_filter_state = state
        self.custom_filter_bar.setVisible(state == "custom")
        if state == "map":
            self._map_bounds = None
            self._map_bounds_timer.start()
            # Poll immediately against the still-loaded pre-reload map so the
            # table narrows right away instead of waiting for the first tick.
            self._poll_map_bounds()
        else:
            self._map_bounds_timer.stop()
            self._map_bounds = None
            self._load_statrep_data()
        self._save_map_position(callback=self._load_map)

    def _set_map_theme(self, theme):
        try:
            self.config.set_map_theme(theme)
            self._save_map_position(callback=self._load_map)
        except Exception as e:
            print(f"Map theme switch failed: {e}")



    def _set_weather_radar(self, checked: bool):
        try:
            enabled = bool(checked and self._internet_available)
            self.config.set_weather_radar(enabled)
            if hasattr(self, "map_radar_action"):
                self.map_radar_action.setChecked(enabled)
                self.map_radar_action.setEnabled(self._internet_available)
            if hasattr(self, "quick_weather_btn"):
                self.quick_weather_btn.setChecked(enabled)
                self.quick_weather_btn.setEnabled(self._internet_available)
            self._restart_radar_refresh_timer()
            self._save_map_position(callback=self._load_map)
        except Exception as e:
            print(f"Weather radar switch failed: {e}")


    def _sync_weather_radar_action(self) -> None:
        if hasattr(self, "map_radar_action"):
            if not self._internet_available:
                self.map_radar_action.setChecked(False)
                self.map_radar_action.setEnabled(False)
            else:
                self.map_radar_action.setEnabled(True)
                self.map_radar_action.setChecked(self.config.get_weather_radar())
        if hasattr(self, "quick_weather_btn"):
            if not self._internet_available:
                self.quick_weather_btn.setChecked(False)
                self.quick_weather_btn.setEnabled(False)
            else:
                self.quick_weather_btn.setEnabled(True)
                self.quick_weather_btn.setChecked(self.config.get_weather_radar())

    def _sync_earthquake_action(self) -> None:
        if hasattr(self, "map_earthquake_action"):
            if not self._internet_available:
                self.map_earthquake_action.setChecked(False)
                self.map_earthquake_action.setEnabled(False)
            else:
                self.map_earthquake_action.setEnabled(True)
                self.map_earthquake_action.setChecked(self.config.get_earthquake_layer())
        if hasattr(self, "quick_earthquake_btn"):
            if not self._internet_available:
                self.quick_earthquake_btn.setChecked(False)
                self.quick_earthquake_btn.setEnabled(False)
            else:
                self.quick_earthquake_btn.setEnabled(True)
                self.quick_earthquake_btn.setChecked(self.config.get_earthquake_layer())

    def _sync_wildfire_action(self) -> None:
        if hasattr(self, "map_wildfire_action"):
            if not self._internet_available:
                self.map_wildfire_action.setChecked(False)
                self.map_wildfire_action.setEnabled(False)
            else:
                self.map_wildfire_action.setEnabled(True)
                self.map_wildfire_action.setChecked(self.config.get_wildfire_layer())
        if hasattr(self, "quick_wildfire_btn"):
            if not self._internet_available:
                self.quick_wildfire_btn.setChecked(False)
                self.quick_wildfire_btn.setEnabled(False)
            else:
                self.quick_wildfire_btn.setEnabled(True)
                self.quick_wildfire_btn.setChecked(self.config.get_wildfire_layer())


    def _setup_radar_refresh_timer(self) -> None:
        if not hasattr(self, "radar_refresh_timer"):
            self.radar_refresh_timer = QTimer(self)
            self.radar_refresh_timer.timeout.connect(self._refresh_weather_radar)
        self._restart_radar_refresh_timer()


    def _restart_radar_refresh_timer(self) -> None:
        if not hasattr(self, "radar_refresh_timer"):
            return
        self.radar_refresh_timer.stop()
        minutes = self.config.get_weather_radar_refresh()
        if self._internet_available and self.config.get_weather_radar() and minutes > 0:
            self.radar_refresh_timer.start(minutes * 60 * 1000)


    def _refresh_weather_radar(self) -> None:
        if self._internet_available and self.config.get_weather_radar():
            self._save_map_position(callback=self._load_map)


    def _set_weather_radar_refresh(self, minutes: int) -> None:
        self.config.set_weather_radar_refresh(minutes)
        if hasattr(self, "map_radar_refresh_actions"):
            for value, action in self.map_radar_refresh_actions.items():
                action.setChecked(value == minutes)
        self._restart_radar_refresh_timer()


    def _set_show_radar_timestamp(self, checked: bool) -> None:
        self.config.set_show_radar_timestamp(bool(checked))
        self._save_map_position(callback=self._load_map)



    def _set_earthquake_layer(self, checked: bool) -> None:
        try:
            enabled = bool(checked and self._internet_available)
            self.config.set_earthquake_layer(enabled)
            if hasattr(self, "map_earthquake_action"):
                self.map_earthquake_action.setChecked(enabled)
                self.map_earthquake_action.setEnabled(self._internet_available)
            if hasattr(self, "quick_earthquake_btn"):
                self.quick_earthquake_btn.setChecked(enabled)
                self.quick_earthquake_btn.setEnabled(self._internet_available)
            self._earthquake_cache = []
            self._earthquake_cache_time = 0
            self._save_map_position(callback=self._load_map)
        except Exception as e:
            print(f"Earthquake layer switch failed: {e}")

    def _set_earthquake_region(self, region: str) -> None:
        try:
            self.config.set_earthquake_region(region)
            if hasattr(self, "map_eq_region_actions"):
                for value, action in self.map_eq_region_actions.items():
                    action.setChecked(value == region)
            self._earthquake_cache = []
            self._earthquake_cache_time = 0
            self._save_map_position(callback=self._load_map)
        except Exception as e:
            print(f"Earthquake region switch failed: {e}")

    def _set_earthquake_min_mag(self, mag: float) -> None:
        try:
            self.config.set_earthquake_min_mag(float(mag))
            if hasattr(self, "map_eq_mag_actions"):
                for value, action in self.map_eq_mag_actions.items():
                    action.setChecked(abs(float(value) - float(mag)) < 0.01)
            self._earthquake_cache = []
            self._earthquake_cache_time = 0
            self._save_map_position(callback=self._load_map)
        except Exception as e:
            print(f"Earthquake magnitude switch failed: {e}")

    def _set_earthquake_refresh(self, minutes: int) -> None:
        try:
            self.config.set_earthquake_refresh(int(minutes))
            if hasattr(self, "map_eq_refresh_actions"):
                for value, action in self.map_eq_refresh_actions.items():
                    action.setChecked(value == minutes)
            self._earthquake_cache = []
            self._earthquake_cache_time = 0
            self._save_map_position(callback=self._load_map)
        except Exception as e:
            print(f"Earthquake refresh switch failed: {e}")

    def _set_wildfire_layer(self, checked: bool) -> None:
        try:
            enabled = bool(checked and self._internet_available)
            self.config.set_wildfire_layer(enabled)
            if hasattr(self, "map_wildfire_action"):
                self.map_wildfire_action.setChecked(enabled)
                self.map_wildfire_action.setEnabled(self._internet_available)
            if hasattr(self, "quick_wildfire_btn"):
                self.quick_wildfire_btn.setChecked(enabled)
                self.quick_wildfire_btn.setEnabled(self._internet_available)
            self._wildfire_cache = []
            self._wildfire_cache_time = 0
            self._save_map_position(callback=self._load_map)
        except Exception as e:
            print(f"Wildfire layer switch failed: {e}")

    def _set_wildfire_min_acres(self, acres: float) -> None:
        try:
            self.config.set_wildfire_min_acres(float(acres))
            if hasattr(self, "map_wildfire_acres_actions"):
                for value, action in self.map_wildfire_acres_actions.items():
                    action.setChecked(abs(float(value) - float(acres)) < 0.01)
            self._wildfire_cache = []
            self._wildfire_cache_time = 0
            self._save_map_position(callback=self._load_map)
        except Exception as e:
            print(f"Wildfire size switch failed: {e}")

    def _set_wildfire_refresh(self, minutes: int) -> None:
        try:
            self.config.set_wildfire_refresh(int(minutes))
            if hasattr(self, "map_wildfire_refresh_actions"):
                for value, action in self.map_wildfire_refresh_actions.items():
                    action.setChecked(value == minutes)
            self._wildfire_cache = []
            self._wildfire_cache_time = 0
            self._save_map_position(callback=self._load_map)
        except Exception as e:
            print(f"Wildfire refresh switch failed: {e}")

    def _on_toggle_hide_green_pins(self, checked: bool) -> None:
        """Hide green (all-clear) statreps from table and map. Session-only — resets on restart."""
        self._hide_green_pins = checked
        self._load_statrep_data()
        self._save_map_position(callback=self._load_map)

    def _on_toggle_hide_all_pins(self, checked: bool) -> None:
        """Hide every statrep pin from table and map. Session-only — resets on restart."""
        self._hide_all_pins = checked
        self._load_statrep_data()
        self._save_map_position(callback=self._load_map)

    def _on_toggle_watchlist_pin(self, watchlist_name: str, checked: bool) -> None:
        """Show or hide one watchlist's member pins on the map."""
        shown = self.config.get_shown_watchlists()
        if checked and watchlist_name not in shown:
            shown.append(watchlist_name)
        elif not checked and watchlist_name in shown:
            shown.remove(watchlist_name)
        self.config.set_shown_watchlists(shown)
        self._save_map_position(callback=self._load_map)

    def _on_watchlist_pin_clicked(self, callsign: str) -> None:
        """Open the QRZ Lookup module for a watchlist pin's callsign — the same
        module that opens when clicking a callsign in the Contacts table."""
        callsign = (callsign or "").strip().upper()
        if not callsign:
            return
        from qrz_lookup import QRZLookupDialog
        dlg = QRZLookupDialog(
            module_background=self.config.get_color('module_background'),
            module_foreground=self.config.get_color('module_foreground'),
            program_background=self.config.get_color('program_background'),
            program_foreground=self.config.get_color('program_foreground'),
            refresh_callback=self._load_message_data,
            parent=self
        )
        dlg.cs_edit.setText(callsign)
        dlg._search()
        dlg.msg_edit.setFocus()
        dlg.exec_()

    def _on_toggle_save_all_alerts(self, checked: bool) -> None:
        """Save all incoming group alerts, not just those for groups in the local table."""
        self.config.set_save_all_alerts(checked)

    def _on_toggle_save_all_messages(self, checked: bool) -> None:
        """Save all incoming group messages, not just those for groups in the local table."""
        self.config.set_save_all_messages(checked)

    def _on_toggle_save_all_videos(self, checked: bool) -> None:
        """Save all incoming YouTube video shares. Behavior TBD."""
        self.config.set_save_all_videos(checked)

    def _on_alerts_messages_help(self) -> None:
        """Explain the 'Save all Alerts' / 'Save all Messages' checkboxes."""
        show = self._resolve_dialog_class("help", "show_alerts_messages_help")
        show(self, **self._help_theme_colors())

    def _on_toggle_hide_live_feed(self, checked: bool) -> None:
        """Hide/show the live feed. Persisted to config.ini."""
        self._hide_live_feed = checked
        self.config.set_hide_live_feed(checked)
        if checked:
            self.feed_text.hide()
        else:
            self.feed_text.show()

    def _map_pane_is_free(self) -> bool:
        """True while the pane is showing a region map.

        New alerts and videos may only take the pane when this is true. If the
        operator has deliberately parked the pane on Images, Alerts, Videos or
        Contacts, incoming data must not yank it out from under them — the
        orange Alerts / Videos button is the notification instead.
        """
        return getattr(self, '_current_view_mode', '') in _MAP_REGION_MODES

    def _trigger_show_alerts(self) -> None:
        """Trigger Show Alerts mode when a new alert is received."""
        if not self._map_pane_is_free():
            return
        self._set_map_view_mode("alerts")

    def _trigger_show_videos(self) -> None:
        """Trigger Videos mode when a new video is received."""
        if not self._map_pane_is_free():
            return
        self._set_map_view_mode("videos")

    def _get_filtered_groups(self) -> tuple:
        """Get groups list and show_all flag based on current filter settings.

        Returns:
            Tuple of (include_groups, exclude_groups, show_all) where:
            - include_groups: Group names to include (used when show_all=False)
            - exclude_groups: Group names to exclude (used when show_all=True)
            - show_all: True if base query shows all records (filtered by exclusions)
        """
        show_all = self.config.get_show_every_group()
        all_groups = self.db.get_all_groups()
        unchecked = set(self.config.get_unchecked_groups())

        if show_all:
            exclude = [g for g in all_groups if g in unchecked]
            return [], exclude, True
        else:
            checked = [g for g in all_groups if g not in unchecked]
            return checked, [], False

    def _populate_table(self, table, data, status_colors: dict = None) -> None:
        """Populate a table widget with data.

        Args:
            table: QTableWidget to populate
            data: List of row tuples
            status_colors: Optional dict mapping values to config color keys
        """
        is_message_table = (table == self.message_table)
        is_statrep_table = (table == self.statrep_table)

        table.setRowCount(0)

        # Bulk-populate with repaints suppressed - without this, a jump from a
        # small filtered set (e.g. Custom Filtering narrowed to a few rows) to
        # a large one (e.g. switching Map Filter back to Off/every group) can
        # visibly freeze the UI while thousands of QTableWidgetItems are laid
        # out one at a time. Same fix already used for contacts_table's load.
        table.setUpdatesEnabled(False)

        # Pre-fetch QRZ callsigns and user callsign for bold highlighting
        qrz_callsigns = self.db.get_qrz_callsigns()
        user_callsign, _, __ = self.db.get_user_settings()
        user_callsign = user_callsign.upper() if user_callsign else ""

        for row_num, row_data in enumerate(data):
            table.insertRow(row_num)

            for col_num, value in enumerate(row_data):
                display_value = str(value) if value is not None else ""

                # Decode || newline placeholders in statrep remarks and messages
                raw_message = None
                if is_statrep_table and col_num == 20 and "||" in display_value:
                    decoded_remarks = display_value.replace("||", "\n")
                    display_value = display_value.replace("||", " ")
                elif is_message_table and col_num == 6 and "||" in display_value:
                    raw_message = display_value          # preserve for detail dialog
                    display_value = display_value.replace("||", " ")
                    decoded_remarks = None
                else:
                    decoded_remarks = None

                # Handle SNR (db) column (first column)
                if (is_statrep_table or is_message_table) and col_num == 0:
                    display_value = ""
                    item = QTableWidgetItem(display_value)
                    try:
                        # Check if source = 2 (Internet source)
                        source_value = None
                        if is_statrep_table and len(row_data) > 21:
                            source_value = int(row_data[21]) if row_data[21] is not None else 0
                        elif is_message_table and len(row_data) > 7:
                            source_value = int(row_data[7]) if row_data[7] is not None else 0

                        if source_value == 2:
                            item.setToolTip("   Internet")
                            color = QColor("#9400ff")
                            item.setBackground(color)
                            table.setItem(row_num, col_num, item)
                            continue

                        if source_value == 3:
                            item.setToolTip("   Internet Only")
                            color = QColor("#FF00FF")
                            item.setBackground(color)
                            table.setItem(row_num, col_num, item)
                            continue

                        # Default SNR-based coloring
                        db_value = int(value) if value is not None else 0
                        item.setToolTip(f"   RF SNR {db_value}")
                        if db_value >= -5:
                            color = QColor(self.config.get_color('condition_green'))
                        elif db_value >= -16:
                            color = QColor(self.config.get_color('condition_yellow'))
                        else:
                            color = QColor(self.config.get_color('condition_red'))
                        item.setBackground(color)
                    except (ValueError, TypeError):
                        pass
                    table.setItem(row_num, col_num, item)
                    continue

                # Format datetime column as "Mon DD HH:MM" - column 1 for both tables
                if (is_message_table or is_statrep_table) and col_num == 1:
                    try:
                        dt = datetime.strptime(display_value[:19], "%Y-%m-%d %H:%M:%S")
                        display_value = dt.strftime("%b-%d  %H:%M")
                    except (ValueError, TypeError):
                        display_value = display_value[:16]

                # Format frequency column (column 2) - convert Hz to MHz
                if (is_message_table or is_statrep_table) and col_num == 2:
                    try:
                        freq_mhz = hz_to_mhz(float(value) if value else 0)
                        display_value = f"{freq_mhz:.3f}"  # Show as 7.110
                    except (ValueError, TypeError):
                        pass

                # ID column (col 5) shows the server-assigned global_id, falling
                # back to sr_id when no global_id has been assigned (0/None) —
                # the map pin/bounce click still keys off the row's internal db
                # id (row_data[22]), set as UserRole data below.
                if is_statrep_table and col_num == 5 and len(row_data) > 23:
                    global_id_value = row_data[23]
                    display_value = str(global_id_value) if global_id_value else display_value

                # Message ID column (col 5) shows the server-assigned global_id,
                # falling back to msg_id when no global_id has been assigned (0/None).
                if is_message_table and col_num == 5 and len(row_data) > 9:
                    global_id_value = row_data[9]
                    display_value = str(global_id_value) if global_id_value else display_value


                item = QTableWidgetItem(display_value)

                # Use Kode Mono for remarks/message text columns and StatRep's
                # Grid column. Freq (both tables) and both tables' From/ID
                # columns use Roboto.
                if (is_statrep_table and col_num in (6, 20)) or (is_message_table and col_num == 6):
                    item.setFont(QtGui.QFont("Kode Mono", -1))
                elif (is_statrep_table or is_message_table) and col_num in (2, 3, 5):
                    item.setFont(QtGui.QFont("Roboto", -1))

                # Add tooltip for multi-line remarks
                if decoded_remarks:
                    item.setToolTip(decoded_remarks)

                # Store raw message text (with ||) so detail dialog can show newlines
                if raw_message is not None and is_message_table and col_num == 6:
                    item.setData(QtCore.Qt.UserRole, raw_message)

                # Bold From callsign (col 3) if callsign is in QRZ cache
                if col_num == 3:
                    from_call = base_callsign(display_value)
                    if from_call in qrz_callsigns:
                        font = item.font()
                        font.setBold(True)
                        item.setFont(font)
                        item.setToolTip("Exists in QRZ local cache")
                    # From is clickable in both tables (opens detail view) —
                    # style it like a hyperlink; bold above still only
                    # applies when the callsign is in the QRZ cache.
                    font = item.font()
                    font.setUnderline(True)
                    item.setFont(font)
                    item.setForeground(QColor(_LINK_COLOR))
                # Bold To callsign (col 4) only when it matches the user's callsign
                elif col_num == 4:
                    to_call = base_callsign(display_value)
                    if is_message_table and user_callsign and to_call == base_callsign(user_callsign):
                        font = item.font()
                        font.setBold(True)
                        item.setFont(font)
                # Bold the statrep ID (col 5) — clickable for map bounce/pan —
                # and style it like a hyperlink to match the From column.
                elif is_statrep_table and col_num == 5:
                    font = item.font()
                    font.setBold(True)
                    font.setUnderline(True)
                    item.setFont(font)
                    item.setForeground(QColor(_LINK_COLOR))
                # Bold the message ID (col 5) with a "Delivered" tooltip once the
                # commsrvr confirms delivery (delivered = 1 in the messages table).
                elif is_message_table and col_num == 5 and len(row_data) > 8 and row_data[8]:
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                    item.setToolTip("  Delivery Confirmed")

                if status_colors and value in status_colors:
                    color = QColor(self.config.get_color(status_colors[value]))
                    item.setBackground(color)
                    item.setForeground(color)

                table.setItem(row_num, col_num, item)

            # Store database id on the callsign cell for statrep rows
            if is_statrep_table and len(row_data) > 22:
                cs_item = table.item(row_num, 3)
                if cs_item:
                    cs_item.setData(QtCore.Qt.UserRole, row_data[22])
                # sr_id (col 5, displayed) is not unique across records; the map
                # pin bounce must key off the unique statrep primary key instead.
                id_item = table.item(row_num, 5)
                if id_item:
                    id_item.setData(QtCore.Qt.UserRole, row_data[22])

            # The message ID cell (col 5) may now display global_id instead of
            # msg_id; stash the real msg_id so lookups/deletes keyed on it
            # (MessageDetailDialog) still work regardless of what's shown.
            if is_message_table and len(row_data) > 5:
                msg_id_item = table.item(row_num, 5)
                if msg_id_item:
                    msg_id_item.setData(QtCore.Qt.UserRole, row_data[5])

        # Alert table (non-statrep, non-message): sort by first column descending
        if not is_message_table and not is_statrep_table:
            table.sortItems(0, QtCore.Qt.DescendingOrder)

        table.setUpdatesEnabled(True)

        # One-time content-based fit for the Interactive middle columns (col 0
        # is Fixed, the last column is Stretch — both skipped here). Only runs
        # once per table so a user's manual drag-narrow afterward sticks
        # across future refreshes instead of being recomputed away.
        if (is_message_table or is_statrep_table) and table.rowCount() > 0:
            sized_attr = "_message_table_sized" if is_message_table else "_statrep_table_sized"
            if not getattr(self, sized_attr, False):
                for col in range(1, table.columnCount() - 1):
                    table.resizeColumnToContents(col)
                setattr(self, sized_attr, True)

        if is_message_table:
            # Fresh rows are all visible; _load_message_data re-applies any
            # active FILTER text afterward, which recounts the visible rows.
            self._update_message_count_label()

    def _get_normalization_settings(self) -> Tuple[bool, Optional[Dict[str, str]]]:
        """Get text normalization flag and abbreviations dict.

        Returns:
            (apply_normalization, abbreviations) where abbreviations is None if disabled.
        """
        apply = self.config.get_apply_text_normalization()
        abbrevs = self.db.get_abbreviations() if apply else None
        return apply, abbrevs

    def _normalize_text(self, text: str) -> str:
        """Apply smart title case with abbreviation expansion if normalization is enabled."""
        apply, abbrevs = self._get_normalization_settings()
        return smart_title_case(text, abbrevs, apply) if apply else text

    def _refresh_all_data(self) -> None:
        """Refresh all data views (statrep, messages, and map)."""
        self._load_statrep_data()
        self._load_message_data()
        self._save_map_position(callback=self._load_map)

    def _on_toggle_show_every_group(self, checked: bool) -> None:
        """Toggle showing all groups data (no group filtering)."""
        self.config.set_show_every_group(checked)
        self._refresh_all_data()

    def _on_toggle_group_filter(self, group_name: str, checked: bool) -> None:
        """Toggle a single group's visibility in the StatRep display."""
        unchecked = self.config.get_unchecked_groups()
        if not checked and group_name not in unchecked:
            unchecked.append(group_name)
        elif checked and group_name in unchecked:
            unchecked.remove(group_name)
        self.config.set_unchecked_groups(unchecked)
        self._refresh_all_data()

    def _on_toggle_text_normalization(self, checked: bool) -> None:
        """Toggle text normalization (abbreviation expansion and smart title case)."""
        self.config.set_apply_text_normalization(checked)
        self._refresh_all_data()

    def _on_manage_groups(self) -> None:
        """Open Manage Groups window."""
        Cls = self._resolve_dialog_class("groups", "GroupsDialog")
        dialog = Cls(self.db, self)
        dialog.exec_()
        self._populate_filter_groups_menu()
        # Prune unchecked_groups entries for groups that no longer exist
        all_groups = set(self.db.get_all_groups())
        pruned = [g for g in self.config.get_unchecked_groups() if g in all_groups]
        self.config.set_unchecked_groups(pruned)
        self._refresh_all_data()

    def _on_manage_watchlists(self) -> None:
        """Open Manage Watchlists window."""
        Cls = self._resolve_dialog_class("watchlists", "WatchlistsDialog")
        dialog = Cls(self.db, self)
        dialog.exec_()
        self._populate_watchlist_pins_menu()
        # Prune shown_watchlists entries for watchlists that no longer exist
        all_watchlists = set(self.db.get_all_watchlists())
        pruned = [w for w in self.config.get_shown_watchlists() if w in all_watchlists]
        self.config.set_shown_watchlists(pruned)
        # Membership or pin styling may have changed — redraw the map.
        self._save_map_position(callback=self._load_map)

    def _on_maintenance(self) -> None:
        """Open Maintenance window."""
        Cls = self._resolve_dialog_class("maintenance", "MaintenanceDialog")
        dialog = Cls(self)
        dialog.exec_()

    def _populate_filter_groups_menu(self) -> None:
        """Populate filter menu with per-group checkboxes above 'Show All Groups'."""
        for action in self.filter_group_actions.values():
            self.filter_menu.removeAction(action)
        self.filter_group_actions.clear()

        unchecked = set(self.config.get_unchecked_groups())
        for name in self.db.get_all_groups():
            action = QtWidgets.QAction(name, self)
            action.setCheckable(True)
            action.setChecked(name not in unchecked)
            action.triggered.connect(lambda checked, g=name: self._on_toggle_group_filter(g, checked))
            self.filter_menu.insertAction(self.show_every_group_action, action)
            self.filter_group_actions[name] = action

    def _populate_watchlist_pins_menu(self) -> None:
        """Populate the Watchlist overlay section with one checkbox per watchlist
        that has members, inserted above the Map Theme Options section."""
        for action in self.watchlist_pin_actions.values():
            self.map_theme_menu.removeAction(action)
        self.watchlist_pin_actions.clear()

        shown = set(self.config.get_shown_watchlists())
        counts = self.db.get_watchlist_member_counts()
        for name in sorted(n for n, c in counts.items() if c > 0):
            action = QtWidgets.QAction(name, self)
            action.setCheckable(True)
            action.setChecked(name in shown)
            action.triggered.connect(lambda checked, w=name: self._on_toggle_watchlist_pin(w, checked))
            self.map_theme_menu.insertAction(self.map_theme_label, action)
            self.watchlist_pin_actions[name] = action

    def _on_js8_connectors(self) -> None:
        """Open JS8 Connectors management window."""
        Cls = self._resolve_dialog_class("js8_connectors", "JS8ConnectorsDialog")
        dialog = Cls(self.connector_manager, self.tcp_pool, self)
        dialog.exec_()

    def _handle_connection_changed(self, rig_name: str, is_connected: bool) -> None:
        """
        Handle TCP connection status changes.

        Args:
            rig_name: Name of the rig.
            is_connected: True if connected, False if disconnected.
        """
        if not is_connected:
            # For disconnects, add the message here
            from datetime import datetime, timezone
            utc_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            status_line = f"{utc_str}\t[{rig_name}] Disconnected"
            self.feed_messages.insert(0, status_line)
            self._update_feed_display()
            # Clear status logged flag so it will log again on reconnect
            self.rig_status_logged.discard(rig_name)
        self._update_connected_rigs_display()

    def _handle_callsign_received(self, rig_name: str, callsign: str) -> None:
        """
        Handle callsign received from JS8Call.

        Args:
            rig_name: Name of the rig.
            callsign: Callsign configured in JS8Call.
        """
        if callsign:
            self.rig_callsigns[rig_name] = callsign
            # Callsign is printed later after frequency is received

    def get_callsign_for_rig(self, rig_name: str) -> str:
        """
        Get cached callsign for a rig.

        Args:
            rig_name: Name of the rig.

        Returns:
            Callsign string or empty string if not known.
        """
        return self.rig_callsigns.get(rig_name, "")

    def _handle_grid_received(self, rig_name: str, grid: str) -> None:
        """
        Handle grid received from JS8Call.

        Prints combined rig status line with all collected info.

        Args:
            rig_name: Name of the rig.
            grid: Maidenhead grid square from JS8Call.
        """
        from statrep import get_state_from_connector

        if grid:
            self.rig_grids[rig_name] = grid

            # Get state from connector table
            state = get_state_from_connector(self.connector_manager, rig_name)
            if state:
                self.rig_states[rig_name] = state

            # Get cached values from the TCP client
            client = self.tcp_pool.clients.get(rig_name)
            if client:
                # Only log the status message once per connection
                if rig_name not in self.rig_status_logged:
                    speed_name = client.speed_name or "UNKNOWN"
                    callsign = client.callsign or "UNKNOWN"
                    frequency = client.frequency

                    # Format: [IC-7300] Running in TURBO mode, N0DDK, EM83CV, GA on 7.110
                    status_line = f"[{rig_name}] Running in {speed_name} mode, {callsign}, {grid}, {state or 'XX'} on {frequency:.3f}"
                    print(status_line)
                    self._handle_status_message(rig_name, status_line)
                    self.rig_status_logged.add(rig_name)

    def _handle_status_message(self, rig_name: str, message: str) -> None:
        """Handle status message from TCP client (for live feed display)."""
        utc_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        self._add_to_feed(f"{utc_str}\t{message}", rig_name)

    def _capture_contacts(
        self,
        from_call: str,
        local_snr: int,
        value: str,
        freq_hz: float,
        offset_hz: float,
    ) -> None:
        """
        Snapshot a directed message into the contacts roster (Direct Message Part 1).

        The sender (`from_call`) is the *relay* — the station we directly heard.
        Any callsign parsed out of the body is the *target* — the station the
        relay reported hearing.

        Primary path: body matches '<TARGET_CS> SNR|HEARTBEAT SNR <SIGNED_NUMBER>'.
        Writes Entry 1 (target as heard via relay) and Entry 2 (relay self-presence).

        Hearing path: body matches '<ADDRESSEE_CS> HEARING <CS1> <CS2> ...'.
        Writes one row per listed callsign with relay_cs=from_call and
        target_cs=listed, plus a self-presence row. SNR is unknown for both
        ends of every heard link, so a fixed placeholder
        (_CONTACTS_HEARING_DEFAULT_SNR) is written in both SNR columns.

        Fallback path: any other RX.DIRECTED body (group posts, relayed
        messages, free-form text). Writes a single self-presence row using the
        relay as both relay_cs and target_cs and our local SNR observation in
        both SNR columns, so the roster still reflects who's on the air.

        Args:
            from_call: Sender callsign from params['FROM'] — the relay.
            local_snr: Our SNR reading of the sender (params['SNR']) — relay SNR.
            value:     Message body (params['value']).
            freq_hz:   JS8Call frequency in Hz (dial + offset).
            offset_hz: Audio offset in Hz.
        """
        relay_cs = _strip_cs_suffix(from_call)
        if not _CONTACTS_BASE_CS_PATTERN.match(relay_cs):
            return
        freq_mhz = round(hz_to_mhz(freq_hz, offset_hz), 3)
        value = re.sub(rf'^(?:{re.escape(relay_cs)}\s*:\s*)+', '', value, flags=re.IGNORECASE)
        parsed = parse_contacts_observation(value)
        if parsed is not None:
            target_cs, target_snr = parsed
            self.db.upsert_contacts_pair(relay_cs, int(local_snr), target_cs, target_snr, freq_mhz)
            return
        heard = parse_hearing_observation(value)
        if heard is not None:
            self.db.upsert_contacts_hearing(relay_cs, heard, freq_mhz)
            return
        self.db.upsert_contact_self(relay_cs, int(local_snr), freq_mhz)

    def _purge_old_contacts(self) -> None:
        """Drop contacts rows older than CONTACTS_RETENTION_HOURS. Runs hourly."""
        removed = self.db.purge_old_contacts(CONTACTS_RETENTION_HOURS)
        if removed:
            print(f"[contacts] purged {removed} row(s) older than {CONTACTS_RETENTION_HOURS}h")

    def _handle_tcp_message(self, rig_name: str, message: dict) -> None:
        """
        Handle incoming TCP message from JS8Call.

        Args:
            rig_name: Name of the rig that received the message.
            message: Parsed JSON message from JS8Call.
        """
        from datetime import datetime, timezone

        msg_type = message.get("type", "")
        value = message.get("value", "")
        params = message.get("params", {})

        # JS8Call appends a non-ASCII terminator (e.g. ♦) at end of every value.
        # A non-ASCII char anywhere else usually means a corrupt/garbled decode —
        # surface it so the operator can see the frame was mangled.
        _v_stripped = value.rstrip() if value else ""
        if _v_stripped and any(ord(c) > 127 for c in _v_stripped[:-1]):
            print(f"{ConsoleColors.WARNING}[{rig_name}] Malformed Data — non-ASCII character in payload: {value!r}{ConsoleColors.RESET}")
            return

        # Handle RX.DIRECTED messages
        if msg_type == "RX.DIRECTED":
            from_call = params.get("FROM", "")
            to_call = params.get("TO", "")
            grid = params.get("GRID", "")
            freq = params.get("FREQ", 0)
            offset = params.get("OFFSET", 0)
            snr = params.get("SNR", 0)
            utc_ms = params.get("UTC", 0)

            # Convert UTC milliseconds to datetime strings
            utc_dt = datetime.fromtimestamp(utc_ms / 1000, tz=timezone.utc)
            utc_db = utc_dt.strftime("%Y-%m-%d %H:%M:%S")  # Single space for database
            utc_display = utc_dt.strftime("%Y-%m-%d %H:%M:%S")

            # Format feed line to match DIRECTED.TXT format:
            # DATETIME    FREQ_MHZ    OFFSET    SNR    CALLSIGN: MESSAGE
            # FREQ from JS8Call is dial + offset, so subtract offset to get dial frequency
            dial_freq_mhz = hz_to_mhz(freq, offset)
            feed_line = f"{utc_display}\t{dial_freq_mhz:.3f}\t{offset}\t{snr:+03d}\t{from_call}: {value}"

            # Add to feed buffer (newest first)
            self._add_to_feed(feed_line, rig_name)

            print(f"[{rig_name}] {feed_line}")

            # Process the message for database insertion
            # Use dial frequency (freq - offset) for database storage
            dial_freq = freq - offset if freq else 0

            # --- Relay message detection ---
            # Handles JS8Call relay protocol: RELAY: USER_CALL> CONTENT *DE* SENDER
            import re as _re_relay
            _clean = self._preprocess_message_value(value, from_call)
            _user_call = self.get_callsign_for_rig(rig_name)

            # RR status-report acknowledgment: "RR CALLSIGN,SRID." — net-wide,
            # not gated to messages addressed to us. See _process_rr_ack.
            if self._process_rr_ack(from_call, _clean, utc_db):
                self._load_statrep_data()
                return  # fully handled

            if _user_call:
                # Pattern A: USER_CALL> ACK *DE* RECIPIENT
                _ack = _re_relay.match(
                    r'^(\w+)>\s+ACK\s+\*DE\*\s+(\w+)', _clean, _re_relay.IGNORECASE
                )
                if _ack and _ack.group(1).upper() == _user_call.upper():
                    _recipient = _ack.group(2).upper()
                    QtWidgets.QMessageBox.information(
                        self,
                        "Message Delivered",
                        f"Your message to {_recipient} was delivered successfully."
                    )
                    return  # fully handled

                # Pattern B: USER_CALL> CONTENT *DE* SENDER (not ACK)
                _relay = _re_relay.match(
                    r'^(\w+)>\s+(?!ACK\b)(.+?)\s+\*DE\*\s+(\w+)\s*$', _clean, _re_relay.IGNORECASE
                )
                if _relay and _relay.group(1).upper() == _user_call.upper():
                    _actual_sender = _relay.group(3)
                    _content = f"{_relay.group(2).strip()} - Relayed by: {from_call}"
                    _data_type = self._process_relay_message(
                        rig_name, _actual_sender, _content, _user_call, dial_freq, snr, utc_db
                    )
                    if _data_type == "message":
                        self._load_message_data()
                    return  # fully handled
            # --- End relay detection ---

            # Direct Message Part 1: snapshot SNR-style observations into contacts.
            # Runs after relay handlers (which return early) so relayed traffic
            # is not double-counted as a direct observation.
            self._capture_contacts(from_call, snr, value, freq, offset)

            data_type = self._process_directed_message(
                rig_name, value, from_call, to_call, grid, dial_freq, snr, utc_db
            )

            # Refresh only the relevant UI component
            if data_type == "statrep":
                self._load_statrep_data()
                if not self.config.get_show_alerts():
                    self._save_map_position(callback=self._load_map)
            elif data_type == "message":
                self._load_message_data()
            elif data_type == "alert":
                self._trigger_show_alerts()
            elif data_type == "video":
                self._update_video_button_indicator()
                self._trigger_show_videos()

        # Handle RX.ACTIVITY messages (band activity for live feed)
        elif msg_type == "RX.ACTIVITY":
            from_call = params.get("FROM", "")
            freq = params.get("FREQ", 0)
            offset = params.get("OFFSET", 0)
            snr = params.get("SNR", 0)
            utc_ms = params.get("UTC", 0)

            if value and from_call:
                utc_dt = datetime.fromtimestamp(utc_ms / 1000, tz=timezone.utc)
                utc_str = utc_dt.strftime("%Y-%m-%d %H:%M:%S")
                dial_freq_mhz = hz_to_mhz(freq, offset)
                feed_line = f"{utc_str}\t{dial_freq_mhz:.3f}\t{offset}\t{snr:+03d}\t{from_call}: {value}"
                self._add_to_feed(feed_line, rig_name)

                # Direct Message Part 1: most "SENDER: RELAY SNR -nn" traffic
                # arrives here (RX.ACTIVITY) rather than RX.DIRECTED, because the
                # local JS8Call is usually not subscribed to the conversation.
                self._capture_contacts(from_call, snr, value, freq, offset)

                # Also try to process as a directed message for database storage.
                # JS8Call users send "CALLSIGN: @GROUP MSG text♦" without CommStat's
                # ",msg_id,{^%}" suffix. These arrive as RX.ACTIVITY when the local
                # JS8Call is not subscribed to the target group. The msg_id is
                # generated from the timestamp inside _parse_message().
                # Relay messages like "K7RIE: N0DDK @MAGNET MSG..." (no colon after
                # the relayed callsign) are excluded — only "CALLSIGN: @GROUP MSG..."
                # matches. Duplicate callsigns (JS8Call bug "K7RIE: K7RIE: @MAGNET")
                # are stripped before the check so they still match correctly.
                import re as _re
                _check_value = strip_duplicate_callsign(value, from_call)
                _check_value = _re.sub(r'[^ -~]', '', _check_value).strip()
                _activity_match = _re.match(
                    r'^(?:\w+:\s+)?(@\w+)\s+MSG\s+', _check_value, _re.IGNORECASE
                )
                if self._process_rr_ack(from_call, _check_value, utc_str):
                    self._load_statrep_data()
                elif _activity_match:
                    to_call = _activity_match.group(1)
                    utc_db = utc_dt.strftime("%Y-%m-%d %H:%M:%S")
                    dial_freq = freq - offset if freq else 0
                    data_type = self._process_directed_message(
                        rig_name, value, from_call, to_call, "", dial_freq, snr, utc_db
                    )
                    if data_type == "message":
                        self._load_message_data()
                elif "{&%}" in _check_value or "{F%}" in _check_value:
                    # STATREP arriving via RX.ACTIVITY — extract target from message body
                    # e.g. "K7RIE: N0DDK  ,CN96OU,..." or "K7RIE: @MAGNET ,CN96OU,..."
                    _sr_match = _re.match(
                        r'^(?:\w+:\s+)?(@?\w+)\s*,', _check_value, _re.IGNORECASE
                    )
                    if _sr_match:
                        _sr_to_call = _sr_match.group(1)
                        utc_db = utc_dt.strftime("%Y-%m-%d %H:%M:%S")
                        dial_freq = freq - offset if freq else 0
                        data_type = self._process_directed_message(
                            rig_name, value, from_call, _sr_to_call, "", dial_freq, snr, utc_db
                        )
                        if data_type == "statrep":
                            self._load_statrep_data()
                            if not self.config.get_show_alerts():
                                self._save_map_position(callback=self._load_map)
                else:
                    # Bare conversational group/direct message (no MSG keyword) —
                    # Radio only. e.g. "KG4AQH: @AMRRON  ANYBODY NEED THE AIB...".
                    # Routed straight to _parse_group_message (not through the
                    # _process_directed_message membership gate) so "Save all
                    # Messages" can capture groups we are not a member of.
                    _pp = self._preprocess_message_value(value, from_call)
                    # Callsign keeps its suffix (see _parse_commstat_message)
                    _from_base = from_call.strip().upper() if from_call else ""
                    _utc_db = utc_dt.strftime("%Y-%m-%d %H:%M:%S")
                    _dial_freq = freq - offset if freq else 0
                    _gm_type, _ = self._parse_group_message(
                        rig_name, _pp, _from_base, "", _dial_freq, snr, _utc_db, source=1
                    )
                    if _gm_type == "message":
                        self._load_message_data()

    def _add_to_feed(self, line: str, rig_name: str) -> None:
        """
        Add a message line to the live feed buffer.

        Args:
            line: Formatted message line.
            rig_name: Name of the rig (unused, frequency identifies rig).
        """
        # Insert at beginning (newest first)
        self.feed_messages.insert(0, line)

        # Trim buffer if too large
        if len(self.feed_messages) > self.max_feed_messages:
            self.feed_messages = self.feed_messages[:self.max_feed_messages]

        # Update display
        self._update_feed_display()

    def _preprocess_message_value(self, value: str, from_call: str) -> str:
        """
        Preprocess message value before parsing.

        Applies:
        1. Duplicate callsign removal (JS8Call bug fix)
        2. Slash suffix stripping from message callsign

        Args:
            value: Raw message text
            from_call: Sender callsign (may include suffix)

        Returns:
            Cleaned message value
        """
        import re

        # Strip duplicate callsign (JS8Call bug: "W8APP: W8APP: @GROUP" → "W8APP: @GROUP")
        value = strip_duplicate_callsign(value, from_call)

        # Strip slash suffix from callsign in message (W3BFO/P: → W3BFO:)
        value = re.sub(r'^(\w+)/\w+:', r'\1:', value)

        # Strip non-ASCII characters (e.g., JS8Call EOL diamond ♦) so the
        # commsrvr regex can correctly match and discard the {^%} terminator
        value = re.sub(r'[^ -~]', '', value).strip()

        return value

    def _process_rr_ack(self, from_call: str, value: str, utc_db: str) -> bool:
        """
        Detect a JS8 'RR' status-report acknowledgment: "RR CALLSIGN,SRID.",
        optionally addressed to a group, e.g. "@AMRRON RR N0DDK,Y26".
        Appends "||ACK <from_call>" to the matching statrep row's comments.

        Returns True if the RR pattern matched (fully handled), else False.
        """
        match = re.match(
            r'^(?:\w+:\s+)?(?:@\w+\s+)?RR\s+([A-Z0-9/]{3,12}),(\w{3})\.?\s*$',
            value, re.IGNORECASE
        )
        if not match:
            return False

        target_callsign = _strip_cs_suffix(match.group(1).strip().upper())
        sr_id = match.group(2).strip().upper()
        if not _CONTACTS_BASE_CS_PATTERN.match(target_callsign):
            return True  # matched shape but not a usable callsign — treat as handled, skip DB work

        date_only = utc_db.split(" ")[0]
        ack_callsign = from_call.strip().upper() if from_call else ""
        if not ack_callsign:
            return True

        try:
            with sqlite3.connect(DATABASE_FILE, timeout=10) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT id, from_callsign, comments FROM statrep WHERE date = ? AND sr_id = ?",
                    (date_only, sr_id)
                )
                row = next(
                    (r for r in cursor.fetchall() if base_callsign(r[1]) == base_callsign(target_callsign)),
                    None
                )
                if row:
                    row_id, _, existing_comments = row
                    cursor.execute(
                        "UPDATE statrep SET comments = ? WHERE id = ?",
                        (f"{existing_comments or ''}||ACK {ack_callsign}", row_id)
                    )
                    conn.commit()
        except sqlite3.Error as e:
            print(f"[RR ack] DB error matching statrep {target_callsign}/{sr_id} on {date_only}: {e}")

        return True

    def _parse_standard_statrep(
        self,
        rig_name: str,
        message_value: str,
        from_callsign: str,
        target: str,
        grid: str,
        freq: int,
        snr: int,
        utc: str,
        source: int,
        global_id: int = 0
    ) -> tuple:
        """
        Parse standard STATREP message format.

        Format: ,GRID,PREC,SRID,SRCODE,COMMENTS,{&%}
        Forwarded: ,GRID,PREC,SRID,SRCODE,COMMENTS,ORIG_CALL,{F%}

        Args:
            rig_name: Name of the rig/source
            message_value: Message text
            from_callsign: Sender callsign as transmitted, '/' suffix included
            target: Target @GROUP or callsign
            grid: Grid square from TCP params or empty for commsrvr
            freq: Frequency in Hz
            snr: Signal-to-noise ratio in dB
            utc: UTC timestamp string "YYYY-MM-DD HH:MM:SS"
            source: 1=Radio (TCP), 2=Internet (commsrvr)

        Returns:
            (message_type, None) where message_type is "statrep" or ""
        """
        import re

        is_forwarded = "{F%}" in message_value
        marker = "{F%}" if is_forwarded else "{&%}"

        # Extract statrep data before marker
        match = re.search(r',(.+?)' + re.escape(marker), message_value)
        if not match:
            return ("", None)

        fields = match.group(1).split(",")

        # Need at least 4 fields: GRID, PREC, SRID, SRCODE
        if len(fields) < 4:
            return ("", None)

        statrep_grid = fields[0].strip()
        prec_num = fields[1].strip()
        sr_id = fields[2].strip()
        srcode = fields[3].strip()

        # Expand "+" shorthand
        srcode = expand_plus_shorthand(srcode)

        # Validate SRCODE: must be at least 12 numeric digits
        if len(srcode) < 12 or not srcode[:12].isdigit():
            print(f"{ConsoleColors.WARNING}[{rig_name}] WARNING: Invalid STATREP SRCODE from {from_callsign} - must be 12 numeric digits, got: {srcode}{ConsoleColors.RESET}")
            return ("", None)

        # Validate and get grid (use QRZ if invalid/missing)
        statrep_grid = self._resolve_grid(rig_name, statrep_grid, from_callsign, grid, "STATREP")

        # Comments — same parsing for both standard and forwarded
        # ("Forwarded By:" is already embedded in the remarks text by the sender)
        comments_raw = ",".join([f for f in fields[4:] if f.strip()]).strip() if len(fields) > 4 else ""
        comments = sanitize_ascii(comments_raw)

        # Map scope (DB-persisted text, not the UI display text)
        scope = scope_db_text_for_code(prec_num)

        # Insert statrep
        sr_fields = list(srcode[:12])  # Use only first 12 digits
        date_only, _ = parse_message_datetime(utc)

        # Commsrvr duplicate detection: if we already have this record, only update
        # global_id. Callsigns are matched on their base form so the copy returned by
        # the server (N0DDK) still matches the one we stored locally (N0DDK/P).
        if source == 2:
            _existing = self._find_callsign_duplicate(
                "statrep", "sr_id", date_only, sr_id, from_callsign
            )
            if _existing:
                _ex_id, _ex_call = _existing
                self._upgrade_duplicate_row(
                    "statrep", _ex_id, _ex_call, from_callsign, global_id
                )
                print(f"[{rig_name}] Skipping duplicate STATREP {sr_id} from {from_callsign} — already received (Global ID: {global_id})")
                return ("", None)

        # Build data dict for insertion
        data = {
            'datetime': utc,
            'date': date_only,
            'freq': freq,
            'db': snr,
            'source': source,
            'sr_id': sr_id,
            'from_callsign': from_callsign,
            'target': target,
            'grid': statrep_grid,
            'scope': scope,
            'map': sr_fields[0],
            'power': sr_fields[1],
            'water': sr_fields[2],
            'med': sr_fields[3],
            'telecom': sr_fields[4],
            'travel': sr_fields[5],
            'internet': sr_fields[6],
            'fuel': sr_fields[7],
            'food': sr_fields[8],
            'crime': sr_fields[9],
            'civil': sr_fields[10],
            'political': sr_fields[11],
            'comments': comments,
            'global_id': global_id
        }

        fwd_marker = " (FORWARDED)" if is_forwarded else ""
        result = self._insert_message_data(
            rig_name, "statrep", data, "sr_id", "statrep", from_callsign, fwd_marker
        )
        if result:
            # Auto-ack: only for genuinely new inserts (not duplicates) received
            # live over the JS8 TCP feed (source=1), and only when the report
            # was addressed to a group (no group slot to echo back otherwise).
            if source == 1 and target:
                self._send_statrep_ack(rig_name, target, from_callsign, sr_id)
            return (result, None)

        return ("", None)

    def _send_statrep_ack(self, rig_name: str, group: str, from_callsign: str, sr_id: str) -> None:
        """
        Auto-transmit a JS8 RR acknowledgment for a status report just saved
        from the JS8 TCP feed: "{my callsign}: {group} RR {from_callsign},{sr_id}".
        Mirrors the pattern _process_rr_ack looks for, so other stations
        (and our own database) can record that the report was copied.
        """
        my_callsign = self.get_callsign_for_rig(rig_name)
        if not my_callsign:
            my_callsign, _, __ = self.db.get_user_settings()
        if not my_callsign:
            return

        client = self.tcp_pool.get_client(rig_name)
        if not client or not client.is_connected():
            return

        ack_message = f"{my_callsign.upper()}: {group} RR {from_callsign.upper()},{sr_id}"
        client.send_tx_message(ack_message)
        print(f"[{rig_name}] Sent STATREP ack: {ack_message}")

    def _parse_group_event(
        self,
        rig_name: str,
        message_value: str,
        from_callsign: str,
        target: str,
        grid: str,
        freq: int,
        snr: int,
        utc: str,
        source: int,
        global_id: int = 0
    ) -> tuple:
        """
        Parse a Group Event message and save it as an Event-flavored StatRep row.

        Format: ,GRID,6,ID,PIN,MESSAGE,{##}
        Forwarded: ,GRID,6,ID,PIN,MESSAGE,{F#}
        See group_event.py — the scope slot is always literal "6" (not a
        geographic scope code), and the normal 12-digit condition string is
        replaced by a single Pin-to-Map digit ("1"/"0"). An Event row is
        distinguished the same way Part 1/2 create one locally: all 12
        condition columns hold "6" and scope holds the literal text "EVENT".

        Args:
            rig_name: Name of the rig/source
            message_value: Message text (already {#3}->{##}-normalized)
            from_callsign: Sender callsign as transmitted, '/' suffix included
            target: Target @GROUP or callsign
            grid: Grid square from TCP params or empty for commsrvr
            freq: Frequency in Hz
            snr: Signal-to-noise ratio in dB
            utc: UTC timestamp string "YYYY-MM-DD HH:MM:SS"
            source: 1=Radio (TCP), 2=Internet (commsrvr), 3=Internet-only marker

        Returns:
            (message_type, None) where message_type is "statrep" or ""
        """
        import re

        is_forwarded = "{F#}" in message_value
        marker = "{F#}" if is_forwarded else "{##}"

        match = re.search(r',(.+?)' + re.escape(marker), message_value)
        if not match:
            return ("", None)

        fields = match.group(1).split(",")

        # Need at least 4 fields: GRID, SCOPE(6), ID, PIN
        if len(fields) < 4:
            return ("", None)

        event_grid = fields[0].strip()
        scope_code = fields[1].strip()
        event_id = fields[2].strip()
        pin_flag = fields[3].strip()

        # Scope slot must be the literal Event sentinel "6" — anything else
        # isn't a Group Event message (shouldn't reach here given the {##}
        # marker check, but guards against a malformed/foreign message).
        if scope_code != "6":
            return ("", None)

        pinned = 1 if pin_flag == "1" else 0

        event_grid = self._resolve_grid(rig_name, event_grid, from_callsign, grid, "EVENT")

        message_raw = ",".join([f for f in fields[4:] if f.strip()]).strip() if len(fields) > 4 else ""
        message_text = sanitize_ascii(message_raw)

        date_only, _ = parse_message_datetime(utc)

        data = {
            'datetime': utc,
            'date': date_only,
            'freq': freq,
            'db': snr,
            'source': source,
            'sr_id': event_id,
            'from_callsign': from_callsign,
            'target': target,
            'grid': event_grid,
            'scope': "EVENT",
            'map': "6",
            'power': "6",
            'water': "6",
            'med': "6",
            'telecom': "6",
            'travel': "6",
            'internet': "6",
            'fuel': "6",
            'food': "6",
            'crime': "6",
            'civil': "6",
            'political': "6",
            'comments': message_text,
            'pinned': pinned,
            'global_id': global_id
        }

        fwd_marker = " (FORWARDED)" if is_forwarded else ""
        result = self._insert_message_data(
            rig_name, "statrep", data, "sr_id", "statrep", from_callsign, fwd_marker
        )
        if result:
            return (result, None)

        return ("", None)

    def _parse_alert(
        self,
        rig_name: str,
        message_value: str,
        from_callsign: str,
        target: str,
        freq: int,
        snr: int,
        utc: str,
        source: int
    ) -> tuple:
        """
        Parse ALERT message format.

        New format: @GROUP ,ALERT_ID,COLOR,TITLE,MESSAGE,{%%}
        Old format: @GROUP ,COLOR,TITLE,MESSAGE,{%%}
        Legacy commsrvr format: LRT ,COLOR,TITLE,MESSAGE,{%%}

        Args:
            rig_name: Name of the rig/source
            message_value: Message text
            from_callsign: Sender callsign as transmitted, '/' suffix included
            target: Target @GROUP or callsign
            freq: Frequency in Hz
            snr: Signal-to-noise ratio in dB
            utc: UTC timestamp string "YYYY-MM-DD HH:MM:SS"
            source: 1=Radio (TCP), 2=Internet (commsrvr)

        Returns:
            (message_type, None) where message_type is "alert" or ""
        """
        import re

        # Try standard @GROUP pattern first
        match = re.search(r'(@\w+)\s*,(.+?)\{\%\%\}', message_value)
        if match:
            alert_target = match.group(1).strip()
            fields_str = match.group(2).strip()
        else:
            # Try LRT pattern (legacy commsrvr format)
            match = re.search(r'LRT\s*,(.+?)\{\%\%\}', message_value)
            if match:
                alert_target = target if target else "@ALL"
                fields_str = match.group(1).strip()
            else:
                return ("", None)

        # Split fields (max 3 splits to preserve commas in message)
        fields = fields_str.split(",", 3)

        # Determine if we have the new format (with alert_id) or old format
        if len(fields) >= 4:
            # New format: ALERT_ID, COLOR, TITLE, MESSAGE
            alert_id = fields[0].strip()
            try:
                alert_color = int(fields[1].strip())
            except ValueError:
                print(f"{ConsoleColors.WARNING}[{rig_name}] WARNING: Invalid alert color in message from {from_callsign}{ConsoleColors.RESET}")
                return ("", None)
            alert_title = sanitize_ascii(fields[2].strip())
            alert_message = sanitize_ascii(fields[3].strip())
            # Extract date for new format
            date_only, _ = parse_message_datetime(utc)
        elif len(fields) >= 3:
            # Old format: COLOR, TITLE, MESSAGE (no alert_id, generate one)
            try:
                alert_color = int(fields[0].strip())
            except ValueError:
                print(f"{ConsoleColors.WARNING}[{rig_name}] WARNING: Invalid alert color in message from {from_callsign}{ConsoleColors.RESET}")
                return ("", None)
            alert_title = sanitize_ascii(fields[1].strip())
            alert_message = sanitize_ascii(fields[2].strip())
            # Generate time-based alert ID for old format
            date_only, alert_id = parse_message_datetime(utc)
        else:
            return ("", None)

        # Filter alerts by target
        if not alert_target.startswith("@"):
            # No @ prefix — target is a callsign; accept if it matches any known callsign.
            # Compared on base callsigns: an operator running as N0DDK/P must still
            # receive traffic addressed to plain N0DDK.
            user_callsigns = [base_callsign(c) for c in self.rig_callsigns.values() if c]
            if not user_callsigns:
                local_callsign, _, __ = self.db.get_user_settings()
                if local_callsign:
                    user_callsigns = [base_callsign(local_callsign)]
            if base_callsign(alert_target) not in user_callsigns:
                return ("", None)
        else:
            # @GROUP — only save if we're a member of that group (active or not),
            # unless "Save all Alerts" is enabled, which imports every group alert,
            # or the target is the always-accepted network-wide group.
            group_name = alert_target[1:].upper()
            if (not self.config.get_save_all_alerts()
                    and group_name != _ALWAYS_SAVE_GROUP):
                all_groups = self.db.get_all_groups()
                if group_name not in all_groups:
                    return ("", None)

        # Build data dict for insertion
        data = {
            'datetime': utc,
            'date': date_only,
            'freq': freq,
            'db': snr,
            'source': source,
            'alert_id': alert_id,
            'from_callsign': from_callsign,
            'target': alert_target,
            'color': alert_color,
            'title': alert_title,
            'message': alert_message
        }

        result = self._insert_message_data(
            rig_name, "alerts", data, "alert_id", "alert", from_callsign
        )
        if result:
            return (result, None)

        return ("", None)

    def _parse_video(
        self,
        rig_name: str,
        message_value: str,
        from_callsign: str,
        utc: str,
        global_id: int = 0
    ) -> tuple:
        """
        Parse VIDEO (Share Video) message format.

        Format: CALLSIGN: TARGET {TITLE}{URL}{&&}
        e.g. "N0DDK: @AMRRON {Extended ALERT: Oil Supply Collapse}{https://www.youtube.com/watch?v=v0wBXXSZa18}{&&}"

        Args:
            rig_name: Name of the rig/source
            message_value: Message text (includes leading "callsign: " prefix)
            from_callsign: Sender callsign as transmitted, '/' suffix included
            utc: UTC timestamp string "YYYY-MM-DD HH:MM:SS"
            global_id: Server-assigned message ID — the videos table's dedup key
                (videos has no separate locally-generated id like alert_id/sr_id)

        Returns:
            (message_type, None) where message_type is "video" or ""
        """
        import re

        match = re.search(r':\s*(\S+)\s*\{(.*?)\}\{(.*?)\}\{&&\}', message_value)
        if not match:
            return ("", None)

        video_target = match.group(1).strip()
        video_title = sanitize_ascii(match.group(2).strip())
        video_url = sanitize_ascii(match.group(3).strip())

        if not video_title or not video_url:
            return ("", None)

        date_only, _ = parse_message_datetime(utc)

        # Filter by target — same membership rule as alerts, gated by the
        # "Save all Videos" toggle.
        if video_target.startswith("@"):
            if not self.config.get_save_all_videos():
                group_name = video_target[1:].upper()
                all_groups = self.db.get_all_groups()
                if group_name not in all_groups:
                    return ("", None)
        else:
            # Base-callsign compare so a /P operator still receives their own traffic
            user_callsigns = [base_callsign(c) for c in self.rig_callsigns.values() if c]
            if not user_callsigns:
                local_callsign, _, __ = self.db.get_user_settings()
                if local_callsign:
                    user_callsigns = [base_callsign(local_callsign)]
            if base_callsign(video_target) not in user_callsigns:
                return ("", None)

        data = {
            'global_id': global_id,
            'datetime': utc,
            'date': date_only,
            'from_callsign': from_callsign,
            'target': video_target,
            'title': video_title,
            'url': video_url,
            'played': 0,
        }

        result = self._insert_message_data(
            rig_name, "videos", data, "global_id", "video", from_callsign
        )
        if result:
            return (result, None)

        return ("", None)

    def _parse_message(
        self,
        rig_name: str,
        message_value: str,
        from_callsign: str,
        target: str,
        freq: int,
        snr: int,
        utc: str,
        source: int,
        global_id: int = 0,
        raw_line: str = ""
    ) -> tuple:
        """
        Parse MESSAGE format.

        TCP format: CALLSIGN: TARGET MSG message_text
        Commsrvr format: @GROUP MSG ,MSG_ID,MESSAGE_TEXT,{^%}

        Args:
            rig_name: Name of the rig/source
            message_value: Message text
            from_callsign: Sender callsign as transmitted, '/' suffix included
            target: Target @GROUP or callsign
            freq: Frequency in Hz
            snr: Signal-to-noise ratio in dB
            utc: UTC timestamp string "YYYY-MM-DD HH:MM:SS"
            source: 1=Radio (TCP), 2=Internet (commsrvr)
            global_id: Server-assigned message ID from commsrvr (0 for Radio/unknown)
            raw_line: Data line as received (verbatim for commsrvr traffic, an
                equivalent built from JS8Call params for TCP traffic), forwarded
                to _insert_message_data so a direct message to one of our
                callsigns can be echoed back to the server as a delivery
                confirmation.

        Returns:
            (message_type, None) where message_type is "message" or ""
        """
        import re

        msg_id = None
        msg_target = target
        message_text = None

        # Try to parse commsrvr format: [SENDER: ]@GROUP MSG ,MSG_ID,MESSAGE[,{^%}]
        # Callsign prefix is optional — new JS8Call omits it (sender is in from_callsign param).
        # Old JS8Call includes it; strip_duplicate_callsign reduces any double prefix first.
        commsrvr_pattern = re.match(r'^(?:\w+:\s+)?(@?\w+)\s+MSG\s+,([^,]+),(.+?)(?:\s*,\{\^%\})?$', message_value, re.IGNORECASE)
        if commsrvr_pattern:
            msg_target = commsrvr_pattern.group(1).strip()
            msg_id = commsrvr_pattern.group(2).strip()
            message_text = commsrvr_pattern.group(3).strip()
        else:
            # Try TCP MSG pattern: [CALLSIGN: ]TARGET MSG message_text
            tcp_pattern = re.match(r'^(?:\w+:\s+)?(@?\w+)\s+MSG\s+(.+)$', message_value, re.IGNORECASE)
            if tcp_pattern:
                msg_target = tcp_pattern.group(1).strip()
                message_text = tcp_pattern.group(2).strip()
            else:
                # No-"MSG"-keyword variant seen from some AmRRON-style traffic:
                # [CALLSIGN: ]@GROUP  ,MSG_ID,MESSAGE_TEXT,{^%} (e.g. an L27 report).
                # Callsign prefix is optional — present when this arrives as
                # RX.ACTIVITY (band monitoring, not subscribed to the group)
                # but absent from RX.DIRECTED's value (sender rides in the
                # FROM param instead, which is the common case for groups
                # we're actually subscribed to, like @AMRRON). The trailing
                # ",{^%}" terminator is mandatory instead, since there's no
                # "MSG" token to anchor on — without it this would risk
                # matching ordinary "@GROUP ,text" conversational chatter as
                # a structured message.
                no_msg_pattern = re.match(r'^(?:\w+:\s+)?(@\w+)\s+,([^,]+),(.+?)\s*,\{\^%\}$', message_value, re.IGNORECASE)
                if no_msg_pattern:
                    msg_target = no_msg_pattern.group(1).strip()
                    msg_id = no_msg_pattern.group(2).strip()
                    message_text = no_msg_pattern.group(3).strip()
                elif source == 2:
                    # Commsrvr fallback: accept raw message (for older formats)
                    message_text = message_value
                    msg_target = target if target else ""
                else:
                    # TCP: require MSG keyword
                    return ("", None)

        # Skip if message is empty
        if not message_text:
            return ("", None)

        # Clean up message text
        message_text = message_text.strip()

        # Extract date and generate msg_id if not extracted from message
        date_only, generated_msg_id = parse_message_datetime(utc)
        if not msg_id:
            msg_id = generated_msg_id

        # Check if message is to a group we're in or to one of our callsigns
        if msg_target.startswith("@"):
            # Group message - only save if we're a member of that group, unless
            # "Save all Messages" is enabled, which imports every group message,
            # or the target is the always-accepted network-wide group.
            group_name = msg_target[1:].upper()  # Remove @ and normalize
            if (not self.config.get_save_all_messages()
                    and group_name != _ALWAYS_SAVE_GROUP):
                all_groups = self.db.get_all_groups()
                if group_name not in all_groups:
                    # Skip messages to groups we're not in
                    return ("", None)
        else:
            # Direct message - only save if to one of our callsigns.
            # Base-callsign compare so a /P operator still receives their own traffic.
            target_call = base_callsign(msg_target)
            user_callsigns = [base_callsign(c) for c in self.rig_callsigns.values() if c]
            if not user_callsigns:
                # No JS8 connectors active — fall back to user settings callsign
                settings_callsign, _, __ = self.db.get_user_settings()
                if settings_callsign:
                    user_callsigns = [base_callsign(settings_callsign)]
            if target_call not in user_callsigns:
                # Skip messages not to our callsigns
                return ("", None)

        # Build data dict for insertion
        data = {
            'datetime': utc,
            'date': date_only,
            'freq': freq,
            'db': snr,
            'source': source,
            'msg_id': msg_id,
            'from_callsign': from_callsign,
            'target': msg_target,
            'message': message_text,
            'global_id': global_id
        }

        result = self._insert_message_data(
            rig_name, "messages", data, "msg_id", "message", from_callsign, raw_line=raw_line
        )
        if result:
            return (result, None)

        return ("", None)

    def _parse_group_message(
        self,
        rig_name: str,
        message_value: str,
        from_callsign: str,
        target: str,
        freq: int,
        snr: int,
        utc: str,
        source: int
    ) -> tuple:
        """
        Final-fallback parser for Radio (TCP) traffic that matched none of the
        earlier patterns. Captures bare conversational net check-ins of the form:

            [CALLSIGN: ]@GROUP <text>      -> save if group is in the groups table
                                              or "Save all Messages" is enabled
            [CALLSIGN: ]YOURCALL <text>    -> save (directed to one of our callsigns)

        where <text> (after the target token, leading whitespace stripped) is
        longer than 17 characters. These carry no msg_id, so a minute-resolution
        one is generated. Radio only (source == 1) — Internet bare-group messages
        are already handled by _parse_message's source==2 fallback.

        Returns:
            ("message", None) on insert, ("", None) otherwise.
        """
        import re

        # Radio only — Internet is handled by _parse_message's source==2 fallback
        if source != 1:
            return ("", None)

        # Give _parse_message first crack — it recognizes structured MESSAGE
        # traffic (including the no-"MSG"-keyword variant), and this function
        # is sometimes called directly (RX.ACTIVITY's bare fallback, and the
        # non-member "Save all Messages" exception) without _parse_message
        # having been tried yet. Only genuine bare conversational text should
        # reach the guard/length-gate logic below.
        structured_result = self._parse_message(
            rig_name, message_value, from_callsign, target, freq, snr, utc, source
        )
        if structured_result[0]:
            return structured_result

        # [CALLSIGN: ] TARGET <text>  — leading callsign prefix is optional
        m = re.match(r'^(?:\w+:\s+)?(@?\w+)\s+(.+)$', message_value, re.IGNORECASE)
        if not m:
            return ("", None)

        msg_target = m.group(1).strip()
        rest = m.group(2).strip()

        # Skip anything carrying a CommStat structured marker (statrep {&%}/{F%},
        # alert {%%}, message {^%}, or F!304/F!301). On the RX.DIRECTED path the
        # priority chain consumes these before us, but the RX.ACTIVITY path routes
        # here directly — so guard against misfiling them as conversational text.
        if re.search(r'\{[&%^F]%3?\}', message_value) or "F!304" in message_value or "F!301" in message_value:
            return ("", None)

        # Length gate: only substantive messages (drops short check-ins like "CK IN HOUSTON")
        if len(rest) <= 17:
            return ("", None)

        # Save criteria — same policy as _parse_message
        if msg_target.startswith("@"):
            # Group message — save if we're a member, unless "Save all Messages"
            # is on or the target is the always-accepted network-wide group
            group_name = msg_target[1:].upper()
            if (not self.config.get_save_all_messages()
                    and group_name != _ALWAYS_SAVE_GROUP):
                if group_name not in self.db.get_all_groups():
                    return ("", None)
        else:
            # Bare callsign target — only save if it's one of our callsigns.
            # Base-callsign compare so a /P operator still receives their own traffic.
            user_callsigns = [base_callsign(c) for c in self.rig_callsigns.values() if c]
            if not user_callsigns:
                settings_callsign, _, __ = self.db.get_user_settings()
                if settings_callsign:
                    user_callsigns = [base_callsign(settings_callsign)]
            if base_callsign(msg_target) not in user_callsigns:
                return ("", None)

        date_only, msg_id = parse_message_datetime(utc)

        data = {
            'datetime': utc,
            'date': date_only,
            'freq': freq,
            'db': snr,
            'source': source,
            'msg_id': msg_id,
            'from_callsign': from_callsign,
            'target': msg_target,
            'message': rest
        }

        result = self._insert_message_data(
            rig_name, "messages", data, "msg_id", "message", from_callsign
        )
        if result:
            return (result, None)

        return ("", None)

    def _process_relay_message(
        self,
        rig_name: str,
        actual_sender: str,
        content: str,
        target: str,
        freq: int,
        snr: int,
        utc: str
    ) -> str:
        """
        Process a relay-forwarded message addressed to the local user.

        Format: RELAY: USER_CALL> CONTENT *DE* ORIGINAL_SENDER

        Args:
            rig_name: Name of the rig.
            actual_sender: Callsign after *DE* (the original message author).
            content: Message text between '>' and '*DE*'.
            target: User's local callsign (the relay destination).
            freq: Frequency in Hz.
            snr: Signal-to-noise ratio.
            utc: UTC timestamp "YYYY-MM-DD HH:MM:SS".

        Returns:
            "message" on successful insert, "" otherwise.
        """
        from id_utils import parse_message_datetime

        # Keep the callsign as transmitted, suffix included (see _parse_commstat_message)
        actual_sender = actual_sender.strip().upper()
        content = content.strip()
        if not content or not actual_sender:
            return ""

        date_only, msg_id = parse_message_datetime(utc)

        data = {
            'datetime': utc,
            'date': date_only,
            'freq': freq,
            'db': snr,
            'source': 1,
            'msg_id': msg_id,
            'from_callsign': actual_sender,
            'target': target,
            'message': content
        }

        result = self._insert_message_data(
            rig_name, "messages", data, "msg_id", "message", actual_sender
        )
        return result if result else ""

    def _parse_commstat_message(
        self,
        rig_name: str,
        from_callsign: str,
        message_value: str,
        target: str,
        grid: str,
        freq: int,
        snr: int,
        utc: str,
        source: int,  # 1=Radio, 2=Internet
        global_id: int = 0,
        raw_line: str = ""
    ) -> tuple:
        """
        Parse and validate CommStat message in any format.

        Processes messages in priority order:
        1. Standard STATREP ({&%} or {F%})
        2. Group Event ({##} or {F#})
        3. F!304 STATREP (8-digit format)
        4. F!301 STATREP (9-digit format)
        5. ALERT ({%%})
        6. VIDEO ({&&})
        7. MESSAGE (contains "MSG" keyword)

        Args:
            rig_name: Name of the rig/source
            from_callsign: Sender callsign as transmitted, '/' suffix included
            message_value: Message text (already preprocessed)
            target: Target @GROUP or callsign
            grid: Grid square from TCP params or empty for commsrvr
            freq: Frequency in Hz
            snr: Signal-to-noise ratio in dB
            utc: UTC timestamp string "YYYY-MM-DD HH:MM:SS"
            source: 1=Radio (TCP), 2=Internet (commsrvr)
            raw_line: Data line as received — the raw commsrvr line verbatim
                (date/time/freq/0/snr/callsign: message, minus only the leading
                "ID:" prefix) for Internet traffic, or an equivalent line built
                from JS8Call params for Radio (TCP) traffic — forwarded to the
                MESSAGE parser so a direct message can echo it back to the
                server as a delivery confirmation.

        Returns:
            (message_type, data_dict) where:
            - message_type: "statrep", "alert", "message", or "" (invalid/skip)
            - data_dict: None (already inserted by sub-parsers)
        """
        # Validate inputs
        if not from_callsign or not message_value:
            return ("", None)

        # Keep the callsign as transmitted, suffix included (N0DDK/P stays N0DDK/P) —
        # only case is normalized. Duplicate detection compares base callsigns, so the
        # suffix no longer splits a record into two rows.
        from_callsign = from_callsign.strip().upper()

        # Detect Internet-Only markers and normalize
        if "{&%3}" in message_value or "{%%3}" in message_value or "{^%3}" in message_value or "{#3}" in message_value:
            source = 3
            message_value = (message_value
                .replace("{&%3}", "{&%}")
                .replace("{%%3}", "{%%}")
                .replace("{^%3}", "{^%}")
                .replace("{#3}", "{##}"))

        # PRIORITY 1: Standard STATREP ({&%} or {F%})
        if "{&%}" in message_value or "{F%}" in message_value:
            return self._parse_standard_statrep(
                rig_name, message_value, from_callsign, target, grid, freq, snr, utc, source, global_id
            )

        # PRIORITY 2: Group Event ({##} or {F#})
        if "{##}" in message_value or "{F#}" in message_value:
            return self._parse_group_event(
                rig_name, message_value, from_callsign, target, grid, freq, snr, utc, source, global_id
            )

        # PRIORITY 3: F!304 STATREP
        if "F!304" in message_value:
            result = self._process_fcode_statrep(
                rig_name, message_value, from_callsign, target, grid, freq, snr, utc, "F!304", source, global_id
            )
            if result:
                return (result, None)

        # PRIORITY 4: F!301 STATREP
        if "F!301" in message_value:
            result = self._process_fcode_statrep(
                rig_name, message_value, from_callsign, target, grid, freq, snr, utc, "F!301", source, global_id
            )
            if result:
                return (result, None)

        # PRIORITY 5: ALERT ({%%})
        if "{%%}" in message_value:
            return self._parse_alert(
                rig_name, message_value, from_callsign, target, freq, snr, utc, source
            )

        # PRIORITY 6: VIDEO ({&&})
        if "{&&}" in message_value:
            return self._parse_video(
                rig_name, message_value, from_callsign, utc, global_id
            )

        # PRIORITY 7: MESSAGE
        result = self._parse_message(
            rig_name, message_value, from_callsign, target, freq, snr, utc, source, global_id, raw_line
        )
        if result[0]:
            return result

        # PRIORITY 8: Radio-only bare group/direct message (final fallback).
        # Runs only after every structured pattern above has declined.
        if source == 1:
            return self._parse_group_message(
                rig_name, message_value, from_callsign, target, freq, snr, utc, source
            )

        return ("", None)

    def _process_directed_message(
        self,
        rig_name: str,
        value: str,
        from_call: str,
        to_call: str,
        grid: str,
        freq: int,
        snr: int,
        utc: str
    ) -> str:
        """
        Process a directed message received via TCP from JS8Call.

        SIMPLIFIED: Only processes messages containing " MSG "
        - Process ALL messages to groups (to_call starts with @)
        - Process messages to user's callsign only

        Args:
            rig_name: Name of the rig that received the message.
            value: The message text content.
            from_call: Sender callsign (from TCP connection).
            to_call: Recipient callsign or @GROUP (from TCP connection).
            grid: Sender's grid square.
            freq: Frequency in Hz.
            snr: Signal-to-noise ratio.
            utc: UTC timestamp string.

        Returns:
            "statrep", "message", "alert", "video", or empty string
        """
        # Build an equivalent of the commsrvr raw line — date time, freq_hz,
        # unused/0, snr, "callsign: message" — from the as-received JS8Call
        # params, before preprocessing touches value. Only used downstream if
        # this turns out to be a direct message to one of our own callsigns.
        raw_line = f"{utc}\t{freq}\t0\t{snr}\t{from_call}: {value}"

        # Preprocess message value
        value = self._preprocess_message_value(value, from_call)

        # Keep the callsign as transmitted, suffix included (see _parse_commstat_message)
        from_callsign = from_call.strip().upper() if from_call else ""

        # Extract target group
        target = ""
        if to_call.startswith("@"):
            target = to_call

        # Determine if message is relevant (to group or to our callsign)
        user_callsign = self.get_callsign_for_rig(rig_name)
        if not user_callsign:
            user_callsign, _, __ = self.db.get_user_settings()
        # Base-callsign compare on both sides: our own rig callsign may carry a suffix
        # (N0DDK/P), and traffic may be addressed to either form.
        is_to_user = base_callsign(to_call) == base_callsign(user_callsign) if user_callsign else False

        # Group check: groups accepted if in our groups list, plus the
        # always-accepted network-wide group (treated as if we were a member,
        # so its traffic reaches the per-type parsers below — each of which
        # still applies its own save rule).
        if to_call.startswith("@"):
            group_name = to_call[1:].upper()
            is_to_group = (group_name in self.db.get_all_groups()
                           or group_name == _ALWAYS_SAVE_GROUP)
        else:
            is_to_group = False

        # Only process if to our group OR to our callsign.
        # Exception: an @group we're not a member of can still be captured by the
        # bare-message fallback when "Save all Messages" is on (mirrors the
        # RX.ACTIVITY path, which routes to _parse_group_message directly). This
        # only applies the conversational fallback — alerts/statreps to non-member
        # groups remain gated out.
        if not (is_to_group or is_to_user):
            if to_call.startswith("@") and self.config.get_save_all_messages():
                return self._parse_group_message(
                    rig_name, value, from_callsign, target, freq, snr, utc, source=1
                )[0]
            return ""

        # For direct-callsign messages, store the recipient callsign as target
        if is_to_user and not target:
            target = to_call.split("/")[0].upper()

        # Parse using unified parser (source=1 for Radio)
        msg_type, _ = self._parse_commstat_message(
            rig_name, from_callsign, value, target, grid, freq, snr, utc, source=1, raw_line=raw_line
        )

        return msg_type

    def _on_qrz_enable(self) -> None:
        """Open QRZ Settings dialog."""
        Cls = self._resolve_dialog_class("qrz_settings", "QRZSettingsDialog")
        dlg = Cls(self.db, parent=self)
        dlg.exec_()

    def _on_user_settings(self) -> None:
        """Open User Settings dialog."""
        Cls = self._resolve_dialog_class("user_settings", "UserSettingsDialog")
        dlg = Cls(self.db, parent=self)
        dlg.exec_()

    @QtCore.pyqtSlot(str)
    def _queue_notification_sound(self, msg_type: str) -> None:
        """Remember the most recently processed record's type and (re)start debounce timer."""
        self._pending_sound_type = msg_type
        self._sound_debounce_timer.start(self._SOUND_DEBOUNCE_MS)

    def _play_pending_sound(self) -> None:
        """Play the sound for the last record processed in this burst, then clear."""
        msg_type = self._pending_sound_type
        if msg_type and self.config.get_sound_enabled(msg_type):
            self._sound_player.play(msg_type)
        self._pending_sound_type = None

    def _on_sound_settings(self) -> None:
        """Open Sound Settings dialog (per-event sound file + enable)."""
        Cls = self._resolve_dialog_class("sound_settings", "SoundSettingsDialog")
        dlg = Cls(self.config, self._sound_player, parent=self)
        dlg.exec_()

    def _show_image_dialog(
        self,
        title: str,
        image_url: str,
        link_html: str,
        loading_text: str,
        error_prefix: str
    ) -> None:
        """
        Display a dialog that fetches and shows an image from a URL.

        This helper method reduces code duplication for dialogs that display
        remote images (band conditions, solar flux, world map, etc.).

        Args:
            title: Window title for the dialog.
            image_url: URL of the image to fetch.
            link_html: HTML string for the attribution/link label.
            loading_text: Text to show while loading.
            error_prefix: Prefix for error message (e.g., "Failed to load band conditions").
        """
        if not netguard.guard(f'"{title}"'):
            QtWidgets.QMessageBox.information(
                self, "Off-Grid Mode",
                f"\"{title}\" needs an internet connection and is disabled while Off-Grid Mode is on.\n\n"
                "Switch back to ONLINE in the header to use it."
            )
            return

        panel_bg = DEFAULT_COLORS.get("module_background", "#DDDDDD")
        panel_fg = DEFAULT_COLORS.get("module_foreground", "#000000")

        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(title)
        dialog.setMinimumSize(480, 200)
        dialog.setWindowFlags(
            Qt.Window |
            Qt.CustomizeWindowHint |
            Qt.WindowTitleHint |
            Qt.WindowCloseButtonHint
        )
        dialog.setStyleSheet(
            f"QDialog {{ background-color:{panel_bg}; color:{panel_fg}; }}"
            f"QLabel {{ font-size:13px; color:{panel_fg}; }}"
        )

        layout = QtWidgets.QVBoxLayout(dialog)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(10)

        # Image label (shows loading text, then image or error)
        image_label = QtWidgets.QLabel(loading_text)
        image_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(image_label)

        # Attribution/link label
        link_label = QtWidgets.QLabel(link_html)
        link_label.setOpenExternalLinks(True)
        link_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(link_label)

        # Close button row (bottom right)
        from ui_helpers import make_button
        btn_row = QtWidgets.QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch()
        close_btn = make_button("Close", "#555555", 80)
        close_btn.clicked.connect(dialog.close)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

        # Storage for fetched data (shared between threads)
        fetch_result = {'data': None, 'error': None}

        def fetch_image():
            """Background thread: fetch image from URL."""
            try:
                request = urllib.request.Request(
                    image_url,
                    headers={'User-Agent': 'CommStat/2.5'}
                )
                with urllib.request.urlopen(request, timeout=15, context=create_insecure_ssl_context()) as response:
                    fetch_result['data'] = response.read()
            except Exception as e:
                fetch_result['error'] = str(e)

        def update_ui():
            """Poll for fetch completion and update dialog."""
            if fetch_result['data']:
                pixmap = QtGui.QPixmap()
                pixmap.loadFromData(fetch_result['data'])
                image_label.setPixmap(pixmap)
                dialog.adjustSize()
            elif fetch_result['error']:
                image_label.setText(f"{error_prefix}: {fetch_result['error']}")
            else:
                # Still loading, check again in 100ms
                QTimer.singleShot(100, update_ui)

        # Start fetch in background thread
        thread = threading.Thread(target=fetch_image, daemon=True)
        thread.start()

        # Start polling for result
        QTimer.singleShot(100, update_ui)

        dialog.exec_()


# =============================================================================
# Application Entry Point
# =============================================================================

def main() -> None:
    """Application entry point."""
    # Replace commstat.py with commstat-copy.py if the copy exists
    _script_dir = Path(__file__).parent
    _copy = _script_dir / "commstat-copy.py"
    _launcher = _script_dir / "commstat.py"
    if _copy.exists():
        try:
            if _launcher.exists():
                _launcher.unlink()
            _copy.rename(_launcher)
            print("Launcher updated: commstat-copy.py → commstat.py")
        except Exception as e:
            print(f"Warning: Could not replace launcher: {e}")

    # Register tiles:// scheme before QApplication (Qt requirement)
    _tile_scheme = QWebEngineUrlScheme(b'tiles')
    _tile_scheme.setFlags(
        QWebEngineUrlScheme.SecureScheme |
        QWebEngineUrlScheme.LocalScheme |
        QWebEngineUrlScheme.CorsEnabled
    )
    QWebEngineUrlScheme.registerScheme(_tile_scheme)

    QtWidgets.QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QtWidgets.QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QtWidgets.QApplication(sys.argv)

    # Install tile scheme handler on the default profile
    _tile_handler = TileSchemeHandler("tilesPNG2")
    QWebEngineProfile.defaultProfile().installUrlSchemeHandler(b'tiles', _tile_handler)

    # Allow media autoplay in web views (video playback starts without an
    # in-page click; play is triggered from the app UI instead)
    _web_settings = QWebEngineProfile.defaultProfile().settings()
    _web_settings.setAttribute(QWebEngineSettings.PlaybackRequiresUserGesture, False)
    _web_settings.setAttribute(QWebEngineSettings.FullScreenSupportEnabled, True)

    # Set tooltip colors to match Windows (tan background, black text)
    app.setStyleSheet("QToolTip { background-color: #FFFFE1; color: black; border: 1px solid black; }")

    # Load bundled fonts
    from PyQt5.QtGui import QFontDatabase
    import os

    font_dir = os.path.join(os.path.dirname(__file__), 'fonts')
    fonts_to_load = [
        'Roboto-Regular.ttf',
        'Roboto-Bold.ttf',
        'RobotoSlab-Regular.ttf',
        'RobotoSlab-Bold.ttf',
        'RobotoSlab-Black.ttf',
        'KodeMono-Regular.ttf',
        'KodeMono-Medium.ttf',
        'KodeMono-Bold.ttf',
    ]

    for font_file in fonts_to_load:
        font_path = os.path.join(font_dir, font_file)
        if os.path.exists(font_path):
            font_id = QFontDatabase.addApplicationFont(font_path)
            if font_id == -1:
                print(f"Warning: Failed to load font {font_file}")
            else:
                families = QFontDatabase.applicationFontFamilies(font_id)
                print(f"Loaded font: {font_file} -> {families}")
        else:
            print(f"Warning: Font file not found: {font_path}")

    # Load configuration
    config = ConfigManager()

    db = DatabaseManager()

    # Create and show main window
    window = MainWindow(config, db)
    window.show()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
