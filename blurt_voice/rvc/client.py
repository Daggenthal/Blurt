"""Talks to the AI voice engine process (see worker.py). Imports no torch."""
import os
import pickle
import struct
import subprocess
import sys
import threading
from pathlib import Path


class EngineError(RuntimeError):
    pass


class EngineClient:
    def __init__(self, app_dir, log_path):
        self.app_dir = str(app_dir)
        self.log_path = Path(log_path)
        self.proc = None
        self.lock = threading.Lock()

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def _spawn(self):
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if self.log_path.exists() and self.log_path.stat().st_size > 2 * 1048576:
                self.log_path.replace(self.log_path.with_suffix(".old.log"))
        except OSError:
            pass
        env = dict(os.environ)
        env["PYTHONPATH"] = self.app_dir + os.pathsep + env.get("PYTHONPATH", "")
        env.setdefault("PYTHONUNBUFFERED", "1")
        log = open(self.log_path, "ab")
        self.proc = subprocess.Popen([sys.executable, "-m", "blurt_voice.rvc.worker"],
                                     cwd=self.app_dir, env=env, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=log)
        log.close()

    def log_tail(self, lines=12):
        try:
            text = self.log_path.read_text(errors="replace").splitlines()
        except OSError:
            return ""
        return "\n".join(text[-lines:])

    def call(self, cmd, *args, progress=None):
        with self.lock:
            if not self.running():
                self._spawn()
            data = pickle.dumps((cmd, args), protocol=pickle.HIGHEST_PROTOCOL)
            try:
                self.proc.stdin.write(struct.pack("<Q", len(data)) + data)
                self.proc.stdin.flush()
                while True:
                    head = self.proc.stdout.read(8)
                    if len(head) < 8:
                        raise EOFError
                    (n,) = struct.unpack("<Q", head)
                    kind, value = pickle.loads(self.proc.stdout.read(n))
                    if kind == "progress":
                        if progress:
                            progress(value)
                        continue
                    if kind == "err":
                        raise EngineError(value)
                    return value
            except (EOFError, BrokenPipeError, OSError):
                code = self.proc.wait(timeout=5) if self.proc else None
                self.proc = None
                why = f"exit code {code}"
                if code is not None and code < 0:
                    why = f"killed by signal {-code}"
                raise EngineError(f"The voice engine stopped unexpectedly ({why}). "
                                  f"Details are in {self.log_path}.") from None

    def stop(self):
        with self.lock:
            if self.proc is not None:
                try:
                    self.proc.stdin.close()
                except OSError:
                    pass
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                self.proc = None
