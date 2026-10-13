#!/usr/bin/env python3
"""Blurt - a soundboard for Linux that plays audio files through your microphone.

How it works (PipeWire / PulseAudio, via pactl):
  * a virtual sink  "Blurt-Mix"  receives your real mic + the sounds you play
  * a virtual mic   "Blurt-Mic"  exposes that mix; pick it in Discord / games
  * every sound is also played to your chosen output so you can hear it
"""
import atexit
import fcntl
import glob
import json
import os
import random
import re
import selectors
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
import uuid
import wave
import zipfile
from array import array
from collections import deque
from pathlib import Path

os.environ.setdefault("QT_AUDIO_BACKEND", "pulseaudio")  # served by pipewire-pulse too

try:
    import numpy as np  # only the editor's effects need it
except ImportError:
    np = None

try:
    from blurt_voice.dsp import B1Droid  # live voice effects (numpy only)
except ImportError:
    B1Droid = None

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt
from PySide6.QtMultimedia import (QAudioDecoder, QAudioFormat, QAudioOutput, QMediaDevices,
                                   QMediaPlayer)

APP = "Blurt"
MIX_SINK, MIX_DESC = "blurt_mix", "Blurt-Mix"
MIC_SOURCE, MIC_DESC = "blurt_mic", "Blurt-Mic"
CAP_SINK, CAP_DESC = "blurt_capture", "Blurt-Clip-Buffer"
AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".oga", ".opus", ".flac", ".m4a", ".aac",
              ".wma", ".webm", ".mka", ".aiff", ".aif"}

DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "blurt"
SOUNDS_DIR = DATA_DIR / "sounds"
LIBRARY_FILE = DATA_DIR / "library.json"
CLIPS_DIR = DATA_DIR / "clips"  # scratch space for the most recent clip

# --------------------------------------------------------------------------
# Hotkeys. A combo is stored as "alt+ctrl+59": sorted modifiers + evdev code.
# --------------------------------------------------------------------------
MOD_CODES = {29: "ctrl", 97: "ctrl", 42: "shift", 54: "shift",
             56: "alt", 100: "alt", 125: "meta", 126: "meta"}
KEY_NAMES = {1: "Esc", 12: "-", 13: "=", 14: "Backspace", 15: "Tab", 26: "[", 27: "]",
             28: "Enter", 39: ";", 40: "'", 41: "`", 43: "\\", 51: ",", 52: ".", 53: "/",
             55: "Num *", 57: "Space", 58: "CapsLock", 69: "NumLock", 70: "ScrollLock",
             74: "Num -", 78: "Num +", 83: "Num .", 87: "F11", 88: "F12", 96: "Num Enter",
             98: "Num /", 99: "PrtSc", 102: "Home", 103: "Up", 104: "PgUp", 105: "Left",
             106: "Right", 107: "End", 108: "Down", 109: "PgDn", 110: "Insert",
             111: "Delete", 119: "Pause", 71: "Num 7", 72: "Num 8", 73: "Num 9",
             75: "Num 4", 76: "Num 5", 77: "Num 6", 79: "Num 1", 80: "Num 2",
             81: "Num 3", 82: "Num 0"}
for _i, _c in enumerate("1234567890"):
    KEY_NAMES[2 + _i] = _c
for _start, _row in ((16, "QWERTYUIOP"), (30, "ASDFGHJKL"), (44, "ZXCVBNM")):
    for _i, _c in enumerate(_row):
        KEY_NAMES[_start + _i] = _c
for _i in range(10):
    KEY_NAMES[59 + _i] = f"F{_i + 1}"
for _i in range(12):
    KEY_NAMES[183 + _i] = f"F{_i + 13}"


def make_combo(mods, code):
    return "+".join(sorted(set(mods)) + [str(code)])


def combo_label(combo):
    if not combo:
        return ""
    *mods, code = combo.split("+")
    order = [m for m in ("ctrl", "shift", "alt", "meta") if m in mods]
    names = {"ctrl": "Ctrl", "shift": "Shift", "alt": "Alt", "meta": "Super"}
    key = KEY_NAMES.get(int(code), f"Key {code}")
    return "+".join([names[m] for m in order] + [key])


def combo_from_qt(ev):
    """Build a combo from a Qt key event (X11/Wayland scan code = evdev + 8)."""
    if ev.key() in (Qt.Key_Control, Qt.Key_Shift, Qt.Key_Alt, Qt.Key_Meta,
                    Qt.Key_AltGr, Qt.Key_unknown):
        return None
    code = ev.nativeScanCode() - 8
    if code <= 0 or code in MOD_CODES:
        return None
    m, mods = ev.modifiers(), []
    if m & Qt.ControlModifier:
        mods.append("ctrl")
    if m & Qt.ShiftModifier:
        mods.append("shift")
    if m & Qt.AltModifier:
        mods.append("alt")
    if m & Qt.MetaModifier:
        mods.append("meta")
    return make_combo(mods, code)


class GlobalKeys(QtCore.QObject):
    """System-wide hotkeys by reading /dev/input directly (X11, Wayland, gamescope).

    Needs read access to /dev/input/event*, i.e. membership of the 'input' group.
    Keys are only observed, never grabbed, so they still reach the focused app.
    """
    pressed = QtCore.Signal(str)
    EVENT = struct.Struct("llHHi")

    def __init__(self):
        super().__init__()
        self.available = False
        self._fds = {}
        self._mods = set()
        self._sel = selectors.DefaultSelector()
        self._scan()
        self.available = bool(self._fds)
        if self.available:
            threading.Thread(target=self._run, daemon=True).start()

    @staticmethod
    def _has_keys(fd):
        buf = bytearray(32)  # capability bits for key codes 0..255
        req = (2 << 30) | (len(buf) << 16) | (ord("E") << 8) | (0x20 + 1)  # EVIOCGBIT(EV_KEY)
        try:
            fcntl.ioctl(fd, req, buf)
        except OSError:
            return False
        return any(buf)

    def _scan(self):
        for path in glob.glob("/dev/input/event*"):
            if path in self._fds:
                continue
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            except OSError:
                continue
            if not self._has_keys(fd):
                os.close(fd)
                continue
            self._fds[path] = fd
            self._sel.register(fd, selectors.EVENT_READ, path)

    def _drop(self, path):
        fd = self._fds.pop(path, None)
        if fd is not None:
            try:
                self._sel.unregister(fd)
                os.close(fd)
            except (OSError, KeyError, ValueError):
                pass

    def _run(self):
        last_scan = time.monotonic()
        while True:
            if time.monotonic() - last_scan > 3:  # pick up hot-plugged keyboards
                self._scan()
                last_scan = time.monotonic()
            if not self._fds:
                time.sleep(1)
                continue
            for key, _ in self._sel.select(timeout=1.0):
                try:
                    data = os.read(key.fd, self.EVENT.size * 64)
                except BlockingIOError:
                    continue
                except OSError:
                    self._drop(key.data)
                    continue
                for off in range(0, len(data) - self.EVENT.size + 1, self.EVENT.size):
                    _, _, etype, code, value = self.EVENT.unpack_from(data, off)
                    if etype != 1:  # EV_KEY
                        continue
                    if code in MOD_CODES:
                        (self._mods.add if value else self._mods.discard)(code)
                    elif value == 1:
                        mods = {MOD_CODES[c] for c in self._mods}
                        self.pressed.emit(make_combo(mods, code))


# --------------------------------------------------------------------------
# Audio routing (virtual mic) through pactl
# --------------------------------------------------------------------------
def pactl(*args):
    try:
        r = subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=5)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except FileNotFoundError:
        return 127, "", "pactl not found"
    except subprocess.TimeoutExpired:
        return 124, "", "pactl timed out"


class Router:
    def __init__(self):
        self.modules = []
        self.loop_module = None
        self.ok = False
        self.message = ""

    def _unload_stale(self):
        rc, out, _ = pactl("list", "short", "modules")
        if rc != 0:
            return
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3 and any(n in parts[2] for n in (MIX_SINK, MIC_SOURCE, CAP_SINK)):
                pactl("unload-module", parts[0])

    def setup(self):
        self._unload_stale()
        rc, out, err = pactl("load-module", "module-null-sink", f"sink_name={MIX_SINK}",
                             f"sink_properties=device.description={MIX_DESC}")
        if rc != 0:
            self.message = f"Could not create the virtual mic ({err or 'pactl failed'})."
            return False
        self.modules.append(out)
        rc, out, err = pactl("load-module", "module-remap-source",
                             f"master={MIX_SINK}.monitor", f"source_name={MIC_SOURCE}",
                             f"source_properties=device.description={MIC_DESC}")
        if rc == 0:
            self.modules.append(out)
            self.message = f"Select “{MIC_DESC}” as the microphone in Discord / your game."
        else:
            self.message = f"Select “Monitor of {MIX_DESC}” as the microphone in your apps."
        self.ok = True
        return True

    def set_mic(self, source_name):
        """Feed the real microphone into the mix (None = sounds only)."""
        if source_name == getattr(self, "loop_source", None) and (self.loop_module or not source_name):
            return
        self.loop_source = source_name
        if self.loop_module:
            pactl("unload-module", self.loop_module)
            self.loop_module = None
        if not (self.ok and source_name):
            return
        rc, out, _ = pactl("load-module", "module-loopback", f"source={source_name}",
                           f"sink={MIX_SINK}", "latency_msec=30")
        if rc == 0:
            self.loop_module = out

    def list_mics(self):
        mics = []
        rc, out, _ = pactl("-f", "json", "list", "sources")
        if rc == 0:
            try:
                for s in json.loads(out):
                    name = s.get("name", "")
                    props = s.get("properties", {}) or {}
                    if (name.endswith(".monitor") or name.startswith("blurt_")
                            or props.get("device.class") == "monitor"):
                        continue
                    mics.append((name, s.get("description") or name))
                return mics
            except (ValueError, AttributeError):
                pass
        rc, out, _ = pactl("list", "short", "sources")
        for line in out.splitlines() if rc == 0 else []:
            parts = line.split("\t")
            if len(parts) > 1 and not parts[1].endswith(".monitor") \
                    and not parts[1].startswith("blurt_"):
                mics.append((parts[1], parts[1]))
        return mics

    def teardown(self):
        if self.loop_module:
            pactl("unload-module", self.loop_module)
            self.loop_module = None
        for m in reversed(self.modules):
            pactl("unload-module", m)
        self.modules = []
        self.ok = False


