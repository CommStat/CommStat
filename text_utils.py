# Copyright (c) 2026 Manuel Ochoa
# This file is part of CommStat.
# Licensed under the GNU General Public License v3.0.
"""
text_utils.py - Text clean-up helpers shared by several modules.
"""

import re

# Words that stay ALL CAPS in an address (compass points, postal abbreviations,
# roman numerals). Single-letter directions (N, S, E, W) need no entry.
_KEEP_UPPER = {"NE", "NW", "SE", "SW", "PO", "RR", "HC", "PMB", "II", "III", "IV"}

_INITIALS = re.compile(r"(?:[A-Za-z]\.){2,}")
_APOSTROPHES = re.compile(r"(['’])")


def _capitalize(part: str) -> str:
    """Upper-case the first character, lower-case the rest ("5th" stays "5th")."""
    return part[:1].upper() + part[1:].lower()


def _title_piece(piece: str) -> str:
    """One hyphen-free piece: handles apostrophes ("O'Brien") and "Mc" names."""
    bits = _APOSTROPHES.split(piece.lower())   # ["o", "'", "brien"]
    out = []
    for i, bit in enumerate(bits):
        if i % 2 == 1:            # an apostrophe
            out.append(bit)
        elif i == 0 or len(bits[i - 2]) == 1:
            # Start of the word, or after a one-letter prefix (O'Brien, D'Angelo).
            # "John's" keeps its lower-case s.
            if len(bit) >= 4 and bit.startswith("mc") and bit[2].isalpha():
                out.append("Mc" + _capitalize(bit[2:]))
            else:
                out.append(_capitalize(bit))
        else:
            out.append(bit)
    return "".join(out)


def _title_word(word: str) -> str:
    # Already mixed case ("McDonald", "DeLuca"): someone capitalized it on purpose.
    if word != word.upper() and word != word.lower():
        return word
    core = word.strip(",;:()#")
    if core and core.strip(".").upper() in _KEEP_UPPER:
        return word.replace(core, core.upper(), 1)
    if _INITIALS.fullmatch(core):      # P.O.  J.R.
        return word.replace(core, core.upper(), 1)
    return "-".join(_title_piece(piece) for piece in word.split("-"))


def title_case(text: str) -> str:
    """Title-case a name, street address, city or county.

    Unlike str.title() this keeps "5th" as "5th" (not "5Th"), leaves compass
    points and "PO" upper case, handles O'Brien / McDonald / Smith-Jones, and
    leaves words that already have mixed capitals alone ("DeLuca").
    Whitespace between words is preserved.
    """
    if not text:
        return text or ""
    return "".join(
        part if part.isspace() or not part else _title_word(part)
        for part in re.split(r"(\s+)", text)
    )


def base_callsign(callsign: str) -> str:
    """Return the base callsign: everything before the first '/', uppercased.

    The callsign always comes first; what follows the slash is a condition
    (/P portable, /M mobile, /MM maritime mobile, ...). N0DDK/P -> N0DDK.
    """
    if not callsign:
        return ""
    return callsign.split("/", 1)[0].strip().upper()
