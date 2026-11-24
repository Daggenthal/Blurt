"""Installs Blurt's virtual microphone driver (Blurt.driver) on macOS.

Core Audio loads drivers from /Library/Audio/Plug-Ins/HAL, which needs an administrator
password once. After installing, the audio system is restarted so Blurt-Mic appears.
"""
import plistlib
import subprocess
import time
from pathlib import Path

from . import audio

INSTALLED = Path("/Library/Audio/Plug-Ins/HAL/Blurt.driver")
_HERE = Path(__file__).resolve().parent
_CANDIDATES = (_HERE.parent.parent / "Blurt.driver",                 # Blurt.app/Contents/Resources
               _HERE.parent / "macos" / "build" / "Blurt.driver")    # running from source


def bundled():
    return next((p for p in _CANDIDATES if (p / "Contents" / "Info.plist").is_file()), None)


def version(path):
    try:
        with open(Path(path) / "Contents" / "Info.plist", "rb") as f:
            return plistlib.load(f).get("CFBundleVersion", "")
    except (OSError, ValueError, plistlib.InvalidFileException):
        return ""


def devices_present():
    uids = {d["uid"] for d in audio.devices()["devices"]}
    return "blurt_mix" in uids and "blurt_mic" in uids


def status():
    """'ok', 'missing' (not installed), 'outdated' (an older version is installed) or
    'unavailable' (not installed and this copy of Blurt has no driver to install)."""
    src = bundled()
    if not INSTALLED.exists():
        return "missing" if src else "unavailable"
    if src and version(src) and version(INSTALLED) != version(src):
        return "outdated"
    return "ok"


def _admin(script):
    """Runs a shell script as root after macOS asks for an administrator password.
    Returns (ok, message)."""
    quoted = script.replace("\\", "\\\\").replace('"', '\\"')
    r = subprocess.run(["osascript", "-e",
                        f'do shell script "{quoted}" with administrator privileges '
                        f'with prompt "Blurt needs to install its virtual microphone (Blurt-Mic)."'],
                       capture_output=True, text=True)
    if r.returncode != 0:
        err = (r.stderr or "").strip()
        if "-128" in err:  # the user pressed Cancel
            return False, "Cancelled."
        return False, err or "The installation failed."
    return True, ""


def _sh(path):
    return "'" + str(path).replace("'", "'\\''") + "'"


def install(wait=15.0):
    """Copies the driver into place and restarts Core Audio. Returns (ok, message)."""
    src = bundled()
    if src is None:
        return False, "This copy of Blurt does not include the Blurt-Mic driver."
    ok, msg = _admin(f"rm -rf {_sh(INSTALLED)} && mkdir -p {_sh(INSTALLED.parent)} && "
                     f"cp -R {_sh(src)} {_sh(INSTALLED)} && chown -R root:wheel {_sh(INSTALLED)} && "
                     f"killall coreaudiod")
    if not ok:
        return False, msg
    end = time.monotonic() + wait
    while time.monotonic() < end:  # Core Audio restarts by itself, then loads the driver
        time.sleep(0.5)
        try:
            if devices_present():
                return True, ""
        except Exception:
            pass
    return False, ("The driver was installed, but Blurt-Mic has not appeared yet. Restarting "
                   "the Mac should fix this.")


def uninstall():
    return _admin(f"rm -rf {_sh(INSTALLED)} && killall coreaudiod")
