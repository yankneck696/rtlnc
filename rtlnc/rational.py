"""Streaming rational resampler built on scipy's C upfirdn.

The add-ons use it to deliver the core's 240 kS/s IQ at whatever rate the
consumer wants: 2.4 MS/s for an rtl_tcp client, 192 kHz for a sound card,
and so on. Block boundaries are seamless: the output is
identical to resampling the whole stream at once.
"""
from __future__ import annotations

from fractions import Fraction

import numpy as np
from scipy import signal


class RationalResampler:
    def __init__(self, fs_in: float, fs_out: float, taps_per_phase: int = 24,
                 max_den: int = 2000):
        fr = Fraction(fs_out / fs_in).limit_denominator(max_den)
        self.up, self.down = fr.numerator, fr.denominator
        self.fs_in, self.fs_out = fs_in, fs_in * self.up / self.down
        if self.up == self.down == 1:
            self.h = None
            return
        m = max(self.up, self.down)
        n = taps_per_phase * m + 1
        self.h = (signal.firwin(n, 0.92 / m, window=("kaiser", 8.0)) * self.up).astype(np.float32)
        self.min_hist = (len(self.h) + self.up - 1) // self.up + 1
        self.hist = np.zeros(0, dtype=np.complex64)
        self.total_in = 0
        self.next_m = 0

    def process(self, x: np.ndarray) -> np.ndarray:
        if self.h is None:
            return x.astype(np.complex64, copy=False)
        up, down, h = self.up, self.down, self.h
        xx = np.concatenate((self.hist, x.astype(np.complex64, copy=False)))
        g0 = self.total_in - len(self.hist)          # global index of xx[0]; multiple of `down`
        self.total_in += len(x)
        y = signal.upfirdn(h, xx, up, down)
        off = g0 * up // down                        # global output index of y[0]
        ml_lo = 0 if g0 == 0 else -(-(len(h) - 1) // down)   # need full filter history
        ml_hi = (len(xx) * up - 1) // down                    # last output with all inputs present
        ml_lo = max(ml_lo, self.next_m - off)
        out = y[ml_lo: ml_hi + 1] if ml_hi >= ml_lo else y[:0]
        if len(out):
            self.next_m = off + ml_lo + len(out)
        # keep history so the next block's g0 is again a multiple of `down`
        if self.total_in > self.min_hist:
            keep = self.min_hist + ((self.total_in - self.min_hist) % down)
            self.hist = xx[-keep:] if keep <= len(xx) else xx
        else:
            self.hist = xx
        return out.astype(np.complex64)

    @property
    def delay_out(self) -> float:
        """Group delay in output samples."""
        return 0.0 if self.h is None else (len(self.h) - 1) / 2 / self.down
