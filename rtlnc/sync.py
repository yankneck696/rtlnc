"""Acquisition: find each aux dongle's start offset and frequency offset.

With no shared clock every aux dongle differs from the main dongle in
  * start time  - USB streams start milliseconds apart (thousands of samples)
  * carrier     - the V4 upconverter and tuner LOs both come from the dongle's
                  own 28.8 MHz TCXO, so the net HF error is about -f_tune*eps.
                  At 28 MHz with 3 ppm between dongles that is about 85 Hz.
  * sample rate - fs*(1+eps); the same eps as the carrier error.

All three are estimated from the noise you want to cancel: it reaches both
antennas, so it is the common signal.

1. Lag: cross-correlate |x|^2 envelopes. Magnitude does not depend on carrier
   offset, so this works before the frequency offset is known. Impulsive
   noise (power lines, switching supplies) gives sharp peaks.
2. CFO: at that lag, correlate short sub-blocks (shorter than 1/CFO), then
   FFT the sequence of sub-block correlations. The peak is the offset.
3. Fine lag: after derotation, a complex correlation over a few lags,
   refined with parabolic interpolation.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


def _nextpow2(n: int) -> int:
    return 1 << (int(n) - 1).bit_length()


def envelope_lag(ref: np.ndarray, x: np.ndarray) -> tuple[int, float]:
    """Return (lag, peak-to-median ratio) such that ref[n + lag] ~ x[n]."""
    a = np.abs(ref).astype(np.float64) ** 2
    b = np.abs(x).astype(np.float64) ** 2
    a -= a.mean()
    b -= b.mean()
    n = _nextpow2(len(a) + len(b))
    c = np.fft.irfft(np.fft.rfft(a, n) * np.conj(np.fft.rfft(b, n)), n)
    # keep lags within +/- 3/4 of the window so overlap stays meaningful
    lim = int(0.75 * min(len(a), len(b)))
    lags = np.r_[np.arange(0, lim), np.arange(-lim, 0)]
    vals = np.r_[c[:lim], c[n - lim:]]
    k = int(np.argmax(vals))
    q = float(vals[k] / (np.median(np.abs(vals)) + 1e-30))
    return int(lags[k]), q


def estimate_cfo(ref: np.ndarray, x: np.ndarray, fs: float, max_cfo: float) -> tuple[float, float]:
    """Frequency (Hz) of x relative to ref, assuming they are time-aligned.

    Returns (cfo_hz, quality); quality is the spectral peak over its median.
    """
    sub = int(fs / (3.0 * max_cfo))
    sub = int(np.clip(sub, 32, 8192))
    ns = min(len(ref), len(x)) // sub
    r = ref[: ns * sub].reshape(ns, sub)
    y = x[: ns * sub].reshape(ns, sub)
    c = np.sum(r * np.conj(y), axis=1)            # ~ exp(-j 2 pi cfo t_s)
    nfft = max(4096, _nextpow2(ns) * 16)
    spec = np.abs(np.fft.fft(c, nfft)) ** 2
    k = int(np.argmax(spec))
    # parabolic refinement
    a, b, d = spec[(k - 1) % nfft], spec[k], spec[(k + 1) % nfft]
    den = a - 2 * b + d
    frac = 0.5 * (a - d) / den if den != 0 else 0.0
    f_sub = ((k + frac + nfft / 2) % nfft - nfft / 2) / nfft   # cycles per sub-block
    cfo = -f_sub * fs / sub
    q = float(b / (np.median(spec) + 1e-30))
    return float(cfo), q


def fine_lag(ref: np.ndarray, x: np.ndarray, span: int = 4) -> tuple[float, float]:
    """Sub-sample lag (ref[n + lag] ~ x[n]) near zero, plus coherence."""
    n = min(len(ref), len(x)) - 2 * span
    r = ref[span: span + n]
    vals = []
    for lag in range(-span, span + 1):
        vals.append(np.vdot(x[span - lag: span - lag + n], r))
    m = np.abs(np.array(vals))
    k = int(np.argmax(m))
    frac = 0.0
    if 0 < k < len(m) - 1:
        a, b, d = m[k - 1], m[k], m[k + 1]
        den = a - 2 * b + d
        frac = 0.5 * (a - d) / den if den != 0 else 0.0
    coh = m[k] / np.sqrt(np.vdot(r, r).real * np.vdot(x, x).real + 1e-30)
    return float(k - span + frac), float(coh)


@dataclass
class AcqResult:
    lag: int           # ref[n + lag] ~ aux[n]
    lag_q: float
    cfo_hz: float      # aux content sits cfo_hz above main content
    cfo_q: float
    frac_lag: float
    coherence: float

    @property
    def ok(self) -> bool:
        return self.lag_q > 6.0 and self.cfo_q > 8.0 and self.coherence > 0.05


def derotate(x: np.ndarray, f_hz: float, fs: float, phase0: float = 0.0) -> np.ndarray:
    n = np.arange(len(x))
    return (x * np.exp(-1j * (phase0 + 2 * np.pi * f_hz * n / fs))).astype(np.complex64)
