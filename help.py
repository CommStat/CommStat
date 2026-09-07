# Copyright (c) 2025, 2026 Manuel Ochoa
# This file is part of CommStat.
# Licensed under the GNU General Public License v3.0.
"""help.py - Help content for features that live in the little_gucci.py monolith.

Help for a feature that has its own module (StatRep, JS8 Direct Message, ...)
lives beside that feature, not here — it changes when the feature changes.
This module is only the overflow for little_gucci.py, which is too large to
carry its own help text.

The dialog chrome (title strip, body, Close button) comes from
ui_helpers.show_help_dialog; everything below is content.
"""

from PyQt5 import QtWidgets
from PyQt5.QtCore import Qt

from constants import DEFAULT_COLORS, COLOR_BTN_CLOSE
from ui_helpers import make_button, apply_standard_dialog_chrome, show_help_dialog


_PROG_BG  = DEFAULT_COLORS.get("program_background", "#000000")
_PROG_FG  = DEFAULT_COLORS.get("program_foreground", "#FFFFFF")
_PANEL_BG = DEFAULT_COLORS.get("module_background",  "#FFFFFF")


# ── Menu bar ▸ Help ────────────────────────────────────────────────────────────
# Deliberately NOT built on show_help_dialog: this is a two-line link card, not
# a help document, and a scrolling body frame would look wrong at 370x170.

class HelpDialog(QtWidgets.QDialog):

    def __init__(self, parent=None):
        super().__init__(parent)
        apply_standard_dialog_chrome(self, "Help", 370, 170)
        self._build_ui()

    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(10)

        self.setStyleSheet(f"background-color: {_PANEL_BG};")

        title = QtWidgets.QLabel("Join The Telegram Community")
        title.setStyleSheet(
            f"font-family: 'Roboto Slab'; font-size: 16px; font-weight: 900;"
            f"background-color: {_PROG_BG}; color: {_PROG_FG}; padding: 9px 0px;"
        )
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        body_style = (
            "font-family: Roboto; font-size: 13px; font-weight: normal;"
            "color: #333333; background: transparent;"
        )

        msg = QtWidgets.QLabel("Click this link to join the Telegram community.")
        msg.setStyleSheet(body_style)
        msg.setAlignment(Qt.AlignCenter)
        msg.setWordWrap(True)
        layout.addWidget(msg)

        layout.addSpacing(2)

        link = QtWidgets.QLabel(
            '<a href="https://t.me/+3k3n7O8a1yI1N2E5">'
            'https://t.me/+3k3n7O8a1yI1N2E5</a>'
        )
        link.setOpenExternalLinks(True)
        link.setTextInteractionFlags(Qt.TextBrowserInteraction)
        link.setStyleSheet(body_style)
        link.setAlignment(Qt.AlignCenter)
        link.setWordWrap(True)
        layout.addWidget(link)

        layout.addStretch()

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch()
        close_btn = make_button("Close", COLOR_BTN_CLOSE, 80)
        close_btn.clicked.connect(self.close)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)


# ── Settings menu ▸ Alerts, Messages & StatReps ▸ Help ─────────────────────────

_ALERTS_MESSAGES_HTML = """
<div style="font-family: Roboto; font-size: 13px; color: #333333;">
<p>By default, CommStat saves <b>alerts</b>, <b>messages</b>, and <b>videos</b>
addressed to your callsign, as well as those addressed to any group listed in
your <b>Groups</b> menu.</p>

<p>Checking <b>Save all Alerts</b>, <b>Save all Messages</b>, or
<b>Save all Videos</b> tells CommStat to <i>also</i> save that type of traffic
when sent to <b>any</b> group.</p>

<p>These options only affect group traffic. They never let you see alerts,
messages, or videos that are addressed to other callsigns.</p>

<p><b>Status Reports are different:</b> CommStat always saves a StatRep sent
to <b>any</b> group, whether or not it's in your Groups menu — there is no
"Save all" toggle for them, because that's simply how they're captured.</p>

<p>What you <i>see</i> in the StatRep table is controlled separately, from the
<b>Filter</b> menu's <b>Status Reports</b> section: check or uncheck any of
your own groups to show or hide its reports, and use <b>Show Other Groups</b>
to reveal (or hide) StatReps sent to groups you haven't added.</p>
</div>
"""


