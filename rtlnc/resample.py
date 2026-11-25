"""Streaming fractional resampler (windowed-sinc polyphase).

Independent RTL-SDR dongles each run from their own TCXO, so their sample
clocks differ by up to a few ppm. At 240 kS/s a 2 ppm difference slips about
0.5 sample per second, which would wreck a sample-accurate canceller within
seconds. Every auxiliary stream is therefore resampled onto the main
dongle's time grid at a ratio that the tracker keeps adjusting.

A 32-tap Kaiser-windowed sinc with 4096 phases keeps the interpolation error
around -70 dB or better across 80% of the band, well below the 8-bit ADC's
quantisation floor (about -45 dB).
"""
from __future__ import annotations

import numpy as np

NT = 32            # taps per output sample
NP = 4096          # fractional phases in the table
_J = np.arange(NT) - (NT // 2 - 1)   # tap offsets relative to floor(t)


def _make_table(beta: float = 8.0, cutoff: float = 0.95) -> np.ndarray:
    mu = np.arange(NP + 1) / NP
    t = _J[None, :] - mu[:, None]
    half = NT / 2
    w = np.i0(beta * np.sqrt(np.clip(1 - (t / half) ** 2, 0, None))) / np.i0(beta)
    h = cutoff * np.sinc(cutoff * t) * w
    h /= h.sum(axis=1, keepdims=True)
    return h.astype(np.float32)


TABLE = _make_table()
_PAD = NT // 2 - 1     # samples of history needed before the first position


class StreamResampler:
    """Push input chunks in and pull output blocks at a variable ratio.

    ``rate`` is input samples consumed per output sample (1 + epsilon).
    """

    def __init__(self, keep: int = 0) -> None:
        # ``keep`` samples of history are retained so the read position can be
        # moved backwards (needed to repair a slip in either direction).
        self.keep = keep
        self.buf = np.zeros(_PAD, dtype=np.complex64)
        self.pos = float(_PAD)

    def push(self, x: np.ndarray) -> None:
        if len(x):
            self.buf = np.concatenate((self.buf, x.astype(np.complex64, copy=False)))

    def available(self, rate: float) -> int:
        room = len(self.buf) - NT // 2 - 1 - self.pos
        return 0 if room < 0 else int(room / rate) + 1

    def nudge(self, d: float) -> None:
        """Shift the read position by d input samples (timing correction)."""
        self.pos = max(float(_PAD), self.pos + d)

    def discard(self, n: int) -> None:
        """Advance by n whole input samples (coarse alignment)."""
        self.pos += n
        self._trim()

    def pull(self, n: int, rate: float) -> np.ndarray:
        t = self.pos + np.arange(n) * rate
        i = np.floor(t).astype(np.int64)
        ph = np.rint((t - i) * NP).astype(np.int64)
        out = np.einsum("ij,ij->i", self.buf[i[:, None] + _J[None, :]], TABLE[ph])
        self.pos = float(t[-1] + rate)
        self._trim()
        return out.astype(np.complex64)

    def _trim(self) -> None:
        drop = int(np.floor(self.pos)) - _PAD - self.keep
        if drop > 0:
            drop = min(drop, len(self.buf))
            self.buf = self.buf[drop:]
            self.pos -= drop

    def buffered(self) -> int:
        return len(self.buf)
