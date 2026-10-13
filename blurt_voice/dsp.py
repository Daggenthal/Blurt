"""Live voice effects that need only numpy (no AI). Mono float32 at 48 kHz.

The core is a streaming phase vocoder that shifts pitch and formants separately: the
spectral envelope (the "shape" of the voice that makes it sound big or small) is measured
with cepstral smoothing, removed, the remaining harmonics are moved to the new pitch, and
the envelope is put back, stretched by its own factor. Effects then shape the result.
"""
import numpy as np

RATE = 48000


class PitchFormantShifter:
    def __init__(self, fft=2048, osamp=4, lifter=56):
        self.fft, self.hop = fft, fft // osamp
        self.osamp = osamp
        self.win = np.hanning(fft + 1)[:-1].astype(np.float32)
        self.bins = fft // 2 + 1
        self.k = np.arange(self.bins, dtype=np.float32)
        self.expect = 2 * np.pi * self.hop / fft
        self.lifter = lifter
        self.inbuf = np.zeros(fft, np.float32)
        self.outacc = np.zeros(fft, np.float32)
        self.last_phase = np.zeros(self.bins, np.float32)
        self.sum_phase = np.zeros(self.bins, np.float32)
        self.pending = np.zeros(0, np.float32)
        self.pitch = 1.0
        self.formant = 1.0
        self.eq = None  # optional per-bin gain curve applied in the spectrum
        self.latency = fft - self.hop

    def _envelope(self, mag):
        ceps = np.fft.irfft(np.log(mag + 1e-7))
        ceps[self.lifter:len(ceps) - self.lifter + 1] = 0
        return np.exp(np.fft.rfft(ceps).real).astype(np.float32)

    def _frame(self):
        spec = np.fft.rfft(self.inbuf * self.win)
        mag = np.abs(spec).astype(np.float32)
        phase = np.angle(spec).astype(np.float32)
        delta = phase - self.last_phase
        self.last_phase = phase
        delta -= self.k * self.expect
        delta -= 2 * np.pi * np.round(delta / (2 * np.pi))
        true_bin = self.k + delta * self.osamp / (2 * np.pi)
        p, f = self.pitch, self.formant
        if p == 1.0 and f == 1.0 and self.eq is None:
            out_mag, out_bin = mag, true_bin
        else:
            env = self._envelope(mag)
            flat = mag / np.maximum(env, 1e-7)
            target = np.rint(self.k * p).astype(np.int64)
            ok = target < self.bins
            out_flat = np.zeros(self.bins, np.float32)
            out_bin = np.zeros(self.bins, np.float32)
            np.add.at(out_flat, target[ok], flat[ok])
            out_bin[target[ok]] = true_bin[ok] * p
            new_env = np.interp(self.k / f, self.k, env, right=env[-1] * 0.01).astype(np.float32)
            out_mag = out_flat * new_env
            if self.eq is not None:
                out_mag = out_mag * self.eq
        self.sum_phase += out_bin * (2 * np.pi / self.osamp)
        self.sum_phase = np.mod(self.sum_phase, 2 * np.pi)
        frame = np.fft.irfft(out_mag * np.exp(1j * self.sum_phase), n=self.fft).astype(np.float32)
        self.outacc += frame * self.win / (1.5 * 1.0)
        out = self.outacc[:self.hop].copy()
        self.outacc = np.concatenate([self.outacc[self.hop:], np.zeros(self.hop, np.float32)])
        return out

    def process(self, x):
        """Any number of samples in, the same number out (delayed by self.latency)."""
        data = np.concatenate([self.pending, x.astype(np.float32)])
        out = []
        pos = 0
        while len(data) - pos >= self.hop:
            self.inbuf = np.concatenate([self.inbuf[self.hop:], data[pos:pos + self.hop]])
            out.append(self._frame())
            pos += self.hop
        self.pending = data[pos:]
        result = np.concatenate(out) if out else np.zeros(0, np.float32)
        # keep output length equal to input length by buffering
        self._outq = np.concatenate([getattr(self, "_outq", np.zeros(0, np.float32)), result])
        if len(self._outq) >= len(x):
            y, self._outq = self._outq[:len(x)], self._outq[len(x):]
        else:  # only at start-up
            y = np.concatenate([np.zeros(len(x) - len(self._outq), np.float32), self._outq])
            self._outq = np.zeros(0, np.float32)
        return y


