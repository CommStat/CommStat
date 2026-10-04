# Copyright (c) 2026 Manuel Ochoa
# This file is part of CommStat.
# Licensed under the GNU General Public License v3.0.
"""
id_utils.py - Time-based message IDs.

IDs are an hour letter plus the minute, so they repeat every day and two items
created in the same UTC minute share one. They are only unique together with
the rest of the record (date, sender, ...).
"""

from datetime import datetime, timezone
from typing import Optional, Tuple

# Hour-to-letter mapping (24 letters for 24 hours, skip O)
HOUR_LETTERS = "ABCDEFGHIJKLMNPQRSTUVWXY"

def generate_time_based_id(dt: Optional[datetime] = None) -> str:
    """
    Generate a 3-character time-based ID from UTC time.

    Format: [Hour Letter A-Y][Minute 00-59]

    Args:
        dt: datetime object (UTC). If None, uses current UTC time.

    Returns:
        3-character ID string (e.g., "A12", "Q47", "Y59")
    """
    if dt is None:
        dt = datetime.now(timezone.utc)

    # Ensure we're working with UTC
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)

    hour = dt.hour  # 0-23
    minute = dt.minute  # 0-59

    # Generate ID: letter + two-digit minute
    letter = HOUR_LETTERS[hour]
    return f"{letter}{minute:02d}"


def parse_message_datetime(utc: str) -> Tuple[str, str]:
    """
    Parse UTC timestamp and generate time-based ID.

    Args:
        utc: UTC timestamp string ("YYYY-MM-DD HH:MM:SS" or with extra spaces)

    Returns:
        (date_only_str, time_based_id)
    """
    dt_str = utc.replace("   ", " ").strip()
    dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    date_only = utc.split()[0] if utc else ""
    return (date_only, generate_time_based_id(dt))
