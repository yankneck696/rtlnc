"""Simulated N+1 independent-clock dongles, so everything can be tested on a desk.

Scene: one wanted SSB-like signal plus N noise sources (bursty power-line
buzz, switching-supply impulses, broadband hash). The main antenna hears all
of them. Aux antenna k is close to and aimed at noise source k, so it hears
that source strongly, the others weakly, and the wanted signal faintly.

Each simulated dongle k has its own crystal error eps_k. That gives a
sample-rate error fs*eps_k (applied with the same resampler), a carrier
error -f_tune*eps_k, random-walk phase noise, a random start offset, its own
gain and phase, and independent receiver noise.
"""
from __future__ import annotations

import numpy as np
from scipy import signal

from .resample import StreamResampler


class SimSource:
    def __init__(self, n_aux: int = 2, fs: float = 240e3, f_tune: float = 7.1e6,
                 listen_offset: float = 30e3, n_sources: int | None = None,
                 ppm_spread: float = 2.0, phase_noise: float = 0.02,
                 noise_to_signal_db: float = 20.0, aux_leak_db: float = -20.0,
                 rx_noise_db: float = -45.0, chunk: int = 16384, seed: int = 1):
        self.rng = np.random.default_rng(seed)
        self.n_aux, self.fs, self.f_tune = n_aux, fs, f_tune
        self.listen_offset, self.chunk = listen_offset, chunk
        self.ns = n_aux if n_sources is None else n_sources
        self.pn = phase_noise
        self.t = 0                                  # true-time sample counter
        nch = n_aux + 1
        rng = self.rng
        # crystal errors relative to the main dongle
        self.eps = np.r_[0.0, rng.uniform(-ppm_spread, ppm_spread, n_aux) * 1e-6]
        self.cfo = -f_tune * self.eps
        self.theta = rng.uniform(0, 2 * np.pi, nch)
        self.gain = rng.uniform(0.6, 1.4, nch)
        self.pn_state = np.zeros(nch)
        self.osc_n = np.zeros(nch)                  # per-dongle output sample count
        self.start_drop = rng.integers(500, 20000, nch)
        self.rx_noise = 10 ** (rx_noise_db / 20)
        # propagation: short random complex FIR per (antenna, source)
        g_noise = 10 ** (noise_to_signal_db / 20)
        self.H = np.zeros((nch, self.ns, 3), dtype=np.complex128)
        for m in range(self.ns):
            self.H[0, m] = g_noise * self._path(rng)
            for k in range(1, nch):
                strong = (k - 1) == m
                lvl = 3.0 if strong else 10 ** (-20 / 20)
                self.H[k, m] = g_noise * lvl * self._path(rng)
        self.h_des = np.r_[1.0, np.full(n_aux, 10 ** (aux_leak_db / 20))] * \
            np.exp(1j * rng.uniform(0, 2 * np.pi, nch))
        # overall level: main antenna around -16 dBFS so the 8-bit ADC never clips
        self.scale = 0.15 / (g_noise * np.sqrt(self.ns) * 1.5)
        self.zi = np.zeros((nch, self.ns, 2), dtype=np.complex128)
        self.colour = [signal.butter(2, rng.uniform(0.3, 0.8), output="ba")
                       for _ in range(self.ns)]
        self.czi = [np.zeros(2, dtype=np.complex128) for _ in range(self.ns)]
        self.rs = [StreamResampler() for _ in range(nch)]
        self.truth_desired: list[np.ndarray] = []   # main-dongle desired, per sample
        self.dropped = np.zeros(nch, dtype=int)

    @staticmethod
    def _path(rng) -> np.ndarray:
        h = (rng.normal(size=3) + 1j * rng.normal(size=3)) * np.array([0.25, 1.0, 0.35])
        return h / np.linalg.norm(h)

    def _sources(self, n: int) -> np.ndarray:
        t = (self.t + np.arange(n)) / self.fs
        S = np.empty((self.ns, n), dtype=np.complex128)
        for m in range(self.ns):
            w = (self.rng.normal(size=n) + 1j * self.rng.normal(size=n)) / np.sqrt(2)
            b, a = self.colour[m]
            w, self.czi[m] = signal.lfilter(b, a, w, zi=self.czi[m])
            kind = m % 3
            if kind == 0:     # power-line buzz: 120 Hz bursts
                env = 0.3 + 3.0 * (np.sin(2 * np.pi * 120 * t + 0.3) > 0.9)
            elif kind == 1:   # SMPS: sharp impulses at 1.37 kHz
                env = 0.3 + 4.0 * ((np.mod(t * 1370.0, 1.0)) < 0.04)
            else:             # broadband hash, slow wander
                env = 1.0 + 0.5 * np.sin(2 * np.pi * 0.7 * t)
            S[m] = w * env
        return S

    def _desired(self, n: int) -> np.ndarray:
        t = (self.t + np.arange(n)) / self.fs
        f = self.listen_offset
        tones = sum(a * np.exp(2j * np.pi * (f + ft) * t)
                    for a, ft in ((1.0, 600), (0.7, 1150), (0.5, 1900)))
        return tones * (0.6 + 0.4 * np.sin(2 * np.pi * 2.3 * t)) * 0.4

    def read(self) -> list[np.ndarray]:
        n = self.chunk
        S = self._sources(n)
        des = self._desired(n)
        self.t += n
        outs = []
        for k in range(self.n_aux + 1):
            ant = self.h_des[k] * des
            for m in range(self.ns):
                y, self.zi[k, m] = signal.lfilter(self.H[k, m], [1.0], S[m], zi=self.zi[k, m])
                ant = ant + y
            if k == 0:
                truth = self.h_des[0] * des
            # dongle k samples at fs*(1+eps): fewer true samples per output
            rs = self.rs[k]
            ant = ant * self.scale
            rs.push(ant.astype(np.complex64))
            if k == 0:
                self._truth_rs = getattr(self, "_truth_rs", StreamResampler())
                self._truth_rs.push((truth * self.scale).astype(np.complex64))
            r = 1.0 / (1.0 + self.eps[k])
            m_out = rs.available(r)
            x = rs.pull(m_out, r).astype(np.complex128) if m_out else np.zeros(0, complex)
            if k == 0 and m_out:
                tr = self._truth_rs.pull(m_out, r).astype(np.complex128)
            nn = self.osc_n[k] + np.arange(len(x))
            self.osc_n[k] += len(x)
            steps = self.rng.normal(scale=self.pn / np.sqrt(self.fs), size=len(x))
            pn = self.pn_state[k] + np.cumsum(steps)
            if len(x):
                self.pn_state[k] = pn[-1]
            rot = np.exp(1j * (self.theta[k] + pn + 2 * np.pi * self.cfo[k] * nn
                               / (self.fs * (1 + self.eps[k]))))
            x = self.gain[k] * x * rot
            x += self.rx_noise * (self.rng.normal(size=len(x)) + 1j * self.rng.normal(size=len(x)))
            if k == 0 and len(x):
                self.truth_desired.append((self.gain[0] * tr * rot).astype(np.complex64))
            # start-time offset: the first samples of each dongle are "missed"
            drop = min(len(x), int(self.start_drop[k] - self.dropped[k]))
            if drop > 0:
                x = x[drop:]
                self.dropped[k] += drop
                if k == 0:
                    self.truth_desired[-1] = self.truth_desired[-1][drop:]
            # crude 8-bit quantisation, like the RTL2832U
            x = np.round(np.clip(x, -1, 1) * 127) / 127
            outs.append(x.astype(np.complex64))
        return outs

    def main_truth(self) -> np.ndarray:
        return np.concatenate(self.truth_desired)