class Clipper:
    """Keeps the last seconds of audio from a source (e.g. the monitor of your output)."""
    RATE, CHANNELS, MAX_SECONDS = 48000, 2, 30
    FRAME = 2 * CHANNELS

    def __init__(self):
        self.proc = None
        self.source = None
        self.chunks = deque()
        self.total = 0
        self.lock = threading.Lock()
        self.available = shutil.which("parec") is not None

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, source):
        if source == self.source and self.running():
            return
        self.stop()
        self.source = source
        if not (source and self.available):
            return
        try:
            self.proc = subprocess.Popen(
                ["parec", f"--device={source}", "--format=s16le", f"--rate={self.RATE}",
                 f"--channels={self.CHANNELS}", "--latency-msec=20", "--raw",
                 "--client-name=Blurt clip buffer"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError:
            self.proc = None
            return
        threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()

    def _read(self, proc):
        fd = proc.stdout.fileno()
        while True:
            try:
                data = os.read(fd, 65536)
            except OSError:
                break
            if not data:
                break
            now = time.monotonic()
            with self.lock:
                if proc is not self.proc:
                    break
                self.chunks.append((now, data))
                self.total += len(data)
                while self.chunks and self.chunks[0][0] < now - self.MAX_SECONDS - 1:
                    self.chunks.popleft()

    def stop(self):
        proc, self.proc = self.proc, None
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
        with self.lock:
            self.chunks.clear()
            self.total = 0

    def grab(self, seconds):
        """The most recent `seconds` of audio as raw s16le PCM (may be shorter)."""
        now = time.monotonic()
        with self.lock:
            data = b"".join(d for t, d in self.chunks if t >= now - seconds - 0.5)
            tail = self.total % self.FRAME
        if tail:
            data = data[:-tail]
        return data[-int(seconds * self.RATE) * self.FRAME:]

    def write_wav(self, pcm, path):
        with wave.open(str(path), "wb") as w:
            w.setnchannels(self.CHANNELS)
            w.setsampwidth(2)
            w.setframerate(self.RATE)
            w.writeframes(pcm)


class OutputTee:
    """Collects what plays on your output *without* Blurt's own playback (PipeWire only).

    A sink's monitor contains everything played on it, including the sounds Blurt plays
    for you, so clips taken from it would contain Blurt's own audio. Instead, every other
    app's stream that is playing on the chosen output is additionally linked (a "tee",
    nothing is rerouted or delayed) into a hidden sink, and clips are taken from that.
    """

    def __init__(self):
        self.module = None
        self.target = None
        self.active = False
        self._wake = threading.Event()
        self._stop = False
        self._sub = None
        self._lock = threading.Lock()
        self.supported = bool(shutil.which("pw-dump") and shutil.which("pw-link"))
        if self.supported:
            rc, out, _ = pactl("info")
            self.supported = rc == 0 and "pipewire" in out.lower()

    def start(self):
        if not self.supported or self.active:
            return self.active
        rc, out, _ = pactl("load-module", "module-null-sink", f"sink_name={CAP_SINK}",
                           f"sink_properties=device.description={CAP_DESC}")
        if rc != 0:
            return False
        self.module = out
        self.active = True
        threading.Thread(target=self._watch, daemon=True).start()
        threading.Thread(target=self._run, daemon=True).start()
        return True

    def set_target(self, sink_name):
        self.target = sink_name
        self._wake.set()

    def stop(self):
        self._stop = True
        self._wake.set()
        if self._sub:
            self._sub.terminate()
        if self.module:
            pactl("unload-module", self.module)  # removes the tee links with it
            self.module = None
        self.active = False

    def _watch(self):
        """Wake the sync loop whenever a playback stream appears, moves or goes away."""
        try:
            self._sub = subprocess.Popen(["pactl", "subscribe"], stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, text=True)
        except OSError:
            return
        for line in self._sub.stdout:
            if "sink-input" in line or "on sink " in line or "on server" in line:
                self._wake.set()
            if self._stop:
                break

    def _run(self):
        while not self._stop:
            self._wake.wait(timeout=3)  # the timeout is a safety net for missed events
            if self._stop:
                break
            self._wake.clear()
            time.sleep(0.05)  # let PipeWire finish linking the new stream
            for _attempt in range(3):
                try:
                    if self.sync():
                        break
                except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
                    pass
                time.sleep(0.15)

    @staticmethod
    def _capture_channels(channel):
        ch = (channel or "").upper()
        if ch == "LFE":
            return ()
        if ch.endswith("L"):
            return ("FL",)
        if ch.endswith("R"):
            return ("FR",)
        return ("FL", "FR")  # mono, centre, unknown

    def sync(self):
        """Make the hidden sink's inputs mirror the target's, minus Blurt. True if settled."""
        target = self.target
        if not (self.active and target):
            return True
        with self._lock:
            out = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=5).stdout
            objs = json.loads(out)
            me = str(os.getpid())
            nodes, clients, ports, links = {}, {}, {}, []
            for o in objs:
                kind, info = o.get("type", "").rsplit(":", 1)[-1], o.get("info") or {}
                props = info.get("props") or {}
                if kind == "Node":
                    nodes[o["id"]] = props
                elif kind == "Client":
                    clients[o["id"]] = props
                elif kind == "Port":
                    ports[o["id"]] = (props.get("node.id"), info.get("direction"),
                                      props.get("audio.channel"))
                elif kind == "Link":
                    links.append((o["id"], info.get("output-node-id"), info.get("output-port-id"),
                                  info.get("input-node-id"), info.get("input-port-id")))

            def by_name(name):
                return next((i for i, p in nodes.items() if p.get("node.name") == name), None)

            def is_mine(node_id):
                p = nodes.get(node_id, {})
                c = clients.get(p.get("client.id"), {})
                return (str(p.get("application.process.id")) == me
                        or str(c.get("application.process.id")) == me
                        or str(p.get("node.name", "")).startswith("blurt_")
                        or str(p.get("application.name", "")).startswith("Blurt voice"))

            sink, cap = by_name(target), by_name(CAP_SINK)
            if sink is None or cap is None or sink == cap:
                return sink == cap
            cap_in = {ch: pid for pid, (nid, d, ch) in ports.items() if nid == cap and d == "input"}
            wanted = set()
            for _lid, out_node, out_port, in_node, in_port in links:
                if in_node != sink or out_node == cap or is_mine(out_node):
                    continue
                for ch in self._capture_channels(ports.get(in_port, (0, 0, ""))[2]):
                    if ch in cap_in:
                        wanted.add((out_port, cap_in[ch]))
            have = {(op, ip): lid for lid, _on, op, in_node, ip in links if in_node == cap}
            for pair in wanted - set(have):
                subprocess.run(["pw-link", str(pair[0]), str(pair[1])],
                               capture_output=True, timeout=5)
            for pair in set(have) - wanted:
                subprocess.run(["pw-link", "-d", str(have[pair])], capture_output=True, timeout=5)
            return wanted == set(have)


# --------------------------------------------------------------------------
# Playback: one player into the virtual mic, one into your headphones
# --------------------------------------------------------------------------
def device_id(dev):
    return dev.id().data().decode(errors="ignore")


def is_mix_device(dev):
    return MIX_SINK in device_id(dev) or MIX_DESC in dev.description()


def is_blurt_device(dev):
    """Any of Blurt's own virtual devices (never offered as an output)."""
    return "blurt_" in device_id(dev) or dev.description().startswith("Blurt-")


class Engine(QtCore.QObject):
    def __init__(self):
        super().__init__()
        self.out_mic, self.out_me = QAudioOutput(), QAudioOutput()
        self.p_mic, self.p_me = QMediaPlayer(), QMediaPlayer()
        self.p_mic.setAudioOutput(self.out_mic)
        self.p_me.setAudioOutput(self.out_me)
        self.mix_ok = False
        self.current = None

    @staticmethod
    def _gain(percent):
        return (max(0, min(100, percent)) / 100.0) ** 2  # smoother than linear

    def set_mic_volume(self, percent):
        self.out_mic.setVolume(self._gain(percent))

    def set_my_volume(self, percent):
        self.out_me.setVolume(self._gain(percent))

    def find_mix(self):
        for dev in QMediaDevices.audioOutputs():
            if is_mix_device(dev):
                self.out_mic.setDevice(dev)
                self.mix_ok = True
                return True
        self.mix_ok = False
        return False

    def set_output(self, dev):
        self.out_me.setDevice(dev)

    def play(self, sound_id, path):
        self.stop()
        url = QtCore.QUrl.fromLocalFile(str(path))
        self.current = sound_id
        self.p_me.setSource(url)
        self.p_me.play()
        if self.mix_ok:  # never fall back to the default device: you'd hear it twice
            self.p_mic.setSource(url)
            self.p_mic.play()

    def toggle_pause(self):
        if self.p_me.playbackState() == QMediaPlayer.PlayingState:
            self.p_me.pause()
            self.p_mic.pause()
        elif self.p_me.playbackState() == QMediaPlayer.PausedState:
            self.p_me.play()
            if self.mix_ok:
                self.p_mic.play()

    def stop(self):
        self.p_me.stop()
        self.p_mic.stop()

    def forget(self):
        """Stop and let go of the current file (it is about to be changed on disk)."""
        self.stop()
        self.p_me.setSource(QtCore.QUrl())
        self.p_mic.setSource(QtCore.QUrl())
        self.current = None

    def seek(self, ms):
        self.p_me.setPosition(ms)
        self.p_mic.setPosition(ms)


# --------------------------------------------------------------------------
# Library storage
# --------------------------------------------------------------------------
class Store:
    def __init__(self):
        SOUNDS_DIR.mkdir(parents=True, exist_ok=True)
        self.data = {"folders": [], "sounds": [], "settings": {}}
        if LIBRARY_FILE.exists():
            try:
                self.data.update(json.loads(LIBRARY_FILE.read_text()))
            except (ValueError, OSError):
                shutil.copy2(LIBRARY_FILE, str(LIBRARY_FILE) + ".broken")
        self.folders = self.data["folders"]
        self.sounds = self.data["sounds"]
        self.settings = self.data["settings"]
        self.settings.setdefault("hotkeys", {})
        self.sync_files()

    # Files live in real folders with readable names:  sounds/<folder>/<name>.<ext>
    @staticmethod
    def _safe(name):
        name = re.sub(r'[/\\\0<>:"|?*]', "_", name).strip().strip(".")
        return name[:120] or "sound"

    def path_of(self, s):
        return SOUNDS_DIR / s["file"]

    def folder_dir(self, fid):
        d = SOUNDS_DIR
        for part in self.folder_name(fid).split("/"):
            if part.strip():
                d = d / self._safe(part)
        return d

    def _place(self, s):
        cur = self.path_of(s)
        if not cur.is_file():
            return
        d, base, ext = self.folder_dir(s.get("folder")), self._safe(s["name"]), cur.suffix
        want, n = d / (base + ext), 2
        while want.exists() and want != cur:
            want, n = d / f"{base} ({n}){ext}", n + 1
        if want != cur:
            d.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(cur), str(want))
            except OSError:
                return
            s["file"] = str(want.relative_to(SOUNDS_DIR))

    def sync_files(self):
        """Make the files on disk match the names and folders shown in Blurt."""
        for s in self.sounds:
            self._place(s)
        for dirpath, _dirs, _files in os.walk(SOUNDS_DIR, topdown=False):
            if Path(dirpath) != SOUNDS_DIR:
                try:
                    os.rmdir(dirpath)  # only succeeds when empty
                except OSError:
                    pass

    def save(self):
        self.sync_files()
        tmp = LIBRARY_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1))
        os.replace(tmp, LIBRARY_FILE)

    def sound(self, sid):
        return next((s for s in self.sounds if s["id"] == sid), None)

    def folder_name(self, fid):
        return next((f["name"] for f in self.folders if f["id"] == fid), "")

    def folder_by_name(self, name):
        for f in self.folders:
            if f["name"] == name:
                return f["id"]
        fid = uuid.uuid4().hex[:8]
        self.folders.append({"id": fid, "name": name})
        return fid

    def import_file(self, path, folder_id):
        p = Path(path)
        if p.suffix.lower() not in AUDIO_EXTS or not p.is_file():
            return False
        src = str(p.resolve())
        if any(s.get("src") == src for s in self.sounds):
            return False  # already imported
        sid = uuid.uuid4().hex[:12]
        dest = SOUNDS_DIR / f"{sid}{p.suffix.lower()}"
        try:
            shutil.copy2(p, dest)
        except OSError:
            return False
        self.sounds.append({"id": sid, "name": p.stem, "file": dest.name, "src": src,
                            "folder": folder_id, "hotkey": ""})
        return True

    def import_dir(self, root):
        """Import a directory tree; each directory with audio becomes a folder."""
        root = os.path.abspath(root)
        count = 0
        for dirpath, dirs, files in os.walk(root):
            dirs.sort()
            audio = sorted(f for f in files if Path(f).suffix.lower() in AUDIO_EXTS)
            if not audio:
                continue
            fid = self.folder_by_name(os.path.relpath(dirpath, os.path.dirname(root)))
            count += sum(self.import_file(os.path.join(dirpath, f), fid) for f in audio)
        return count

    def remove_sound(self, sid):
        s = self.sound(sid)
        if s:
            try:
                self.path_of(s).unlink()
            except OSError:
                pass
            self.sounds.remove(s)


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------
class HotkeyDialog(QtWidgets.QDialog):
    def __init__(self, parent, title):
        super().__init__(parent)
        self.setWindowTitle("Set hotkey")
        self.combo = None
        self.cleared = False
        lay = QtWidgets.QVBoxLayout(self)
        lab = QtWidgets.QLabel(f"<b>{title}</b><br><br>Press a key or key combination…"
                               "<br><small>Esc cancels</small>")
        lab.setAlignment(Qt.AlignCenter)
        lab.setMinimumSize(320, 110)
        lay.addWidget(lab)
        clear = QtWidgets.QPushButton("Remove hotkey")
        clear.setFocusPolicy(Qt.NoFocus)
        clear.clicked.connect(self._clear)
        lay.addWidget(clear)
        self.setFocusPolicy(Qt.StrongFocus)

    def _clear(self):
        self.cleared = True
        self.accept()

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key_Escape:
            self.reject()
            return
        combo = combo_from_qt(ev)
        if combo:
            self.combo = combo
            self.accept()


# --------------------------------------------------------------------------
# Waveform editor (trim / cut)
# --------------------------------------------------------------------------
ED_RATE, ED_CH = 48000, 2


def write_wav(pcm, path):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(ED_CH)
        w.setsampwidth(2)
        w.setframerate(ED_RATE)
        w.writeframes(pcm)


def fmt_time(frames):
    s = frames / ED_RATE
    return f"{int(s // 60)}:{s % 60:06.3f}"


# --------------------------------------------------------------------------
# Effects for the editor (need numpy). Audio is float32, shape (frames, 2), range -1..1.
# --------------------------------------------------------------------------
FX_FFT, FX_HOP = 2048, 512
FX_MAX_SPECTRAL = 180  # seconds; the spectral effects hold the whole sound in memory


def _stft(x):
    win = np.hanning(FX_FFT + 1)[:-1].astype(np.float32)
    xp = np.concatenate([np.zeros(FX_FFT, np.float32), x.astype(np.float32),
                         np.zeros(2 * FX_FFT, np.float32)])
    count = 1 + (len(xp) - FX_FFT) // FX_HOP
    idx = np.arange(FX_FFT)[None, :] + FX_HOP * np.arange(count)[:, None]
    return np.fft.rfft(xp[idx] * win, axis=1).astype(np.complex64), win


def _istft(spec, win, length):
    frames = np.fft.irfft(spec, n=FX_FFT, axis=1).astype(np.float32) * win
    out = np.zeros(FX_HOP * (len(frames) - 1) + FX_FFT, np.float32)
    for i, frame in enumerate(frames):
        out[i * FX_HOP:i * FX_HOP + FX_FFT] += frame
    out = out[FX_FFT:FX_FFT + length] / 1.5  # Hann window at 75% overlap sums to 1.5
    if len(out) < length:
        out = np.concatenate([out, np.zeros(length - len(out), np.float32)])
    return out


def _per_channel(x, fn):
    return np.stack([fn(x[:, c]) for c in range(x.shape[1])], axis=1)


def _smooth(v, width):
    width = max(1, int(width))
    if width == 1:
        return v
    return np.convolve(v, np.ones(width, np.float32) / width, mode="same")


def _freq_filter(x, curve):
    """Filter by multiplying the spectrum with curve(frequencies in Hz)."""
    def one(ch):
        spec = np.fft.rfft(ch)
        freqs = np.fft.rfftfreq(len(ch), 1 / ED_RATE)
        return np.fft.irfft(spec * curve(freqs), n=len(ch)).astype(np.float32)
    return _per_channel(x, one)


def fx_noise_reduction(x, strength):
    """Spectral gating: learn the steady background from the quietest moments, subtract it."""
    s = strength / 100
    if len(x) < FX_FFT:
        return x
    over = 1.0 + 3.0 * s
    floor = 10 ** (-(6 + 30 * s) / 20)

    def one(ch):
        spec, win = _stft(ch)
        mag = np.abs(spec)
        noise = np.percentile(mag, 15, axis=0)
        gain = np.clip((mag - over * noise) / (mag + 1e-9), floor, 1).astype(np.float32)
        gain[1:-1] = (gain[:-2] + gain[1:-1] + gain[2:]) / 3            # over time
        gain[:, 1:-1] = (gain[:, :-2] + gain[:, 1:-1] + gain[:, 2:]) / 3  # over frequency
        return _istft(spec * gain, win, len(ch))
    return _per_channel(x, one)


