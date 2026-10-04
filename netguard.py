# Copyright (c) 2025, 2026 Manuel Ochoa
# This file is part of CommStat.
# Licensed under the GNU General Public License v3.0.
"""
netguard.py - Global Off-Grid / Online mode switch.

A single process-wide flag that every outbound network call in the app
should check before doing anything (DNS/connectivity probes, the commsrvr
heartbeat + auto-update, RSS news fetch, map tiles, QRZ lookups, and the
"INTERNET ONLY" rig option in Alert / Group Message / StatRep).

Two things feed the effective online/off-grid state:
  - the user's preference (did they click the header switch to force
    Off-Grid?), persisted to config.ini under [DIRECTEDCONFIG]
    network_enabled so it survives a restart.
  - live reachability, set by check_internet()'s real DNS probes, not
    persisted — it's only ever "what did the last probe find."

is_network_enabled()/guard() reflect the effective state: online only when
the user hasn't forced Off-Grid AND the last probe found real connectivity.
That way the header switch (and every gated network call) auto-reflects
losing/regaining real internet, without needing a click, while a user who
manually forces Off-Grid stays Off-Grid even if internet is present.

Widgets that need to reflect the effective state (e.g. the header toggle
switch) can register a listener via add_listener() and will be called back
with the new bool whenever it changes, from anywhere in the app.

Default is online/reachable so existing installs behave exactly as before
until someone flips the switch or their connection drops.
"""

import threading
from pathlib import Path
from typing import Callable, List

from config_utils import read_config, write_config
from constants import CONFIG_FILE

_SECTION = "DIRECTEDCONFIG"
_KEY = "network_enabled"

_lock = threading.Lock()
_listeners: List[Callable[[bool], None]] = []


def _config_path() -> Path:
    return Path(CONFIG_FILE)


def _read_from_disk() -> bool:
    """Saved preference; Online when the file, section or value is missing or
    unreadable. This runs at import, so it must never raise: a damaged
    config.ini would otherwise stop the whole app from starting."""
    try:
        config = read_config(_config_path())
        if config.has_section(_SECTION):
            return config.getboolean(_SECTION, _KEY, fallback=True)
    except ValueError as e:
        print(f"[netguard] Could not read {_KEY} from {_config_path()}: {e}. Assuming Online.")
    return True


def _write_to_disk(value: bool) -> bool:
    """Persist the preference. Returns False (the reason is printed) instead of
    raising, so a read-only or full disk can't crash a Qt slot."""
    config = read_config(_config_path())
    if not config.has_section(_SECTION):
        config.add_section(_SECTION)
    config.set(_SECTION, _KEY, str(value))
    return write_config(config, _config_path())


_user_enabled: bool = _read_from_disk()
_reachable: bool = True


def get_user_enabled() -> bool:
    """Raw user preference: False only once they've manually forced Off-Grid Mode."""
    with _lock:
        return _user_enabled


def is_reachable() -> bool:
    """Last-known real internet reachability, as probed by check_internet()."""
    with _lock:
        return _reachable


def is_network_enabled() -> bool:
    """Effective state: True only if the user hasn't forced Off-Grid AND
    real internet was reachable on the last probe. False = Off-Grid mode."""
    with _lock:
        return _user_enabled and _reachable


def _notify_if_changed(changed: bool) -> None:
    if not changed:
        return
    value = is_network_enabled()
    for callback in list(_listeners):
        try:
            callback(value)
        except Exception as e:
            print(f"[netguard] listener error: {e}")


def set_user_enabled(value: bool) -> None:
    """User clicked the Online/Off-Grid switch: set intent, persist it, and
    notify listeners if the effective state actually changed as a result."""
    global _user_enabled
    with _lock:
        before = _user_enabled and _reachable
        _user_enabled = value
        changed = before != (_user_enabled and _reachable)
    _write_to_disk(value)   # file I/O outside the lock
    _notify_if_changed(changed)


def set_reachable(value: bool) -> None:
    """Update live reachability from a real check_internet() probe (not
    persisted), and notify listeners if the effective state changed."""
    global _reachable
    with _lock:
        before = _user_enabled and _reachable
        _reachable = value
        changed = before != (_user_enabled and _reachable)
    _notify_if_changed(changed)


def add_listener(callback: Callable[[bool], None]) -> None:
    """Register callback(is_enabled: bool), invoked whenever the effective state changes."""
    _listeners.append(callback)


def remove_listener(callback: Callable[[bool], None]) -> None:
    if callback in _listeners:
        _listeners.remove(callback)


def guard(feature_name: str = "This feature") -> bool:
    """
    Convenience check for call sites that need a friendly print/log message.
    Returns True if network calls are permitted; False (and logs) if not.
    """
    if is_network_enabled():
        return True
    print(f"[Off-Grid Mode] {feature_name} skipped — network access is disabled.")
    return False
