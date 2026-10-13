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
from collections import deque
from math import gcd
from pathlib import Path

import numpy as np


def _rocm_override():
    """Many consumer RDNA 3 GPUs (Steam Machine, laptops) are not on ROCm's official list but
    run fine as gfx1100. Set the usual override before torch starts, unless the user did."""
    if "HSA_OVERRIDE_GFX_VERSION" in os.environ:
        return
    try:
        for props in Path("/sys/class/kfd/kfd/topology/nodes").glob("*/properties"):
            for line in props.read_text().splitlines():
                if line.startswith("gfx_target_version"):
                    v = int(line.split()[1])
                    if 110000 <= v < 120000 and v not in (110000, 110001, 110002):
                        os.environ["HSA_OVERRIDE_GFX_VERSION"] = "11.0.0"
                        return
    except (OSError, ValueError):
        pass


_rocm_override()

import torch  # noqa: E402  (after the GPU environment is set up)
import torch.nn.functional as F  # noqa: E402
from scipy import signal

from .models import (SynthesizerTrnMs256NSFsid, SynthesizerTrnMs256NSFsid_nono,
                     SynthesizerTrnMs768NSFsid, SynthesizerTrnMs768NSFsid_nono)
from .rmvpe import RMVPE

try:
    import faiss  # optional: the .index file makes the voice closer to the original
except ImportError:
    faiss = None

from .assets import ASSETS, assets_missing, download_assets  # noqa: F401

SR = 16000      # what ContentVec and RMVPE work at
WINDOW = 160    # 10 ms per pitch frame
F0_MIN, F0_MAX = 50, 1100
F0_MEL_MIN = 1127 * np.log(1 + F0_MIN / 700)
F0_MEL_MAX = 1127 * np.log(1 + F0_MAX / 700)
_HIGHPASS = signal.butter(N=5, Wn=48, btype="high", fs=SR)


