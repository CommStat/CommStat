# Brevity 2.0 PyQt — CommStat-compatible window
# 8-character code: List, Event, Phase, Severity, Impact, Official Response, Source, Station
# Keeps CommStat hooks: code_selected, argv colors/prefill/return_file, Paste Code to StatRep, Cancel

import sys
import re
import json
import traceback
import os
import glob
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s: %(message)s")

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QComboBox, QTextEdit,
    QFrame, QCheckBox, QStatusBar, QListView, QGridLayout, QStyledItemDelegate,
)
from PyQt5.QtCore import Qt, QRegExp, pyqtSignal
from PyQt5.QtGui import QFont, QRegExpValidator, QIcon, QColor

try:
    from constants import (
        DEFAULT_COLORS, COLOR_BTN_GREEN, COLOR_BTN_RED, COLOR_BTN_CYAN,
        COLOR_BTN_BLUE, COLOR_BTN_CLOSE, COLOR_INPUT_TEXT, COLOR_INPUT_BORDER,
    )
    _PROG_BG = DEFAULT_COLORS.get("program_background", "#A52A2A")
    _PROG_FG = DEFAULT_COLORS.get("program_foreground", "#FFFFFF")
except Exception:
    DEFAULT_COLORS = {}
    COLOR_BTN_GREEN = "#28a745"
    COLOR_BTN_RED = "#c0392b"
    COLOR_BTN_CYAN = "#17a2b8"
    COLOR_BTN_BLUE = "#2471a3"
    COLOR_BTN_CLOSE = "#555555"
    COLOR_INPUT_TEXT = "#333333"
    COLOR_INPUT_BORDER = "#cccccc"
    _PROG_BG = "#A52A2A"
    _PROG_FG = "#FFFFFF"

from ui_helpers import make_button, apply_standard_dialog_chrome

positions = {}
updating_menus = False
suppress_event_cascade = False
last_event_code = None
emergency_list_mapping = {}
current_file = None
gui_widgets = {}

TITLES_DEFAULT = {
    "select_list": "1. Select List:",
    "emergency": "2. Event / Hazard:",
    "status": "3. Phase:",
    "severity": "4. Severity:",
    "primary": "5. Impact:",
    "secondary_impact": "6. Official Response:",
    "source": "7. Trust / Source:",
    "station": "8. Station Status:",
}


def show_status_message(message, timeout=5000):
    try:
        if "status_bar" in globals():
            globals()["status_bar"].showMessage(message, timeout)
    except Exception as e:
        logging.debug(f"status: {e}")


def script_dir():
    return os.path.dirname(os.path.abspath(__file__))


def validate_json_structure(data):
    required = [
        "emergency_type", "severity", "station_response",
        "shared_impacts", "emergency_group_order", "impact_group_order", "status_codes",
    ]
    missing = [k for k in required if k not in data]
    if missing:
        logging.warning(f"Missing keys: {missing}")
        return False
    if "A" not in data["emergency_type"] or "A" not in data["status_codes"]:
        return False
    return True


def get_json_files():
    global emergency_list_mapping
    emergency_list_mapping = {}
    folder = script_dir()
    files = sorted(glob.glob(os.path.join(folder, "[0-9]-*.json")))
    for file_path in files:
        filename = os.path.basename(file_path)
        if not re.match(r"^[0-9]-.*\.json$", filename, flags=re.IGNORECASE):
            continue
        prefix = filename[0]
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not validate_json_structure(data):
                continue
            if prefix in emergency_list_mapping:
                continue
            emergency_list_mapping[prefix] = filename
        except Exception as e:
            logging.error(f"Error reading {filename}: {e}")
    return emergency_list_mapping


def letter_entries(section):
    out = {}
    if not isinstance(section, dict):
        return out
    for key, val in section.items():
        if re.match(r"^[A-Z]$", str(key)) and isinstance(val, dict) and "name" in val:
            out[key] = val
        elif str(key).startswith("***") and isinstance(val, dict):
            for subkey, subval in val.items():
                if re.match(r"^[A-Z]$", str(subkey)) and isinstance(subval, dict) and "name" in subval:
                    out[subkey] = subval
    return out


def hide_reserved():
    return bool(positions.get("hide_reserved", True))


def is_reserved_entry(entry):
    return str((entry or {}).get("name") or "").strip().lower() == "reserved"


def lookup(section, code):
    return letter_entries(section).get(code, {})


def combo_code(combo):
    if combo is None:
        return None
    text = combo.currentText().strip()
    if not text or text == "Select Code" or text.startswith("***") or text.startswith("No "):
        return None
    return text.split("-")[0].strip()


def current_event_code():
    return combo_code(gui_widgets.get("emergency_combo"))


def event_record(event_code=None):
    code = event_code or current_event_code()
    if not code:
        return {}
    return lookup(positions.get("emergency_type", {}), code)


def impact_section_for_event(event_code=None):
    if not positions:
        return {}
    code = event_code or current_event_code()
    overrides = positions.get("impact_overrides") or {}
    if code and code in overrides:
        block = overrides[code]
        if isinstance(block, dict) and "impacts" in block:
            return block["impacts"]
        if isinstance(block, dict):
            return block
    return positions.get("shared_impacts", {})


def official_response_section():
    return positions.get("official_response") or positions.get("shared_impacts", {})


def severity_section():
    return positions.get("severity") or positions.get("public_reaction") or {}


def strip_article(phrase):
    return re.sub(r"^(an|a)\s+", "", (phrase or "").strip(), flags=re.IGNORECASE)


def group_narrative_for(section, code):
    _key, gdict = group_for_letter(section, code)
    if not isinstance(gdict, dict):
        return ""
    return (gdict.get("narrative") or "").strip()


