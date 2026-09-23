# Copyright (c) 2025, 2026 Manuel Ochoa
# This file is part of CommStat.
# Licensed under the GNU General Public License v3.0.

"""
CommStat UI and application constants.
Import with: from constants import *
"""

from typing import Dict

# =============================================================================
# Application Identity
# =============================================================================

VERSION = "5.0"

# When True, MainWindow._resolve_dialog_class() re-imports a dialog's module
# from disk every time it's opened, instead of reusing the cached import —
# lets edits to files like maintenance.py show up without restarting
# CommStat. Leave False for normal/shipped runs.
DEV_RELOAD_DIALOGS = False

WINDOW_TITLE = f"CommStat (v{VERSION}) by N0DDK"
WINDOW_SIZE = (1360, 768)
CONFIG_FILE = "config.ini"
ICON_FILE = "radiation-32.png"
DATABASE_FILE = "traffic.db3"
SOUNDS_DIR = "sounds"


# =============================================================================
# Fonts
# =============================================================================

# Timing
HEARTBEAT_DELAY_MS    = 5000       # initial delay before first CommStat server heartbeat
HEARTBEAT_INTERVAL_MS = 180000     # normal heartbeat interval (3 minutes)
RIG_FETCH_DELAY_MS  = 100    # staggered delay for grid/callsign requests after rig select
RIG_FREQ_DELAY_MS   = 200    # staggered delay for frequency request after rig select

FONT_ROBOTO   = "Roboto"
FONT_MONO     = "Kode Mono"

# CSS font-family stacks — use in stylesheets for graceful fallback if fonts
# are not installed (e.g. fresh Linux install without bundled .ttf files).
FONT_ROBOTO_STACK = "Roboto"
FONT_MONO_STACK   = "'Kode Mono'"

# =============================================================================
# UI Colors
# =============================================================================

# Input / form fields
COLOR_INPUT_TEXT    = "#333333"
COLOR_INPUT_BORDER  = "#cccccc"
COLOR_DISABLED_BG   = "#e9ecef"
COLOR_DISABLED_TEXT = "#999999"

# Semantic button colors
COLOR_BTN_RED   = "#dc3545"
COLOR_BTN_GREEN = "#28a745"
COLOR_BTN_BLUE  = "#007bff"
COLOR_BTN_CYAN  = "#4477ff"  ##17a2b8
COLOR_BTN_GRAY  = "#6c757d"
COLOR_BTN_HELP  = "#e83e8c"  # Help buttons app-wide
COLOR_BTN_CLOSE = "#555555"  # Close/dismiss buttons app-wide

# =============================================================================
# Default Color Scheme (used by ConfigManager / config.ini)
# =============================================================================

DEFAULT_COLORS: Dict[str, str] = {
    # Main window
    'program_background': '#A52A2A',       # Maroon
    'program_foreground': '#FFFFFF',
    'menu_background': '#3050CC',          # Blue
    'menu_foreground': '#FFFFFF',
    'title_bar_background': '#F07800',     # Orange
    'title_bar_foreground': '#FFFFFF',
    # News feed marquee
    'newsfeed_background': '#242424',      # Dark gray
    'newsfeed_foreground': '#00FF00',      # Green text
    # Clock display
    'time_background': '#282864',          # Navy blue
    'time_foreground': '#FFFF00',
    # StatRep condition indicators (traffic light)
    #'condition_green': '#28A745',          # Good / normal
    'condition_green': '#20AA20',          # Good / normal
    #'condition_yellow': '#FFFF77',         # Caution / degraded
    'condition_yellow': '#FFDC78',         # Caution / degraded
    #'condition_red': '#DC3534',            # Critical / emergency
    'condition_red': '#CC2020',            # Critical / emergency
    'condition_gray': '#6C757D',           # Unknown / no data
    'condition_purple': '#8000FF',         # Event marker
    # Data tables
    'data_background': '#F5EDD7',          # Cream was F8F6F4
    'data_foreground': '#000000',
    # Live feed display
    'feed_background': '#000000',
    'feed_foreground': '#FFFFFF',
    # Module / dialog background
    'module_background': '#E4E4E4',
    'module_foreground': '#242424',
}

# =============================================================================
# Watchlist objects (map overlay)
# =============================================================================

