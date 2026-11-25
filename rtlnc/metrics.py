"""Measure in-channel SINR against simulator ground truth."""
from __future__ import annotations

import numpy as np


def band_sinr(x: np.ndarray, truth: np.ndarray, fs: float, f_lo: float, f_hi: float) -> float:
    """SINR (dB) of x relative to the wanted signal, inside [f_lo, f_hi] Hz."""
    n = min(len(x), len(truth))
    X, T = np.fft.fft(x[:n]), np.fft.fft(truth[:n])
    f = np.fft.fftfreq(n, 1 / fs)
    m = (f >= f_lo) & (f <= f_hi)
    Xb, Tb = X[m], T[m]
    a = np.vdot(Tb, Xb) / np.vdot(Tb, Tb)
    s = np.sum(np.abs(a * Tb) ** 2)
    e = np.sum(np.abs(Xb - a * Tb) ** 2)
    return float(10 * np.log10(s / e))


def run_sim(n_aux=2, seconds=12.0, seed=3, measure_last=5.0, **kw):
    from .pipeline import Pipeline
    from .sim import SimSource
    src_kw = {k: kw.pop(k) for k in list(kw) if k in
              ("ppm_spread", "phase_noise", "noise_to_signal_db", "aux_leak_db",
               "n_sources", "f_tune")}
    src = SimSource(n_aux=n_aux, seed=seed, **src_kw)
    excl = kw.pop("exclude", None)
    fast = kw.pop("fast", None)
    pl = Pipeline(src.fs, src.f_tune, n_aux, listen_offset=src.listen_offset, **kw)
    if fast is not None:
        pl.set_fast(fast)
    if excl is not None:
        pl.set_exclusion(excl)
    outs = []
    for _ in range(int(src.fs * seconds / src.chunk)):
        pl.feed(src.read())
        while (o := pl.step()) is not None:
            outs.append(o)
    nlast = int(measure_last * src.fs / pl.L)
    tail = outs[-nlast:]
    i0 = tail[0]["index"]
    y = np.concatenate([o["y"] for o in tail])
    d = np.concatenate([o["d"] for o in tail])
    truth = src.main_truth()[i0:i0 + len(y)]
    lo, hi = src.listen_offset + 300, src.listen_offset + 2700
    before = band_sinr(d, truth, src.fs, lo, hi)
    after = band_sinr(y, truth, src.fs, lo, hi)
    return {"before_db": before, "after_db": after, "gain_db": after - before,
            "pipeline": pl, "src": src}