def apply_group_phrase(base, group_phrase):
    """Prefix base with a group narrative if it is not already in the words."""
    base = (base or "").strip()
    phrase = (group_phrase or "").strip()
    if not phrase or not base:
        return base
    g_core = strip_article(phrase)
    b_core = strip_article(base)
    if not g_core:
        return base
    if g_core.lower() in b_core.lower():
        return base
    article = "an" if re.match(r"^[aeiou]", g_core, flags=re.IGNORECASE) else "a"
    if base.lower().startswith(("a ", "an ")):
        return f"{article} {g_core} {b_core}"
    return f"{g_core} {base}"


def apply_impact_from_phrase(base, group_phrase):
    """Attach motive/context after the harm: 'injuries from an unknown motive'."""
    base = (base or "").strip()
    phrase = (group_phrase or "").strip()
    if not phrase or not base:
        return base
    if phrase.lower() in base.lower():
        return base
    return f"{base} from {phrase}"


def build_lead_sentence(event, phase, severity):
    ev = lookup(positions.get("emergency_type", {}), event)
    ph = lookup(positions.get("status_codes", {}), phase)
    sv = lookup(severity_section(), severity)
    event_noun = ev.get("narrative") or ev.get("name") or "an unknown hazard"
    event_noun = apply_group_phrase(event_noun, group_narrative_for(positions.get("emergency_type", {}), event))
    phase_bit = ph.get("narrative") or ""
    family = ph.get("family") or "state"
    adj = (sv.get("narrative") or "").strip()
    if adj.lower() == "reserved":
        adj = ""
    core = strip_article(event_noun)
    has_article = event_noun.lower().startswith(("an ", "a "))
    mass = core.lower().startswith(("civil unrest", "resource pressure", "strain on"))
    if mass:
        has_article = False
    if phase == "A" or family == "unknown":
        if adj and has_article:
            return f"A {adj} {core} is being reported. The phase is unknown."
        if adj:
            return f"{adj[0].upper() + adj[1:]} {core} is being reported. The phase is unknown."
        if has_article:
            return f"{event_noun[0].upper() + event_noun[1:]} is being reported. The phase is unknown."
        if mass:
            return f"{core[0].upper() + core[1:]} is being reported. The phase is unknown."
        article = "An" if core[:1].lower() in "aeiou" else "A"
        return f"{article} {core} is being reported. The phase is unknown."
    if family == "standalone":
        lead = phase_bit if phase_bit.endswith(".") else (phase_bit + ".")
        if adj and has_article:
            return f"A {adj} {core}. {lead}".replace("..", ".")
        if adj:
            return f"{adj[0].upper() + adj[1:]} {core}. {lead}".replace("..", ".")
        return lead
    if family == "issued":
        if adj and has_article:
            return f"A {adj} {core} {phase_bit}."
        if adj:
            return f"{adj[0].upper() + adj[1:]} {core} {phase_bit}."
        if mass:
            return f"{core[0].upper() + core[1:]} {phase_bit}."
        article = "An" if core[:1].lower() in "aeiou" else "A"
        return f"{article} {core} {phase_bit}."
    if adj and has_article:
        return f"A {adj} {core} {phase_bit}."
    if adj:
        return f"{adj[0].upper() + adj[1:]} {core} {phase_bit}."
    if has_article:
        return f"{event_noun[0].upper() + event_noun[1:]} {phase_bit}."
    if mass:
        return f"{core[0].upper() + core[1:]} {phase_bit}."
    article = "An" if core[:1].lower() in "aeiou" else "A"
    return f"{article} {core} {phase_bit}."


def impact_phase_tense(phase):
    if phase in ("B", "C", "D", "E", "F", "G"):
        return "expected"
    if phase in ("N", "O", "P", "Q", "R", "S", "T"):
        return "past"
    return "current"


def concern_thing(narrative):
    text = (narrative or "").strip()
    plural = bool(re.search(r"\bare the main concern\b", text, flags=re.IGNORECASE))
    text = re.sub(r"\s+are the main concern\.?$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+is the main concern\.?$", "", text, flags=re.IGNORECASE)
    return text.strip(), plural


def group_for_letter(section, code):
    """Return (group_key, group_dict) for a letter, or (None, None)."""
    if not isinstance(section, dict) or not code:
        return None, None
    for key, val in section.items():
        if not str(key).startswith("***") or not isinstance(val, dict):
            continue
        items = val.get("items")
        if isinstance(items, list) and code in items:
            return key, val
        if code in val and re.match(r"^[A-Z]$", str(code)):
            return key, val
    return None, None


def group_description_text(group_key, group_dict):
    """Print the stored description only. Put any label in that JSON text."""
    catalog = positions.get("group_descriptions") or {}
    text = (catalog.get(group_key) or "").strip()
    if not text and isinstance(group_dict, dict) and group_dict.get("show_in_report"):
        text = (group_dict.get("description") or "").strip()
    return text


def selected_group_descriptions(codes):
    """Unique group descriptions for the letters actually chosen."""
    checks = [
        (positions.get("emergency_type", {}), codes.get("event")),
        (positions.get("status_codes", {}), codes.get("phase")),
        (impact_section_for_event(codes.get("event")), codes.get("primary")),
        (official_response_section(), codes.get("secondary")),
        (severity_section(), codes.get("severity")),
        (positions.get("information_codes", {}), codes.get("source")),
        (positions.get("station_response", {}), codes.get("station")),
    ]
    seen = set()
    out = []
    for section, code in checks:
        key, gdict = group_for_letter(section, code)
        if not key:
            continue
        # Event group footers only when the Event row is Other
        if section is positions.get("emergency_type") or section == positions.get("emergency_type"):
            ev = lookup(section, code)
            if (ev.get("name") or "").strip().lower() != "other":
                continue
        text = group_description_text(key, gdict)
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _cap_sent(text):
    text = (text or "").strip()
    if not text:
        return ""
    if text[0].isupper():
        return text if text.endswith(".") else text + "."
    return text[0].upper() + text[1:] + ("." if not text.endswith(".") else "")


