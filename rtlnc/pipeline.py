"""Glue: raw dongle streams -> acquisition -> tracking -> cancellation.

Channel 0 is the main (signal) antenna and also the time and frequency
reference. Channels 1..N are the aux (noise) antennas. Each aux channel is
put through, in order:

    raw IQ -> StreamResampler(rate_k) -> NCO derotate(f_k) -> Canceller

and two loops run on it:
    FLL    f_k     += a_f * rot_k / (2 pi T_block)   (rot_k = weight rotation)
    timing pos_k   -= a_t * tau_k * rate_k            (tau_k = fractional lag)
           rate_k   = 1 - f_k / f_tune + integ_k       (clock-linked + PI trim)

The rate comes mostly from f_k. On the V4 the HF carrier error and the
sample-clock error share one TCXO, so eps = -cfo / f_tune. The timing PI
loop trims whatever the linked estimate gets wrong.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import numpy as np
from scipy import ndimage

from .canceller import Canceller
from .resample import StreamResampler
from .sync import AcqResult, envelope_lag, estimate_cfo, fine_lag, derotate


SLIP_KEEP = 1 << 15      # history kept for slip repair (samples)
SLIP_BLOCKS = 4          # blocks used for the slip search


@dataclass
class AuxState:
    rs: StreamResampler = field(default_factory=lambda: StreamResampler(keep=SLIP_KEEP))
    f: float = 0.0           # NCO frequency (Hz) removed from this channel
    phase: float = 0.0
    integ: float = 0.0       # timing-loop integrator (rate trim)
    coh: float = 0.0
    coh_lock: float = 0.0
    tau: float = 0.0
    acq: AcqResult | None = None
    bad: int = 0


class Pipeline:
    def __init__(self, fs: float, f_tune: float, n_aux: int, *, block: int = 8192,
                 taps: int = 5, lam: float = 0.9, acq_len: int = 1 << 17,
                 max_ppm: float = 4.0, listen_offset: float = 30e3,
                 exclude_bw: float = 8e3, fll_gain: float = 0.15,
                 timing_gain: float = 0.05, link_clock: bool = True):
        self.fs, self.f_tune, self.n_aux = fs, f_tune, n_aux
        self.L, self.acq_len = block, acq_len
        self.max_cfo = max(20.0, abs(f_tune) * max_ppm * 1e-6 * 1.5)
        self.listen_offset, self.exclude_bw = listen_offset, exclude_bw
        self.auto_exclude, self.auto_db = False, 10.0
        self.psd: np.ndarray | None = None
        self.auto_mask = np.ones(block)
        self.hold = False            # True while transmitting: no tracking/adaptation
        self.fll_gain, self.timing_gain, self.link = fll_gain, timing_gain, link_clock
        self.canc = Canceller(n_aux, taps=taps, lam=lam)
        self.canc.fs = fs
        self.fast_mode = "auto"         # auto: on above 30 MHz | on | off
        self._apply_fast()
        self.log: list[str] = []
        self.reset_streams()
        self.set_exclusion(exclude_bw)

    # ------------------------------------------------------------------
    def reset_streams(self) -> None:
        self.main = np.zeros(0, dtype=np.complex64)
        self.raw = [np.zeros(0, dtype=np.complex64) for _ in range(self.n_aux)]
        self.aux = [AuxState() for _ in range(self.n_aux)]
        self.state = "acquire"
        self.gen = getattr(self, "gen", -1) + 1
        self.main_consumed = 0      # main samples dropped/consumed so far
        self.out_index = 0          # main-stream index of next output sample
        self.canc.reset()
        self.recent_d: deque = deque(maxlen=SLIP_BLOCKS)
        self.recent_x: deque = deque(maxlen=SLIP_BLOCKS)

    def set_exclusion(self, bw: float | str) -> None:
        """bw in Hz around the listen frequency, 0 = off, or "auto".

        "auto" leaves out of training any narrow peak that stands more than
        auto_db above the local noise floor (stations, carriers), plus the
        listen band when one is known. Use it when an SDR program or the
        radio picks the frequency and the core can't know where you listen.
        """
        if bw == "auto":
            self.auto_exclude = True
            bw = self.exclude_bw if isinstance(self.exclude_bw, (int, float)) else 0
        else:
            self.auto_exclude = False
        self.exclude_bw = float(bw)
        self._listen_mask = np.ones(self.L)
        if self.exclude_bw > 0 and self.listen_offset is not None:
            f = np.fft.fftfreq(self.L, 1 / self.fs)
            self._listen_mask[np.abs(f - self.listen_offset) < self.exclude_bw / 2] = 0.0
        self._apply_mask()

    def _apply_mask(self) -> None:
        m = self._listen_mask * (self.auto_mask if self.auto_exclude else 1.0)
        self.canc.train_mask = None if m.min() == 1.0 else m

    def set_listen(self, offset: float | None) -> None:
        self.listen_offset = offset
        self.set_exclusion("auto" if self.auto_exclude else self.exclude_bw)

    def _update_auto_mask(self, d: np.ndarray) -> None:
        p = np.abs(np.fft.fft(d)) ** 2
        self.psd = p if self.psd is None else 0.8 * self.psd + 0.2 * p
        if self.canc.blocks % 4:
            return
        lp = 10 * np.log10(self.psd + 1e-30)
        floor = ndimage.median_filter(lp, size=257, mode="wrap")
        peak = lp > floor + self.auto_db
        peak = ndimage.binary_dilation(peak, iterations=4)
        self.auto_mask = (~peak).astype(float)
        self._apply_mask()

    def _apply_fast(self) -> None:
        self.canc.fast_track = (self.fast_mode == "on" or
                                (self.fast_mode == "auto" and abs(self.f_tune) > 30e6))
        while self.L % self.canc.fast_sub:
            self.canc.fast_sub //= 2

    def set_fast(self, mode: str) -> None:
        if mode not in ("auto", "on", "off"):
            raise ValueError("fast phase tracking: auto | on | off")
        self.fast_mode = mode
        self._apply_fast()

    def retune(self, f_tune: float, listen_offset: float | None = None) -> None:
        self.f_tune = f_tune
        self._apply_fast()
        if listen_offset is not None:
            self.listen_offset = listen_offset
        self.max_cfo = max(20.0, abs(f_tune) * 4e-6 * 1.5)
        self.set_exclusion(self.exclude_bw)
        self.reset_streams()

    # ------------------------------------------------------------------
    def feed(self, chunks: list[np.ndarray]) -> None:
        self.main = np.concatenate((self.main, chunks[0].astype(np.complex64, copy=False)))
        for k in range(self.n_aux):
            c = chunks[k + 1].astype(np.complex64, copy=False)
            if self.state == "acquire":
                self.raw[k] = np.concatenate((self.raw[k], c))
            else:
                self.aux[k].rs.push(c)

    def rate(self, k: int) -> float:
        a = self.aux[k]
        base = 1.0 - a.f / self.f_tune if self.link else 1.0
        return base + a.integ

    # ------------------------------------------------------------------
    def _acquire(self) -> bool:
        A = self.acq_len
        if len(self.main) < 2 * A or any(len(r) < 2 * A for r in self.raw):
            return False
        lags = []
        for k in range(self.n_aux):
            lag, q = envelope_lag(self.main[:A], self.raw[k][:A])
            lags.append((lag, q))
        d_ref = max(0, max(l for l, _ in lags))
        self.main = self.main[d_ref:]
        self.main_consumed += d_ref
        for k, (lag, q) in enumerate(lags):
            self.raw[k] = self.raw[k][d_ref - lag:]
        if len(self.main) < A or any(len(r) < A for r in self.raw):
            # not enough data after alignment; wait for more (keep alignment)
            self._pending_lags = lags
            return False
        ok_all = True
        for k in range(self.n_aux):
            ref, x = self.main[:A], self.raw[k][:A]
            cfo, cq = estimate_cfo(ref, x, self.fs, self.max_cfo)
            # fine lag needs the derotated (and roughly resampled) signal
            xr = derotate(x, cfo, self.fs)
            fl, coh = fine_lag(ref, xr)
            a = self.aux[k]
            a.f, a.phase, a.integ = cfo, 0.0, 0.0
            a.acq = AcqResult(lags[k][0], lags[k][1], cfo, cq, fl, coh)
            a.coh_lock = coh
            ok_all &= a.acq.ok
            ppm = -cfo / self.f_tune * 1e6
            self.log.append(
                f"acq ch{k+1}: lag {lags[k][0]:+d} smp (q {lags[k][1]:.0f}), "
                f"cfo {cfo:+.2f} Hz (q {cq:.0f}) -> {ppm:+.2f} ppm, "
                f"frac {fl:+.2f}, coh {coh:.2f}" + ("" if a.acq.ok else "  ** WEAK **"))
            # hand the raw stream to the resampler, timing-corrected
            a.rs = StreamResampler(keep=SLIP_KEEP)
            a.rs.push(self.raw[k])
            a.rs.nudge(-fl)
            self.raw[k] = np.zeros(0, dtype=np.complex64)
        if not ok_all:
            self.log.append("acq: weak correlation on some channel(s); make sure "
                            "the noise is present on main AND aux, or inject a "
                            "common calibration noise. Continuing anyway.")
        self.out_index = self.main_consumed
        self.state = "track"
        return True

    # ------------------------------------------------------------------
    def step(self) -> dict | None:
        if self.state == "acquire":
            if not self._acquire():
                return None
        L = self.L
        if len(self.main) < L:
            return None
        rates = [self.rate(k) for k in range(self.n_aux)]
        if any(self.aux[k].rs.available(rates[k]) < L for k in range(self.n_aux)):
            return None

        d = self.main[:L]
        self.main = self.main[L:]
        self.main_consumed += L
        X = np.empty((self.n_aux, L), dtype=np.complex64)
        n = np.arange(L)
        for k in range(self.n_aux):
            a = self.aux[k]
            x = a.rs.pull(L, rates[k])
            ph = a.phase + 2 * np.pi * a.f * n / self.fs
            X[k] = x * np.exp(-1j * ph).astype(np.complex64)
            a.phase = float((a.phase + 2 * np.pi * a.f * L / self.fs) % (2 * np.pi))

        if self.auto_exclude and not self.hold:
            self._update_auto_mask(d)
        self.canc.adapt = not self.hold
        th0 = self.canc.theta.copy()
        r = self.canc.process(d, X)
        if not self.hold:
            self._track(r)
            if self.canc.fast_track:
                # hand the fast PLL's average frequency to the NCO (and clock link)
                dth = np.angle(np.exp(1j * (self.canc.theta - th0)))
                T = self.L / self.fs
                for k, a in enumerate(self.aux):
                    a.f += 0.2 * dth[k] / (2 * np.pi * T)
        out = {"y": r["y"], "d": r["d"], "index": self.out_index - self.canc.D,
               "w": r["w"]}
        self.out_index += L
        return out

    # ------------------------------------------------------------------
    def _track(self, r: dict) -> None:
        K, D, L = self.canc.K, self.canc.D, self.L
        T = L / self.fs
        dd = r["d"]
        self.recent_d.append(dd)
        self.recent_x.append(r["X"][:, D::K].T.copy())     # (n_aux, L), tap D
        pd = np.vdot(dd, dd).real / L + 1e-30
        for k in range(self.n_aux):
            a = self.aux[k]
            c = r["xcorr"][k * K:(k + 1) * K]
            px = np.mean(np.abs(r["X"][:, k * K + D]) ** 2) + 1e-30
            a.coh = float(abs(c[D]) / np.sqrt(pd * px))
            if not self.canc.enabled[k]:
                continue
            gate = a.coh > max(0.05, 0.25 * a.coh_lock)
            if not gate:
                a.bad += 1
                if a.bad >= 3 * SLIP_BLOCKS and len(self.recent_d) == SLIP_BLOCKS:
                    self._repair_slip(k)
                continue
            a.bad = 0
            a.coh_lock = max(0.999 * a.coh_lock, a.coh)
            # FLL from weight rotation (skip the very first blocks: no history)
            if self.canc.blocks > 3:
                # residual +df on aux makes the optimal weight spin at -df
                a.f -= self.fll_gain * r["rot"][k] / (2 * np.pi * T)
            # timing from the correlation peak across taps
            m = np.abs(c)
            j = int(np.argmax(m))
            frac = 0.0
            if 0 < j < K - 1:
                den = m[j - 1] - 2 * m[j] + m[j + 1]
                frac = 0.5 * (m[j - 1] - m[j + 1]) / den if den != 0 else 0.0
            tau = (j + frac) - D            # >0: aux is early, read earlier
            a.tau = 0.9 * a.tau + 0.1 * tau
            if K >= 3:
                kp = self.timing_gain
                a.rs.nudge(-kp * tau)
                a.integ -= (kp * kp / 4) * tau / L
            if abs(a.tau) > max(2, D + 0.5):
                self.log.append(f"ch{k+1}: lost timing (tau {a.tau:+.1f}); re-acquiring")
                self.reset_streams()
                return

    def _repair_slip(self, k: int) -> None:
        """Coherence lost: a dropped USB buffer, or the noise source went quiet.
        Search a wide lag range on recent data. If there is a clean peak away
        from zero, move this channel's read position; otherwise keep going."""
        a = self.aux[k]
        a.bad = 0
        dcat = np.concatenate(list(self.recent_d))
        xcat = np.concatenate([x[k] for x in self.recent_x])
        lag, q = envelope_lag(dcat, xcat)
        if q > 8 and lag != 0 and abs(lag) < SLIP_KEEP:
            a.rs.nudge(-lag * self.rate(k))
            a.tau = 0.0
            self.canc.reset()
            self.recent_d.clear(); self.recent_x.clear()
            self.log.append(f"ch{k+1}: stream slipped {lag:+d} samples (USB drop?) - realigned")

    # ------------------------------------------------------------------
    def status(self) -> list[dict]:
        out = []
        c = self.canc
        for k, a in enumerate(self.aux):
            g, p = c.channel_knob(k)
            ga, pa = c.knob(c.w_ls[k * c.K:(k + 1) * c.K].sum())
            gm, pm = c.knob(c.manual[k])
            out.append({"ch": k + 1, "cfo_hz": a.f, "ppm": -a.f / self.f_tune * 1e6,
                        "rate_ppm": (self.rate(k) - 1) * 1e6, "coh": a.coh,
                        "tau": a.tau, "gain_db": g, "phase_deg": p,
                        "auto_gain_db": ga, "auto_phase_deg": pa,
                        "man_gain_db": gm, "man_phase_deg": pm,
                        "jitter_deg": float(np.rad2deg(c.jitter_rms[k]))})
        return out