def _eq_curve(bins, fft, points):
    """points: list of (kind, freq, gain_db, width_octaves)."""
    freqs = np.arange(bins) * RATE / fft
    db = np.zeros(bins)
    for kind, f0, gain, width in points:
        with np.errstate(divide="ignore"):
            octs = np.log2(np.maximum(freqs, 1) / f0)
        if kind == "peak":
            db += gain * np.exp(-0.5 * (octs / width) ** 2)
        elif kind == "highpass":  # gain = slope in dB per octave below f0
            db += np.where(octs < 0, gain * -octs, 0)
        elif kind == "lowpass":
            db += np.where(octs > 0, gain * octs, 0)
    return (10 ** (db / 20)).astype(np.float32)


class B1Droid:
    """Star Wars B1 battle droid: small, nasal, tinny, compressed, with a metallic ring."""

    PRESET = {"pitch": 3, "size": 25, "metal": 60}

    def __init__(self, pitch=3, size=25, metal=60):
        self.shifter = PitchFormantShifter()
        self.shifter.eq = _eq_curve(self.shifter.bins, self.shifter.fft, [
            ("highpass", 320, -24, 0), ("lowpass", 4800, -18, 0),
            ("peak", 1750, 8, 0.45), ("peak", 3100, 3, 0.35), ("peak", 520, -5, 0.5)])
        self.comb_delay = int(RATE / 285)  # a short metal tube resonating around 285 Hz
        self.comb_hist = np.zeros(self.comb_delay, np.float32)
        self.ring_phase = 0.0
        self.env = 0.0
        self.gate = 0.0
        self.set(pitch, size, metal)
        self.latency = self.shifter.latency

    def set(self, pitch=None, size=None, metal=None):
        if pitch is not None:
            self.shifter.pitch = 2 ** (pitch / 12)
        if size is not None:
            self.shifter.formant = 1 + size / 100
        if metal is not None:
            self.metal = metal / 100

    def _comb(self, x, g):
        out = np.empty_like(x)
        d = self.comb_delay
        hist = self.comb_hist
        pos = 0
        while pos < len(x):
            n = min(d, len(x) - pos)
            seg = x[pos:pos + n] + g * hist[:n]
            out[pos:pos + n] = seg
            hist = np.concatenate([hist[n:], seg])
            pos += n
        self.comb_hist = hist
        return out

    def process(self, x):
        x = x.astype(np.float32)
        # quiet input stays quiet: no amplified room noise between words
        level = float(np.sqrt(np.mean(x ** 2)) + 1e-9)
        target = 1.0 if level > 0.004 else 0.0
        self.gate += (target - self.gate) * (0.6 if target else 0.08)
        y = self.shifter.process(x * self.gate)
        g = 0.62 * self.metal
        y = self._comb(y, g) * (1 - 0.45 * g)
        t = np.arange(len(y), dtype=np.float32)
        ring = np.sin(self.ring_phase + 2 * np.pi * 58 * t / RATE)
        self.ring_phase = float((self.ring_phase + 2 * np.pi * 58 * len(y) / RATE) % (2 * np.pi))
        mix = 0.22 * self.metal
        y = y * (1 - mix) + y * ring * mix * 1.6
        y = np.tanh(y * 2.2) / np.tanh(2.2)
        # compressor: droids talk at one level
        rms = float(np.sqrt(np.mean(y ** 2)) + 1e-9)
        coef = 0.5 if rms > self.env else 0.05
        self.env += (rms - self.env) * coef
        gain = min(6.0, 0.16 / max(self.env, 1e-4)) if self.env > 0.01 else 1.0
        return np.clip(y * gain, -0.98, 0.98)


PRESETS = {"b1": ("B1 Battle Droid", B1Droid)}