def generate_social_summary(codes):
    """List 9: focus + task + status, attitude, how I see it, station; inspiration last."""
    focus = lookup(positions.get("emergency_type", {}), codes["event"])
    task = lookup(impact_section_for_event(codes["event"]), codes["primary"])
    status = lookup(positions.get("status_codes", {}), codes["phase"])
    attitude = lookup(severity_section(), codes["severity"])
    outlook = lookup(official_response_section(), codes["secondary"])
    insp = lookup(positions.get("information_codes", {}), codes["source"])
    station = lookup(positions.get("station_response", {}), codes["station"])
    parts = []
    fn = strip_article((focus.get("narrative") or focus.get("name") or "").strip())
    tn = strip_article((task.get("narrative") or task.get("name") or "").strip())
    sn = (status.get("narrative") or "").strip().lower()
    thing = tn if (tn and codes.get("primary") not in ("", "A")) else fn
    if codes.get("event") not in ("", "A") and fn and tn and codes.get("primary") not in ("", "A"):
        if codes.get("event") in ("X", "Y"):
            thing = tn
        elif fn.lower() not in tn.lower() and tn.lower() not in fn.lower():
            thing = f"{tn} ({fn})"
        else:
            thing = tn
    lead = ""
    if thing:
        if sn.startswith("planning"):
            lead = f"I'm planning {thing}"
        elif "underway" in sn:
            lead = f"I'm working on {thing}"
        elif "finished" in sn:
            lead = f"I finished {thing}"
        elif sn.startswith("stuck"):
            lead = f"I'm stuck on {thing}"
        elif "asking" in sn:
            lead = f"I'm looking for ideas on {thing}"
        elif "offering" in sn:
            lead = f"I'm offering help with {thing}"
        elif "chatting" in sn:
            lead = f"Just talking about {thing}"
        elif sn.startswith("expecting"):
            lead = f"I'm expecting {thing}"
        elif "anticipating" in sn:
            lead = f"I'm anticipating {thing}"
        elif "hoping" in sn:
            lead = f"I'm hoping for {thing}"
        elif "not looking forward" in sn:
            lead = f"I'm not looking forward to {thing}"
        else:
            lead = f"On my plate: {thing}"
        parts.append(_cap_sent(lead))
    elif sn and codes.get("phase") not in ("", "A"):
        parts.append(_cap_sent(status.get("narrative") or ""))
    an = (attitude.get("narrative") or "").strip()
    if an and codes.get("severity") not in ("", "A"):
        parts.append(_cap_sent(an))
    on = (outlook.get("narrative") or "").strip()
    og = group_narrative_for(official_response_section(), codes.get("secondary"))
    if on and codes.get("secondary") not in ("", "A"):
        parts.append(_cap_sent(on))
    stn = (station.get("narrative") or "").strip()
    if stn and codes.get("station") not in ("", "A"):
        parts.append(_cap_sent(stn))
    body = " ".join(p for p in parts if p)
    iname = (insp.get("name") or "").strip()
    itxt = (insp.get("narrative") or "").strip()
    if codes.get("source") in ("", "A") or (iname.lower() == "none") or (iname.lower() == "reserved"):
        return body
    gline = group_narrative_for(positions.get("information_codes", {}), codes.get("source"))
    if itxt.lower() == iname.lower():
        block = iname
    elif itxt:
        block = f"{iname} — {itxt}"
    elif iname:
        block = iname
    else:
        return body
    if not block.endswith("."):
        block = block.rstrip() + "."
    if gline:
        gline = gline.rstrip()
        if not gline.endswith("."):
            gline += "."
        block = gline + "\n" + block
    return body + "\n\n" + block if body else block


def generate_summary(codes):
    if (positions.get("summary_style") or "").strip().lower() == "social":
        return generate_social_summary(codes)
    sentences = []
    lead = build_lead_sentence(codes["event"], codes["phase"], codes["severity"])
    prim = lookup(impact_section_for_event(codes["event"]), codes["primary"])
    p_txt = (prim.get("narrative") or "").strip()
    tense = impact_phase_tense(codes["phase"])
    thing, plural = concern_thing(p_txt) if "main concern" in p_txt.lower() else ("", False)
    group_bit = group_narrative_for(impact_section_for_event(codes["event"]), codes["primary"])
    if thing:
        thing = apply_impact_from_phrase(thing, group_bit)
    elif p_txt and group_bit:
        p_txt = apply_impact_from_phrase(p_txt, group_bit)
    if thing:
        sentences.append(lead if lead.endswith(".") else lead + ".")
        cap = thing[0].upper() + thing[1:]
        be = "are" if plural else "is"
        if tense == "expected":
            sentences.append(f"{cap} {be} the potential impact.")
        elif tense == "past":
            sentences.append(f"{cap} {'were' if plural else 'was'} reported.")
        else:
            sentences.append(f"{cap} {be} the main concern.")
    elif p_txt:
        sentences.append(lead)
        cap = p_txt[0].upper() + p_txt[1:]
        sentences.append(cap if cap.endswith(".") else cap + ".")
    else:
        sentences.append(lead)
    resp = lookup(official_response_section(), codes["secondary"])
    r_txt = (resp.get("narrative") or "").strip()
    if r_txt:
        cap = r_txt[0].upper() + r_txt[1:]
        sentences.append(cap if cap.endswith(".") else cap + ".")
    elif codes["secondary"] == "A":
        sentences.append("Official response is unknown.")
    src = lookup(positions.get("information_codes", {}), codes["source"])
    if codes["source"] == "A":
        sentences.append("Source and credibility are unknown.")
    else:
        src_txt = (src.get("narrative") or "").strip()
        if src_txt:
            cap = src_txt[0].upper() + src_txt[1:]
            sentences.append(cap if cap.endswith(".") else cap + ".")
    st = lookup(positions.get("station_response", {}), codes["station"])
    st_txt = (st.get("narrative") or "").strip()
    if codes["station"] == "A":
        sentences.append("This station's status is unknown.")
    elif st_txt:
        cap = st_txt[0].upper() + st_txt[1:]
        sentences.append(cap if cap.endswith(".") else cap + ".")
    text = re.sub(r"\s+", " ", " ".join(sentences))
    text = re.sub(r"\.\.", ".", text).strip()
    extras = selected_group_descriptions(codes)
    if extras:
        text = text + "\n\n" + "\n\n".join(extras)
    return text


