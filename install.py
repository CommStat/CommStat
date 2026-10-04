# Copyright (c) 2025 Manuel Ochoa
# This file is part of CommStat.
# Licensed under the GNU General Public License v3.0.

#!/usr/bin/env python3
import subprocess
import sys
import platform

import commstat   # the launcher: owns the database setup and the install folder paths

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


def setup_files():
    """Create the database from the template (in the CommStat folder) if missing."""
    try:
        commstat.setup_database()
    except OSError as e:
        print(f"Error: {e}")
        sys.exit(1)


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


def in_virtualenv() -> bool:
    """True inside a venv/virtualenv, where pip installs into the environment
    and "--user" is refused."""
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


def install_commands(package):
    """Candidate pip commands for this package, to try in order."""
    # Some packages must be refreshed even when an older version is already
    # present. certifi ships the CA trust bundle: a stale bundle causes TLS
    # "certificate has expired" failures against renewed Let's Encrypt certs,
    # so always pull the newest one (plain `pip install` would skip it as
    # "already satisfied").
    pkg_name = package.split("==")[0].split(">=")[0].split("<")[0].strip().lower()
    upgrade = ["--upgrade"] if pkg_name in ("certifi",) else []
    pip = [sys.executable, "-m", "pip", "install"]

    is_unix = sys.platform == 'darwin' or sys.platform.startswith('linux')
    if in_virtualenv() or not is_unix:
        # Windows, or a virtualenv: install into the active environment
        return [pip + upgrade + [package]]

    attempts = []
    if pip_supports_break_system_packages():
        # Preferred: user install with break-system-packages (needed on Ubuntu 24.04+ / Mint 22+)
        attempts.append(pip + ["--user", "--break-system-packages", *upgrade, package])
    # Fallback: user install without break-system-packages (older distros)
    attempts.append(pip + ["--user", *upgrade, package])
    return attempts


def install(package, optional=False):
    print(f"  Installing {package}...")

    failures = []   # (command, pip's error output) for each failed attempt
    for cmd in install_commands(package):
        result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if result.returncode == 0:
            print(f"  OK: {package}")
            return
        failures.append((cmd, (result.stderr or "").strip()))

    # All attempts failed: show pip's own explanation, not just "could not install"
    label = "WARNING" if optional else "ERROR"
    print(f"\n{label}: Could not install '{package}'.")
    for cmd, error_text in failures:
        print("  Tried: " + " ".join(cmd[2:]))
        for line in error_text.splitlines()[-8:]:
            print("    " + line)
    if optional:
        print(f"  {package} is optional; CommStat will run without it. Continuing.")
        return
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


# Packages every platform needs.
COMMON_PACKAGES = [
    "branca>=0.6.0",
    "folium",
    # numpy>=2.4 wheels default to an x86-64-v2 CPU baseline and crash on
    # older/virtualized CPUs (RuntimeError: "doesn't support: (X86_V2)").
    # Pin below that until numpy restores a broader default baseline.
    "numpy<2.4",
    "pandas",
    "maidenhead",
    "certifi",
]

# The Qt packages come from pip on Windows and macOS. On Linux/Raspberry Pi they are
# left to the distribution's own packages (python3-pyqt5, python3-pyqt5.qtwebengine),
# because pip has no PyQt5 wheels for ARM and the distro build matches the system Qt.
QT_PACKAGES = ["PyQt5", "PyQtWebEngine"]

# Nice to have: CommStat starts without them (the import is guarded), so a failed
# install is reported but does not stop the installation.
OPTIONAL_PACKAGES = ["pyenchant"]


def install_all(platform_name):
    """Install the dependencies for this platform, then set up the database."""
    packages = list(COMMON_PACKAGES)
    if platform_name != "Linux":
        packages = QT_PACKAGES + packages
    print(f"\nInstalling {len(packages) + len(OPTIONAL_PACKAGES)} packages...")
    for package in packages:
        install(package)
    for package in OPTIONAL_PACKAGES:
        install(package, optional=True)
    runsettings()


def lininstall():
    """Install dependencies for Linux/Pi systems."""
    install_all("Linux")


def macinstall():
    """Install dependencies for macOS systems."""
    install_all("macOS")


def wininstall():
    """Install dependencies for Windows systems."""
    install_all("Windows")


if __name__ == "__main__":
    oscheck()
