#!/usr/bin/env python3
# Copyright (c) 2025 Manuel Ochoa
# This file is part of CommStat.
# Licensed under the GNU General Public License v3.0.
"""
commstat.py - CommStat Launcher

Checks for pending updates before launching the main application.
If an update zip file is present, extracts it to overwrite program files.
"""

import os
import sys
import zipfile
import subprocess
import shutil
from pathlib import Path

# Constants
SCRIPT_DIR = Path(__file__).parent.resolve()
UPDATE_FOLDER = SCRIPT_DIR / "updates"
UPDATE_ZIP = UPDATE_FOLDER / "update.zip"
MAIN_APP = SCRIPT_DIR / "little_gucci.py"
DATABASE_FILE = SCRIPT_DIR / "traffic.db3"
DATABASE_TEMPLATE = SCRIPT_DIR / "traffic.db3.template"

# User data that an update must never overwrite (compared case-insensitively,
# relative to the install folder). The database's -journal/-wal/-shm files too.
PROTECTED_FILES = {"traffic.db3", "config.ini"}


def is_protected(member_name: str) -> bool:
    """True if a zip member would overwrite the user's database or settings."""
    parts = [p for p in member_name.replace("\\", "/").split("/") if p not in ("", ".")]
    if len(parts) != 1:
        return False   # only files directly in the install folder
    name = parts[0].lower()
    return name in PROTECTED_FILES or name.startswith("traffic.db3-")


def apply_update() -> bool:
    """
    Check for and apply pending update.

    Files that hold the user's data (traffic.db3, config.ini) are skipped even
    if the zip contains them.

    Returns:
        True if update was applied, False otherwise.
    """
    if not UPDATE_ZIP.exists():
        return False

    print("Update found. Applying...")

    try:
        with zipfile.ZipFile(UPDATE_ZIP, 'r') as zf:
            members = [m for m in zf.infolist() if not is_protected(m.filename)]
            for m in zf.infolist():
                if is_protected(m.filename):
                    print(f"Skipping {m.filename}: user data is never overwritten by an update")
            print(f"Updating {len(members)} files...")
            zf.extractall(SCRIPT_DIR, members=members)

        UPDATE_ZIP.unlink()
        print("Update applied successfully.")

        if UPDATE_FOLDER.exists() and not any(UPDATE_FOLDER.iterdir()):
            UPDATE_FOLDER.rmdir()

        return True

    except zipfile.BadZipFile:
        print(f"Error: {UPDATE_ZIP} is not a valid zip file.")
        bad_zip = UPDATE_FOLDER / "update_bad.zip"
        UPDATE_ZIP.rename(bad_zip)
        return False

    except PermissionError as e:
        print(f"Error: Permission denied - {e}")
        return False

    except Exception as e:
        print(f"Error applying update: {e}")
        return False


def setup_database() -> bool:
    """
    Ensure the database file exists, copying from template if needed.

    Returns:
        True if database was created from template, False if it already existed.

    Raises:
        OSError: the copy was incomplete (for example, the disk is full).
    """
    if DATABASE_FILE.exists():
        return False

    if DATABASE_TEMPLATE.exists():
        shutil.copy(DATABASE_TEMPLATE, DATABASE_FILE)
        src_size = DATABASE_TEMPLATE.stat().st_size
        dst_size = DATABASE_FILE.stat().st_size
        if dst_size != src_size:
            DATABASE_FILE.unlink()
            raise OSError(f"{DATABASE_FILE.name} copy was incomplete ({dst_size} of {src_size} bytes). "
                          "Check available disk space and try again.")
        print(f"Created {DATABASE_FILE.name} from template")
        return True
    else:
        print(f"Warning: {DATABASE_TEMPLATE.name} not found, cannot create {DATABASE_FILE.name}")
        return False


def launch_main_app() -> int:
    """Launch the main CommStat application and return its exit code."""
    if not MAIN_APP.exists():
        print(f"Error: {MAIN_APP} not found.")
        sys.exit(1)

    # Fix Linux menu bar issues by disabling global menu integration
    env = os.environ.copy()
    if sys.platform.startswith('linux'):
        env['QT_QPA_PLATFORMTHEME'] = ''  # Disable platform theme that steals menu bar

    python = sys.executable
    args = [python, str(MAIN_APP)] + sys.argv[1:]  # Pass through any command line args
    return subprocess.run(args, cwd=str(SCRIPT_DIR), env=env).returncode


def main() -> None:
    """Main entry point."""
    if not UPDATE_FOLDER.exists():
        UPDATE_FOLDER.mkdir(parents=True, exist_ok=True)

    apply_update()
    try:
        setup_database()
    except OSError as e:
        print(f"Error: {e}")
        sys.exit(1)
    sys.exit(launch_main_app())


if __name__ == "__main__":
    main()