def fx_click_removal(x, sensitivity):
    """Find very short bursts (key and mouse clicks) and duck them, mostly in the highs."""
    if len(x) < ED_RATE // 10:
        return x
    split = 1500.0
    low = _freq_filter(x, lambda f: 1 / (1 + (f / split) ** 6))
    high = x - low
    block = ED_RATE // 500  # 2 ms
    count = len(x) // block
    energy = (high[:count * block] ** 2).sum(axis=1).reshape(count, block).mean(axis=1)
    span = 41  # ~80 ms: anything much shorter than this that sticks out is a click
    padded = np.pad(energy, span // 2, mode="edge")
    base = np.median(np.lib.stride_tricks.sliding_window_view(padded, span), axis=1)
    ratio_needed = 30 - 26 * (sensitivity / 100)  # 30x (careful) .. 4x (aggressive)
    hit = (energy > ratio_needed * (base + 1e-9)) & (energy > 1e-7)
    if not hit.any():
        return x
    hit = np.convolve(hit.astype(np.float32), np.ones(5), mode="same") > 0  # +-4 ms around it
    duck = np.repeat(hit.astype(np.float32), block)
    duck = np.concatenate([duck, np.zeros(len(x) - len(duck), np.float32)])
    duck = _smooth(_smooth(duck, block * 2), block * 2)[:, None]
    return low * (1 - 0.6 * duck) + high * (1 - 0.97 * duck)


def fx_noise_gate(x, threshold_db):
    """Silence everything quieter than the threshold (typing between words, room noise)."""
    block = ED_RATE // 200  # 5 ms
    count = max(1, len(x) // block)
    level = np.abs(x[:count * block]).max(axis=1).reshape(count, block).max(axis=1)
    is_open = level > 10 ** (threshold_db / 20)
    is_open = np.convolve(is_open.astype(np.float32), np.ones(25), mode="same") > 0  # hold 60 ms
    gain = np.repeat(is_open.astype(np.float32), block)
    gain = np.concatenate([gain, np.full(len(x) - len(gain), gain[-1] if len(gain) else 1.0,
                                         np.float32)])
    return x * _smooth(gain, ED_RATE // 100)[:, None]


def fx_amplify(x, db):
    return x * 10 ** (db / 20)


def fx_normalize(x):
    peak = float(np.abs(x).max()) if len(x) else 0
    return x * (0.89 / peak) if peak > 1e-5 else x


def fx_fade_in(x):
    return x * np.linspace(0, 1, len(x), dtype=np.float32)[:, None] ** 2


def fx_fade_out(x):
    return x * np.linspace(1, 0, len(x), dtype=np.float32)[:, None] ** 2


def fx_reverse(x):
    return x[::-1].copy()


def fx_silence(x):
    return np.zeros_like(x)


def fx_bass_boost(x, db):
    extra = 10 ** (db / 20) - 1
    return _freq_filter(x, lambda f: 1 + extra / (1 + (f / 160.0) ** 2))


def fx_speed(x, percent):
    """Faster and higher, or slower and deeper (like changing a record's speed)."""
    factor = percent / 100
    pos = np.arange(0, max(1, len(x) - 1), factor)
    idx = np.arange(len(x))
    return _per_channel(x, lambda ch: np.interp(pos, idx, ch).astype(np.float32))


def _stretch(ch, rate):
    """Phase vocoder: change the length by 1/rate without changing the pitch."""
    spec, win = _stft(ch)
    bins = spec.shape[1]
    steps = np.arange(0, len(spec) - 1, rate)
    advance = (2 * np.pi * FX_HOP * np.arange(bins) / FX_FFT).astype(np.float32)
    angles = np.angle(spec)
    mags = np.abs(spec)
    phase = angles[0].copy()
    all_bins = np.arange(bins)
    out = np.empty((len(steps), bins), np.complex64)
    for t, step in enumerate(steps):
        i = int(step)
        frac = step - i
        mag = (1 - frac) * mags[i] + frac * mags[i + 1]
        # Lock every bin to the phase of its nearest spectral peak, otherwise the partials
        # smear and the result sounds hollow and quiet.
        peaks = np.flatnonzero((mag[1:-1] > mag[:-2]) & (mag[1:-1] >= mag[2:])) + 1
        if len(peaks):
            owner = peaks[np.searchsorted((peaks[:-1] + peaks[1:]) // 2, all_bins, side="right")]
            locked = phase[owner] + angles[i] - angles[i][owner]
        else:
            locked = phase
        out[t] = mag * np.exp(1j * locked)
        delta = angles[i + 1] - angles[i] - advance
        delta -= 2 * np.pi * np.round(delta / (2 * np.pi))
        phase += advance + delta
    return _istft(out, win, int(len(ch) / rate))


def fx_pitch(x, semitones):
    """Higher or lower voice at the same length."""
    if semitones == 0 or len(x) < FX_FFT * 2:
        return x
    ratio = 2 ** (semitones / 12)
    n = len(x)

    def one(ch):
        longer = _stretch(ch, 1 / ratio)
        pos = np.linspace(0, len(longer) - 1, n)
        return np.interp(pos, np.arange(len(longer)), longer).astype(np.float32)
    return _per_channel(x, one)


def fx_echo(x, delay_ms, feedback, tail=True):
    delay = max(1, int(ED_RATE * delay_ms / 1000))
    fb = feedback / 100
    repeats = 1
    while fb ** repeats > 0.03 and repeats < 40:
        repeats += 1
    out = np.zeros((len(x) + delay * repeats, x.shape[1]), np.float32)
    for k in range(repeats + 1):
        out[k * delay:k * delay + len(x)] += x * (fb ** k if k else 1.0)
    return out if tail else out[:len(x)]


def fx_reverb(x, size, mix, tail=True):
    rt = 0.3 + 2.7 * size / 100  # seconds for the room to ring out
    length = int(rt * ED_RATE)
    rng = np.random.default_rng(7)
    t = np.arange(length, dtype=np.float32) / ED_RATE
    ir = rng.standard_normal((length, x.shape[1])).astype(np.float32) * np.exp(-6.9 * t / rt)[:, None]
    ir = _per_channel(ir, lambda ch: _smooth(ch, 6))  # darker, less hissy
    ir /= np.sqrt((ir ** 2).sum(axis=0, keepdims=True)) + 1e-9
    total = len(x) + length - 1
    size_fft = 1 << (total - 1).bit_length()
    wet = np.stack([np.fft.irfft(np.fft.rfft(x[:, c], size_fft) * np.fft.rfft(ir[:, c], size_fft),
                                 size_fft)[:total] for c in range(x.shape[1])], axis=1)
    m = mix / 100
    out = wet.astype(np.float32) * m
    out[:len(x)] += x * (1 - 0.4 * m)
    return out if tail else out[:len(x)]


def fx_distortion(x, drive):
    gain = 1 + 59 * (drive / 100) ** 2
    out = np.tanh(x * gain)
    peak_in, peak_out = float(np.abs(x).max()), float(np.abs(out).max())
    target = min(0.95, max(peak_in, 0.5 * drive / 100 + 0.3))  # louder the harder you push
    return out * (target / peak_out) if peak_out > 1e-6 else out


def fx_telephone(x):
    band = _freq_filter(x, lambda f: (1 / (1 + (300.0 / np.maximum(f, 1)) ** 6))
                        * (1 / (1 + (f / 3400.0) ** 8)))
    peak = float(np.abs(x).max())
    out = np.tanh(band * 3)
    peak_out = float(np.abs(out).max())
    return out * (peak / peak_out) if peak_out > 1e-6 else out


def fx_robot(x, hz):
    t = np.arange(len(x), dtype=np.float32) / ED_RATE
    return x * np.sign(np.sin(2 * np.pi * hz * t))[:, None] * 0.5 + x * np.sin(2 * np.pi * hz * t)[:, None] * 0.5


def fx_repeat(x, times):
    return np.tile(x, (int(times), 1))


# key: (label, description, [(param label, min, max, default, unit)], function, spectral?)
EFFECTS = {
    "denoise": ("Noise reduction", "Removes steady background noise: hiss, fans, hum, room tone.",
                [("Strength", 0, 100, 60, "%")], fx_noise_reduction, True),
    "declick": ("Click removal", "Softens short clicks such as keyboard and mouse. Works best on "
                "clicks between words; raise the sensitivity until they go.",
                [("Sensitivity", 0, 100, 50, "%")], fx_click_removal, False),
    "gate": ("Noise gate", "Silences everything quieter than the threshold, e.g. typing between "
             "sentences.", [("Threshold", -70, -10, -40, " dB")], fx_noise_gate, False),
    "amplify": ("Amplify / volume", "Makes the selection louder or quieter, e.g. to bring up a quiet part.", [("Gain", -20, 20, 6, " dB")],
                fx_amplify, False),
    "bass": ("Bass boost", "Adds low end.", [("Amount", 0, 18, 9, " dB")], fx_bass_boost, False),
    "speed": ("Speed", "Faster and higher (chipmunk) or slower and deeper.",
              [("Speed", 50, 200, 130, "%")], fx_speed, False),
    "pitch": ("Pitch", "Higher or lower voice without changing the length.",
              [("Semitones", -12, 12, 4, "")], fx_pitch, True),
    "echo": ("Echo", "Repeating echoes.", [("Delay", 50, 800, 250, " ms"),
                                           ("Feedback", 0, 80, 40, "%")], fx_echo, False),
    "reverb": ("Reverb", "Puts the sound in a room, hall or cave.",
               [("Room size", 0, 100, 50, "%"), ("Amount", 0, 100, 35, "%")], fx_reverb, False),
    "distort": ("Distortion", "Crunchy at low drive, blown-out and loud at high drive.",
                [("Drive", 0, 100, 60, "%")], fx_distortion, False),
    "phone": ("Telephone", "Thin, crackly phone or radio sound.", [], fx_telephone, False),
    "robot": ("Robot", "Metallic robot voice.", [("Frequency", 20, 200, 60, " Hz")],
              fx_robot, False),
    "repeat": ("Repeat (loop)", "Repeats the selection back to back.",
               [("Times", 2, 20, 4, "×")], fx_repeat, False),
}
QUICK_FX = (("Fade in", fx_fade_in), ("Fade out", fx_fade_out), ("Normalize", fx_normalize),
            ("Reverse", fx_reverse), ("Silence", fx_silence))
TAIL_FX = (fx_echo, fx_reverb)


def run_effect(fn, samples, a, b, params=(), tail=False):
    """Apply fn to frames a..b of an array('h') of interleaved stereo. Returns array('h')."""
    x = np.frombuffer(samples[a * ED_CH:b * ED_CH].tobytes(), dtype=np.int16)
    x = x.reshape(-1, ED_CH).astype(np.float32) / 32768
    y = fn(x, *params, tail=tail) if fn in TAIL_FX else fn(x, *params)
    y = np.nan_to_num(np.asarray(y, dtype=np.float32))
    out = array("h")
    out.frombytes((np.clip(y, -1, 1) * 32767).astype(np.int16).tobytes())
    return out


class WaveView(QtWidgets.QWidget):
    """Draws the waveform; drag to select, drag a selection edge to adjust, wheel to zoom."""
    changed = QtCore.Signal()

    def __init__(self):
        super().__init__()
        self.samples = array("h")
        self.sel = (0, 0)
        self.view = (0, 1)
        self.playhead = None
        self._cache_key = None
        self._peaks = []
        self._drag = None
        self.version = 0
        self.setMinimumSize(700, 220)
        self.setMouseTracking(True)

    @property
    def nframes(self):
        return len(self.samples) // ED_CH

    def set_samples(self, samples, keep_view=False):
        self.samples = samples
        self.version += 1
        n = self.nframes
        self.sel = (min(self.sel[0], n), min(self.sel[1], n))
        if not keep_view or self.view[1] > n or self.view[1] <= self.view[0]:
            self.view = (0, max(1, n))
        self.changed.emit()
        self.update()

    def set_view(self, v0, v1):
        n = max(1, self.nframes)
        span = max(64, min(n, v1 - v0))
        v0 = max(0, min(n - span, v0))
        self.view = (v0, v0 + span)
        self.changed.emit()
        self.update()

    def frame_at(self, x):
        v0, v1 = self.view
        f = v0 + int(x / max(1, self.width()) * (v1 - v0))
        return max(0, min(self.nframes, f))

    def x_of(self, frame):
        v0, v1 = self.view
        return (frame - v0) / (v1 - v0) * self.width()

    def _compute_peaks(self):
        key = (self.view, self.width(), self.version)
        if key == self._cache_key:
            return self._peaks
        v0, v1 = self.view
        w, span, s, n = self.width(), v1 - v0, self.samples, self.nframes
        peaks = []
        for x in range(w):
            a = v0 + x * span // w
            b = min(n, max(a + 1, v0 + (x + 1) * span // w))
            if a >= n:
                peaks.append((0, 0))
                continue
            step = max(1, (b - a) * ED_CH // 2048)  # sample big columns sparsely
            chunk = s[a * ED_CH:b * ED_CH:step]
            peaks.append((min(chunk), max(chunk)))
        self._cache_key, self._peaks = key, peaks
        return peaks

    def paintEvent(self, _ev):
        p = QtGui.QPainter(self)
        w, h = self.width(), self.height()
        mid, half = h / 2, h / 2 - 4
        p.fillRect(self.rect(), QtGui.QColor("#1e2127"))
        a, b = self.sel
        xa, xb = self.x_of(a), self.x_of(b)
        if b > a:
            p.fillRect(QtCore.QRectF(xa, 0, max(1.0, xb - xa), h), QtGui.QColor("#2f4a6b"))
        p.setPen(QtGui.QColor("#3a3f4b"))
        p.drawLine(0, int(mid), w, int(mid))
        if self.nframes:
            p.setPen(QtGui.QColor("#6cc04a"))
            for x, (lo, hi) in enumerate(self._compute_peaks()):
                p.drawLine(x, int(mid - hi / 32768 * half), x, int(mid - lo / 32768 * half))
        p.setPen(QtGui.QPen(QtGui.QColor("#e6c15a"), 2))
        for x in {xa, xb}:
            if -2 <= x <= w + 2:
                p.drawLine(int(x), 0, int(x), h)
        if self.playhead is not None:
            p.setPen(QtGui.QPen(QtGui.QColor("#ffffff"), 1))
            x = int(self.x_of(self.playhead))
            p.drawLine(x, 0, x, h)

    def _edge_at(self, x):
        a, b = self.sel
        if b > a:
            if abs(x - self.x_of(a)) <= 6:
                return 0
            if abs(x - self.x_of(b)) <= 6:
                return 1
        return None

    def mousePressEvent(self, ev):
        if ev.button() != Qt.LeftButton or not self.nframes:
            return
        x = ev.position().x()
        edge = self._edge_at(x)
        if edge is None:
            f = self.frame_at(x)
            self.sel, self._drag = (f, f), f
        else:
            self._drag = self.sel[1 - edge]  # the opposite edge stays fixed
        self.changed.emit()
        self.update()

    def mouseMoveEvent(self, ev):
        x = ev.position().x()
        if self._drag is None:
            self.setCursor(Qt.SizeHorCursor if self._edge_at(x) is not None else Qt.IBeamCursor)
            return
        f = self.frame_at(x)
        self.sel = (min(f, self._drag), max(f, self._drag))
        self.changed.emit()
        self.update()

    def mouseReleaseEvent(self, _ev):
        self._drag = None

    def wheelEvent(self, ev):
        if not self.nframes:
            return
        v0, v1 = self.view
        span = v1 - v0
        dy, dx = ev.angleDelta().y(), ev.angleDelta().x()
        if ev.modifiers() & Qt.ShiftModifier or (dx and not dy):  # scroll sideways
            d = dx or dy
            self.set_view(v0 - int(span * 0.15 * (1 if d > 0 else -1)), v1)
            return
        factor = 0.75 if dy > 0 else 1 / 0.75
        anchor = self.frame_at(ev.position().x())
        new_span = int(span * factor)
        rel = (anchor - v0) / span
        nv0 = int(anchor - rel * new_span)
        self.set_view(nv0, nv0 + new_span)


class EditorDialog(QtWidgets.QDialog):
    """Returns Accepted with .mode in ("overwrite", "new") and the edited audio in .pcm()."""

    def __init__(self, parent, title, path, out_device, volume=1.0, ask_new=None, clip=False):
        super().__init__(parent)
        self.setWindowTitle(f"Edit - {title}")
        self.resize(1060, 620)
        self.mode = None
        self.target = None
        self.ask_new = ask_new
        self._loaded = False
        self.undo = []
        self._chunks = []
        self._preview_file = None
        self._play_from = 0
        W = QtWidgets
        lay = W.QVBoxLayout(self)
        self.hint = W.QLabel("Loading…")
        lay.addWidget(self.hint)
        self.view = WaveView()
        self.view.changed.connect(self._refresh)
        lay.addWidget(self.view, 1)
        self.scroll = W.QScrollBar(Qt.Horizontal)
        self.scroll.valueChanged.connect(self._scrolled)
        lay.addWidget(self.scroll)
        self.info = W.QLabel("")
        lay.addWidget(self.info)

        row = W.QHBoxLayout()
        self.buttons = {}
        for key, text, slot, tip in (
                ("play", "▶ Play selection", self.play, "Space"),
                ("stop", "⏹ Stop", self.stop, ""),
                ("keep", "Trim to selection", self.keep, "Keep only the highlighted part"),
                ("cut", "Delete selection", self.cut, "Remove the highlighted part (Del)"),
                ("all", "Select all", self.select_all, "Ctrl+A"),
                ("undo", "Undo", self.undo_last, "Ctrl+Z"),
                ("fit", "Zoom to fit", self.zoom_fit, ""),
                ("zsel", "Zoom to selection", self.zoom_sel, "")):
            b = W.QPushButton(text)
            b.setToolTip(tip)
            b.setFocusPolicy(Qt.NoFocus)
            b.clicked.connect(slot)
            row.addWidget(b)
            self.buttons[key] = b
        self.loop_box = W.QCheckBox("Loop playback")
        self.loop_box.setFocusPolicy(Qt.NoFocus)
        row.addWidget(self.loop_box)
        row.addStretch(1)
        lay.addLayout(row)

        fx_box = W.QGroupBox("Effects - applied to the selection, or to everything if nothing "
                             "is selected")
        fx_lay = W.QVBoxLayout(fx_box)
        quick = W.QHBoxLayout()
        for label, fn in QUICK_FX:
            b = W.QPushButton(label)
            b.setFocusPolicy(Qt.NoFocus)
            b.clicked.connect(lambda _=False, f=fn, l=label: self.apply_fx(f, (), l))
            quick.addWidget(b)
        quick.addStretch(1)
        fx_lay.addLayout(quick)
        frow = W.QHBoxLayout()
        self.fx_combo = W.QComboBox()
        for key, spec in EFFECTS.items():
            self.fx_combo.addItem(spec[0], key)
        frow.addWidget(self.fx_combo)
        self.fx_sliders = []
        for _ in range(2):
            name, slider, value = W.QLabel(), W.QSlider(Qt.Horizontal), W.QLabel()
            slider.setMinimumWidth(140)
            slider.setFocusPolicy(Qt.NoFocus)
            value.setMinimumWidth(56)
            for wdg in (name, slider, value):
                frow.addWidget(wdg)
            self.fx_sliders.append((name, slider, value))
            slider.valueChanged.connect(self._fx_values_changed)
        frow.addStretch(1)
        self.fx_preview_btn = W.QPushButton("▶ Preview")
        self.fx_preview_btn.setToolTip("Hear the effect without changing anything")
        self.fx_preview_btn.clicked.connect(self.preview_fx)
        self.fx_apply_btn = W.QPushButton("Apply")
        self.fx_apply_btn.clicked.connect(self.apply_current_fx)
        for b in (self.fx_preview_btn, self.fx_apply_btn):
            b.setFocusPolicy(Qt.NoFocus)
            frow.addWidget(b)
        fx_lay.addLayout(frow)
        self.fx_desc = W.QLabel()
        self.fx_desc.setWordWrap(True)
        fx_lay.addWidget(self.fx_desc)
        lay.addWidget(fx_box)
        self.fx_combo.currentIndexChanged.connect(self._fx_changed)
        self._fx_changed()
        if np is None:
            fx_box.setEnabled(False)
            self.fx_desc.setText("Effects need the numpy package - run install.sh again.")

        row = W.QHBoxLayout()
        row.addStretch(1)
        self.save_btn = W.QPushButton("Save (replace)")
        self.save_btn.clicked.connect(lambda: self._finish("overwrite"))
        self.new_btn = W.QPushButton("Save to library…" if clip else "Save as new sound…")
        self.save_btn.setVisible(not clip)
        self.new_btn.clicked.connect(lambda: self._finish("new"))
        cancel = W.QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        for b in (self.save_btn, self.new_btn, cancel):
            b.setAutoDefault(False)
            row.addWidget(b)
        lay.addLayout(row)

        for seq, slot in (("Space", self.toggle_play), ("Delete", self.cut),
                          ("Ctrl+Z", self.undo_last), ("Ctrl+A", self.select_all)):
            QtGui.QShortcut(QtGui.QKeySequence(seq), self, activated=slot)

        self.out = QAudioOutput()
        self.out.setDevice(out_device)
        self.out.setVolume(volume)  # same loudness as normal playback
        self.player = QMediaPlayer()
        self.player.setAudioOutput(self.out)
        self.player.positionChanged.connect(self._on_pos)
        self.player.playbackStateChanged.connect(self._on_state)

        fmt = QAudioFormat()
        fmt.setSampleRate(ED_RATE)
        fmt.setChannelCount(ED_CH)
        fmt.setSampleFormat(QAudioFormat.Int16)
        self.decoder = QAudioDecoder(self)
        self.decoder.setAudioFormat(fmt)
        self.decoder.setSource(QtCore.QUrl.fromLocalFile(str(path)))
        self.decoder.bufferReady.connect(self._on_buffer)
        self.decoder.finished.connect(self._decoded)
        self.decoder.error.connect(
            lambda _e: self.hint.setText("Could not read this file: " + self.decoder.errorString()))
        self._refresh()
        self.decoder.start()

    # ---- loading ---------------------------------------------------------
    def _on_buffer(self):
        buf = self.decoder.read()
        if buf.isValid():
            self._chunks.append(bytes(buf.constData())[:buf.byteCount()])

    def _decoded(self):
        if self._loaded:  # stopping the decoder fires "finished" again
            return
        self._loaded = True
        samples = array("h")
        samples.frombytes(b"".join(self._chunks))
        self._chunks = []
        self.view.sel = (0, len(samples) // ED_CH)
        self.view.set_samples(samples)
        self.hint.setText("Drag on the waveform to select. Drag a yellow edge to adjust it. "
                          "Mouse wheel zooms, Shift+wheel scrolls.")

    def pcm(self):
        return self.view.samples.tobytes()

    # ---- state -----------------------------------------------------------
    def _refresh(self):
        v = self.view
        n, (a, b), (v0, v1) = v.nframes, v.sel, v.view
        self.info.setText(f"Selection: {fmt_time(a)} – {fmt_time(b)}  ({(b - a) / ED_RATE:.3f} s)"
                          f"      Total length: {n / ED_RATE:.3f} s")
        has_sel = b > a
        self.buttons["keep"].setEnabled(has_sel and (b - a) < n)
        self.buttons["cut"].setEnabled(has_sel)
        self.buttons["zsel"].setEnabled(has_sel)
        self.buttons["play"].setEnabled(n > 0)
        self.buttons["undo"].setEnabled(bool(self.undo))
        self.save_btn.setEnabled(n > 0)
        self.new_btn.setEnabled(n > 0)
        self.scroll.blockSignals(True)
        self.scroll.setRange(0, max(0, n - (v1 - v0)))
        self.scroll.setPageStep(v1 - v0)
        self.scroll.setSingleStep(max(1, (v1 - v0) // 10))
        self.scroll.setValue(v0)
        self.scroll.blockSignals(False)

    def _scrolled(self, value):
        v0, v1 = self.view.view
        self.view.set_view(value, value + (v1 - v0))

    # ---- editing ---------------------------------------------------------
    def _push_undo(self):
        self.stop()
        self.undo.append((array("h", self.view.samples), self.view.sel))
        del self.undo[:-8]

    def keep(self):
        a, b = self.view.sel
        if b <= a or b - a >= self.view.nframes:
            return
        self._push_undo()
        self.view.sel = (0, b - a)
        self.view.set_samples(self.view.samples[a * ED_CH:b * ED_CH])

    def cut(self):
        a, b = self.view.sel
        if b <= a:
            return
        self._push_undo()
        samples = self.view.samples
        del samples[a * ED_CH:b * ED_CH]
        self.view.sel = (a, a)
        self.view.set_samples(samples, keep_view=True)

    def undo_last(self):
        if not self.undo:
            return
        self.stop()
        samples, sel = self.undo.pop()
        self.view.sel = sel
        self.view.set_samples(samples)

    def select_all(self):
        self.view.sel = (0, self.view.nframes)
        self.view.changed.emit()
        self.view.update()

    def zoom_fit(self):
        self.view.set_view(0, self.view.nframes)

    def zoom_sel(self):
        a, b = self.view.sel
        if b > a:
            pad = max(1, (b - a) // 20)
            self.view.set_view(a - pad, b + pad)

    # ---- effects ---------------------------------------------------------
    def _fx_changed(self):
        _label, desc, params, _fn, _spectral = EFFECTS[self.fx_combo.currentData()]
        for i, (name, slider, value) in enumerate(self.fx_sliders):
            used = i < len(params)
            for wdg in (name, slider, value):
                wdg.setVisible(used)
            if used:
                text, lo, hi, default, _unit = params[i]
                name.setText(text + ":")
                slider.blockSignals(True)
                slider.setRange(lo, hi)
                slider.setValue(default)
                slider.blockSignals(False)
        self.fx_desc.setText(desc)
        self._fx_values_changed()

    def _fx_values_changed(self):
        params = EFFECTS[self.fx_combo.currentData()][2]
        for (_name, slider, value), (_t, _lo, _hi, _d, unit) in zip(self.fx_sliders, params):
            value.setText(f"{slider.value():+d}{unit}" if _lo < 0 else f"{slider.value()}{unit}")

    def _region(self):
        a, b = self.view.sel
        return (a, b) if b > a else (0, self.view.nframes)

    def _fx_render(self, fn, params, spectral=False):
        """Returns (a, b, new audio for frames a..b) or None."""
        a, b = self._region()
        if np is None or b <= a:
            return None
        if spectral and b - a > FX_MAX_SPECTRAL * ED_RATE:
            self.hint.setText(f"That effect works on up to {FX_MAX_SPECTRAL // 60} minutes at a "
                              "time - select a shorter part.")
            return None
        QtWidgets.QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            out = run_effect(fn, self.view.samples, a, b, params, tail=b >= self.view.nframes)
        except (MemoryError, ValueError, FloatingPointError, IndexError) as e:
            self.hint.setText(f"The effect failed: {e}")
            return None
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        return a, b, out

    def _fx_selected(self):
        label, _desc, params, fn, spectral = EFFECTS[self.fx_combo.currentData()]
        return label, fn, [self.fx_sliders[i][1].value() for i in range(len(params))], spectral

    def preview_fx(self):
        label, fn, values, spectral = self._fx_selected()
        result = self._fx_render(fn, values, spectral)
        if result:
            self._play_pcm(result[2].tobytes(), result[0])
            self.hint.setText(f"Previewing {label} - nothing has been changed yet. "
                              "Click Apply to keep it.")

    def apply_current_fx(self):
        label, fn, values, spectral = self._fx_selected()
        self.apply_fx(fn, values, label, spectral)

    def apply_fx(self, fn, params, label, spectral=False):
        had_selection = self.view.sel[1] > self.view.sel[0]
        result = self._fx_render(fn, params, spectral)
        if not result:
            return
        a, b, out = result
        self._push_undo()
        samples = self.view.samples
        new = samples[:a * ED_CH] + out + samples[b * ED_CH:]
        if had_selection:
            self.view.sel = (a, a + len(out) // ED_CH)
        self.view.set_samples(new, keep_view=len(out) == (b - a) * ED_CH)
        self.hint.setText(f"Applied {label}. Ctrl+Z undoes it.")

    # ---- preview (your output only, never the mic) -------------------------
    def play(self):
        a, b = self.view.sel
        if b <= a:  # nothing highlighted: play from the cursor to the end
            b = self.view.nframes
            if a >= b:
                a = 0
        if b <= a:
            return
        self._play_pcm(self.view.samples[a * ED_CH:b * ED_CH].tobytes(), a)

    def _play_pcm(self, pcm, start_frame):
        self.stop()
        CLIPS_DIR.mkdir(parents=True, exist_ok=True)
        self._preview_file = CLIPS_DIR / f"preview-{uuid.uuid4().hex[:8]}.wav"
        write_wav(pcm, self._preview_file)
        self._play_from = start_frame
        self.player.setLoops(QMediaPlayer.Infinite if self.loop_box.isChecked() else 1)
        self.player.setSource(QtCore.QUrl.fromLocalFile(str(self._preview_file)))
        self.player.play()

    def toggle_play(self):
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.stop()
        else:
            self.play()

    def stop(self):
        self.player.stop()
        self.player.setSource(QtCore.QUrl())
        if self._preview_file:
            try:
                self._preview_file.unlink()
            except OSError:
                pass
            self._preview_file = None

    def _on_pos(self, ms):
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.view.playhead = self._play_from + ms * ED_RATE // 1000
            self.view.update()

    def _on_state(self, state):
        if state == QMediaPlayer.StoppedState:
            self.view.playhead = None
            self.view.update()

    def _finish(self, mode):
        if mode == "new" and self.ask_new:
            self.stop()
            self.target = self.ask_new()
            if self.target is None:
                return  # cancelled: stay in the editor
        self.mode = mode
        self.accept()

    def done(self, result):
        self._loaded = True
        self.decoder.stop()
        self.stop()
        super().done(result)


class SaveDialog(QtWidgets.QDialog):
    NO_FOLDER = "(no folder)"

    def __init__(self, parent, title, name, folder_names, folder):
        super().__init__(parent)
        self.setWindowTitle(title)
        form = QtWidgets.QFormLayout(self)
        self.name = QtWidgets.QLineEdit(name)
        self.name.setMinimumWidth(320)
        self.folder = QtWidgets.QComboBox()
        self.folder.setEditable(True)
        self.folder.addItems([self.NO_FOLDER] + sorted(folder_names, key=str.lower))
        self.folder.setCurrentText(folder or self.NO_FOLDER)
        form.addRow("Name:", self.name)
        form.addRow("Folder:", self.folder)
        form.addRow("", QtWidgets.QLabel("<small>Pick a folder or type a new one.</small>"))
        box = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Save
                                         | QtWidgets.QDialogButtonBox.Cancel)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        form.addRow(box)
        self.name.selectAll()

    def values(self):
        folder = self.folder.currentText().strip()
        return self.name.text().strip(), ("" if folder == self.NO_FOLDER else folder)


def show_in_file_manager(path):
    """Open the file manager (Dolphin on KDE) at a folder, or with a file highlighted."""
    path = Path(path)
    if path.is_file() and shutil.which("dbus-send"):
        try:
            r = subprocess.run(
                ["dbus-send", "--session", "--print-reply", "--dest=org.freedesktop.FileManager1",
                 "/org/freedesktop/FileManager1", "org.freedesktop.FileManager1.ShowItems",
                 f"array:string:{QtCore.QUrl.fromLocalFile(str(path)).toString()}", "string:"],
                capture_output=True, timeout=5)
            if r.returncode == 0:
                return
        except (OSError, subprocess.TimeoutExpired):
            pass
    folder = path if path.is_dir() else path.parent
    QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(folder)))


# --------------------------------------------------------------------------
# Voice changer: live effects on your microphone, plus AI voices (RVC models)
# --------------------------------------------------------------------------
VOICE_RATE = 48000
VOICE_DIR = DATA_DIR / "voices"   # models unpacked from .zip files
AI_ASSETS = DATA_DIR / "ai"       # shared helper networks, downloaded on first use
RESPONSIVENESS = {  # key: (label, block seconds, context seconds)
    "fast": ("Fast (shorter delay, more glitches)", 0.3, 0.8),
    "balanced": ("Balanced", 0.45, 1.0),
    "smooth": ("Smooth (longer delay)", 0.7, 1.2),
}
TEST_SECONDS = 5
B1_DEFAULTS = {"pitch": 3, "size": 25, "metal": 60}


def ai_voice_installed():
    import importlib.util
    return np is not None and all(importlib.util.find_spec(m) is not None
                                  for m in ("torch", "transformers", "scipy"))


def to_wav_pcm(mono, rate):
    """Mono float audio at any rate -> 48 kHz stereo int16 bytes (what write_wav expects)."""
    mono = np.asarray(mono, dtype=np.float32)
    if rate != ED_RATE and len(mono):
        n = int(round(len(mono) * ED_RATE / rate))
        mono = np.interp(np.linspace(0, len(mono) - 1, n), np.arange(len(mono)), mono)
    stereo = np.repeat(np.clip(mono, -1, 1)[:, None], 2, axis=1)
    return (stereo * 32767).astype(np.int16).tobytes()


class Background(QtCore.QObject):
    """Run a function on a worker thread; done(result, error) arrives on the GUI thread."""
    done = QtCore.Signal(object, object)
    progress = QtCore.Signal(str)

    def __init__(self, parent, fn, on_done, on_progress=None):
        super().__init__(parent)
        self.done.connect(lambda r, e: (on_done(r, e), self.deleteLater()))
        if on_progress:
            self.progress.connect(on_progress)
        threading.Thread(target=self._run, args=(fn,), daemon=True).start()

    def _run(self, fn):
        try:
            result = fn(self.progress.emit)
            self.done.emit(result, None)
        except Exception as e:  # shown to the user
            self.done.emit(None, e)


class VoiceLive:
    """Mic -> processor -> Blurt-Mic and/or your headphones, via parec/pacat."""
    CHUNK = 480  # 10 ms

    def __init__(self):
        self.reader = None
        self.outs = {}
        self.lock = threading.Lock()
        self.running = False
        self.processor = None
        self.block = 0
        self.cost = 0.0      # processing time as a fraction of real time
        self.dropped = 0
        self.queue = deque()
        self.wake = threading.Event()
        self.gen = 0  # bumps on every (re)start so old threads quit

    def start(self, source, processor, block=0):
        """block=0: process every 10 ms chunk inline (DSP). Otherwise fixed blocks (AI)."""
        self.stop()
        self.processor, self.block = processor, block
        self.cost, self.dropped = 0.0, 0
        self.gen += 1
        self.running = True
        self.reader = subprocess.Popen(
            ["parec", f"--device={source}", "--format=float32le", f"--rate={VOICE_RATE}",
             "--channels=1", "--latency-msec=10", "--raw", "--client-name=Blurt voice input"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        threading.Thread(target=self._read, args=(self.reader, self.gen), daemon=True).start()
        if block:
            threading.Thread(target=self._work, args=(self.gen,), daemon=True).start()

    def set_output(self, name, sink):
        with self.lock:
            old = self.outs.pop(name, None)
            if old:
                try:
                    old.stdin.close()
                except OSError:
                    pass
                old.terminate()
            if sink:
                self.outs[name] = subprocess.Popen(
                    ["pacat", "--playback", f"--device={sink}", "--format=float32le",
                     f"--rate={VOICE_RATE}", "--channels=1", "--latency-msec=30", "--raw",
                     f"--client-name=Blurt voice {name}"],
                    stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def stop(self):
        self.running = False
        self.wake.set()
        if self.reader:
            self.reader.terminate()
            self.reader = None
        self.queue.clear()

    def close(self):
        self.stop()
        for name in list(self.outs):
            self.set_output(name, None)

    def _write(self, y):
        data = np.asarray(y, dtype="<f4").tobytes()
        with self.lock:
            for name, proc in list(self.outs.items()):
                try:
                    proc.stdin.write(data)
                    proc.stdin.flush()
                except (BrokenPipeError, OSError, ValueError):
                    self.outs.pop(name, None)

    def _read(self, reader, gen):
        size = self.CHUNK * 4
        pending = []
        have = 0
        while self.running and gen == self.gen:
            data = reader.stdout.read(size)
            if not data:
                break
            x = np.frombuffer(data, dtype="<f4").astype(np.float32)
            if not self.block:
                t0 = time.perf_counter()
                y = self.processor.process(x)
                spent = (time.perf_counter() - t0) / (len(x) / VOICE_RATE)
                self.cost += (spent - self.cost) * 0.05
                self._write(y)
                continue
            pending.append(x)
            have += len(x)
            if have >= self.block:
                joined = np.concatenate(pending)
                self.queue.append(joined[:self.block])
                rest = joined[self.block:]
                pending, have = ([rest], len(rest)) if len(rest) else ([], 0)
                while len(self.queue) > 2:  # falling behind: skip audio to keep the delay down
                    self.queue.popleft()
                    self.dropped += 1
                self.wake.set()

    def _work(self, gen):
        while self.running and gen == self.gen:
            self.wake.wait(0.5)
            self.wake.clear()
            while self.running and gen == self.gen and self.queue:
                block = self.queue.popleft()
                try:
                    y = self.processor.process(block)
                except Exception:
                    y = np.zeros(len(block), np.float32)
                if gen != self.gen:
                    break
                self.cost += (self.processor.last_cost / (len(block) / VOICE_RATE)
                              - self.cost) * 0.3
                self._write(y)


class VoiceDialog(QtWidgets.QDialog):
    def __init__(self, win):
        super().__init__(win)
        self.win, self.store = win, win.store
        self.setWindowTitle("Voice changer")
        self.resize(720, 560)
        cfg = self.store.settings.setdefault("voice", {})
        cfg.setdefault("models", [])
        cfg.setdefault("mode", "off")
        cfg.setdefault("b1", dict(B1_DEFAULTS))
        cfg.setdefault("responsiveness", "balanced")
        self.cfg = cfg
        self.live = VoiceLive()
        self.helpers = None          # shared AI helper networks, loaded once
        self.voices = {}             # model id -> loaded RVCVoice
        self.loading = False
        self.test_raw = self.test_out = None
        self.player = QMediaPlayer(self)
        self.player_out = QAudioOutput(self)
        self.player.setAudioOutput(self.player_out)
        self._build()
        self._fill_voices()
        self._mode_changed()
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._update_live_status)
        self.timer.start(500)

    # ---- layout ----------------------------------------------------------
    def _build(self):
        W = QtWidgets
        lay = W.QVBoxLayout(self)
        row = W.QHBoxLayout()
        row.addWidget(W.QLabel("Voice:"))
        self.mode = W.QComboBox()
        self.mode.setMinimumWidth(260)
        self.mode.activated.connect(lambda _i: self._mode_chosen())
        row.addWidget(self.mode, 1)
        add = W.QPushButton("Add AI voice model…")
        add.clicked.connect(self.add_model)
        self.remove_btn = W.QPushButton("Remove")
        self.remove_btn.clicked.connect(self.remove_model)
        row.addWidget(add)
        row.addWidget(self.remove_btn)
        lay.addLayout(row)

        # settings for the droid
        self.b1_box = W.QGroupBox("B1 Battle Droid")
        form = W.QFormLayout(self.b1_box)
        self.b1_sliders = {}
        for key, label, lo, hi, unit in (("pitch", "Pitch", -12, 12, " semitones"),
                                         ("size", "Size (higher = smaller droid)", -30, 60, "%"),
                                         ("metal", "Metal", 0, 100, "%")):
            self.b1_sliders[key] = self._slider(form, label, lo, hi, self.cfg["b1"][key], unit,
                                                lambda v, k=key: self._b1_changed(k, v))
        reset = W.QPushButton("Reset to preset")
        reset.clicked.connect(self._b1_reset)
        form.addRow("", reset)
        lay.addWidget(self.b1_box)

        # settings for AI models
        self.ai_box = W.QGroupBox("AI voice")
        form = W.QFormLayout(self.ai_box)
        self.ai_files = W.QLabel()
        self.ai_files.setWordWrap(True)
        self.ai_files.setTextInteractionFlags(Qt.TextSelectableByMouse)
        form.addRow("Files:", self.ai_files)
        idx_btn = W.QPushButton("Choose .index file…")
        idx_btn.clicked.connect(self.choose_index)
        form.addRow("", idx_btn)
        self.ai_sliders = {
            "pitch": self._slider(form, "Pitch shift", -24, 24, 12, " semitones",
                                  lambda v: self._ai_changed("pitch", v)),
            "index_rate": self._slider(form, "Voice match (index)", 0, 100, 75, "%",
                                       lambda v: self._ai_changed("index_rate", v)),
            "protect": self._slider(form, "Protect breaths and consonants", 0, 50, 33, "%",
                                    lambda v: self._ai_changed("protect", v)),
        }
        self.resp = W.QComboBox()
        for key, (label, _b, _c) in RESPONSIVENESS.items():
            self.resp.addItem(label, key)
        self.resp.setCurrentIndex(max(0, self.resp.findData(self.cfg["responsiveness"])))
        self.resp.activated.connect(self._resp_changed)
        form.addRow("Live responsiveness:", self.resp)
        hint = W.QLabel("<small>Pitch shift: about +12 for a deep voice to a high one, "
                        "0 if your voice is already similar.</small>")
        hint.setWordWrap(True)
        form.addRow("", hint)
        lay.addWidget(self.ai_box)

        # testing
        test = W.QGroupBox(f"Test - record {TEST_SECONDS} seconds and hear the result "
                           "(nobody else hears it)")
        tl = W.QVBoxLayout(test)
        trow = W.QHBoxLayout()
        self.rec_btn = W.QPushButton(f"● Record {TEST_SECONDS} s test")
        self.rec_btn.clicked.connect(self.record_test)
        self.play_btn = W.QPushButton("▶ Play with voice")
        self.play_btn.clicked.connect(lambda: self._play_test(self.test_out))
        self.orig_btn = W.QPushButton("▶ Play original")
        self.orig_btn.clicked.connect(lambda: self._play_test(self.test_raw))
        self.redo_btn = W.QPushButton("Apply current settings again")
        self.redo_btn.setToolTip("Re-run the voice on the same recording after changing "
                                 "a setting")
        self.redo_btn.clicked.connect(self._convert_test)
        self.save_test_btn = W.QPushButton("Save to library…")
        self.save_test_btn.clicked.connect(self.save_test)
        for b in (self.rec_btn, self.play_btn, self.orig_btn, self.redo_btn, self.save_test_btn):
            trow.addWidget(b)
        trow.addStretch(1)
        tl.addLayout(trow)
        lay.addWidget(test)

        # live
        live = W.QGroupBox("Live")
        ll = W.QVBoxLayout(live)
        self.on_mic = W.QCheckBox("Others hear this voice (on Blurt-Mic)")
        self.on_mic.toggled.connect(self._live_changed)
        self.monitor = W.QCheckBox("Hear myself live")
        self.monitor.setToolTip("Plays your changed voice in your headphones. Use headphones, "
                                "or the speakers will feed back into the mic.")
        self.monitor.toggled.connect(self._live_changed)
        ll.addWidget(self.on_mic)
        ll.addWidget(self.monitor)
        self.live_status = W.QLabel("")
        self.live_status.setWordWrap(True)
        ll.addWidget(self.live_status)
        lay.addWidget(live)

        self.status = W.QLabel("")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        lay.addStretch(1)
        self._update_test_buttons()

    def _slider(self, form, label, lo, hi, value, unit, on_change):
        box = QtWidgets.QHBoxLayout()
        s = QtWidgets.QSlider(Qt.Horizontal)
        s.setRange(lo, hi)
        s.setValue(int(value))
        val = QtWidgets.QLabel()
        val.setMinimumWidth(110)

        def show(v):
            val.setText((f"{v:+d}" if lo < 0 else f"{v}") + unit)
        show(s.value())
        s.valueChanged.connect(lambda v: (show(v), on_change(v)))
        box.addWidget(s, 1)
        box.addWidget(val)
        form.addRow(label + ":", box)
        return s

    # ---- voices ----------------------------------------------------------
    def _model(self, mid=None):
        mid = mid or self.cfg["mode"]
        return next((m for m in self.cfg["models"] if m["id"] == mid), None)

    def _fill_voices(self):
        self.mode.blockSignals(True)
        self.mode.clear()
        self.mode.addItem("Normal voice (off)", "off")
        self.mode.addItem("B1 Battle Droid", "b1")
        for m in self.cfg["models"]:
            self.mode.addItem(f"AI: {m['name']}", m["id"])
        i = self.mode.findData(self.cfg["mode"])
        if i < 0:
            self.cfg["mode"] = "off"
            i = 0
        self.mode.setCurrentIndex(i)
        self.mode.blockSignals(False)

    def _mode_chosen(self):
        self.cfg["mode"] = self.mode.currentData()
        self.store.save()
        self._mode_changed()

    def _mode_changed(self):
        mode = self.cfg["mode"]
        model = self._model()
        self.b1_box.setVisible(mode == "b1")
        self.ai_box.setVisible(model is not None)
        self.remove_btn.setEnabled(model is not None)
        self.test_out = None
        if model:
            for key, s in self.ai_sliders.items():
                s.blockSignals(True)
                s.setValue(int(model.get(key, {"pitch": 12, "index_rate": 75, "protect": 33}[key])))
                s.blockSignals(False)
                s.valueChanged.emit(s.value())
            index = model.get("index") or "none (the voice will be a little less accurate)"
            self.ai_files.setText(f"Model: {model['pth']}\nIndex: {index}")
            self._prepare_ai(model)
        else:
            self.status.setText("" if mode != "off" else
                                "Pick a voice. Your mic is not changed while this is off.")
        self._restart_live()
        self._update_test_buttons()

    def add_model(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Choose an RVC voice model", str(Path.home()),
            "RVC voice models (*.pth *.zip);;All files (*)")
        if not path:
            return
        try:
            pth, index = self._import_model_files(Path(path))
        except (OSError, ValueError, zipfile.BadZipFile) as e:
            QtWidgets.QMessageBox.warning(self, "Add voice model", str(e))
            return
        default = Path(pth).stem.replace("_", " ").strip() or "Voice"
        name, ok = QtWidgets.QInputDialog.getText(self, "Add voice model", "Name:",
                                                  text=default)
        if not ok:
            return
        mid = uuid.uuid4().hex[:8]
        self.cfg["models"].append({"id": mid, "name": name.strip() or default, "pth": pth,
                                   "index": index, "pitch": 12, "index_rate": 75,
                                   "protect": 33})
        self.cfg["mode"] = mid
        self.store.save()
        self._fill_voices()
        self._mode_changed()

    def _import_model_files(self, path):
        """Returns (pth, index or ""). Zips are unpacked into Blurt's voices folder."""
        if path.suffix.lower() == ".zip":
            dest = VOICE_DIR / re.sub(r"[^\w.-]+", "_", path.stem)
            dest.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(path) as z:
                for info in z.infolist():
                    name = Path(info.filename).name
                    if info.is_dir() or Path(name).suffix.lower() not in (".pth", ".index"):
                        continue
                    with z.open(info) as src, open(dest / name, "wb") as out:
                        shutil.copyfileobj(src, out)
            folder = dest
            pths = sorted(folder.glob("*.pth"), key=lambda p: p.stat().st_size)
            pths = [p for p in pths if not re.match(r"^[GD]_\d+", p.name)]
            if not pths:
                raise ValueError("That zip has no voice model (.pth) in it.")
            pth = pths[-1]
        else:
            pth, folder = path, path.parent
        if re.match(r"^[GD]_\d+", pth.name):
            raise ValueError(f"{pth.name} is a training checkpoint, not a voice model. Use "
                             "the other, smaller .pth that came with it.")
        indexes = sorted(folder.glob("*.index"))
        added = [i for i in indexes if i.name.startswith("added")] or indexes
        index = ""
        if len(added) == 1:
            index = str(added[0])
        elif len(added) > 1:
            index, _ = QtWidgets.QFileDialog.getOpenFileName(
                self, "Several .index files - pick the one for this voice", str(folder),
                "Index files (*.index)")
        return str(pth), index

    def choose_index(self):
        model = self._model()
        if not model:
            return
        start = str(Path(model["pth"]).parent)
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Choose the voice's .index file",
                                                        start, "Index files (*.index)")
        if path:
            model["index"] = path
            self.voices.pop(model["id"], None)
            self.store.save()
            self._mode_changed()

    def remove_model(self):
        model = self._model()
        if not model:
            return
        if QtWidgets.QMessageBox.question(
                self, "Remove voice", f"Remove “{model['name']}” from Blurt? The model "
                "files themselves are not deleted.") != QtWidgets.QMessageBox.Yes:
            return
        self.cfg["models"].remove(model)
        self.voices.pop(model["id"], None)
        self.cfg["mode"] = "off"
        self.store.save()
        self._fill_voices()
        self._mode_changed()

    # ---- AI loading ------------------------------------------------------
    def _ai_ready(self, model):
        return model is not None and model["id"] in self.voices

    def _prepare_ai(self, model):
        if not ai_voice_installed():
            self.status.setText(
                "AI voices need extra components (PyTorch, about 1 GB). Install them once "
                "with:\n    bash install.sh --ai-voice\nthen restart Blurt.")
            return
        if self._ai_ready(model) or self.loading:
            if self._ai_ready(model):
                self.status.setText(f"{model['name']} is ready.")
            return
        if not Path(model["pth"]).is_file():
            self.status.setText(f"The model file is missing: {model['pth']}")
            return
        from blurt_voice.rvc import engine
        missing = engine.assets_missing(AI_ASSETS)
        if missing:
            if QtWidgets.QMessageBox.question(
                    self, "Download voice engine",
                    "AI voices use two shared helper files (about 560 MB, from Hugging Face). "
                    "They are downloaded once and kept in Blurt's folder.\n\n"
                    "Download them now?") != QtWidgets.QMessageBox.Yes:
                self.status.setText("The voice engine files are needed before AI voices work.")
                return
        self.loading = True
        self._update_test_buttons()
        self.status.setText(f"Loading {model['name']}…")
        mid, pth, index = model["id"], model["pth"], model.get("index") or None

        def work(progress):
            def report(name, done, total):
                pct = f" {done * 100 // total}%" if total else ""
                progress(f"Downloading {name}{pct} ({done // 1048576} MB)")
            engine.download_assets(AI_ASSETS, report)
            engine.set_threads()
            if self.helpers is None:
                progress("Starting the voice engine…")
                self.helpers = engine.Helpers(AI_ASSETS)
            progress(f"Loading {model['name']}…")
            return engine.RVCVoice(self.helpers, pth, index)

        def finished(voice, error):
            self.loading = False
            if error:
                self.status.setText(f"Could not load the voice: {error}")
            else:
                self.voices[mid] = voice
                note = "" if voice.index is not None or not index else \
                    " (the .index file needs faiss, which is not installed, so it is skipped)"
                self.status.setText(f"{model['name']} is ready{note}. Record a test to hear it.")
                if self.cfg["mode"] == mid:
                    self._restart_live()
            self._update_test_buttons()

        Background(self, work, finished, self.status.setText)

    def _ai_changed(self, key, value):
        model = self._model()
        if not model:
            return
        model[key] = value
        self.store.save()
        proc = self.live.processor
        if self.live.running and hasattr(proc, "shift"):
            proc.shift = model["pitch"]
            proc.index_rate = model["index_rate"] / 100
            proc.protect = model["protect"] / 100

    def _resp_changed(self, _i):
        self.cfg["responsiveness"] = self.resp.currentData()
        self.store.save()
        self._restart_live()

    def _b1_changed(self, key, value):
        self.cfg["b1"][key] = value
        self.store.save()
        if isinstance(self.live.processor, B1Droid):
            self.live.processor.set(**{key: value})

    def _b1_reset(self):
        for key, s in self.b1_sliders.items():
            s.setValue(B1_DEFAULTS[key])

    # ---- building processors ---------------------------------------------
    def _make_processor(self, live):
        """Returns (processor, block size or 0) for the current voice, or (None, 0)."""
        mode = self.cfg["mode"]
        if mode == "b1":
            b = self.cfg["b1"]
            return B1Droid(b["pitch"], b["size"], b["metal"]), 0
        model = self._model()
        if model and self._ai_ready(model):
            from blurt_voice.rvc import engine
            _label, block, context = RESPONSIVENESS[self.cfg["responsiveness"]]
            stream = engine.RVCStream(self.voices[model["id"]], block_s=block, context_s=context,
                                      shift=model["pitch"], index_rate=model["index_rate"] / 100,
                                      protect=model["protect"] / 100)
            return stream, stream.block
        return None, 0

    # ---- testing ---------------------------------------------------------
    def _update_test_buttons(self):
        mode = self.cfg["mode"]
        ready = mode == "b1" or self._ai_ready(self._model())
        self.rec_btn.setEnabled(ready and not getattr(self, "recording", False))
        self.play_btn.setEnabled(self.test_out is not None)
        self.orig_btn.setEnabled(self.test_raw is not None)
        self.redo_btn.setEnabled(ready and self.test_raw is not None)
        self.save_test_btn.setEnabled(self.test_out is not None)

    def record_test(self):
        source = self.store.settings.get("mic", "")
        if not source:
            self.status.setText("Pick your microphone under “Input” in the main window first.")
            return
        self.recording = True
        self._update_test_buttons()
        self.player.stop()
        self._countdown = TEST_SECONDS
        self.status.setText(f"Recording… speak now ({self._countdown} s)")
        tick = QtCore.QTimer(self)

        def step():
            self._countdown -= 1
            if self._countdown > 0 and getattr(self, "recording", False):
                self.status.setText(f"Recording… speak now ({self._countdown} s)")
            else:
                tick.stop()
        tick.timeout.connect(step)
        tick.start(1000)

        def work(_progress):
            proc = subprocess.Popen(
                ["parec", f"--device={source}", "--format=float32le", f"--rate={VOICE_RATE}",
                 "--channels=1", "--latency-msec=20", "--raw",
                 "--client-name=Blurt voice test"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            want = TEST_SECONDS * VOICE_RATE * 4
            data = b""
            try:
                while len(data) < want:
                    chunk = proc.stdout.read(min(65536, want - len(data)))
                    if not chunk:
                        break
                    data += chunk
            finally:
                proc.terminate()
            return np.frombuffer(data[:len(data) // 4 * 4], dtype="<f4").astype(np.float32)

        def finished(audio, error):
            self.recording = False
            tick.stop()
            if error or audio is None or len(audio) < VOICE_RATE // 2:
                self.status.setText(f"Recording failed: {error or 'no audio from the mic'}")
                self._update_test_buttons()
                return
            self.test_raw = audio
            if float(np.abs(audio).max()) < 0.01:
                self.status.setText("That was nearly silent - check the Input microphone in "
                                    "the main window.")
            self._convert_test()
        Background(self, work, finished)

    def _convert_test(self):
        if self.test_raw is None:
            return
        processor, _block = self._make_processor(live=False)
        model = self._model()
        if self.cfg["mode"] == "b1":
            b1 = processor
            pad = np.zeros(b1.latency + 4800, np.float32)
            src = np.concatenate([self.test_raw, pad])
            out = np.concatenate([b1.process(src[i:i + 480]) for i in range(0, len(src), 480)])
            self.test_out = out[b1.latency:b1.latency + len(self.test_raw)]
            self.status.setText("Done. Press “Play with voice”.")
            self._update_test_buttons()
            self._play_test(self.test_out)
            return
        if not self._ai_ready(model):
            return
        voice = self.voices[model["id"]]
        raw = self.test_raw
        settings = (model["pitch"], model["index_rate"] / 100, model["protect"] / 100)
        self.status.setText(f"Converting with {model['name']}… (this takes a few seconds)")
        self.rec_btn.setEnabled(False)
        self.redo_btn.setEnabled(False)
        started = time.perf_counter()

        def work(_progress):
            y, sr = voice.convert(raw, VOICE_RATE, *settings)
            from blurt_voice.rvc.engine import resample
            return resample(y, sr, VOICE_RATE)

        def finished(out, error):
            if error:
                self.status.setText(f"Conversion failed: {error}")
            else:
                self.test_out = out
                took = time.perf_counter() - started
                self.status.setText(f"Done in {took:.1f} s. Playing it now.")
                self._play_test(out)
            self._update_test_buttons()
        Background(self, work, finished)

    def _play_test(self, audio):
        if audio is None:
            return
        CLIPS_DIR.mkdir(parents=True, exist_ok=True)
        path = CLIPS_DIR / f"voice-test-{uuid.uuid4().hex[:6]}.wav"
        write_wav(to_wav_pcm(audio, VOICE_RATE), path)
        old = getattr(self, "_test_file", None)
        self._test_file = path
        self.player.stop()
        self.player_out.setDevice(self.win.engine.out_me.device())
        self.player_out.setVolume(max(0.3, self.win.engine.out_me.volume()))
        self.player.setSource(QtCore.QUrl.fromLocalFile(str(path)))
        self.player.play()
        if old:
            try:
                old.unlink()
            except OSError:
                pass

    def save_test(self):
        if self.test_out is None:
            return
        label = self.mode.currentText().replace("AI: ", "")
        target = self.win._ask_save("Save voice test", f"{label} test", "Voice tests")
        if target:
            self.win._add_sound(*target, pcm=to_wav_pcm(self.test_out, VOICE_RATE))

    # ---- live --------------------------------------------------------------
    def wants_mic(self):
        """True when Blurt-Mic should carry the changed voice instead of the raw mic."""
        return self.on_mic.isChecked() and self.live.running

    def _live_changed(self, *_):
        self._restart_live()

    def _restart_live(self):
        self.live.stop()
        want = self.on_mic.isChecked() or self.monitor.isChecked()
        source = self.store.settings.get("mic", "")
        processor, block = self._make_processor(live=True) if want else (None, 0)
        if want and not source:
            self.live_status.setText("Pick your microphone under “Input” in the main window.")
        elif want and processor is None:
            self.live_status.setText("Live voice starts once the voice is ready." if
                                     self.cfg["mode"] != "off" else
                                     "Pick a voice above to use it live.")
        if processor is not None and source:
            self.live.set_output("mic", MIX_SINK if self.on_mic.isChecked() else None)
            out = device_id(self.win.engine.out_me.device()) or "@DEFAULT_SINK@"
            self.live.set_output("monitor", out if self.monitor.isChecked() else None)
            self.live.start(source, processor, block)
        else:
            self.live.set_output("mic", None)
            self.live.set_output("monitor", None)
        self.win.apply_mic_routing()
        self.win.voice_btn.setText("🎙 Voice (on)" if self.wants_mic() else "🎙 Voice…")

    def _update_live_status(self):
        if not self.live.running:
            if self.on_mic.isChecked() or self.monitor.isChecked():
                return
            self.live_status.setText("Live voice is off. Others hear your normal mic.")
            return
        proc = self.live.processor
        own = proc.latency() if callable(getattr(proc, "latency", None)) else \
            getattr(proc, "latency", 0) / VOICE_RATE
        delay = 0.04 + own
        where = []
        if self.on_mic.isChecked():
            where.append("others hear it")
        if self.monitor.isChecked():
            where.append("you hear it")
        text = (f"Live ({' and '.join(where)}): about {delay:.2f} s delay, processing uses "
                f"{self.live.cost * 100:.0f}% of real time.")
        if self.live.cost > 0.9 or self.live.dropped:
            text += (" Your computer is struggling to keep up, so bits get skipped. Try "
                     "“Smooth” responsiveness.")
        self.live_status.setText(text)

    def toggle_on_mic(self):
        self.on_mic.setChecked(not self.on_mic.isChecked())

    def shutdown(self):
        self.live.close()

    def closeEvent(self, ev):
        self.player.stop()
        super().closeEvent(ev)


ACTIONS = {"stop": "Stop", "pause": "Pause / resume", "random": "Play random sound",
           "clip": "Clip what just played", "replay_clip": "Replay last clip",
           "save_clip": "Save last clip to library",
           "voice": "Voice changer on/off (for others)"}


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, store, router, engine, keys):
        super().__init__()
        self.store, self.router, self.engine, self.keys = store, router, engine, keys
        self.capturing = False
        self.hk = {}
        self.clipper = Clipper()
        self.tee = OutputTee()
        self.tee.start()
        self.last_clip = None
        shutil.rmtree(CLIPS_DIR, ignore_errors=True)
        self.setWindowTitle(APP)
        self.resize(900, 600)
        self.setAcceptDrops(True)
        self._build()
        self.media_devices = QMediaDevices(self)
        self.media_devices.audioOutputsChanged.connect(self.refresh_outputs)
        self.refresh_mics()
        self.refresh_outputs()
        self.refresh_folders()
        self.rebuild_hotkeys()
        keys.pressed.connect(self.on_combo)
        QtWidgets.QApplication.instance().installEventFilter(self)
        e = engine
        e.p_me.positionChanged.connect(self._on_position)
        e.p_me.durationChanged.connect(lambda d: self.seek.setRange(0, max(0, d)))
        e.p_me.playbackStateChanged.connect(self._on_state)
        e.p_me.errorOccurred.connect(lambda _e, msg: self.statusBar().showMessage(msg, 6000))

    # ---- layout ----------------------------------------------------------
    def _build(self):
        W = QtWidgets
        central = W.QWidget()
        self.setCentralWidget(central)
        root = W.QVBoxLayout(central)

        top = W.QHBoxLayout()
        for text, slot in (("Import files…", self.import_files),
                           ("Import folder…", self.import_folder),
                           ("New folder", self.new_folder)):
            b = W.QPushButton(text)
            b.clicked.connect(slot)
            top.addWidget(b)
        self.voice_btn = W.QPushButton("🎙 Voice…")
        self.voice_btn.setToolTip("Voice changer: B1 Battle Droid and AI voices on your mic")
        self.voice_btn.clicked.connect(self.open_voice)
        if B1Droid is None or np is None:
            self.voice_btn.setEnabled(False)
            self.voice_btn.setToolTip("The voice changer needs numpy and the blurt_voice folder "
                                      "- run install.sh again.")
        top.addWidget(self.voice_btn)
        self.hk_btn = W.QPushButton("Control hotkeys")
        self.hk_menu = W.QMenu(self)
        self.hk_menu.aboutToShow.connect(self._fill_hk_menu)
        self.hk_btn.setMenu(self.hk_menu)
        top.addWidget(self.hk_btn)
        top.addStretch(1)
        self.search = W.QLineEdit()
        self.search.setPlaceholderText("Search…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.refresh_sounds)
        self.search.setMaximumWidth(240)
        top.addWidget(self.search)
        root.addLayout(top)

        split = W.QSplitter()
        self.folders = W.QListWidget()
        self.folders.setContextMenuPolicy(Qt.CustomContextMenu)
        self.folders.customContextMenuRequested.connect(self._folder_menu)
        self.folders.currentRowChanged.connect(lambda _r: self.refresh_sounds())
        split.addWidget(self.folders)
        self.table = W.QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Name", "Hotkey", "Folder"])
        self.table.horizontalHeader().setSectionResizeMode(0, W.QHeaderView.Stretch)
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(W.QAbstractItemView.SelectRows)
        self.table.setEditTriggers(W.QAbstractItemView.NoEditTriggers)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._sound_menu)
        self.table.itemDoubleClicked.connect(lambda it: self.play(self._row_id(it.row())))
        split.addWidget(self.table)
        split.setStretchFactor(1, 1)
        split.setSizes([200, 700])
        root.addWidget(split, 1)

        tr = W.QHBoxLayout()
        self.play_btn = W.QPushButton("▶ Play")
        self.play_btn.clicked.connect(self.play_selected)
        self.pause_btn = W.QPushButton("⏸ Pause")
        self.pause_btn.clicked.connect(self.engine.toggle_pause)
        stop = W.QPushButton("⏹ Stop")
        stop.clicked.connect(self.engine.stop)
        rnd = W.QPushButton("🎲 Random")
        rnd.clicked.connect(self.play_random)
        edit = W.QPushButton("✎ Edit…")
        edit.setToolTip("Trim or cut the selected sound (Ctrl+E)")
        edit.clicked.connect(self.edit_selected)
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+E"), self, activated=self.edit_selected)
        for b in (self.play_btn, self.pause_btn, stop, rnd, edit):
            tr.addWidget(b)
        self.now = W.QLabel("")
        self.now.setMinimumWidth(140)
        tr.addWidget(self.now)
        self.seek = W.QSlider(Qt.Horizontal)
        self.seek.sliderMoved.connect(self.engine.seek)
        tr.addWidget(self.seek, 1)
        self.time = W.QLabel("0:00 / 0:00")
        tr.addWidget(self.time)
        root.addLayout(tr)

        grid = W.QGridLayout()
        self.vol_mic = self._volume("vol_mic", 80, self.engine.set_mic_volume)
        self.vol_me = self._volume("vol_me", 60, self.engine.set_my_volume)
        self.mic_combo = W.QComboBox()
        self.mic_combo.activated.connect(self._mic_chosen)
        self.out_combo = W.QComboBox()
        self.out_combo.activated.connect(self._output_chosen)
        refresh = W.QPushButton("Refresh devices")
        refresh.clicked.connect(lambda: (self.refresh_mics(), self.refresh_outputs()))
        grid.addWidget(W.QLabel("Input (your microphone):"), 0, 0)
        grid.addWidget(self.mic_combo, 0, 1)
        grid.addWidget(W.QLabel("Volume others hear:"), 0, 2)
        grid.addWidget(self.vol_mic, 0, 3)
        grid.addWidget(W.QLabel("Output (where you hear it):"), 1, 0)
        grid.addWidget(self.out_combo, 1, 1)
        grid.addWidget(W.QLabel("Volume you hear:"), 1, 2)
        grid.addWidget(self.vol_me, 1, 3)
        grid.addWidget(refresh, 0, 4, 2, 1)
        self.clip_combo = W.QComboBox()
        self.clip_combo.activated.connect(self._clip_source_chosen)
        self.clip_len = W.QDoubleSpinBox()
        self.clip_len.setRange(0.2, Clipper.MAX_SECONDS)
        self.clip_len.setSingleStep(0.5)
        self.clip_len.setDecimals(1)
        self.clip_len.setSuffix(" s")
        self.clip_len.setValue(float(self.store.settings.get("clip_len", 1.0)))
        self.clip_len.valueChanged.connect(self._clip_len_changed)
        clip_row = W.QHBoxLayout()
        clip_row.addWidget(W.QLabel("Length:"))
        clip_row.addWidget(self.clip_len)
        for text, slot in (("✂ Clip", self.clip), ("Replay clip", self.replay_clip),
                           ("Edit clip…", self.edit_clip), ("Save clip", self.save_clip)):
            b = W.QPushButton(text)
            b.clicked.connect(slot)
            clip_row.addWidget(b)
        clip_row.addStretch(1)
        grid.addWidget(W.QLabel("Clip from:"), 2, 0)
        grid.addWidget(self.clip_combo, 2, 1)
        grid.addLayout(clip_row, 2, 2, 1, 3)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        root.addLayout(grid)

        self.route_label = W.QLabel(self.router.message)
        self.statusBar().addPermanentWidget(self.route_label)
        if not self.keys.available:
            self.statusBar().showMessage(
                "Hotkeys only work while this window is focused. For global hotkeys: "
                "sudo usermod -aG input $USER, then log out and back in.")

    def _volume(self, key, default, setter):
        s = QtWidgets.QSlider(Qt.Horizontal)
        s.setRange(0, 100)
        s.setValue(int(self.store.settings.get(key, default)))
        setter(s.value())

        def changed(v):
            setter(v)
            self.store.settings[key] = v
        s.valueChanged.connect(changed)
        s.sliderReleased.connect(self.store.save)
        return s

    # ---- devices ---------------------------------------------------------
    def refresh_mics(self):
        saved = self.store.settings.get("mic", "")
        self.mic_combo.clear()
        self.mic_combo.addItem("None (sounds only)", "")
        for name, desc in self.router.list_mics():
            self.mic_combo.addItem(desc, name)
        idx = max(0, self.mic_combo.findData(saved))
        self.mic_combo.setCurrentIndex(idx)
        self.apply_mic_routing()
        self.clip_combo.clear()
        self.clip_combo.addItem("What I hear (the selected output)", "")
        for i in range(1, self.mic_combo.count()):
            self.clip_combo.addItem("Microphone: " + self.mic_combo.itemText(i),
                                    self.mic_combo.itemData(i))
        self.clip_combo.setCurrentIndex(
            max(0, self.clip_combo.findData(self.store.settings.get("clip_source", ""))))

    def _mic_chosen(self, _i):
        self.store.settings["mic"] = self.mic_combo.currentData()
        self.store.save()
        if getattr(self, "voice_dlg", None):
            self.voice_dlg._restart_live()  # also re-applies the mic routing
        else:
            self.apply_mic_routing()

    def apply_mic_routing(self):
        """Blurt-Mic carries the raw mic, unless the voice changer is feeding it instead."""
        dlg = getattr(self, "voice_dlg", None)
        mic = self.store.settings.get("mic", "")
        self.router.set_mic(None if dlg and dlg.wants_mic() else mic)

    def open_voice(self):
        if not getattr(self, "voice_dlg", None):
            self.voice_dlg = VoiceDialog(self)
        self.voice_dlg.show()
        self.voice_dlg.raise_()
        self.voice_dlg.activateWindow()

    def toggle_voice(self):
        if B1Droid is None or np is None:
            return
        if not getattr(self, "voice_dlg", None):
            self.voice_dlg = VoiceDialog(self)
        self.voice_dlg.toggle_on_mic()
        self.statusBar().showMessage(
            "Voice changer on" if self.voice_dlg.on_mic.isChecked() else "Voice changer off",
            3000)

    def shutdown_voice(self):
        if getattr(self, "voice_dlg", None):
            self.voice_dlg.shutdown()

    def refresh_outputs(self):
        saved = self.store.settings.get("output", "")
        self.out_combo.blockSignals(True)
        self.out_combo.clear()
        self.out_combo.addItem("System default", "")
        self._outputs = {}
        for dev in QMediaDevices.audioOutputs():
            if is_blurt_device(dev):
                continue
            self._outputs[device_id(dev)] = dev
            self.out_combo.addItem(dev.description(), device_id(dev))
        self.out_combo.setCurrentIndex(max(0, self.out_combo.findData(saved)))
        self.out_combo.blockSignals(False)
        self._apply_output()
        if self.router.ok and not self.engine.find_mix():
            self.route_label.setText("Virtual mic not visible yet - click “Refresh devices”.")
        elif self.router.ok:
            self.route_label.setText(self.router.message)

    def _apply_output(self):
        dev = self._outputs.get(self.out_combo.currentData())
        if dev is None:
            dev = QMediaDevices.defaultAudioOutput()
            if is_blurt_device(dev) and self._outputs:
                dev = next(iter(self._outputs.values()))
        self.engine.set_output(dev)
        self.restart_clipper()

    def _output_chosen(self, _i):
        self.store.settings["output"] = self.out_combo.currentData()
        self.store.save()
        self._apply_output()
        if getattr(self, "voice_dlg", None):
            self.voice_dlg._restart_live()  # "hear myself" follows the output

    # ---- folders ---------------------------------------------------------
    def current_folder(self):
        it = self.folders.currentItem()
        return it.data(Qt.UserRole) if it else None

    def refresh_folders(self, select=None):
        if select is None:
            select = self.current_folder()
        self.folders.blockSignals(True)
        self.folders.clear()
        it = QtWidgets.QListWidgetItem("All sounds")
        it.setData(Qt.UserRole, None)
        self.folders.addItem(it)
        row = 0
        for f in sorted(self.store.folders, key=lambda f: f["name"].lower()):
            it = QtWidgets.QListWidgetItem("📁 " + f["name"])
            it.setData(Qt.UserRole, f["id"])
            self.folders.addItem(it)
            if f["id"] == select:
                row = self.folders.count() - 1
        self.folders.setCurrentRow(row)
        self.folders.blockSignals(False)
        self.refresh_sounds()

    def new_folder(self):
        name, ok = QtWidgets.QInputDialog.getText(self, "New folder", "Folder name:")
        if ok and name.strip():
            fid = self.store.folder_by_name(name.strip())
            self.store.save()
            self.refresh_folders(select=fid)

    def _folder_menu(self, pos):
        it = self.folders.itemAt(pos)
        fid = it.data(Qt.UserRole) if it else None
        menu = QtWidgets.QMenu(self)
        menu.addAction("New folder", self.new_folder)
        menu.addAction("Open in file manager", lambda: self._open_folder(fid))
        if fid:
            menu.addAction("Rename", lambda: self._rename_folder(fid))
            menu.addAction("Delete folder (keep sounds)", lambda: self._delete_folder(fid, False))
            menu.addAction("Delete folder and its sounds", lambda: self._delete_folder(fid, True))
        menu.exec(self.folders.mapToGlobal(pos))

    def _open_folder(self, fid):
        d = self.store.folder_dir(fid)
        d.mkdir(parents=True, exist_ok=True)
        show_in_file_manager(d)

    def _rename_folder(self, fid):
        old = self.store.folder_name(fid)
        name, ok = QtWidgets.QInputDialog.getText(self, "Rename folder", "Name:", text=old)
        if ok and name.strip():
            for f in self.store.folders:
                if f["id"] == fid:
                    f["name"] = name.strip()
            self.store.save()
            self.refresh_folders()

    def _delete_folder(self, fid, with_sounds):
        for s in [s for s in self.store.sounds if s.get("folder") == fid]:
            if with_sounds:
                self.store.remove_sound(s["id"])
            else:
                s["folder"] = None
        self.store.folders[:] = [f for f in self.store.folders if f["id"] != fid]
        self.store.save()
        self.rebuild_hotkeys()
        self.refresh_folders(select="")

    # ---- sounds ----------------------------------------------------------
    def _row_id(self, row):
        it = self.table.item(row, 0)
        return it.data(Qt.UserRole) if it else None

    def selected_ids(self):
        return [self._row_id(i.row()) for i in self.table.selectionModel().selectedRows()]

    def visible_sounds(self):
        fid, text = self.current_folder(), self.search.text().strip().lower()
        return [s for s in self.store.sounds
                if (not fid or s.get("folder") == fid) and text in s["name"].lower()]

    def refresh_sounds(self):
        t = self.table
        t.setSortingEnabled(False)
        t.setRowCount(0)
        for s in self.visible_sounds():
            r = t.rowCount()
            t.insertRow(r)
            name = QtWidgets.QTableWidgetItem(s["name"])
            name.setData(Qt.UserRole, s["id"])
            t.setItem(r, 0, name)
            t.setItem(r, 1, QtWidgets.QTableWidgetItem(combo_label(s.get("hotkey", ""))))
            t.setItem(r, 2, QtWidgets.QTableWidgetItem(self.store.folder_name(s.get("folder"))))
        t.setSortingEnabled(True)

    def _sound_menu(self, pos):
        ids = self.selected_ids()
        if not ids:
            return
        menu = QtWidgets.QMenu(self)
        if len(ids) == 1:
            sid = ids[0]
            menu.addAction("Play", lambda: self.play(sid))
            menu.addAction("Set hotkey…", lambda: self.set_sound_hotkey(sid))
            menu.addAction("Rename…", lambda: self._rename_sound(sid))
            menu.addAction("Edit / trim…", lambda: self.edit_sound(sid))
            menu.addAction("Show in file manager",
                           lambda: show_in_file_manager(self.store.path_of(self.store.sound(sid))))
        bound = [i for i in ids if self.store.sound(i).get("hotkey")]
        if bound:
            label = "Remove hotkey"
            if len(ids) == 1:
                label += f" ({combo_label(self.store.sound(ids[0])['hotkey'])})"
            menu.addAction(label, lambda: self.clear_sound_hotkeys(bound))
        move = menu.addMenu("Move to folder")
        move.addAction("(no folder)", lambda: self._move(ids, None))
        for f in sorted(self.store.folders, key=lambda f: f["name"].lower()):
            move.addAction(f["name"], lambda fid=f["id"]: self._move(ids, fid))
        menu.addSeparator()
        menu.addAction("Remove", lambda: self._remove(ids))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _rename_sound(self, sid):
        s = self.store.sound(sid)
        name, ok = QtWidgets.QInputDialog.getText(self, "Rename", "Name:", text=s["name"])
        if ok and name.strip():
            s["name"] = name.strip()
            self.store.save()
            self.refresh_sounds()

    def _move(self, ids, fid):
        for sid in ids:
            self.store.sound(sid)["folder"] = fid
        self.store.save()
        self.refresh_sounds()

    def _remove(self, ids):
        if QtWidgets.QMessageBox.question(
                self, "Remove", f"Remove {len(ids)} sound(s) from Blurt?"
        ) != QtWidgets.QMessageBox.Yes:
            return
        for sid in ids:
            self.store.remove_sound(sid)
        self.store.save()
        self.rebuild_hotkeys()
        self.refresh_sounds()

    # ---- importing -------------------------------------------------------
    def _imported(self, count):
        self.store.save()
        self.refresh_folders()
        self.statusBar().showMessage(f"Imported {count} sound(s).", 5000)

    def import_files(self):
        exts = " ".join("*" + e for e in sorted(AUDIO_EXTS))
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, "Import audio files", str(Path.home()), f"Audio ({exts});;All files (*)")
        fid = self.current_folder()
        self._imported(sum(self.store.import_file(f, fid) for f in files))

    def import_folder(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Import folder", str(Path.home()))
        if d:
            QtWidgets.QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                n = self.store.import_dir(d)
            finally:
                QtWidgets.QApplication.restoreOverrideCursor()
            self._imported(n)

    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        n, fid = 0, self.current_folder()
        for url in ev.mimeData().urls():
            p = url.toLocalFile()
            n += self.store.import_dir(p) if os.path.isdir(p) else self.store.import_file(p, fid)
        self._imported(n)

    # ---- playback --------------------------------------------------------
    def play(self, sid):
        s = self.store.sound(sid) if sid else None
        if not s:
            return
        path = self.store.path_of(s)
        if not path.exists():
            self.statusBar().showMessage(f"File missing for “{s['name']}”.", 5000)
            return
        self.now.setText(s["name"])
        self.engine.play(sid, path)

    def play_selected(self):
        ids = self.selected_ids()
        if ids:
            self.play(ids[0])

    def play_random(self):
        pool = self.visible_sounds() or self.store.sounds
        if pool:
            self.play(random.choice(pool)["id"])

    @staticmethod
    def _fmt(ms):
        s = max(0, ms) // 1000
        return f"{s // 60}:{s % 60:02d}"

    def _on_position(self, pos):
        if not self.seek.isSliderDown():
            self.seek.setValue(pos)
        self.time.setText(f"{self._fmt(pos)} / {self._fmt(self.engine.p_me.duration())}")

    def _on_state(self, state):
        self.pause_btn.setText("▶ Resume" if state == QMediaPlayer.PausedState else "⏸ Pause")
        if state == QMediaPlayer.StoppedState:
            self.now.setText("")
            self.seek.setValue(0)

    # ---- clipping --------------------------------------------------------
    def restart_clipper(self):
        source = self.clip_combo.currentData()
        if not source:  # what plays on the output you listen on
            out = device_id(self.engine.out_me.device())
            if self.tee.active and out:
                self.tee.set_target(out)  # everything on that output except Blurt itself
                source = CAP_SINK + ".monitor"
            else:
                self.tee.set_target(None)
                source = out + ".monitor" if out else "@DEFAULT_MONITOR@"
        else:
            self.tee.set_target(None)
        self.clipper.start(source)

    def _clip_source_chosen(self, _i):
        self.store.settings["clip_source"] = self.clip_combo.currentData()
        self.store.save()
        self.restart_clipper()

    def _clip_len_changed(self, value):
        self.store.settings["clip_len"] = value
        self.store.save()

    def clip(self):
        if not self.clipper.available:
            self.statusBar().showMessage("Clipping needs the 'parec' tool (libpulse).", 6000)
            return
        if not self.clipper.running():
            self.restart_clipper()
        pcm = self.clipper.grab(self.clip_len.value())
        seconds = len(pcm) / (Clipper.RATE * Clipper.FRAME)
        if seconds < 0.05 or pcm.count(0) == len(pcm):
            self.statusBar().showMessage(
                f"Nothing to clip - no sound came from “{self.clip_combo.currentText()}”.", 5000)
            return
        CLIPS_DIR.mkdir(parents=True, exist_ok=True)
        path = CLIPS_DIR / f"clip-{uuid.uuid4().hex[:8]}.wav"
        self.clipper.write_wav(pcm, path)
        old, self.last_clip = self.last_clip, path
        if old and self.engine.current == "clip":
            self.engine.stop()  # the old clip may still be playing
        if old:
            try:
                old.unlink()
            except OSError:
                pass
        self.statusBar().showMessage(f"Clipped {seconds:.1f} s.", 4000)

    def replay_clip(self):
        if not self.last_clip:
            self.statusBar().showMessage("No clip yet.", 4000)
            return
        self.now.setText("Clip")
        self.engine.play("clip", self.last_clip)

    def _clip_name(self):
        return time.strftime("Clip %Y-%m-%d %H.%M.%S")

    def save_clip(self):
        """Button: ask for a name and folder."""
        if not self.last_clip:
            self.statusBar().showMessage("No clip to save yet.", 4000)
            return
        target = self._ask_save("Save clip", self._clip_name(), "Clips")
        if target:
            self._add_sound(*target, src=self.last_clip)

    def save_clip_quick(self):
        """Hotkey: save straight into the Clips folder without a prompt."""
        if not self.last_clip:
            self.statusBar().showMessage("No clip to save yet.", 4000)
            return
        self._add_sound(self._clip_name(), self.store.folder_by_name("Clips"), src=self.last_clip)

    # ---- saving and editing ------------------------------------------------
    def _ask_save(self, title, name, folder):
        """Ask for a name and folder. Returns (name, folder_id) or None."""
        dlg = SaveDialog(self, title, name, [f["name"] for f in self.store.folders], folder)
        ok = dlg.exec() == QtWidgets.QDialog.Accepted
        name, folder = dlg.values()
        dlg.deleteLater()
        if not ok or not name:
            return None
        return name, (self.store.folder_by_name(folder) if folder else None)

    def _add_sound(self, name, folder_id, pcm=None, src=None):
        sid = uuid.uuid4().hex[:12]
        if src is not None:
            fname = f"{sid}{Path(src).suffix.lower()}"
            shutil.copy2(src, SOUNDS_DIR / fname)
        else:
            fname = f"{sid}.wav"
            write_wav(pcm, SOUNDS_DIR / fname)
        self.store.sounds.append({"id": sid, "name": name, "file": fname, "src": "",
                                  "folder": folder_id, "hotkey": ""})
        self.store.save()  # also moves the file into its folder under a readable name
        self._reveal(sid)
        where = self.store.folder_name(folder_id) or "All sounds"
        self.statusBar().showMessage(f"Saved “{name}” in “{where}”.", 6000)

    def _reveal(self, sid):
        """Show the folder a sound is in and select it."""
        s = self.store.sound(sid)
        self.search.clear()
        self.refresh_folders(select=s.get("folder") or "")
        for row in range(self.table.rowCount()):
            if self._row_id(row) == sid:
                self.table.selectRow(row)
                self.table.scrollToItem(self.table.item(row, 0))
                break

    def _open_editor(self, title, path, ask_new, clip=False):
        """Returns (mode, pcm, target); mode is None if cancelled."""
        self.engine.forget()
        dlg = EditorDialog(self, title, path, self.engine.out_me.device(),
                           self.engine.out_me.volume(), ask_new, clip)
        ok = dlg.exec() == QtWidgets.QDialog.Accepted and dlg.mode
        result = (dlg.mode, dlg.pcm(), dlg.target) if ok else (None, None, None)
        dlg.deleteLater()
        if result[0] and not result[1]:
            self.statusBar().showMessage("Nothing to save - the sound is empty.", 5000)
            return None, None, None
        return result

    def edit_selected(self):
        ids = self.selected_ids()
        if len(ids) != 1:
            self.statusBar().showMessage("Select one sound in the list to edit it.", 4000)
            return
        self.edit_sound(ids[0])

    def edit_sound(self, sid):
        s = self.store.sound(sid)
        path = self.store.path_of(s)
        if not path.exists():
            self.statusBar().showMessage(f"File missing for “{s['name']}”.", 5000)
            return
        mode, pcm, target = self._open_editor(
            s["name"], path,
            lambda: self._ask_save("Save as new sound", s["name"] + " (edited)",
                                   self.store.folder_name(s.get("folder"))))
        if mode == "new":
            self._add_sound(*target, pcm=pcm)
        elif mode == "overwrite":
            tmp = f"{sid}-edit.wav"
            write_wav(pcm, SOUNDS_DIR / tmp)
            try:
                path.unlink()
            except OSError:
                pass
            s["file"] = tmp
            self.store.save()  # renames it back to "<name>.wav" in its folder
            self.statusBar().showMessage(f"Saved changes to “{s['name']}”.", 5000)

    def edit_clip(self):
        if not self.last_clip:
            self.statusBar().showMessage("No clip yet.", 4000)
            return
        mode, pcm, target = self._open_editor(
            "last clip", self.last_clip,
            lambda: self._ask_save("Save clip", self._clip_name(), "Clips"), clip=True)
        if mode != "new":
            return
        path = CLIPS_DIR / f"clip-{uuid.uuid4().hex[:8]}.wav"  # "Replay clip" gets the edit too
        write_wav(pcm, path)
        old, self.last_clip = self.last_clip, path
        try:
            old.unlink()
        except OSError:
            pass
        self._add_sound(*target, pcm=pcm)

    # ---- hotkeys ---------------------------------------------------------
    def rebuild_hotkeys(self):
        self.hk = {s["hotkey"]: ("sound", s["id"]) for s in self.store.sounds if s.get("hotkey")}
        for act, combo in self.store.settings["hotkeys"].items():
            if combo:
                self.hk[combo] = ("action", act)

    def _capture(self, title):
        """Returns a combo, "" to clear, or None if cancelled."""
        self.capturing = True
        dlg = HotkeyDialog(self, title)
        ok = dlg.exec() == QtWidgets.QDialog.Accepted
        self.capturing = False
        if not ok:
            return None
        return "" if dlg.cleared else dlg.combo

    def _release(self, combo):
        """Take a combo away from whatever currently uses it."""
        for s in self.store.sounds:
            if s.get("hotkey") == combo:
                s["hotkey"] = ""
        hk = self.store.settings["hotkeys"]
        for act in list(hk):
            if hk[act] == combo:
                hk[act] = ""

    def set_sound_hotkey(self, sid):
        s = self.store.sound(sid)
        combo = self._capture(s["name"])
        if combo is None:
            return
        if combo:
            self._release(combo)
        s["hotkey"] = combo
        self._hotkeys_changed()

    def clear_sound_hotkeys(self, ids):
        for sid in ids:
            self.store.sound(sid)["hotkey"] = ""
        self._hotkeys_changed()
        self.statusBar().showMessage("Hotkey removed.", 4000)

    def set_action_hotkey(self, act):
        combo = self._capture(ACTIONS[act])
        if combo is None:
            return
        if combo:
            self._release(combo)
        self.store.settings["hotkeys"][act] = combo
        self._hotkeys_changed()

    def _hotkeys_changed(self):
        self.store.save()
        self.rebuild_hotkeys()
        self.refresh_sounds()

    def _fill_hk_menu(self):
        self.hk_menu.clear()
        for act, label in ACTIONS.items():
            cur = combo_label(self.store.settings["hotkeys"].get(act, "")) or "not set"
            self.hk_menu.addAction(f"{label}:  {cur}",
                                   lambda a=act: self.set_action_hotkey(a))

    def on_combo(self, combo):
        if self.capturing:
            return
        kind, target = self.hk.get(combo, (None, None))
        if kind == "sound":
            self.play(target)
        elif kind == "action":
            {"stop": self.engine.stop, "pause": self.engine.toggle_pause,
             "random": self.play_random, "clip": self.clip,
             "replay_clip": self.replay_clip, "save_clip": self.save_clip_quick,
             "voice": self.toggle_voice}[target]()

    def eventFilter(self, obj, ev):
        # Fallback when /dev/input is not readable: hotkeys while the window has focus.
        if (ev.type() == QtCore.QEvent.KeyPress and not self.keys.available
                and not self.capturing and isinstance(obj, QtGui.QWindow)
                and not ev.isAutoRepeat()
                and not isinstance(QtWidgets.QApplication.focusWidget(), QtWidgets.QLineEdit)):
            combo = combo_from_qt(ev)
            if combo and combo in self.hk:
                self.on_combo(combo)
                return True
        return False


def main():
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName(APP)
    app.setDesktopFileName("blurt")
    store = Store()
    router = Router()
    router.setup()
    atexit.register(router.teardown)
    engine = Engine()
    keys = GlobalKeys()
    win = MainWindow(store, router, engine, keys)
    win.show()
    # The new virtual sink can take a moment to show up in Qt's device list.
    QtCore.QTimer.singleShot(800, win.refresh_outputs)
    app.aboutToQuit.connect(store.save)
    app.aboutToQuit.connect(win.clipper.stop)
    app.aboutToQuit.connect(win.tee.stop)
    app.aboutToQuit.connect(win.shutdown_voice)
    atexit.register(win.tee.stop)
    app.aboutToQuit.connect(router.teardown)
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    signal.signal(signal.SIGTERM, lambda *_: app.quit())
    tick = QtCore.QTimer()
    tick.timeout.connect(lambda: None)  # lets Python handle Ctrl+C
    tick.start(300)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
