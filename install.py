# Copyright (c) 2025 Manuel Ochoa
# This file is part of CommStat.
# Licensed under the GNU General Public License v3.0.

#!/usr/bin/env python3
import subprocess
import sys
import os
import platform
import shutil

DATABASE_FILE = "traffic.db3"
DATABASE_TEMPLATE = "traffic.db3.template"

pyver = ""
osver = ""


def oscheck():
    global osver

    if sys.platform == 'win32':
        print("Detected: Windows")
        osver = "Windows"
        test_python()
    elif sys.platform == 'darwin':
        print("Detected: macOS")
        osver = "macOS"
        test_python()
    elif sys.platform.startswith('linux'):
        # Check for Raspberry Pi
        if "aarch64" in platform.platform():
            print("Detected: Raspberry Pi 64-bit")
        else:
            print("Detected: Linux")
        osver = "Linux"
        test_python()
    else:
        print("CommStat does not recognize this operating system and cannot proceed.")
        print(f"Platform detected: {sys.platform}")
        return


def create_from_template(target: str, template: str) -> None:
    """Create a file from template if it doesn't exist."""
    if not os.path.exists(target):
        if os.path.exists(template):
            shutil.copy(template, target)
            src_size = os.path.getsize(template)
            dst_size = os.path.getsize(target)
            if dst_size != src_size:
                os.remove(target)
                print(f"Error: {target} copy was incomplete ({dst_size} of {src_size} bytes). "
                      f"Check available disk space and try again.")
                sys.exit(1)
            print(f"Created {target} from template")
        else:
            print(f"Warning: {template} not found, cannot create {target}")


def setup_files():
    """Create database from template if missing."""
    create_from_template(DATABASE_FILE, DATABASE_TEMPLATE)


def runsettings():
    setup_files()
    print("\nInstallation complete. Run 'python commstat.py' to start the program.")


def pip_supports_break_system_packages():
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "--version"],
            capture_output=True, text=True
        )
        version_str = result.stdout.split()[1]
        parts = version_str.split('.')
        major, minor = int(parts[0]), int(parts[1])
        return (major, minor) >= (22, 1)
    except Exception:
        return False


def install(package):
    print(f"  Installing {package}...")
    is_unix = sys.platform == 'darwin' or sys.platform.startswith('linux')

    # Some packages must be refreshed even when an older version is already
    # present. certifi ships the CA trust bundle: a stale bundle causes TLS
    # "certificate has expired" failures against renewed Let's Encrypt certs,
    # so always pull the newest one (plain `pip install` would skip it as
    # "already satisfied").
    pkg_name = package.split("==")[0].split(">=")[0].split("<")[0].strip().lower()
    upgrade = ["--upgrade"] if pkg_name in ("certifi",) else []

    # Build candidate command lists to try in order
    attempts = []
    if is_unix:
        if pip_supports_break_system_packages():
            # Preferred: user install with break-system-packages (needed on Ubuntu 24.04+ / Mint 22+)
            attempts.append([sys.executable, "-m", "pip", "install", "--user", "--break-system-packages", *upgrade, package])
        # Fallback: user install without break-system-packages (older distros)
        attempts.append([sys.executable, "-m", "pip", "install", "--user", *upgrade, package])
    else:
        attempts.append([sys.executable, "-m", "pip", "install", *upgrade, package])

    last_error = None
    for cmd in attempts:
        try:
            subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            print(f"  OK: {package}")
            return
        except subprocess.CalledProcessError as e:
            last_error = e

    # All attempts failed
    print(f"\nERROR: Could not install '{package}'.")
    print("  This may be due to a network issue or a restricted Python environment.")
    print("  Try manually: pip3 install " + package + " --user --break-system-packages")
    print("  For help, join the community support channel: https://t.me/+3k3n7O8a1yI1N2E5")
    sys.exit(1)


def test_python():
    global osver
    try:
        if int(sys.version_info[0]) < 3:
            print("You are using Python " + str(sys.version_info[0]))
            print("CommStat requires Python 3.8 or newer, install cannot continue")
            sys.exit(1)

        if int(sys.version_info[1]) < 8:
            print("You are using Python 3." + str(sys.version_info[1]))
            print("CommStat requires Python 3.8 or newer")
            sys.exit(1)
        else:
            print("Appropriate version of Python found: Python 3." + str(
                sys.version_info[1]) + ", continuing installation")

    except Exception:
        print("Exception while testing Python version, cannot continue installation")
        sys.exit(1)
    if osver == "Windows":
        print("Installing for Windows 10 or 11")
        wininstall()
    elif osver == "macOS":
        print("Installing for macOS")
        macinstall()
    elif osver == "Linux":
        print("Installing for Linux")
        lininstall()
    else:
        print("System not recognized")


def lininstall():
    """Install dependencies for Linux/Pi systems."""
    packages = [
        "branca>=0.6.0",
        "folium",
        # numpy>=2.4 wheels default to an x86-64-v2 CPU baseline and crash on
        # older/virtualized CPUs (RuntimeError: "doesn't support: (X86_V2)").
        # Pin below that until numpy restores a broader default baseline.
        "numpy<2.4",
        "pandas",
        "maidenhead",
        "pyenchant",
        "certifi",
    ]
    print(f"\nInstalling {len(packages)} packages...")
    for package in packages:
        install(package)
    runsettings()


def macinstall():
    """Install dependencies for macOS systems."""
    packages = [
        "PyQt5",
        "PyQtWebEngine",
        "branca>=0.6.0",
        "folium",
        "numpy<2.4",
        "pandas",
        "maidenhead",
        "pyenchant",
        "certifi",
    ]
    print(f"\nInstalling {len(packages)} packages...")
    for package in packages:
        install(package)
    runsettings()


def wininstall():
    """Install dependencies for Windows systems."""
    packages = [
        "pyqt5",
        "PyQtWebEngine",
        "branca>=0.6.0",
        "folium",
        "numpy<2.4",
        "pandas",
        "maidenhead",
        "pyenchant",
        "certifi",
    ]
    print(f"\nInstalling {len(packages)} packages...")
    for package in packages:
        install(package)
    runsettings()


oscheck()
