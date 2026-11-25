"""Pieces shared by the add-ons: frequency shifter, output leveller, rate converter."""
from __future__ import annotations

import numpy as np

from ..rational import RationalResampler


class Shifter:
    """Continuous-phase frequency shift (positive = move content up)."""

    def __init__(self):
        self.phase = 0.0

    def process(self, x: np.ndarray, shift_hz: float, fs: float) -> np.ndarray:
        if shift_hz == 0:
            return x
        w = 2 * np.pi * shift_hz / fs
        ph = self.phase + w * np.arange(len(x))
        self.phase = float((self.phase + w * len(x)) % (2 * np.pi))
        return (x * np.exp(1j * ph)).astype(np.complex64)


class Leveler:
    """Output level control: fixed gain (dB) or slow AGC towards a target RMS.

    The AGC is deliberately slow (seconds), so the S-meter and the waterfall
    don't pump, and it backs off fast if peaks would clip.
    """

    def __init__(self, target_dbfs: float = -18.0, fixed_db: float | None = None,
                 tau_s: float = 3.0):
        self.target = 10 ** (target_dbfs / 20)
        self.fixed_db = fixed_db
        self.tau = tau_s
        self.g = None

    def process(self, x: np.ndarray, fs: float) -> np.ndarray:
        if self.fixed_db is not None:
            return x * np.float32(10 ** (self.fixed_db / 20))
        rms = float(np.sqrt(np.mean(np.abs(x) ** 2))) + 1e-9
        want = self.target / rms
        if self.g is None:
            self.g = want
        a = min(1.0, len(x) / (fs * self.tau))
        self.g = (1 - a) * self.g + a * want
        pk = float(np.max(np.abs(x))) * self.g
        if pk > 0.95:
            self.g *= 0.95 / pk
        return x * np.float32(self.g)


class Converter:
    """Per-consumer chain: frequency shift -> rate change -> level.

    It rebuilds itself whenever the core re-tunes (``gen`` changes) or the
    rate changes."""

    def __init__(self, fs_out: float, leveler: Leveler | None = None):
        self.fs_out = fs_out
        self.lev = leveler or Leveler()
        self.rs: RationalResampler | None = None
        self.key = None
        self.sh = Shifter()

    def process(self, hdr: dict, iq: np.ndarray, shift_hz: float = 0.0) -> np.ndarray:
        key = (hdr["gen"], hdr["fs"], self.fs_out)
        if key != self.key:
            self.rs = RationalResampler(hdr["fs"], self.fs_out)
            self.key = key
        x = self.sh.process(iq, shift_hz, hdr["fs"])
        y = self.rs.process(x)
        return self.lev.process(y, self.fs_out) if len(y) else y


class SoundOut:
    """Non-blocking sound-card output (mono audio or stereo I/Q float32)."""

    def __init__(self, device, rate: float, channels: int, latency_s: float = 0.25):
        import queue as _q
        import sounddevice as sd
        dev = int(device) if device is not None and str(device).isdigit() else device
        self.ch = channels
        self.q: "_q.Queue" = _q.Queue()
        self.max_frames = int(latency_s * rate * 2)
        self.buf = np.zeros((0, channels), np.float32)
        self.queued = 0
        self.stream = sd.OutputStream(samplerate=int(rate), channels=channels, dtype="float32",
                                      device=dev, blocksize=2048, callback=self._cb)
        self.stream.start()

    def _cb(self, out, frames, _t, _s):
        import queue as _q
        while len(self.buf) < frames:
            try:
                b = self.q.get_nowait()
            except _q.Empty:
                break
            self.queued -= len(b)
            self.buf = np.concatenate((self.buf, b))
        n = min(frames, len(self.buf))
        out[:n] = self.buf[:n]
        out[n:] = 0
        self.buf = self.buf[n:]

    def put(self, frames: np.ndarray) -> None:
        frames = frames.reshape(-1, self.ch).astype(np.float32)
        if self.queued > self.max_frames:      # falling behind: drop to hold latency
            return
        self.queued += len(frames)
        self.q.put(frames)

    def close(self):
        self.stream.stop(); self.stream.close()


def iq_to_stereo(y: np.ndarray, swap: bool = False, gain_db: float = 0.0,
                 phase_deg: float = 0.0) -> np.ndarray:
    """Complex IQ -> (n, 2) float [I, Q], with optional amplitude/phase trim."""
    i, q = y.real, y.imag
    if gain_db or phase_deg:
        ph = np.deg2rad(phase_deg)
        q = 10 ** (gain_db / 20) * (q * np.cos(ph) + i * np.sin(ph))
    st = np.stack((q, i) if swap else (i, q), axis=1)
    return np.clip(st, -1, 1).astype(np.float32)