def resample(x, sr_from, sr_to):
    if sr_from == sr_to:
        return x.astype(np.float32, copy=False)
    g = gcd(int(sr_from), int(sr_to))
    return signal.resample_poly(x, sr_to // g, sr_from // g).astype(np.float32)


def cpu_has_bf16():
    try:
        flags = Path("/proc/cpuinfo").read_text()
    except OSError:
        return False
    return "avx512_bf16" in flags or "amx_bf16" in flags


GPU_ERROR = ""  # why the graphics card is not used, for the performance report


def pick_device(use_gpu=True):
    """'cuda' (NVIDIA, or AMD through ROCm) when it really works, otherwise 'cpu'."""
    global GPU_ERROR
    GPU_ERROR = ""
    if not use_gpu:
        GPU_ERROR = "turned off in Blurt"
        return "cpu"
    if not (torch.version.hip or torch.version.cuda):
        GPU_ERROR = "this PyTorch is the CPU-only build"
        return "cpu"
    if not torch.cuda.is_available():
        GPU_ERROR = "PyTorch found no usable graphics card"
        return "cpu"
    try:
        a = torch.randn(256, 256, device="cuda")
        torch.cuda.synchronize()
        if torch.isfinite(a @ a).all():
            return "cuda"
        GPU_ERROR = "the graphics card returned wrong results"
    except Exception as e:
        GPU_ERROR = f"the graphics card failed a test: {e}"
    return "cpu"


def gpu_diagnosis():
    """Everything that decides whether PyTorch can use an AMD graphics card, as text lines."""
    import grp
    lines, advice = [], []
    build = "CPU only"
    if torch.version.hip:
        build = f"ROCm {torch.version.hip}"
    elif torch.version.cuda:
        build = f"CUDA {torch.version.cuda} (NVIDIA)"
    lines.append(f"PyTorch {torch.__version__}: {build}")
    if not torch.version.hip:
        advice.append("Install the ROCm build: bash install.sh --ai-voice-gpu (it now removes "
                      "the CPU build first).")
    targets = []
    for props in Path("/sys/class/kfd/kfd/topology/nodes").glob("*/properties"):
        try:
            for line in props.read_text().splitlines():
                if line.startswith("gfx_target_version") and line.split()[1] != "0":
                    v = int(line.split()[1])
                    targets.append(f"gfx{v // 10000}{(v // 100) % 100:x}{v % 100:x}")
        except (OSError, ValueError):
            pass
    lines.append("GPU type (ROCm): " + (", ".join(targets) or "none found"))
    override = os.environ.get("HSA_OVERRIDE_GFX_VERSION")
    lines.append(f"HSA_OVERRIDE_GFX_VERSION: {override or 'not set'}")
    kfd = Path("/dev/kfd")
    if not kfd.exists():
        lines.append("/dev/kfd: missing (the kernel's AMD compute driver is not available)")
        advice.append("/dev/kfd is missing, so ROCm cannot reach the GPU on this system.")
    else:
        ok = os.access(kfd, os.R_OK | os.W_OK)
        lines.append(f"/dev/kfd: {'accessible' if ok else 'no permission'}")
        if not ok:
            advice.append("Add yourself to the render and video groups, then log out and back "
                          "in: sudo usermod -aG render,video $USER")
    render = sorted(Path("/dev/dri").glob("renderD*")) if Path("/dev/dri").exists() else []
    if render:
        ok = all(os.access(r, os.R_OK | os.W_OK) for r in render)
        lines.append(f"{', '.join(r.name for r in render)}: {'accessible' if ok else 'no permission'}")
    try:
        groups = sorted({grp.getgrgid(g).gr_name for g in os.getgroups()})
        lines.append("Your groups: " + ", ".join(groups))
    except (KeyError, OSError):
        pass
    if torch.version.hip:
        try:
            available = torch.cuda.is_available()
            lines.append(f"PyTorch sees a GPU: {'yes' if available else 'no'}")
            if available:
                lines.append(f"GPU name: {torch.cuda.get_device_name(0)}")
            elif targets and not override:
                advice.append(f"Try starting Blurt with HSA_OVERRIDE_GFX_VERSION=11.0.0 "
                              f"(your GPU reports {targets[0]}).")
        except Exception as e:
            lines.append(f"PyTorch GPU check failed: {e}")
    if GPU_ERROR:
        lines.append(f"Why the CPU is used: {GPU_ERROR}")
    return lines, advice


class Helpers:
    """ContentVec + RMVPE, shared by every voice model."""

    def __init__(self, folder, device="cpu", fast_math=True):
        from transformers import HubertModel
        folder = Path(folder)
        self.device = device
        # bfloat16 (CPU with AVX-512 BF16, e.g. Ryzen 7000/9000) or float16 (GPU) roughly
        # halves the work with no audible difference
        self.autocast = None
        if fast_math and device == "cuda":
            self.autocast = ("cuda", torch.float16)
        elif fast_math and cpu_has_bf16():
            self.autocast = ("cpu", torch.bfloat16)
        self.hubert = HubertModel.from_pretrained(str(folder / "contentvec"),
                                                  local_files_only=True).eval().float()
        self.hubert.to(device)
        state = torch.load(folder / "contentvec" / "pytorch_model.bin", map_location="cpu",
                           weights_only=True)
        proj = {k.split("final_proj.", 1)[1]: v for k, v in state.items() if "final_proj." in k}
        self.final_proj = None
        if "weight" in proj:  # only v1 models need this projection
            self.final_proj = torch.nn.Linear(proj["weight"].shape[1], proj["weight"].shape[0])
            self.final_proj.load_state_dict(proj)
            self.final_proj.eval().to(device)
        self.rmvpe = RMVPE(folder / "rmvpe.pt", device=device)

    def maths(self):
        """Context manager for the fast-maths mode (or a no-op)."""
        if self.autocast:
            return torch.autocast(self.autocast[0], dtype=self.autocast[1])
        return torch.autocast("cpu", enabled=False)

    def describe(self):
        where = "graphics card" if self.device == "cuda" else "CPU"
        fast = {torch.float16: ", float16", torch.bfloat16: ", bfloat16"}.get(
            self.autocast[1] if self.autocast else None, "")
        return where + fast

    def features(self, audio16, version):
        x = torch.from_numpy(np.ascontiguousarray(audio16, dtype=np.float32)).view(1, -1)
        x = x.to(self.device)
        with torch.no_grad(), self.maths():
            if version == "v1":
                out = self.hubert(x, output_hidden_states=True)
                feats = out.hidden_states[9]
                if self.final_proj is None:
                    raise RuntimeError("This ContentVec download has no final_proj, which "
                                       "v1 models need.")
                feats = self.final_proj(feats)
            else:
                feats = self.hubert(x).last_hidden_state
        return feats.float()


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
        self.device = helpers.device
        self.net = net.eval().float().to(self.device)
        self.index = self.big_npy = None
        if index_path and faiss is not None and os.path.isfile(index_path):
            self.index, self.big_npy = load_index(index_path)

    # ---- core ------------------------------------------------------------
    def _f0(self, audio16, shift, f0=None):
        if callable(f0):  # computed alongside the speech features; wait for it here
            f0 = f0()
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

    def _sync(self):
        if self.device == "cuda":
            torch.cuda.synchronize()  # so GPU timings are real, not just queued work

    def _infer_inner(self, audio16, shift, index_rate, protect, keep, f0):
        times = self.times = {}
        t = time.perf_counter()
        feats = self.helpers.features(audio16, self.version)
        self._sync()
        times["features"] = time.perf_counter() - t
        t = time.perf_counter()
        use_protect = protect < 0.5 and self.if_f0
        if use_protect:
            feats0 = feats.clone()
        if self.index is not None and index_rate > 0:
            npy = feats[0].cpu().numpy()
            score, ix = self.index.search(npy, k=8)
            weight = np.square(1 / np.maximum(score, 1e-9))
            weight /= weight.sum(axis=1, keepdims=True)
            npy = np.sum(self.big_npy[ix] * np.expand_dims(weight, axis=2), axis=1)
            feats = torch.from_numpy(npy.astype(np.float32)).unsqueeze(0).to(self.device) \
                * index_rate \
                + (1 - index_rate) * feats
        times["index"] = time.perf_counter() - t
        feats = F.interpolate(feats.permute(0, 2, 1), scale_factor=2).permute(0, 2, 1)
        if use_protect:
            feats0 = F.interpolate(feats0.permute(0, 2, 1), scale_factor=2).permute(0, 2, 1)
        p_len = min(audio16.shape[0] // WINDOW, feats.shape[1])
        feats = feats[:, :p_len]
        dev = self.device
        sid = torch.LongTensor([0]).to(dev)
        lengths = torch.LongTensor([p_len]).to(dev)
        with torch.no_grad(), self.helpers.maths():
            if self.if_f0:
                t = time.perf_counter()
                coarse, f0 = self._f0(audio16, shift, f0)
                times["pitch"] = time.perf_counter() - t
                pitch = torch.from_numpy(coarse[:p_len]).unsqueeze(0).to(dev)
                pitchf = torch.from_numpy(f0[:p_len]).unsqueeze(0).to(dev)
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
                t = time.perf_counter()
                out = self.net.infer(feats, lengths, pitch, pitchf, sid, rate=keep)[0]
            else:
                t = time.perf_counter()
                out = self.net.infer(feats, lengths, sid, rate=keep)[0]
            self._sync()
            times["synthesis"] = time.perf_counter() - t
        return out[0, 0].float().cpu().numpy()

    # ---- whole recordings ------------------------------------------------
    def convert(self, audio, sr, shift=0, index_rate=0.75, protect=0.33):
        """Convert a whole recording (mono float32). Returns (audio, sample_rate)."""
        audio16 = resample(level_input(audio), sr, SR)
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
                 shift=0, index_rate=0.75, protect=0.33, threshold_db=-54.0, rate=48000):
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
        self.stats = {}   # step name -> recent timings in seconds (for the performance window)
        self.silent_blocks = 0
        self.level = 0.0  # running loudness of speech, for automatic input gain
        self.hold = 0     # blocks to keep converting after speech goes quiet

    def latency(self):
        return (self.block + self.cross) / self.rate + self.last_cost

    def process(self, block):
        """block: float32 array of exactly self.block samples at 48 kHz."""
        t0 = time.perf_counter()
        rms = float(np.sqrt(np.mean(block ** 2)))
        if rms >= self.threshold:  # quiet mics convert badly, so bring speech up to a steady level
            self.level = rms if self.level == 0 else self.level + (rms - self.level) * 0.2
        gain = min(8.0, max(1.0, TARGET_RMS / self.level)) if self.level else 1.0
        x16 = resample(np.clip(block * gain, -1, 1), self.rate, SR)
        n = len(x16)
        self.buf16 = np.concatenate([self.buf16[n:], x16])
        frames = n // WINDOW
        self.f0_cache = np.concatenate([self.f0_cache[frames:], np.zeros(frames, np.float32)])
        if rms >= self.threshold:
            self.hold = max(1, int(round(0.6 * self.rate / self.block)))
        elif self.hold > 0:
            self.hold -= 1
        if rms < self.threshold and self.hold == 0:
            out = np.zeros(self.block, np.float32)
            out[:self.cross] = self.prev * self.fade_out
            self.prev[:] = 0
            self.last_cost = time.perf_counter() - t0
            self.silent_blocks += 1
            return out
        audio16 = signal.lfilter(*_HIGHPASS, self.buf16).astype(np.float32)
        keep = (self.tail16 + WINDOW * 2) / len(audio16)
        prep = time.perf_counter() - t0
        pitch_t = [0.0]
        with self.lock:
            f0_arg = self.f0_cache.copy()
            if self.voice.if_f0:
                seg = audio16[-(n + self.f0_ctx16):]

                def track():
                    t = time.perf_counter()
                    with torch.inference_mode():
                        f = self.voice.helpers.rmvpe.infer_from_audio(seg, thred=0.03)
                    pitch_t[0] = time.perf_counter() - t
                    return f
                # pitch tracking and speech features don't depend on each other, so the pitch
                # runs on a second thread while the features are computed
                future = _pitch_pool().submit(track)

                def f0_arg():
                    f = future.result()
                    self.f0_cache[-(frames + 1):] = f[-(frames + 1):]
                    return self.f0_cache.copy()
            y = self.voice._infer(audio16, self.shift, self.index_rate, self.protect, keep=keep,
                                  f0=f0_arg)
            steps = dict(self.voice.times)
        t = time.perf_counter()
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
        steps["pitch_wait"] = steps.get("pitch", 0.0)  # time spent waiting for the pitch
        steps["pitch"] = pitch_t[0]
        steps["audio"] = prep + (time.perf_counter() - t)
        steps["total"] = self.last_cost
        for name, value in steps.items():
            hist = self.stats.setdefault(name, deque(maxlen=60))
            hist.append(value)
        return np.clip(out, -1, 1)

    STEP_NAMES = {
        "features": "Speech features (ContentVec)",
        "index": "Voice match (.index search)",
        "pitch": "Pitch tracking (RMVPE, in parallel)",
        "pitch_wait": "  waiting for pitch afterwards",
        "synthesis": "Voice synthesis (the model)",
        "audio": "Resampling, level and blending",
        "total": "Total per block",
    }

    def report_rows(self):
        """[(step, average ms, worst ms, share of the time budget %)] over recent blocks."""
        budget = self.block / self.rate
        rows = []
        for key, label in self.STEP_NAMES.items():
            hist = self.stats.get(key)
            if not hist:
                continue
            avg, worst = float(np.mean(hist)), float(np.max(hist))
            rows.append((key, label, avg * 1000, worst * 1000, avg / budget * 100))
        return rows


TARGET_RMS = 0.08


def level_input(audio):
    """Bring a quiet recording up to a normal speaking level (never turns loud audio down)."""
    audio = np.asarray(audio, dtype=np.float32)
    frames = audio[:len(audio) // 480 * 480].reshape(-1, 480) if len(audio) >= 480 else audio[None]
    rms = np.sqrt((frames ** 2).mean(axis=1))
    voiced = rms[rms > 10 ** (-48 / 20)]
    if not len(voiced):
        return audio
    gain = min(8.0, max(1.0, TARGET_RMS / float(np.median(voiced))))
    peak = float(np.abs(audio).max())
    if peak * gain > 0.95:
        gain = max(1.0, 0.95 / peak)
    return audio * gain


def voice_pitch(helpers, audio, sr):
    """Median speaking pitch in Hz of a recording, or 0 if no voice was found."""
    audio16 = resample(audio, sr, SR)
    with torch.inference_mode():
        f0 = helpers.rmvpe.infer_from_audio(audio16, thred=0.03)
    voiced = f0[f0 > 0]
    if len(voiced) <= 20:
        return 0.0
    pitch = float(np.median(voiced))
    return pitch if 60 <= pitch <= 600 else 0.0  # outside human speech: don't trust it


def benchmark(voice, block_s=0.45, context_s=1.0):
    """Seconds of processing per second of live audio on this computer (lower is better)."""
    stream = RVCStream(voice, block_s=block_s, context_s=context_s)
    t = np.arange(stream.block * 3) / stream.rate
    test = (0.1 * np.sign(np.sin(2 * np.pi * 140 * t)) * np.sin(2 * np.pi * 3 * t) ** 2
            ).astype(np.float32)
    costs = []
    for i in range(3):
        stream.process(test[i * stream.block:(i + 1) * stream.block])
        costs.append(stream.last_cost)
    return min(costs[1:]) / block_s


def load_index(path):
    """Read a .index for fast searching. Returns (index, all stored vectors).

    RVC indexes are normally "IVF" indexes searched with nprobe=1, which is quick. Some
    shared models come with a plain (flat) index of every training frame instead; searching
    that takes over a second per live block, so Blurt builds a quick IVF copy once and keeps
    it next to the original as <name>.blurt-fast.index.
    """
    index = faiss.read_index(str(path))
    big_npy = index.reconstruct_n(0, index.ntotal)
    try:
        faiss.extract_index_ivf(index).nprobe = 1
        return index, big_npy
    except (RuntimeError, AttributeError):
        pass  # not an IVF index
    if index.ntotal <= 20000:
        return index, big_npy
    fast_path = Path(str(path) + ".blurt-fast.index")
    try:
        if fast_path.is_file() and fast_path.stat().st_mtime >= Path(path).stat().st_mtime:
            fast = faiss.read_index(str(fast_path))
            faiss.extract_index_ivf(fast).nprobe = 1
            return fast, big_npy
    except (RuntimeError, OSError):
        pass
    vecs = np.ascontiguousarray(big_npy, dtype=np.float32)
    nlist = int(min(4096, max(64, 4 * np.sqrt(len(vecs)))))
    quantizer = faiss.IndexFlatL2(vecs.shape[1])
    fast = faiss.IndexIVFFlat(quantizer, vecs.shape[1], nlist)
    rng = np.random.default_rng(0)
    train = vecs[rng.choice(len(vecs), min(len(vecs), nlist * 40), replace=False)]
    fast.train(train)
    fast.add(vecs)
    fast.nprobe = 1
    try:
        faiss.write_index(fast, str(fast_path))
    except RuntimeError:
        pass
    return fast, big_npy


_POOL = None


def _pitch_pool():
    global _POOL
    if _POOL is None:
        from concurrent.futures import ThreadPoolExecutor
        _POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="blurt-pitch")
    return _POOL


def set_threads():
    """One thread per physical core for torch, and avoid slow denormal maths."""
    torch.set_flush_denormal(True)
    try:
        cores = len({tuple(Path(f"/sys/devices/system/cpu/cpu{c}/topology/thread_siblings_list")
                           .read_text().strip().split(","))
                     for c in os.sched_getaffinity(0)})
        torch.set_num_threads(max(1, cores))
    except (OSError, AttributeError):
        pass
    if faiss is not None:
        faiss.omp_set_num_threads(1)