def show_alerts_messages_help(parent=None, **colors) -> None:
    """Explain the Save all Alerts / Messages / Videos checkboxes and how
    Status Reports are saved and filtered."""
    show_help_dialog(parent, "Alerts, Messages & StatReps",
                     _ALERTS_MESSAGES_HTML, width=470, height=430, **colors)


# ── Map ▸ Filter dropdown ▸ Help ───────────────────────────────────────────────

_MAP_FILTER_HTML = """
<div style="font-family: Roboto; font-size: 13px; color: #333333;">

<p>The <b>Map Filter</b> button in the top-right corner of the map offers two
ways to filter: <b>Map</b> and <b>Custom</b>. Each one changes what the map and
the <b>StatRep table</b> show, and each one answers a different question.</p>

<p style="background-color:#FFF6DA; padding:8px;">
<b>In both modes</b>, the <b>date range</b> you picked in the Filter menu still
applies. Neither mode will show you records older than that range.</p>

<hr>

<h3 style="color:#28a745;">MAP &mdash; let the map do the filtering</h3>
<p>Use this when you want to ask <i>"what is happening in this area?"</i></p>
<ul>
<li>The map ignores your Filter menu choices and plots
    <b>every station</b> in the date range &mdash; including green pins and
    internet-sourced reports you may have hidden.</li>
<li>The StatRep table is then cut down to <b>only the stations whose pin you can
    currently see on the map</b>.</li>
<li>Pan or zoom and the table follows about a second later.</li>
</ul>
<p style="background-color:#F0F7F1; padding:8px;">
<b>Example.</b> You switch to Map and see 40 pins nationwide, and 40 rows in the
table. You zoom into Colorado &mdash; the table drops to the 3 Colorado stations.
You zoom all the way into one pin, and the table shows that single station.
Zoom back out and the rows come back.</p>

<hr>

<h3 style="color:#6f42c1;">CUSTOM &mdash; filter by what is in the record</h3>
<p>Use this when you want to ask <i>"where is this station / this group /
this grid?"</i></p>
<p>A <b>Custom Filtering</b> bar appears directly above the StatRep table
column headings, with four boxes. Each box shows its name &mdash;
<b>From</b>, <b>To</b>, <b>Grid</b>, <b>Remarks</b> &mdash; in gray until you
type in it. It filters the <b>table and the map together</b>, and it also
ignores your Filter menu choices.</p>

<p><b style="color:#6f42c1;">How matching works</b></p>
<ul>
<li><b>Partial matches count.</b> Typing <b>EM8</b> in Grid finds EM83CV,
    EM84KQ and so on. Capitals do not matter &mdash; the boxes uppercase what
    you type automatically.</li>
<li><b>Commas or spaces mean "or" inside one box.</b> <b>N0DDK, W1ABC</b> or
    <b>N0DDK W1ABC</b> in the From box finds either station.</li>
<li><b>The AND / OR dropdown joins the boxes.</b> The word shown between
    the boxes changes to match, so the bar always reads as a sentence.</li>
<li><b>Empty boxes are ignored.</b> Leave all four empty and you get every
    station in the date range.</li>
</ul>

<p><b style="color:#6f42c1;">Examples</b></p>
<table cellspacing="0" cellpadding="7" width="100%" style="font-size:12px;">
<tr bgcolor="#E6E6E6">
  <td width="46%"><b>What you enter</b></td>
  <td><b>What you get</b></td>
</tr>
<tr bgcolor="#FAFAFA">
  <td>From = <b>N0DDK</b></td>
  <td>Only status reports sent by N0DDK.</td>
</tr>
<tr bgcolor="#F2F2F2">
  <td>From = <b>N0DDK, W1ABC, K9ROB</b></td>
  <td>Reports from any of those three stations.</td>
</tr>
<tr bgcolor="#FAFAFA">
  <td>Grid = <b>EM</b></td>
  <td>Every station in an EM grid square &mdash; a quick way to see one
      region.</td>
</tr>
<tr bgcolor="#F2F2F2">
  <td>To = <b>AMRRON</b></td>
  <td>Traffic sent to the @AMRRON group. You do not need to type the @.</td>
</tr>
<tr bgcolor="#FAFAFA">
  <td>Remarks = <b>SHELTER</b></td>
  <td>Every status report whose remarks mention SHELTER.</td>
</tr>
<tr bgcolor="#F2F2F2">
  <td>From = <b>N0DDK</b><br>Grid = <b>EM83</b><br>with <b>AND</b></td>
  <td><b>Both</b> must be true &mdash; N0DDK's reports, but only the ones in
      grid EM83.</td>
</tr>
<tr bgcolor="#FAFAFA">
  <td>From = <b>N0DDK</b><br>Grid = <b>EM83</b><br>with <b>OR</b></td>
  <td><b>Either</b> can be true &mdash; all of N0DDK's reports, <i>plus</i>
      everything else in grid EM83.</td>
</tr>
<tr bgcolor="#F2F2F2">
  <td>From = <b>N0DDK, W1ABC</b><br>Grid = <b>EM83</b><br>with <b>AND</b></td>
  <td>Commas or spaces still mean "or" inside the box: either station, but
      only when it is in grid EM83.</td>
</tr>
</table>

<hr>

<h3 style="color:#6f42c1;">Saving a filter you use often</h3>
<p>A filter you set up can be kept and reused instead of retyped. The dropdown
at the far left of the bar &mdash; it reads <b>-- SAVED FILTERS --</b> until you
pick one &mdash; is where your saved filters live.</p>
<ul>
<li><b>Save.</b> Fill in the boxes, choose AND or OR, then click <b>Save</b> and
    give the filter a name. It is stored with all four boxes <i>and</i> your
    AND/OR choice.</li>
<li><b>Recall.</b> Pick the name from the dropdown. The boxes and the AND/OR
    setting fill in and the table and map re-filter right away.</li>
<li><b>Update.</b> Change the boxes, click <b>Save</b> and type the same name
    again. CommStat asks whether to replace the old one.</li>
<li><b>Delete.</b> Select the filter in the dropdown and click <b>Delete</b>.
    Only the saved copy goes away &mdash; whatever is in the boxes keeps
    filtering.</li>
</ul>

<hr>

<h3 style="color:#555555;">Good to know</h3>
<ul>
<li>While <b>Map</b> or <b>Custom</b> is on, your Filter menu group checkboxes,
    <b>Hide Green Pins</b>, <b>Hide All Pins</b>, and <b>Hide Internet Feed</b>
    are <b>ignored</b>. Switch back to <b>Off</b> to use them again.</li>
<li>Anything you type in the Custom boxes is remembered while CommStat is
    running, so you can switch modes and come back to it. It clears when you
    restart &mdash; <b>saved</b> filters do not, they are always there.</li>
<li>The table updates as you type, a moment after you stop.</li>
</ul>

</div>
"""