def parse_brevity_code(raw):
    """Return (list_id, codes_dict) or (None, error_string).
    Wire order: List, Event, Phase, Severity, Impact, Response, Source, Station.
    """
    code = (raw or "").strip().upper()
    if re.match(r"^[0-9][A-Z]{7}$", code):
        list_id = code[0]
        event_c, phase_c, sev_c, prim_c, sec_c, src_c, stat_c = code[1:]
        codes = {
            "event": event_c, "phase": phase_c, "primary": prim_c,
            "secondary": sec_c, "severity": sev_c, "source": src_c, "station": stat_c,
        }
        return list_id, codes
    return None, "Invalid code: use 8 characters (#AAAAAAA)"


def load_list_data(list_id):
    """Load one JSON list from disk. Does not touch the GUI."""
    mapping = get_json_files()
    filename = mapping.get(list_id)
    if not filename:
        return None, f"Unknown list ID {list_id}"
    path = os.path.join(script_dir(), filename)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return None, f"Error loading {filename}: {e}"
    if not validate_json_structure(data):
        return None, f"Invalid JSON: {filename}"
    return data, filename


def decode_to_summary(code):
    """Headless: code string in, summary sentence out. No window required."""
    global positions
    list_id, codes = parse_brevity_code(code)
    if list_id is None:
        return codes
    data, filename = load_list_data(list_id)
    if data is None:
        return filename
    saved = positions
    try:
        positions = data
        return generate_summary(codes)
    finally:
        positions = saved


def decode_to_report(code):
    """CommStat hook used by view_statrep.py. Returns a short text report."""
    global positions
    raw = (code or "").strip().upper()
    list_id, codes = parse_brevity_code(raw)
    if list_id is None:
        return f"Invalid {raw}: {codes}"
    data, filename = load_list_data(list_id)
    if data is None:
        return f"Error {raw}: {filename}"
    saved = positions
    try:
        positions = data
        summary = generate_summary(codes)
    finally:
        positions = saved
    return f"Brevity Code: {raw}                    File: {filename}\n\n{summary}"


def generate_narrative(*args, **kwargs):
    """Old CommStat name. Prefer decode_to_summary."""
    if args:
        first = args[0]
        if isinstance(first, str) and re.match(r"^[0-9][A-Z]{7}$", first.strip().upper()):
            return decode_to_summary(first)
        if isinstance(first, dict) and "event" in first:
            return generate_summary(first)
    if len(args) >= 7 and isinstance(args[6], str):
        return decode_to_summary(args[6])
    return decode_to_summary(str(kwargs.get("code") or ""))


def find_brevity_codes(text):
    """All 8-character codes in text."""
    blob = text or ""
    found = []
    seen = set()
    for m in re.finditer(r"(?<![^\s])[0-9][A-Z]{7}(?![^\s])", blob.upper()):
        if m.group() not in seen:
            seen.add(m.group())
            found.append(m.group())
    return found


def current_list_id():
    text = gui_widgets.get("list_combo").currentText() if gui_widgets.get("list_combo") else ""
    for lid, fname in emergency_list_mapping.items():
        if fname == text:
            return lid
    return None


def current_code_string():
    lid = current_list_id()
    codes = current_codes()
    if not lid or not codes:
        return ""
    return lid + "".join([
        codes["event"], codes["phase"], codes["severity"],
        codes["primary"], codes["secondary"], codes["source"], codes["station"],
    ])


def current_codes():
    keys = (
        ("event", "emergency_combo"),
        ("phase", "status_combo"),
        ("primary", "primary_combo"),
        ("secondary", "secondary_combo"),
        ("severity", "severity_combo"),
        ("source", "source_combo"),
        ("station", "station_combo"),
    )
    out = {}
    for name, widget in keys:
        code = combo_code(gui_widgets.get(widget))
        if not code:
            return None
        out[name] = code
    return out


def style_combo_for_selection(combo):
    """Standard combo styling, matching the StatRep dropdowns (Kode Mono 13px)."""
    if combo is None:
        return
    combo.setStyleSheet(
        f"QComboBox {{ background-color: #ffffff; color: {COLOR_INPUT_TEXT}; border: 1px solid {COLOR_INPUT_BORDER};"
        f" border-radius: 4px; padding: 2px 4px; font-family: 'Kode Mono'; font-size: 13px; min-width: 210px; }}"
    )


def restyle_all_combos():
    for key in (
        "emergency_combo", "status_combo", "primary_combo", "secondary_combo",
        "severity_combo", "source_combo", "station_combo",
    ):
        style_combo_for_selection(gui_widgets.get(key))