# Display name -> hex. The friendly names are what users see and what the
# watchlists table stores.
WATCHLIST_OBJECT_COLORS: Dict[str, str] = {
    'BLACK':  '#000000',
    'BLUE':   '#0000FF',
    'GRAY':   '#808080',
    'GREEN':  '#00FF00',
    'ORANGE': '#f07800',
    'PINK':   '#ff66ff',
    'PURPLE': '#8000ff',
    'RED':    '#FF0000',
    'WHITE':  '#FFFFFF',
    'YELLOW': '#ffff00',
}
WATCHLIST_OBJECT_SHAPES = ['CIRCLE', 'SQUARE', 'TRIANGLE']
DEFAULT_OBJECT_COLOR = 'PINK'
DEFAULT_OBJECT_SHAPE = 'CIRCLE'


#'PINK':  '#e83e8c',
#'BLUE':  '#336699',
#'GREEN': '#20c997',


# =============================================================================
# Filter / Map / Slideshow
# =============================================================================

DEFAULT_FILTER_START  = "2023-01-01"
MAX_GROUP_NAME_LENGTH = 8
MAP_WIDTH             = 604
MAP_HEIGHT            = 340
SLIDESHOW_INTERVAL    = 5   # minutes between image changes

# =============================================================================
# Timing
# =============================================================================

INTERNET_CHECK_INTERVAL    = 30 * 60 * 1000   # 30 minutes in ms
NEWSFEED_TYPE_INTERVAL_MS  = 60               # ms per character during type-on
NEWSFEED_PAUSE_MS          = 20000            # ms to hold when window is full
NEWSFEED_SCROLL_DURATION_MS = 1000            # total ms for scroll-off phase

# Contacts roster retention. Hourly cleanup deletes rows whose insert_date is
# older than this many hours, keeping the JS8 Direct Message target/relay
# pickers focused on recently-heard stations.
CONTACTS_RETENTION_HOURS   = 6

# =============================================================================
# StatRep Table Headers
# =============================================================================

STATREP_HEADERS = [
    "", "Date Time", "Freq", "From", "To", "GID", "TID", "Grid", "Scope", "Map",
    "Powr", "H2O", "Med", "Comm", "Trvl", "Inet", "Fuel", "Food",
    "Crime", "Civil", "Pol", "Remarks"
]

# =============================================================================
# StatRep Scope (old/new label compatibility)
# =============================================================================
# The wire/DB representation is always a single digit code ("1"-"5"); the
# combo box + DB + RF/commsrvr text is the display string for that code.
# A future update renames codes 1-4 and retires code 5 (no successor, but
# it must stay decodable forever since old DB rows / other stations' RF
# traffic may still send it).
#
# To ship the rename: flip SCOPE_USE_NEW_LABELS to True. Everything that
# picks display text (SCOPE_OPTIONS, scope_text_for_code) follows
# automatically. Everything that resolves existing/incoming text back to a
# code (scope_code_for_text, SCOPE_RADIUS) already understands both label
# sets, so no other edit is required.
SCOPE_USE_NEW_LABELS = True  # flip to True when the rename ships

SCOPE_CODE_TO_TEXT_OLD = {
    "1": "My Location",
    "2": "My Community",
    "3": "My County",
    "4": "My Region",
    "5": "Other Location",
}

# Code "5" has no successor; intentionally absent so it drops out of the
# combo box once active, while remaining decodable via the OLD map fallback.
SCOPE_CODE_TO_TEXT_NEW = {
    "1": "My QTH",
    "2": "Community",
    "3": "County",
    "4": "Region",
}

SCOPE_CODE_TO_TEXT = SCOPE_CODE_TO_TEXT_NEW if SCOPE_USE_NEW_LABELS else SCOPE_CODE_TO_TEXT_OLD

# Text actually PERSISTED to the DB/RF wire remarks — deliberately distinct
# from SCOPE_CODE_TO_TEXT (the combo box picker stays "My QTH"/"Community"/
# "County"/"Region"; only what gets written on save/receive changed). Every
# statrep write path (local compose in statrep.py, and RF/commsrvr receipt
# parsing in little_gucci.py) should resolve through scope_db_text_for_code(),
# never scope_text_for_code(), so all newly-written rows agree.
SCOPE_CODE_TO_TEXT_DB = {
    "1": "MY QTH",
    "2": "CMNTY",
    "3": "COUNTY",
    "4": "REGION",
}

