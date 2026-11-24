"""Global hotkeys on macOS, through a listen-only keyboard event tap.

Blurt stores hotkeys as "mods+code" with Linux (evdev) key codes. Mac key codes are
translated to the same numbers, so a library moved between Linux and a Mac keeps its hotkeys
and every key name Blurt shows stays the same.
"""
import ctypes
import threading

from PySide6 import QtCore

from . import audio

# macOS virtual key code (kVK_*) -> Linux evdev key code
MAC_TO_EVDEV = {
    0x00: 30, 0x01: 31, 0x02: 32, 0x03: 33, 0x04: 35, 0x05: 34, 0x06: 44, 0x07: 45,  # A S D F H G Z X
    0x08: 46, 0x09: 47, 0x0A: 86, 0x0B: 48, 0x0C: 16, 0x0D: 17, 0x0E: 18, 0x0F: 19,  # C V § B Q W E R
    0x10: 21, 0x11: 20, 0x12: 2, 0x13: 3, 0x14: 4, 0x15: 5, 0x16: 7, 0x17: 6,        # Y T 1 2 3 4 6 5
    0x18: 13, 0x19: 10, 0x1A: 8, 0x1B: 12, 0x1C: 9, 0x1D: 11, 0x1E: 27, 0x1F: 24,    # = 9 7 - 8 0 ] O
    0x20: 22, 0x21: 26, 0x22: 23, 0x23: 25, 0x24: 28, 0x25: 38, 0x26: 36, 0x27: 40,  # U [ I P ⏎ L J '
    0x28: 37, 0x29: 39, 0x2A: 43, 0x2B: 51, 0x2C: 53, 0x2D: 49, 0x2E: 50, 0x2F: 52,  # K ; \ , / N M .
    0x30: 15, 0x31: 57, 0x32: 41, 0x33: 14, 0x35: 1,                                 # Tab Space ` ⌫ Esc
    0x36: 126, 0x37: 125, 0x38: 42, 0x39: 58, 0x3A: 56, 0x3B: 29,                    # ⌘R ⌘ ⇧ Caps ⌥ ⌃
    0x3C: 54, 0x3D: 100, 0x3E: 97,                                                   # ⇧R ⌥R ⌃R
    0x40: 187, 0x41: 83, 0x43: 55, 0x45: 78, 0x47: 69, 0x48: 115, 0x49: 114,         # F17 Num. Num* Num+ Clear Vol+ Vol-
    0x4A: 113, 0x4B: 98, 0x4C: 96, 0x4E: 74, 0x4F: 188, 0x50: 189, 0x51: 117,        # Mute Num/ NumEnter Num- F18 F19 Num=
    0x52: 82, 0x53: 79, 0x54: 80, 0x55: 81, 0x56: 75, 0x57: 76, 0x58: 77, 0x59: 71,  # Num0-7
    0x5A: 190, 0x5B: 72, 0x5C: 73, 0x5D: 124, 0x5E: 89, 0x5F: 121,                   # F20 Num8 Num9 ¥ _ Num,
    0x60: 63, 0x61: 64, 0x62: 65, 0x63: 61, 0x64: 66, 0x65: 67, 0x66: 94, 0x67: 87,  # F5 F6 F7 F3 F8 F9 英数 F11
    0x68: 92, 0x69: 183, 0x6A: 186, 0x6B: 184, 0x6D: 68, 0x6F: 88, 0x71: 185,        # かな F13 F16 F14 F10 F12 F15
    0x72: 110, 0x73: 102, 0x74: 104, 0x75: 111, 0x76: 62, 0x77: 107, 0x78: 60,       # Help Home PgUp ⌦ F4 End F2
    0x79: 109, 0x7A: 59, 0x7B: 105, 0x7C: 106, 0x7D: 108, 0x7E: 103,                 # PgDn F1 ← → ↓ ↑
}
UNMAPPED_BASE = 1000  # a Mac key with no Linux equivalent still gets a stable code

# CGEventFlags
_SHIFT, _CONTROL, _OPTION, _COMMAND = 0x20000, 0x40000, 0x80000, 0x100000

# What Blurt calls each modifier, as Mac users know them
MOD_LABELS = {"ctrl": "Ctrl", "shift": "Shift", "alt": "Option", "meta": "Cmd"}


def evdev_code(mac_code):
    return MAC_TO_EVDEV.get(mac_code, UNMAPPED_BASE + mac_code)


def mods_from_flags(flags):
    mods = []
    if flags & _CONTROL:
        mods.append("ctrl")
    if flags & _SHIFT:
        mods.append("shift")
    if flags & _OPTION:
        mods.append("alt")
    if flags & _COMMAND:
        mods.append("meta")
    return mods


def has_permission(prompt=False):
    """True when Blurt may watch the keyboard (Input Monitoring). prompt=True makes macOS
    ask the user (and list Blurt in System Settings)."""
    return bool(audio.lib.ba_keys_access(1 if prompt else 0))


class GlobalKeys(QtCore.QObject):
    """System-wide hotkeys on macOS. Same interface as the Linux GlobalKeys: `pressed`
    carries a combo like "ctrl+shift+59", `available` says whether it works. Keys are only
    observed, never captured, so they still reach the focused app."""
    pressed = QtCore.Signal(str)
    ready = QtCore.Signal()  # the permission was granted while Blurt was running

    def __init__(self, ignore=()):
        super().__init__()
        self.ignore = set(ignore)  # evdev codes that are modifiers on their own
        self.available = False
        if not self._start(prompt=True):
            # Granting the permission does not need a restart: keep checking for it.
            self._timer = QtCore.QTimer(self)
            self._timer.timeout.connect(self._retry)
            self._timer.start(2000)

    def _start(self, prompt=False):
        if has_permission(prompt=prompt) and audio.lib.ba_keys_start():
            self.available = True
            threading.Thread(target=self._run, daemon=True).start()
        return self.available

    def _retry(self):
        if self._start():
            self._timer.stop()
            self.ready.emit()

    def _run(self):
        code, flags = ctypes.c_uint32(), ctypes.c_uint64()
        while True:
            if not audio.lib.ba_keys_wait(ctypes.byref(code), ctypes.byref(flags), 1.0):
                continue
            evdev = evdev_code(code.value)
            if evdev in self.ignore:
                continue
            self.pressed.emit("+".join(sorted(set(mods_from_flags(flags.value))) + [str(evdev)]))