def populate_combo(combo, section, group_order=None, hide_reserved_items=None):
    if combo is None:
        return
    combo.blockSignals(True)
    combo.clear()
    combo.addItem("Select Code")
    entries = letter_entries(section)
    if hide_reserved_items is None:
        hide_reserved_items = hide_reserved()
    if hide_reserved_items:
        entries = {k: v for k, v in entries.items() if not is_reserved_entry(v)}
    if not entries:
        combo.addItem("No options")
        combo.blockSignals(False)
        return
    if "A" in entries:
        combo.addItem(f"A-{entries['A'].get('name', 'Unknown')}")
        combo.insertSeparator(combo.count())
    groups = [k for k in (section or {}).keys() if str(k).startswith("***")]
    if group_order:
        ordered = [g for g in group_order if g in (section or {})] + [g for g in groups if g not in (group_order or [])]
    else:
        ordered = sorted(groups, key=lambda g: section[g].get("order", 99) if isinstance(section.get(g), dict) else 99)
    used = {"A"}
    for group in ordered:
        if hide_reserved_items and "reserved" in str(group).lower():
            continue
        group_data = section.get(group, {})
        if isinstance(group_data, dict) and "items" in group_data:
            codes = [c for c in group_data["items"] if c in entries]
        else:
            codes = sorted(c for c in group_data.keys() if re.match(r"^[A-Z]$", str(c)) and c in entries)
        if not codes:
            continue
        combo.addItem(group)
        item = combo.model().item(combo.count() - 1)
        item.setEnabled(False)
        font = QFont()
        font.setBold(True)
        item.setFont(font)
        item.setForeground(QColor("#00008B"))
        for code in codes:
            combo.addItem(" " + f"{code}-{entries[code].get('name', 'Unknown')}")
            used.add(code)
        combo.insertSeparator(combo.count())
    for code in sorted(c for c in entries if c not in used):
        combo.addItem(" " + f"{code}-{entries[code].get('name', 'Unknown')}")
    combo.blockSignals(False)
    style_combo_for_selection(combo)


def set_combo_by_code(combo, code):
    if combo is None or not code:
        return
    for i in range(combo.count()):
        text = combo.itemText(i).strip()
        if text.startswith(f"{code}-"):
            combo.blockSignals(True)
            combo.setCurrentIndex(i)
            combo.blockSignals(False)
            style_combo_for_selection(combo)
            return


def refresh_all_menus():
    global updating_menus
    if updating_menus or not positions:
        return
    updating_menus = True
    try:
        populate_combo(
            gui_widgets.get("emergency_combo"),
            positions.get("emergency_type", {}),
            positions.get("emergency_group_order"),
        )
        populate_combo(gui_widgets.get("status_combo"), positions.get("status_codes", {}))
        populate_combo(
            gui_widgets.get("primary_combo"),
            impact_section_for_event(),
            [k for k in impact_section_for_event().keys() if str(k).startswith("***")] or positions.get("impact_group_order"),
        )
        populate_combo(
            gui_widgets.get("secondary_combo"),
            official_response_section(),
            positions.get("official_response_group_order"),
        )
        populate_combo(gui_widgets.get("severity_combo"), severity_section())
        populate_combo(gui_widgets.get("source_combo"), positions.get("information_codes", {}))
        populate_combo(
            gui_widgets.get("station_combo"),
            positions.get("station_response", {}),
            hide_reserved_items=False,
        )
    finally:
        updating_menus = False
        restyle_all_combos()


def apply_titles():
    t = dict(TITLES_DEFAULT)
    t.update(positions.get("gui_titles_v3") or positions.get("gui_titles") or {})
    mapping = [
        ("label_select", "select_list"),
        ("label_emergency", "emergency"),
        ("label_status", "status"),
        ("label_primary", "primary"),
        ("label_secondary", "secondary_impact"),
        ("label_severity", "severity"),
        ("label_source", "source"),
        ("label_station", "station"),
    ]
    for widget, key in mapping:
        if gui_widgets.get(widget):
            gui_widgets[widget].setText(t.get(key, TITLES_DEFAULT[key]))


def apply_event_defaults(event_code):
    rec = event_record(event_code)
    if not rec:
        return
    if rec.get("default_phase"):
        set_combo_by_code(gui_widgets.get("status_combo"), rec["default_phase"])
    if rec.get("default_response"):
        set_combo_by_code(gui_widgets.get("secondary_combo"), rec["default_response"])
    elif gui_widgets.get("secondary_combo"):
        gui_widgets["secondary_combo"].blockSignals(True)
        gui_widgets["secondary_combo"].setCurrentText("Select Code")
        gui_widgets["secondary_combo"].blockSignals(False)
    if rec.get("default_severity"):
        set_combo_by_code(gui_widgets.get("severity_combo"), rec["default_severity"])
    if rec.get("default_source"):
        set_combo_by_code(gui_widgets.get("source_combo"), rec["default_source"])
    if rec.get("default_impact"):
        set_combo_by_code(gui_widgets.get("primary_combo"), rec["default_impact"])
    else:
        gui_widgets["primary_combo"].blockSignals(True)
        gui_widgets["primary_combo"].setCurrentText("Select Code")
        gui_widgets["primary_combo"].blockSignals(False)
    if rec.get("default_station"):
        set_combo_by_code(gui_widgets.get("station_combo"), rec["default_station"])


def load_selected_file(list_id, reset_fields=True):
    global positions, current_file, last_event_code
    filename = emergency_list_mapping.get(list_id)
    if not filename:
        return
    path = os.path.join(script_dir(), filename)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not validate_json_structure(data):
            show_status_message(f"Invalid JSON: {filename}", 8000)
            return
        positions = data
        current_file = filename
        if gui_widgets.get("list_combo"):
            gui_widgets["list_combo"].blockSignals(True)
            gui_widgets["list_combo"].setCurrentText(filename)
            gui_widgets["list_combo"].blockSignals(False)
        apply_titles()
        win = globals().get("main_window")
        if win is not None and hasattr(win, "apply_menu_layout"):
            win.apply_menu_layout((data.get("summary_style") or "").strip().lower() == "social")
        if reset_fields:
            last_event_code = None
            for key in ("emergency_combo", "status_combo", "primary_combo", "secondary_combo",
                        "severity_combo", "source_combo", "station_combo"):
                if gui_widgets.get(key):
                    gui_widgets[key].blockSignals(True)
                    gui_widgets[key].setCurrentText("Select Code")
                    gui_widgets[key].blockSignals(False)
                    style_combo_for_selection(gui_widgets[key])
            if gui_widgets.get("output_text"):
                gui_widgets["output_text"].clear()
            if gui_widgets.get("narrative_text"):
                gui_widgets["narrative_text"].clear()
        refresh_all_menus()
        show_status_message(f"Loaded {filename}", 4000)
    except Exception as e:
        logging.error(f"Load error: {e}")
        show_status_message(f"Error loading {filename}", 8000)