def show_map_filter_help(parent=None, **colors) -> None:
    """Explain the map's Map and Custom filtering modes."""
    show_help_dialog(parent, "Map & Custom Filtering",
                     _MAP_FILTER_HTML, width=720, **colors)


# ── Map menu ▸ Watchlist Overlay ▸ Help ────────────────────────────────────────

_WATCHLIST_PINS_HTML = """
<div style="font-family: Roboto; font-size: 13px; color: #333333;">

<p>A <b>Watchlist</b> is a named list of callsigns or simple names that puts an
<span style="color:#e83e8c;"><b>object</b></span> on the map for every
entry. It is made for anybody that wants to view callsigns or locations of 
interest. Group and net leaders can see on the map <i>where their people are</i>,
not just who has reported in.</p>

<p>Watchlists are <b>separate from Groups</b>. Groups name the JS8
<b>@GROUP</b> targets your station listens for; a watchlist is just a map
overlay and never affects which messages you receive.</p>

<p>Each watchlist has its own <b>Object Color</b> (Yellow, Orange, Pink, Purple or Blue) and
<b>Object Shape</b> (Circle, Square, or Triangle), set in
<b>Config &rarr; Manage Watchlists</b>. With five colors and three shapes you
can give up to fifteen watchlists a distinct look on the same map.</p>

<p style="background-color:#FFF6DA; padding:8px;">
<b>Watchlist objects never expire.</b> They ignore the date range, the group
checkboxes, Hide Green Pins, Hide All Pins, Hide Internet Feed, and the Map
Filter. Once a
member is on the map, they stay on the map until you remove them from the
watchlist or uncheck the watchlist checkbox in the map menu.</p>

<hr>

<h3 style="color:#e83e8c;">Turning objects on and off</h3>
<ul>
<li>Under <b>Map &rarr; Watchlist Overlay</b> you will see one checkbox per
    watchlist &mdash; but <b>only watchlists that have members</b> are listed.
    A watchlist you have not added members to yet will not appear.</li>
<li>Check a watchlist and its members appear on the map right away; uncheck it
    and they disappear. Your choices are remembered between sessions.</li>
</ul>

<hr>

<h3 style="color:#e83e8c;">Reading the map objects</h3>
<ul>
<li>A watchlist object is a semi-translucent shape with no solid
    center &mdash; in the watchlist's <b>Object Color</b> and <b>Object Shape</b>, so
    you can tell it apart from live status report pins (and from other
    watchlists' members) at a glance.</li>
<li><b>Hover over an object to see the member's callsign or name.</b> Click it
    to open the QRZ Lookup window for that member.</li>
<li>Status report pins always draw <b>on top</b> of watchlist objects &mdash; when
    a member sends a report, their live pin covers the object.</li>
<li>Members are placed by their grid square (or exact location when QRZ.com
    provides one). A member with no grid on file draws no pin.</li>
</ul>

<hr>

<h3 style="color:#e83e8c;">Adding members to a watchlist</h3>
<ol>
<li>Open <b>Config &rarr; Manage Watchlists</b>, click a watchlist, then click
    <b>Members</b>. The Members column shows how many each watchlist has.</li>
<li>Click <b>Add</b>, type a callsign, and click <b>Search</b> &mdash; or click
    <b>Bulk Add</b> and paste a whole comma-separated list at once.</li>
<li>CommStat checks your <b>local contact database</b> first. If the callsign
    is not there and your QRZ.com subscription is set up
    (Config &rarr; QRZ Settings), it looks the callsign up on
    <b>QRZ.com</b> and saves the result.</li>
<li>If the callsign is found, the details fill in automatically. If not, you
    can type the name, city, state, and grid yourself.</li>
<li>Click <b>Save</b> &mdash; the entry box stays open so you can add the next
    callsign right away.</li>
</ol>

<p style="background-color:#FDEEF5; padding:8px;">
<b>It does not have to be a callsign.</b> Suppose you want to enter nuclear
power plants. You simply enter the name &mdash; Fermi, Palo Verde, or Diablo
Canyon &mdash; and click the <b>Search</b> button. A lookup for the callsign
will fail, since there isn't one, but you can then enter the gridsquare
yourself and click the <b>Save</b> button. You can also add friends, family,
taco stands, or doughnut shops &mdash; anything you want to see on the map.</p>

<p style="text-align:center;">
<img src="watchlist-example.png" width="520" height="281" style="border:1px solid #ccc;"><br>
<span style="font-size:12px; color:#666666;">This is an example of 35 of the
57 nuclear power plants in the United States.</span>
</p>

<p style="background-color:#EAF2FA; padding:8px;">
<b>Wrong location?</b> Callsign locations come from FCC records, and people
move. If a member's grid square is out of date, run their callsign through
<b>Add</b> again and simply type the correct grid before clicking Save &mdash;
the <b>Grid</b> box is always editable. Your correction sticks, even when the
QRZ.com data is refreshed later.</p>

<p style="background-color:#F0F7F1; padding:8px;">
<b>Example.</b> A HUB leader adds all of their region's callsigns to a
HUB4 watchlist, then checks <b>HUB4</b> under Watchlist Overlay. Every member
shows as an object in the watchlist's color and shape &mdash; say a pink circle
for HUB4 and a blue triangle for a second watchlist. As members send status
reports, live pins appear on top of the objects &mdash; making it easy to see who
has checked in and who has not.</p>

</div>
"""


def show_watchlist_pins_help(parent=None, **colors) -> None:
    """Explain the Map menu's Watchlist Overlay section."""
    show_help_dialog(parent, "Watchlist Overlay",
                     _WATCHLIST_PINS_HTML, width=560, **colors)
