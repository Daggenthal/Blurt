"""AI voice conversion (RVC models) for Blurt.

Loads a standard RVC v1 or v2 voice model (.pth, plus an optional .index) and converts
speech either in one go (for tests and saved sounds) or block by block for the live mic.

The two shared helper networks are downloaded on first use:
  * ContentVec (speech content features), loaded through the transformers library
  * RMVPE (pitch tracking)

Every .pth is loaded with torch.load(weights_only=True), which refuses files that carry
embedded code.
"""
import os
import threading
import time
import urllib.request
from math import gcd
from pathlib import Path

import numpy as np
import torch
import torch.nn.functional as F
from scipy import signal

from .models import (SynthesizerTrnMs256NSFsid, SynthesizerTrnMs256NSFsid_nono,
                     SynthesizerTrnMs768NSFsid, SynthesizerTrnMs768NSFsid_nono)
from .rmvpe import RMVPE

try:
    import faiss  # optional: the .index file makes the voice closer to the original
except ImportError:
    faiss = None

HF = "https://huggingface.co"
ASSETS = {
    "rmvpe.pt": [
        f"{HF}/lj1995/VoiceConversionWebUI/resolve/main/rmvpe.pt",
        f"{HF}/IAHispano/Applio/resolve/main/Resources/predictors/rmvpe.pt",
    ],
    "contentvec/config.json": [
        f"{HF}/lengyue233/content-vec-best/resolve/main/config.json",
        f"{HF}/IAHispano/Applio/resolve/main/Resources/embedders/contentvec/config.json",
    ],
    "contentvec/pytorch_model.bin": [
        f"{HF}/lengyue233/content-vec-best/resolve/main/pytorch_model.bin",
        f"{HF}/IAHispano/Applio/resolve/main/Resources/embedders/contentvec/pytorch_model.bin",
    ],
}
SR = 16000      # what ContentVec and RMVPE work at
WINDOW = 160    # 10 ms per pitch frame
F0_MIN, F0_MAX = 50, 1100
F0_MEL_MIN = 1127 * np.log(1 + F0_MIN / 700)
F0_MEL_MAX = 1127 * np.log(1 + F0_MAX / 700)
_HIGHPASS = signal.butter(N=5, Wn=48, btype="high", fs=SR)


def assets_missing(folder):
    return [name for name in ASSETS if not (Path(folder) / name).is_file()]


def download_assets(folder, progress=None):
    """Fetch the helper networks. progress(name, done_bytes, total_bytes)."""
    folder = Path(folder)
    for name in assets_missing(folder):
        dest = folder / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        last_error = None
        for url in ASSETS[name]:
            tmp = dest.with_suffix(dest.suffix + ".part")
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Blurt"})
                with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
                    total = int(r.headers.get("Content-Length") or 0)
                    done = 0
                    while True:
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        f.write(chunk)
                        done += len(chunk)
                        if progress:
                            progress(name, done, total)
                if total and done != total:
                    raise OSError(f"download of {name} was cut short")
                os.replace(tmp, dest)
                last_error = None
                break
            except Exception as e:  # try the next mirror
                last_error = e
                try:
                    tmp.unlink()
                except OSError:
                    pass
        if last_error is not None:
            raise RuntimeError(f"Could not download {name}: {last_error}")


