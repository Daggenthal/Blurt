"""macOS audio for Blurt: the stand-ins for pacat, parec and module-loopback.

Playback and Recording look like the subprocess.Popen objects Blurt uses on Linux (a
playback has .stdin.write(), a recording has .stdout.read() and a real file descriptor), so
the code that feeds and reads them is the same on both systems. The audio itself is handled
natively by libblurtaudio (see macos/src/blurtaudio.m).
"""
import ctypes
import json
import os
import threading
from pathlib import Path

try:
    import numpy as np
except ImportError:  # Blurt's Mac app always bundles numpy
    np = None

TAP_SOURCE = "@BLURT_TAP@"        # "what I hear": everything the Mac plays except Blurt
DEFAULT_SINK = "@DEFAULT_SINK@"    # the same placeholders Blurt uses with PulseAudio
DEFAULT_SOURCE = "@DEFAULT_SOURCE@"

_HERE = Path(__file__).resolve().parent
_CANDIDATES = (_HERE / "libblurtaudio.dylib",                       # inside Blurt.app
               _HERE.parent / "macos" / "build" / "libblurtaudio.dylib")  # running from source


def _load():
    for path in _CANDIDATES:
        if path.is_file():
            lib = ctypes.CDLL(str(path))
            break
    else:
        raise OSError("libblurtaudio.dylib is missing. Build it with: bash macos/build_app.sh "
                      "--native-only")
    p, i, d, c = ctypes.c_void_p, ctypes.c_int, ctypes.c_double, ctypes.c_char_p
    sig = {
        "ba_last_error": (c, []),
        "ba_version": (i, []),
        "ba_free": (None, [p]),
        "ba_devices_json": (p, []),
        "ba_out_open": (p, [c, d, i, d]),
        "ba_out_write": (i, [p, p, i]),
        "ba_out_abort": (None, [p]),
        "ba_out_close": (None, [p]),
        "ba_in_open": (i, [c, d, i, i, i, ctypes.POINTER(p)]),
        "ba_tap_open": (i, [i, d, i, i, i, ctypes.POINTER(p)]),
        "ba_tap_supported": (i, []),
        "ba_in_close": (None, [p]),
        "ba_in_running": (i, [p]),
        "ba_loop_open": (p, [c, c, d]),
        "ba_loop_close": (None, [p]),
        "ba_keys_access": (i, [i]),
        "ba_keys_start": (i, []),
        "ba_keys_wait": (i, [ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint64), d]),
    }
    for name, (res, args) in sig.items():
        fn = getattr(lib, name)
        fn.restype, fn.argtypes = res, args
    return lib


lib = _load()


def last_error():
    return (lib.ba_last_error() or b"").decode(errors="replace")


def devices():
    """{"devices": [{"uid", "name", "inputs", "outputs", "rate"}], "default_input": uid,
    "default_output": uid}"""
    ptr = lib.ba_devices_json()
    try:
        return json.loads(ctypes.string_at(ptr).decode())
    finally:
        lib.ba_free(ptr)


def tap_supported():
    return bool(lib.ba_tap_supported())


def _uid(device, default):
    return b"" if not device or device == default else str(device).encode()


