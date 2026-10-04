# Copyright (c) 2026 Manuel Ochoa
# This file is part of CommStat.
# Licensed under the GNU General Public License v3.0.
"""
config_utils.py - Safe reading and writing of config.ini.

Every module that touches config.ini goes through these two functions:

  read_config()   never raises: a missing, unreadable or damaged file gives an
                  empty parser (callers fall back to their defaults), and "%"
                  in a value is just text (no interpolation).
  write_config()  writes a temporary file next to config.ini and swaps it in,
                  so a crash or a full disk cannot leave a truncated file.
"""

import configparser
import os
import shutil
import tempfile
from pathlib import Path


def new_parser() -> configparser.ConfigParser:
    """A parser that treats "%" as plain text and tolerates duplicate keys (last one wins)."""
    return configparser.ConfigParser(interpolation=None, strict=False)


def read_config(path) -> configparser.ConfigParser:
    """Parse config.ini. Never raises.

    A file that cannot be parsed is copied once to "<name>.bad" (so nothing the
    user wrote is lost when the settings are saved again) and an empty parser
    is returned."""
    path = Path(path)
    config = new_parser()
    try:
        config.read(path)
    except (configparser.Error, OSError, UnicodeDecodeError) as e:
        print(f"[config] Could not read {path}: {e}. Using defaults.")
        backup = path.with_name(path.name + ".bad")
        try:
            if path.exists() and not backup.exists():
                shutil.copy2(path, backup)
                print(f"[config] A copy of the unreadable file was kept as {backup.name}.")
        except OSError:
            pass
        return new_parser()
    return config


def write_config(config: configparser.ConfigParser, path) -> bool:
    """Atomically write config to path. Returns False (after printing why) instead of raising."""
    path = Path(path)
    try:
        fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                config.write(f)
                f.flush()
                os.fsync(f.fileno())
            if path.exists():
                shutil.copymode(path, tmp_path)   # keep the file's permissions
            os.replace(tmp_path, path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return True
    except OSError as e:
        print(f"[config] Could not save {path}: {e}")
        return False