def on_event_changed():
    global last_event_code, suppress_event_cascade, updating_menus
    if updating_menus or suppress_event_cascade or not positions:
        last_event_code = current_event_code()
        return
    new_code = current_event_code()
    changed = new_code != last_event_code
    last_event_code = new_code
    populate_combo(
        gui_widgets.get("primary_combo"),
        impact_section_for_event(new_code),
        [k for k in impact_section_for_event(new_code).keys() if str(k).startswith("***")],
    )
    if changed and new_code:
        apply_event_defaults(new_code)
        show_status_message("Defaults applied from Event — change any field if needed", 5000)
    on_field_change()


def on_field_change(*_args):
    if updating_menus or not positions or not current_list_id():
        return
    codes = current_codes()
    if not codes:
        return
    summary = generate_summary(codes)
    code = current_code_string()
    report = f"Brevity Code: {code}                    File: {emergency_list_mapping.get(current_list_id(), '')}\n\n{summary}"
    if gui_widgets.get("output_text"):
        gui_widgets["output_text"].setPlainText(report)
    show_status_message(f"Code {code}", 4000)


def decode_code(event=None):
    global suppress_event_cascade, last_event_code
    entry = gui_widgets.get("decode_entry")
    raw = (entry.text().strip().upper() if entry else "")
    if re.match(r"^[0-9][A-Z]{7}$", raw):
        list_id = raw[0]
        event_c, phase_c, sev_c, prim_c, sec_c, src_c, stat_c = raw[1:]
    else:
        show_status_message("Invalid code: use 8 characters (#AAAAAAA)", 8000)
        return
    if list_id not in emergency_list_mapping:
        show_status_message(f"Unknown list ID {list_id}", 8000)
        return
    suppress_event_cascade = True
    try:
        load_selected_file(list_id, reset_fields=False)
        set_combo_by_code(gui_widgets.get("emergency_combo"), event_c)
        last_event_code = event_c
        populate_combo(
            gui_widgets.get("primary_combo"),
            impact_section_for_event(event_c),
            [k for k in impact_section_for_event(event_c).keys() if str(k).startswith("***")],
        )
        set_combo_by_code(gui_widgets.get("status_combo"), phase_c)
        set_combo_by_code(gui_widgets.get("primary_combo"), prim_c)
        set_combo_by_code(gui_widgets.get("secondary_combo"), sec_c)
        set_combo_by_code(gui_widgets.get("severity_combo"), sev_c)
        set_combo_by_code(gui_widgets.get("source_combo"), src_c)
        set_combo_by_code(gui_widgets.get("station_combo"), stat_c)
    finally:
        suppress_event_cascade = False
    on_field_change()


def clear_fields():
    global last_event_code
    last_event_code = None
    for key in ("emergency_combo", "status_combo", "primary_combo", "secondary_combo",
                "severity_combo", "source_combo", "station_combo"):
        if gui_widgets.get(key):
            gui_widgets[key].blockSignals(True)
            gui_widgets[key].setCurrentText("Select Code")
            gui_widgets[key].blockSignals(False)
    if gui_widgets.get("decode_entry"):
        gui_widgets["decode_entry"].clear()
    if gui_widgets.get("output_text"):
        gui_widgets["output_text"].clear()
    if gui_widgets.get("narrative_text"):
        gui_widgets["narrative_text"].clear()
    show_status_message("Fields cleared (list unchanged)", 4000)


def handle_menu_select(key, text):
    restyle_all_combos()
    if not text or text == "Select Code" or text.startswith("***"):
        return
    if key == "list" and text != "Select Emergency List":
        load_selected_file(text[0], reset_fields=True)
        return
    if key == "emergency":
        on_event_changed()
        return
    on_field_change()


def extract_code_from_report():
    text = gui_widgets.get("output_text").toPlainText() if gui_widgets.get("output_text") else ""
    if text.startswith("Brevity Code:"):
        first = text.split("\n")[0]
        return first[13:].split("File:")[0].strip()
    return current_code_string()


def copy_sitrep():
    text = ""
    if gui_widgets.get("narrative_text"):
        text = gui_widgets["narrative_text"].toPlainText().strip()
    if not text and gui_widgets.get("output_text"):
        text = gui_widgets["output_text"].toPlainText().strip()
    if not text:
        show_status_message("No report to copy", 6000)
        return
    QApplication.clipboard().setText(text)
    show_status_message("Report copied", 4000)


def copy_all():
    code = extract_code_from_report()
    summary = gui_widgets.get("narrative_text").toPlainText().strip() if gui_widgets.get("narrative_text") else ""
    blob = "\n".join(p for p in (code, summary) if p)
    if not blob:
        show_status_message("Nothing to copy", 6000)
        return
    QApplication.clipboard().setText(blob)
    show_status_message("Code and report copied", 4000)


def toggle_narrative():
    check = gui_widgets.get("narrative_check")
    frame = gui_widgets.get("narrative_frame")
    label = gui_widgets.get("narrative_label")
    visible = bool(check and check.isChecked())
    if frame:
        frame.setVisible(visible)
    if label:
        label.setVisible(visible)