# Codes no longer offered as a picker choice, but still decodable from
# legacy DB rows / incoming RF traffic (see scope_text_for_code's fallback).
# "Other Location" (5) is dropped now, ahead of the full label rename.
SCOPE_RETIRED_CODES = {"5"}

# (display, code) pairs for building the Scope QComboBox.
SCOPE_OPTIONS = [
    (text, code) for code, text in SCOPE_CODE_TO_TEXT.items()
    if code not in SCOPE_RETIRED_CODES
]

# Reverse lookup covering ALL label sets (old, current-UI, and DB-persisted),
# so stored/incoming text resolves to its code regardless of which set, or
# which app version, produced it.
SCOPE_TEXT_TO_CODE = {}
for _code, _text in SCOPE_CODE_TO_TEXT_OLD.items():
    SCOPE_TEXT_TO_CODE[_text] = _code
for _code, _text in SCOPE_CODE_TO_TEXT_NEW.items():
    SCOPE_TEXT_TO_CODE[_text] = _code
for _code, _text in SCOPE_CODE_TO_TEXT_DB.items():
    SCOPE_TEXT_TO_CODE[_text] = _code
del _code, _text


def scope_code_for_text(text: str) -> str:
    """Display text (any label set: old, current UI, or DB-persisted) ->
    digit code. "" if unrecognized."""
    return SCOPE_TEXT_TO_CODE.get((text or "").strip(), "")


def scope_text_for_code(code: str) -> str:
    """Digit code -> current UI display text (for the combo box / console
    debug output). Falls back to the retired OLD text for any code the
    active set no longer defines. "Unknown" if invalid. NOT for DB writes —
    use scope_db_text_for_code() there."""
    return SCOPE_CODE_TO_TEXT.get(code, SCOPE_CODE_TO_TEXT_OLD.get(code, "Unknown"))


def scope_db_text_for_code(code: str) -> str:
    """Digit code -> text to persist to the DB / RF wire remarks. Falls back
    to the current UI text for any code the DB set doesn't define (e.g. the
    retired code "5", which has no successor)."""
    return SCOPE_CODE_TO_TEXT_DB.get(code, scope_text_for_code(code))


# Map-pin radius (px) by scope CODE, so every label set resolves consistently.
SCOPE_RADIUS_BY_CODE = {
    "1": 3,
    "2": 10,
    "3": 16,
    "4": 24,
    "5": 16,  # "Other Location" — retired but historic rows still carry it
}
SCOPE_RADIUS_DEFAULT = 3  # unknown/missing scope, treated like "My Location"/"My QTH"

# Back-compat TEXT-keyed view built from ALL label sets, since map rows key
# off whatever raw text is stored in the DB (old rows keep whichever text
# was current when they were written, forever).
SCOPE_RADIUS = {}
for _code, _text in SCOPE_CODE_TO_TEXT_OLD.items():
    SCOPE_RADIUS[_text] = SCOPE_RADIUS_BY_CODE[_code]
for _code, _text in SCOPE_CODE_TO_TEXT_NEW.items():
    SCOPE_RADIUS[_text] = SCOPE_RADIUS_BY_CODE[_code]
for _code, _text in SCOPE_CODE_TO_TEXT_DB.items():
    SCOPE_RADIUS[_text] = SCOPE_RADIUS_BY_CODE[_code]
del _code, _text

# =============================================================================
# Console Colors (ANSI)
# =============================================================================

class ConsoleColors:
    """ANSI color codes for console output."""
    SUCCESS = "\033[92m"   # Green
    WARNING = "\033[93m"   # Yellow
    ERROR   = "\033[91m"   # Red
    RESET   = "\033[0m"

# =============================================================================
# Default RSS News Feeds
# =============================================================================

DEFAULT_RSS_FEEDS: Dict[str, str] = {
    "Al Jazeera": "https://www.aljazeera.com/xml/rss/all.xml",
    "AP News":    "https://news.google.com/rss/search?q=when:24h+site:apnews.com&hl=en-US&gl=US&ceid=US:en",
    "BBC World":  "http://feeds.bbci.co.uk/news/world/rss.xml",
    "Fox News":   "https://moxie.foxnews.com/google-publisher/latest.xml",
    "NPR News":   "https://feeds.npr.org/1001/rss.xml",
    "Reuters":    "https://news.google.com/rss/search?q=when:24h+site:reuters.com&hl=en-US&gl=US&ceid=US:en",
}