class Playback:
    """Plays raw PCM written to it (like `pacat --playback`). write() blocks while about
    `latency_ms` of audio is queued, which paces the writer exactly like pacat does."""

    def __init__(self, device, fmt, rate, channels, latency_ms):
        self.fmt, self.channels = fmt, channels
        self.error = ""
        self._lock = threading.Lock()
        self._h = lib.ba_out_open(_uid(device, DEFAULT_SINK), float(rate), channels,
                                  float(latency_ms))
        if not self._h:
            self.error = last_error()

    @property
    def stdin(self):
        return self

    def write(self, data):
        if np is None:
            raise BrokenPipeError("numpy is missing")
        if self.fmt == "s16le":
            x = np.frombuffer(data, dtype="<i2").astype(np.float32) * (1.0 / 32768.0)
        else:
            x = np.ascontiguousarray(np.frombuffer(data, dtype="<f4"), dtype=np.float32)
        frames = len(x) // self.channels
        with self._lock:
            if not self._h or lib.ba_out_write(self._h, x.ctypes.data, frames) < 0:
                raise BrokenPipeError(self.error or "the audio output stopped")
        return len(data)

    def flush(self):
        pass

    def close(self):
        h = self._h
        if h:
            lib.ba_out_abort(h)   # makes a write that is waiting return right away
            with self._lock:      # ...then nothing uses the stream any more
                self._h = None
                lib.ba_out_close(h)

    def terminate(self):
        self.close()

    kill = terminate

    def poll(self):
        return None if self._h else 0

    def wait(self, timeout=None):
        return 0


class Recording:
    """Records raw PCM (like `parec`). `.stdout` is a pipe that delivers `fmt` audio at
    `rate`; it reaches end-of-file when the recording stops, or by itself when the device
    goes away or changes (its sample rate, or for TAP_SOURCE the default output); poll()
    then reports it as ended.

    The first recording makes macOS ask for permission (microphone, or system audio for
    TAP_SOURCE), and Core Audio waits for the answer. So the stream is opened on a
    background thread: the pipe exists right away and audio flows once macOS allows it.
    """

    def __init__(self, device, fmt, rate, channels):
        self.error = ""
        self._lock = threading.Lock()
        self._h = None
        self._closed = False
        r, w = os.pipe()
        self.stdout = os.fdopen(r, "rb")
        args = (device, 1 if fmt == "s16le" else 0, float(rate), channels, w)
        threading.Thread(target=self._open, args=args, daemon=True).start()

    def _open(self, device, fmt_code, rate, channels, w):
        h = ctypes.c_void_p()
        if device == TAP_SOURCE:  # the write end `w` belongs to the native stream now
            ok = lib.ba_tap_open(os.getpid(), rate, channels, fmt_code, w, ctypes.byref(h)) >= 0
        else:
            ok = lib.ba_in_open(_uid(device, DEFAULT_SOURCE), rate, channels, fmt_code, w,
                                ctypes.byref(h)) >= 0
        with self._lock:
            if not ok:  # (the pipe is closed, so the reader sees end-of-file, like parec)
                self.error = last_error()
                self._closed = True
            elif self._closed:  # stopped while waiting for macOS
                lib.ba_in_close(h)
            else:
                self._h = h

    def terminate(self):
        with self._lock:
            self._closed = True
            h, self._h = self._h, None
        if h:
            lib.ba_in_close(h)  # the writer closes its end; readers then see end-of-file

    kill = terminate

    def poll(self):
        with self._lock:
            if self._closed or (self._h and not lib.ba_in_running(self._h)):
                return 0
        return None

    def wait(self, timeout=None):
        return 0


class Loopback:
    """A microphone played into an output device (like PulseAudio's module-loopback).
    Opened in the background, for the same reason as Recording."""

    def __init__(self, source, sink, latency_ms=30):
        self.error = ""
        self._lock = threading.Lock()
        self._h = None
        self._closed = False
        threading.Thread(target=self._open, daemon=True,
                         args=(_uid(source, DEFAULT_SOURCE), _uid(sink, DEFAULT_SINK),
                               float(latency_ms))).start()

    def _open(self, source, sink, latency_ms):
        h = lib.ba_loop_open(source, sink, latency_ms)
        with self._lock:
            if not h:
                self.error = last_error()
                self._closed = True
            elif self._closed:
                lib.ba_loop_close(h)
            else:
                self._h = h

    @property
    def ok(self):
        return not self._closed

    def close(self):
        with self._lock:
            self._closed = True
            h, self._h = self._h, None
        if h:
            lib.ba_loop_close(h)