def resample(x, sr_from, sr_to):
    if sr_from == sr_to:
        return x.astype(np.float32, copy=False)
    g = gcd(int(sr_from), int(sr_to))
    return signal.resample_poly(x, sr_to // g, sr_from // g).astype(np.float32)


class Helpers:
    """ContentVec + RMVPE, shared by every voice model."""

    def __init__(self, folder):
        from transformers import HubertModel
        folder = Path(folder)
        self.hubert = HubertModel.from_pretrained(str(folder / "contentvec"),
                                                  local_files_only=True).eval().float()
        state = torch.load(folder / "contentvec" / "pytorch_model.bin", map_location="cpu",
                           weights_only=True)
        proj = {k.split("final_proj.", 1)[1]: v for k, v in state.items() if "final_proj." in k}
        self.final_proj = None
        if "weight" in proj:  # only v1 models need this projection
            self.final_proj = torch.nn.Linear(proj["weight"].shape[1], proj["weight"].shape[0])
            self.final_proj.load_state_dict(proj)
            self.final_proj.eval()
        self.rmvpe = RMVPE(folder / "rmvpe.pt")

    def features(self, audio16, version):
        x = torch.from_numpy(np.ascontiguousarray(audio16, dtype=np.float32)).view(1, -1)
        with torch.no_grad():
            if version == "v1":
                out = self.hubert(x, output_hidden_states=True)
                feats = out.hidden_states[9]
                if self.final_proj is None:
                    raise RuntimeError("This ContentVec download has no final_proj, which "
                                       "v1 models need.")
                feats = self.final_proj(feats)
            else:
                feats = self.hubert(x).last_hidden_state
        return feats


class RVCVoice:
    def __init__(self, helpers, model_path, index_path=None):
        self.helpers = helpers
        try:
            cpt = torch.load(model_path, map_location="cpu", weights_only=True)
        except Exception as e:
            raise RuntimeError(
                "Blurt could not load this model safely. It is either not an RVC voice "
                f"model or it contains extra data Blurt refuses to run. ({e})") from None
        if not isinstance(cpt, dict) or "weight" not in cpt or "config" not in cpt:
            raise RuntimeError("This .pth is not an RVC voice model (it has no 'weight' and "
                               "'config'). Training checkpoints like G_*.pth or D_*.pth are "
                               "not voice models; use the small exported .pth.")
        config = list(cpt["config"])
        self.tgt_sr = config[-1]
        if isinstance(self.tgt_sr, str):
            self.tgt_sr = {"32k": 32000, "40k": 40000, "48k": 48000}[self.tgt_sr]
        config[-3] = cpt["weight"]["emb_g.weight"].shape[0]
        self.if_f0 = int(cpt.get("f0", 1))
        self.version = cpt.get("version", "v1")
        cls = {("v1", 1): SynthesizerTrnMs256NSFsid, ("v1", 0): SynthesizerTrnMs256NSFsid_nono,
               ("v2", 1): SynthesizerTrnMs768NSFsid, ("v2", 0): SynthesizerTrnMs768NSFsid_nono
               }[(self.version, self.if_f0)]
        net = cls(*config, is_half=False) if self.if_f0 else cls(*config)
        del net.enc_q
        net.load_state_dict({k: v.float() for k, v in cpt["weight"].items()}, strict=False)
        self.net = net.eval().float()
        self.index = self.big_npy = None
        if index_path and faiss is not None and os.path.isfile(index_path):
            self.index = faiss.read_index(str(index_path))
            self.big_npy = self.index.reconstruct_n(0, self.index.ntotal)

    # ---- core ------------------------------------------------------------
    def _f0(self, audio16, shift, f0=None):
        if f0 is None:
            f0 = self.helpers.rmvpe.infer_from_audio(audio16, thred=0.03)
        f0 = f0 * 2 ** (shift / 12)
        f0_mel = 1127 * np.log(1 + f0 / 700)
        voiced = f0_mel > 0
        f0_mel[voiced] = (f0_mel[voiced] - F0_MEL_MIN) * 254 / (F0_MEL_MAX - F0_MEL_MIN) + 1
        f0_mel[f0_mel <= 1] = 1
        f0_mel[f0_mel > 255] = 255
        return np.rint(f0_mel).astype(np.int64), f0.astype(np.float32)

    def _infer(self, audio16, shift, index_rate, protect, keep=None, f0=None):
        """audio16 already padded. keep: fraction of the end to synthesize (live mode).
        f0: pitch track in Hz if the caller already has it."""
        with torch.inference_mode():
            return self._infer_inner(audio16, shift, index_rate, protect, keep, f0)

    def _infer_inner(self, audio16, shift, index_rate, protect, keep, f0):
        feats = self.helpers.features(audio16, self.version)
        use_protect = protect < 0.5 and self.if_f0
        if use_protect:
            feats0 = feats.clone()
        if self.index is not None and index_rate > 0:
            npy = feats[0].numpy()
            score, ix = self.index.search(npy, k=8)
            weight = np.square(1 / np.maximum(score, 1e-9))
            weight /= weight.sum(axis=1, keepdims=True)
            npy = np.sum(self.big_npy[ix] * np.expand_dims(weight, axis=2), axis=1)
            feats = torch.from_numpy(npy.astype(np.float32)).unsqueeze(0) * index_rate \
                + (1 - index_rate) * feats
        feats = F.interpolate(feats.permute(0, 2, 1), scale_factor=2).permute(0, 2, 1)
        if use_protect:
            feats0 = F.interpolate(feats0.permute(0, 2, 1), scale_factor=2).permute(0, 2, 1)
        p_len = min(audio16.shape[0] // WINDOW, feats.shape[1])
        feats = feats[:, :p_len]
        sid = torch.LongTensor([0])
        with torch.no_grad():
            if self.if_f0:
                coarse, f0 = self._f0(audio16, shift, f0)
                pitch = torch.from_numpy(coarse[:p_len]).unsqueeze(0)
                pitchf = torch.from_numpy(f0[:p_len]).unsqueeze(0)
                if pitch.shape[1] < p_len:  # pitch track can be a frame short
                    pad = p_len - pitch.shape[1]
                    pitch = F.pad(pitch, (0, pad), value=1)
                    pitchf = F.pad(pitchf, (0, pad))
                if use_protect:
                    mask = pitchf.clone()
                    mask[pitchf > 0] = 1
                    mask[pitchf < 1] = protect
                    mask = mask.unsqueeze(-1)
                    feats = feats * mask + feats0[:, :p_len] * (1 - mask)
                out = self.net.infer(feats, torch.LongTensor([p_len]), pitch, pitchf, sid,
                                     rate=keep)[0]
            else:
                out = self.net.infer(feats, torch.LongTensor([p_len]), sid, rate=keep)[0]
        return out[0, 0].float().numpy()

    # ---- whole recordings ------------------------------------------------
    def convert(self, audio, sr, shift=0, index_rate=0.75, protect=0.33):
        """Convert a whole recording (mono float32). Returns (audio, sample_rate)."""
        audio16 = resample(audio, sr, SR)
        audio16 = signal.filtfilt(*_HIGHPASS, audio16).astype(np.float32)
        pad = SR // 2  # half a second of reflection padding on each side
        pad_tgt = self.tgt_sr // 2
        pieces, seg = [], 20 * SR
        starts = list(range(0, len(audio16), seg)) or [0]
        bounds = []
        for s in starts:  # cut near the quietest moment around each boundary
            if s == 0:
                bounds.append(0)
                continue
            lo, hi = max(0, s - SR), min(len(audio16), s + SR)
            env = np.convolve(np.abs(audio16[lo:hi]), np.ones(WINDOW), mode="same")
            bounds.append(lo + int(np.argmin(env)))
        bounds.append(len(audio16))
        for a, b in zip(bounds[:-1], bounds[1:]):
            chunk = audio16[a:b]
            if len(chunk) < WINDOW * 4:
                pieces.append(np.zeros(int(len(chunk) * self.tgt_sr / SR), np.float32))
                continue
            padded = np.pad(chunk, (pad, pad), mode="reflect")
            y = self._infer(padded, shift, index_rate, protect)
            pieces.append(y[pad_tgt:len(y) - pad_tgt])
        y = np.concatenate(pieces) if pieces else np.zeros(0, np.float32)
        peak = float(np.abs(y).max()) if len(y) else 0
        if peak > 0.99:
            y = y * (0.99 / peak)
        return y.astype(np.float32), self.tgt_sr


class RVCStream:
    """Live conversion: feed fixed-size blocks of 48 kHz mono audio, get converted blocks back.

    Each block is converted together with some earlier audio as context, then lined up with
    the previous block (SOLA) and crossfaded, the same way RVC's own real-time mode works.
    """

    def __init__(self, voice, block_s=0.4, context_s=1.0, crossfade_s=0.06, sola_s=0.012,
                 shift=0, index_rate=0.75, protect=0.33, threshold_db=-48.0, rate=48000):
        self.voice, self.rate = voice, rate
        self.shift, self.index_rate, self.protect = shift, index_rate, protect
        self.threshold = 10 ** (threshold_db / 20)
        self.block = int(round(block_s * rate / 480)) * 480       # whole 10 ms frames
        self.cross = int(crossfade_s * rate)
        self.sola = int(sola_s * rate)
        self.extra16 = int(round(context_s * SR / WINDOW)) * WINDOW
        tail = self.block + self.cross + self.sola
        self.tail16 = int(np.ceil(tail / 3 / WINDOW)) * WINDOW
        self.buf16 = np.zeros(self.extra16 + self.tail16, np.float32)
        self.prev = np.zeros(self.cross, np.float32)
        # The pitch tracker only looks at the newest audio (plus a little context); older
        # pitch frames are kept from earlier blocks.
        self.f0_ctx16 = int(0.32 * SR)
        self.f0_cache = np.zeros(len(self.buf16) // WINDOW + 1, np.float32)
        fade = np.sin(0.5 * np.pi * np.linspace(0, 1, self.cross, dtype=np.float32)) ** 2
        self.fade_in, self.fade_out = fade, 1 - fade
        self.last_cost = 0.0
        self.lock = threading.Lock()

    def latency(self):
        return (self.block + self.cross) / self.rate + self.last_cost

    def process(self, block):
        """block: float32 array of exactly self.block samples at 48 kHz."""
        t0 = time.perf_counter()
        x16 = resample(block, self.rate, SR)
        n = len(x16)
        self.buf16 = np.concatenate([self.buf16[n:], x16])
        frames = n // WINDOW
        self.f0_cache = np.concatenate([self.f0_cache[frames:], np.zeros(frames, np.float32)])
        if float(np.sqrt(np.mean(block ** 2))) < self.threshold:
            out = np.zeros(self.block, np.float32)
            out[:self.cross] = self.prev * self.fade_out
            self.prev[:] = 0
            self.last_cost = time.perf_counter() - t0
            return out
        audio16 = signal.lfilter(*_HIGHPASS, self.buf16).astype(np.float32)
        keep = (self.tail16 + WINDOW * 2) / len(audio16)
        with self.lock:
            if self.voice.if_f0:
                seg = audio16[-(n + self.f0_ctx16):]
                with torch.inference_mode():
                    f0 = self.voice.helpers.rmvpe.infer_from_audio(seg, thred=0.03)
                self.f0_cache[-(frames + 1):] = f0[-(frames + 1):]
            y = self.voice._infer(audio16, self.shift, self.index_rate, self.protect, keep=keep,
                                  f0=self.f0_cache.copy())
        y = resample(y, self.voice.tgt_sr, self.rate)
        need = self.block + self.cross + self.sola
        if len(y) < need:
            y = np.concatenate([np.zeros(need - len(y), np.float32), y])
        y = y[len(y) - need:]
        # SOLA: find where the new audio lines up best with the end of the last block
        if self.prev.any():
            ref = self.prev
            corr = np.correlate(y[:self.cross + self.sola], ref, mode="valid")
            energy = np.sqrt(np.convolve(y[:self.cross + self.sola] ** 2,
                                         np.ones(self.cross), mode="valid") + 1e-8)
            off = int(np.argmax(corr / energy))
        else:
            off = 0
        out = y[off:off + self.block].copy()
        out[:self.cross] = out[:self.cross] * self.fade_in + self.prev * self.fade_out
        self.prev = y[off + self.block:off + self.block + self.cross].copy()
        if len(self.prev) < self.cross:
            self.prev = np.pad(self.prev, (0, self.cross - len(self.prev)))
        self.last_cost = time.perf_counter() - t0
        return np.clip(out, -1, 1)


def set_threads():
    """Keep torch's default (one thread per physical core) and avoid slow denormal maths."""
    torch.set_flush_denormal(True)