def paste_into_decode():
    clip = QApplication.clipboard().text().strip().upper()
    if gui_widgets.get("decode_entry"):
        gui_widgets["decode_entry"].setText(clip)
        decode_code()


def _copy_code_and_return(return_file: str, code: str = None) -> None:
    if not code:
        code = extract_code_from_report()
    if not code or not return_file:
        return
    try:
        with open(return_file, "w", encoding="utf-8") as f:
            f.write(code)
    except Exception as e:
        logging.error(f"return file: {e}")


class BrevityApp(QMainWindow):
    code_selected = pyqtSignal(str)

    def __init__(self, panel_bg: str = "#d8d8d8", panel_fg: str = "#333333",
                 prefill_code: str = "", parent=None):
        super().__init__(parent)
        self.panel_bg = panel_bg
        self.panel_fg = panel_fg
        apply_standard_dialog_chrome(self, "Brevity 2.0", 930, 580)
        self._setup_ui()
        self._load_data(prefill_code)

    def _combo_block(self, label_attr, combo_attr, caption):
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lab = QLabel(caption)
        lab.setAlignment(Qt.AlignCenter)
        combo = QComboBox()
        combo.setView(QListView())
        combo.setItemDelegate(QStyledItemDelegate(combo))
        combo.setMaxVisibleItems(20)
        combo.view().setMinimumWidth(280)
        combo.addItem("Select Code")
        lay.addWidget(lab)
        lay.addWidget(combo)
        setattr(self, label_attr, lab)
        setattr(self, combo_attr, combo)
        if not hasattr(self, "_menu_boxes"):
            self._menu_boxes = {}
        self._menu_boxes[combo_attr] = box
        return box

    def _setup_ui(self):
        self.setStyleSheet(f"""
            QMainWindow {{ background-color: {self.panel_bg}; color: {self.panel_fg}; }}
            QLabel {{ color: {self.panel_fg}; font-family: Roboto; font-weight: bold; font-size: 13px; }}
            QLineEdit {{ background-color: #ffffff; color: #333333; border: 1px solid #cccccc; padding: 4px; font-size: 11pt; }}
            QComboBox {{ background-color: #ffffff; color: {COLOR_INPUT_TEXT}; border: 1px solid {COLOR_INPUT_BORDER}; border-radius: 4px; padding: 2px 4px; font-family: 'Kode Mono'; font-size: 13px; min-width: 210px; }}
            QComboBox QAbstractItemView {{ background-color: #ffffff; color: {COLOR_INPUT_TEXT}; selection-background-color: #cce5ff; selection-color: #000000; font-family: 'Kode Mono'; font-size: 13px; }}
            QTextEdit {{ background-color: #ffffff; color: #333333; border: 1px solid #cccccc; font-family: 'Kode Mono'; font-size: 12pt; }}
            QCheckBox {{ color: {self.panel_fg}; font-weight: normal; font-size: 11pt; }}
        """)
        central = QWidget()
        self.setCentralWidget(central)
        main = QVBoxLayout(central)
        main.setContentsMargins(15, 15, 15, 2)
        main.setSpacing(10)

        title = QLabel("Brevity 2.0 Encoder/Decoder")
        title.setAlignment(Qt.AlignCenter)
        title.setFixedHeight(36)
        title.setStyleSheet(
            f"QLabel {{ background-color: {_PROG_BG}; color: {_PROG_FG};"
            f" font-family: 'Roboto Slab'; font-size: 16px; font-weight: 900; padding: 9px; }}"
        )
        main.addWidget(title)

        decode_row = QHBoxLayout()
        decode_row.addStretch()
        self.decode_entry = QLineEdit()
        self.decode_entry.setMaxLength(8)
        self.decode_entry.setFixedWidth(160)
        self.decode_entry.setValidator(QRegExpValidator(QRegExp("[0-9]?[A-Za-z]{0,7}")))
        decode_row.addWidget(self.decode_entry)
        decode_button = make_button("Decode", COLOR_BTN_GREEN, min_w=100)
        decode_row.addWidget(decode_button)
        decode_row.addStretch()
        main.addLayout(decode_row)

        grid = QGridLayout()
        list_box = QWidget()
        list_lay = QVBoxLayout(list_box)
        list_lay.setContentsMargins(0, 0, 0, 0)
        self.label_select = QLabel("1. Select List:")
        self.label_select.setAlignment(Qt.AlignCenter)
        self.list_combo = QComboBox()
        self.list_combo.setView(QListView())
        self.list_combo.setItemDelegate(QStyledItemDelegate(self.list_combo))
        self.list_combo.setMaxVisibleItems(20)
        self.list_combo.addItem("Select Emergency List")
        list_lay.addWidget(self.label_select)
        list_lay.addWidget(self.list_combo)
        self.list_box = list_box
        self.menu_grid = grid
        grid.addWidget(list_box, 0, 0)
        grid.addWidget(self._combo_block("label_emergency", "emergency_combo", "2. Event / Hazard:"), 0, 1)
        grid.addWidget(self._combo_block("label_status", "status_combo", "3. Phase:"), 0, 2)
        grid.addWidget(self._combo_block("label_severity", "severity_combo", "4. Severity:"), 1, 0)
        grid.addWidget(self._combo_block("label_primary", "primary_combo", "5. Impact:"), 1, 1)
        grid.addWidget(self._combo_block("label_secondary", "secondary_combo", "6. Official Response:"), 1, 2)
        grid.addWidget(self._combo_block("label_source", "source_combo", "7. Trust / Source:"), 2, 0)
        grid.addWidget(self._combo_block("label_station", "station_combo", "8. Station Status:"), 2, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 1)
        main.addLayout(grid)

        header = QHBoxLayout()
        header.addWidget(QLabel("Brevity Report"))
        header.addStretch()
        main.addLayout(header)

        self.output_text = QTextEdit()
        self.output_text.setMinimumHeight(130)
        main.addWidget(self.output_text)
        narrative_text = self.output_text

        main.addSpacing(8)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch()
        clear_button = make_button("Clear", COLOR_BTN_RED, min_w=100)
        btn_row.addWidget(clear_button)
        copy_code_button = make_button("Copy", COLOR_BTN_GREEN, min_w=100)
        btn_row.addWidget(copy_code_button)
        cancel_button = make_button("Cancel", COLOR_BTN_CLOSE, min_w=100)
        btn_row.addWidget(cancel_button)
        main.addLayout(btn_row)

        status = QStatusBar()
        self.setStatusBar(status)

        globals()["gui_widgets"] = {
            "decode_entry": self.decode_entry,
            "list_combo": self.list_combo,
            "emergency_combo": self.emergency_combo,
            "status_combo": self.status_combo,
            "primary_combo": self.primary_combo,
            "secondary_combo": self.secondary_combo,
            "severity_combo": self.severity_combo,
            "source_combo": self.source_combo,
            "station_combo": self.station_combo,
            "output_text": self.output_text,
            "narrative_text": narrative_text,
            "label_select": self.label_select,
            "label_emergency": self.label_emergency,
            "label_status": self.label_status,
            "label_primary": self.label_primary,
            "label_secondary": self.label_secondary,
            "label_severity": self.label_severity,
            "label_source": self.label_source,
            "label_station": self.label_station,
        }
        globals()["status_bar"] = status
        globals()["main_window"] = self

        decode_button.clicked.connect(decode_code)
        self.decode_entry.returnPressed.connect(decode_code)
        clear_button.clicked.connect(clear_fields)
        copy_code_button.clicked.connect(self._on_copy_code)
        cancel_button.clicked.connect(self.close)
        self.list_combo.currentTextChanged.connect(
            lambda text: handle_menu_select("list", text) if text != "Select Emergency List" else None
        )
        self.emergency_combo.currentTextChanged.connect(lambda text: handle_menu_select("emergency", text))
        self.status_combo.currentTextChanged.connect(lambda text: handle_menu_select("status", text))
        self.primary_combo.currentTextChanged.connect(lambda text: handle_menu_select("primary", text))
        self.secondary_combo.currentTextChanged.connect(lambda text: handle_menu_select("secondary", text))
        self.severity_combo.currentTextChanged.connect(lambda text: handle_menu_select("severity", text))
        self.source_combo.currentTextChanged.connect(lambda text: handle_menu_select("source", text))
        self.station_combo.currentTextChanged.connect(lambda text: handle_menu_select("station", text))

    def apply_menu_layout(self, social=False):
        """Emergency lists keep Event-Phase-Severity. List 9 is Focus-Task-Status."""
        grid = getattr(self, "menu_grid", None)
        boxes = getattr(self, "_menu_boxes", None)
        if grid is None or not boxes:
            return
        order = [
            (getattr(self, "list_box", None), 0, 0),
            (boxes.get("emergency_combo"), 0, 1),
        ]
        if social:
            order += [
                (boxes.get("primary_combo"), 0, 2),
                (boxes.get("status_combo"), 1, 0),
                (boxes.get("severity_combo"), 1, 1),
                (boxes.get("secondary_combo"), 1, 2),
            ]
        else:
            order += [
                (boxes.get("status_combo"), 0, 2),
                (boxes.get("severity_combo"), 1, 0),
                (boxes.get("primary_combo"), 1, 1),
                (boxes.get("secondary_combo"), 1, 2),
            ]
        order += [
            (boxes.get("source_combo"), 2, 0),
            (boxes.get("station_combo"), 2, 1),
        ]
        for box, _r, _c in order:
            if box is not None:
                grid.removeWidget(box)
        for box, r, c in order:
            if box is not None:
                grid.addWidget(box, r, c)

    def _on_copy_code(self):
        code = extract_code_from_report()
        if not code:
            show_status_message("No brevity code available to copy", 8000)
            return
        QApplication.clipboard().setText(code)
        show_status_message("Code copied to clipboard", 4000)
        self.code_selected.emit(code)

    def _load_data(self, prefill_code: str):
        mapping = get_json_files()
        self.list_combo.blockSignals(True)
        for _lid, filename in sorted(mapping.items()):
            self.list_combo.addItem(filename)
        self.list_combo.blockSignals(False)
        if mapping:
            first = sorted(mapping.keys())[0]
            load_selected_file(first, reset_fields=True)
        else:
            show_status_message("No valid JSON files found", 10000)
        if prefill_code:
            self.decode_entry.setText(prefill_code[:8])
            decode_code()


if __name__ == "__main__":
    try:
        panel_bg = sys.argv[1] if len(sys.argv) > 1 else "#d8d8d8"
        panel_fg = sys.argv[2] if len(sys.argv) > 2 else "#333333"
        prefill_code = sys.argv[3] if len(sys.argv) > 3 else ""
        return_file = sys.argv[4] if len(sys.argv) > 4 else ""
        parent_rect = None
        if len(sys.argv) > 8:
            try:
                parent_rect = tuple(int(sys.argv[i]) for i in range(5, 9))
            except (ValueError, TypeError):
                parent_rect = None
        app = QApplication(sys.argv)
        if os.path.exists("radiation-32.png"):
            app.setWindowIcon(QIcon("radiation-32.png"))
        window = BrevityApp(panel_bg, panel_fg, prefill_code)
        if return_file:
            window.code_selected.connect(lambda code: _copy_code_and_return(return_file, code))
        if parent_rect is not None:
            px, py, pw, ph = parent_rect
            window.move(px + (pw - window.width()) // 2, py + (ph - window.height()) // 2)
        window.show()
        sys.exit(app.exec_())
    except Exception as e:
        logging.error(f"Exception in main: {e}")
        traceback.print_exc()