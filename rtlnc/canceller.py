"""Joint multi-channel adaptive noise canceller with per-channel phasing control.

    y[n] = main[n - D] - sum_k sum_j w[k, j] * aux_k[n - j]

N auxiliary antennas give N degrees of freedom, so up to N independent noise
sources can be nulled at once. Each aux channel has K taps instead of a
single gain/phase knob, which also covers delay differences and
frequency-dependent coupling across the passband.

Each aux channel can be set to:
  auto    weights solved by least squares (the default)
  manual  one complex weight from the panel's gain and phase knobs, like an
          MFJ-1026 noise channel
  hold    the channel's last weights, frozen
  off     the channel isn't used
The auto channels are always solved jointly *given* the fixed ones:
    w_A = R_AA^-1 (p_A - R_AF w_F)
So you can hand-tune one null and the other dongles re-optimise around it.

Weights are the exponentially weighted least-squares (Wiener) solution,
recomputed every block. Training can leave out the band you are listening
to: the canceller learns the noise coupling from the rest of the spectrum,
so a station that also reaches the aux antennas isn't nulled with the noise.

Separately, an unconstrained solution of every channel is always computed.
Its block-to-block rotation per channel is the error signal for the
clock-tracking FLL: with independent clocks, any carrier offset left after
derotation makes that weight spin at exactly the offset. Because tracking
never depends on the knobs, manual and held settings don't drift.
"""
from __future__ import annotations

import numpy as np

CH_MODES = ("auto", "manual", "hold", "off")


class Canceller:
    def __init__(self, n_aux: int, taps: int = 5, lam: float = 0.9,
                 reg: float = 1e-3):
        if taps < 1 or taps % 2 == 0:
            raise ValueError("taps must be odd and >= 1")
        self.n_aux, self.K, self.lam, self.reg = n_aux, taps, lam, reg
        self.D = (taps - 1) // 2
        self.M = n_aux * taps
        self.train_mask: np.ndarray | None = None
        self.manual = np.zeros(n_aux, dtype=np.complex128)
        # Manual mode applies knob * shape_k: the knobs scale and rotate the
        # channel's own multi-tap filter, captured when you take over, so the
        # hand-over from auto is seamless. The default shape is a single centre tap.
        self.shape = np.zeros((n_aux, taps), dtype=np.complex128)
        self.shape[:, (taps - 1) // 2] = 1.0
        self.ch_mode = ["auto"] * n_aux
        self.bypass = False
        self.adapt = True            # False: hold R/p (e.g. while transmitting)
        # Fast phase lock. The dongles' oscillators are independent, and their
        # phase noise grows with frequency, so at 2 m / 70 cm the relative
        # phase jitters faster than the block-rate solution can follow. A PLL
        # per aux channel, updated every `fast_sub` samples (~2 ms), keeps each
        # stream phase-stable before training and cancelling.
        self.fast_track = False
        self.fast_sub = 512
        self.fast_gain = 0.5
        self.fs = 240e3
        self.reset()

    # ---------------------------------------------------------------- state
    def reset(self) -> None:
        self.R = np.zeros((self.M, self.M), dtype=np.complex128)
        self.p = np.zeros(self.M, dtype=np.complex128)
        self.w_ls = np.zeros(self.M, dtype=np.complex128)
        self.w = np.zeros(self.M, dtype=np.complex128)     # weights actually applied
        self.w_hold = np.zeros(self.M, dtype=np.complex128)
        self.hist_x = np.zeros((self.n_aux, self.K - 1), dtype=np.complex64)
        self.hist_d = np.zeros(self.D, dtype=np.complex64)
        self.blocks = 0
        self.theta = np.zeros(self.n_aux)
        self.jitter_rms = np.zeros(self.n_aux)

    def _sl(self, k: int) -> slice:
        return slice(k * self.K, (k + 1) * self.K)

    @property
    def enabled(self) -> np.ndarray:
        return np.array([m != "off" for m in self.ch_mode])

    # global mode, kept for the console and status line
    @property
    def mode(self) -> str:
        if self.bypass:
            return "bypass"
        live = [m for m in self.ch_mode if m != "off"] or ["off"]
        return live[0] if len(set(live)) == 1 else "mixed"

    @mode.setter
    def mode(self, m: str) -> None:
        if m == "bypass":
            self.bypass = True
            return
        self.bypass = False
        if m == "freeze":
            m = "hold"
        if m in ("auto", "manual", "hold"):
            for k in range(self.n_aux):
                if self.ch_mode[k] != "off":
                    self.set_channel(k, m)

    def freeze(self) -> None:
        self.mode = "hold"

    def set_channel(self, k: int, mode: str) -> None:
        if mode not in CH_MODES:
            raise ValueError(f"channel mode must be one of {CH_MODES}")
        if mode == "hold":
            self.w_hold[self._sl(k)] = self.w[self._sl(k)]
        if mode == "manual" and self.ch_mode[k] != "manual":
            self._capture(k)                                # start from where it was
        self.ch_mode[k] = mode

    def _capture(self, k: int) -> None:
        """Split channel k's present taps into knob value x normalised shape."""
        t = self.w[self._sl(k)]
        tot = t.sum()
        if np.linalg.norm(t) < 1e-12:
            return                                          # nothing yet: keep old knob/shape
        if abs(tot) < 0.2 * np.linalg.norm(t):              # taps nearly cancel: use biggest
            tot = t[np.argmax(np.abs(t))]
        self.shape[k] = t / tot
        self.manual[k] = tot

    def set_manual(self, k: int, gain_db: float | None = None,
                   phase_deg: float | None = None) -> None:
        if self.ch_mode[k] != "manual":
            self._capture(k)            # first touch: start from what the channel does now
        g, p = self.knob(self.manual[k])
        g = g if gain_db is None else gain_db
        p = p if phase_deg is None else phase_deg
        self.manual[k] = 10 ** (g / 20) * np.exp(1j * np.deg2rad(p))
        self.ch_mode[k] = "manual"

    def copy_auto(self, k: int) -> None:
        """Set channel k's knobs and shape from its unconstrained auto solution."""
        sl = self._sl(k)
        saved = self.w[sl].copy()
        self.w[sl] = self.w_ls[sl]
        self._capture(k)
        self.w[sl] = saved
        self.ch_mode[k] = "manual"

    # ---------------------------------------------------------------- dsp
    def _tap_matrix(self, aux: np.ndarray) -> np.ndarray:
        L = aux.shape[1]
        ext = np.concatenate((self.hist_x, aux), axis=1)
        X = np.empty((L, self.M), dtype=np.complex64)
        for k in range(self.n_aux):
            for j in range(self.K):
                s = self.K - 1 - j
                X[:, k * self.K + j] = ext[k, s: s + L]
        if self.K > 1:
            self.hist_x = ext[:, -(self.K - 1):]
        return X

    def _applied(self) -> np.ndarray:
        w = np.zeros(self.M, dtype=np.complex128)
        if self.bypass:
            return w
        auto_idx, fixed_idx = [], []
        for k, m in enumerate(self.ch_mode):
            sl = self._sl(k)
            if m == "auto":
                auto_idx.extend(range(sl.start, sl.stop))
                continue
            fixed_idx.extend(range(sl.start, sl.stop))
            if m == "manual":
                w[sl] = self.manual[k] * self.shape[k]
            elif m == "hold":
                w[sl] = self.w_hold[sl]
        if auto_idx and self.blocks:
            A, F = np.array(auto_idx), np.array(fixed_idx, dtype=int)
            Raa = self.R[np.ix_(A, A)]
            rhs = self.p[A] - (self.R[np.ix_(A, F)] @ w[F] if len(F) else 0)
            tr = np.real(np.trace(Raa)) / len(A)
            w[A] = np.linalg.solve(Raa + (self.reg * tr + 1e-12) * np.eye(len(A)), rhs)
        return w

    def process(self, main: np.ndarray, aux: np.ndarray) -> dict:
        """main: (L,), aux: (n_aux, L). Returns dict with y and diagnostics."""
        L = len(main)
        dd = np.concatenate((self.hist_d, main))[:L]
        if self.D:
            self.hist_d = np.concatenate((self.hist_d, main))[-self.D:]
        if self.fast_track and L % self.fast_sub == 0 and self.blocks > 3:
            aux = self._fast_pll(dd, aux)
        X = self._tap_matrix(aux)
        colmask = np.repeat(self.enabled, self.K)

        w_prev = self.w_ls
        if self.adapt:
            Xa = X * colmask[None, :]
            if self.train_mask is not None:
                Xt = np.fft.ifft(np.fft.fft(Xa, axis=0) * self.train_mask[:, None], axis=0)
                dt = np.fft.ifft(np.fft.fft(dd) * self.train_mask)
            else:
                Xt, dt = Xa, dd
            self.R = self.lam * self.R + (Xt.conj().T @ Xt) / L
            self.p = self.lam * self.p + (Xt.conj().T @ dt) / L
            tr = np.real(np.trace(self.R)) / self.M
            A = self.R + (self.reg * tr + 1e-12) * np.eye(self.M)
            self.w_ls = np.linalg.solve(A, self.p) * colmask
            self.blocks += 1

        rot = np.zeros(self.n_aux)
        for k in range(self.n_aux):
            v = np.vdot(w_prev[self._sl(k)], self.w_ls[self._sl(k)])
            rot[k] = np.angle(v) if abs(v) > 0 else 0.0

        self.w = self._applied()
        y = (dd - X @ self.w).astype(np.complex64)
        c = X.conj().T @ dd / L          # raw tap correlations: timing + coherence
        return {"y": y, "d": dd, "X": X, "w": self.w, "rot": rot, "xcorr": c}

    def _fast_pll(self, dd: np.ndarray, aux: np.ndarray) -> np.ndarray:
        """Per-channel phase-locked loop on the aux streams, updated every
        `fast_sub` samples. The error is the phase between each channel's
        predicted noise (aux * unconstrained LS weights) and what that channel
        should cancel. Knob settings never enter it."""
        L, S, K, n = aux.shape[1], self.fast_sub, self.K, self.n_aux
        ext = np.concatenate((self.hist_x, aux), axis=1).astype(np.complex64)
        W = self.w_ls.reshape(n, K)
        live = np.array([m != "off" and np.any(W[k] != 0) for k, m in enumerate(self.ch_mode)])
        th = self.theta
        jit = []
        for b in range(L // S):
            a0 = K - 1 + b * S
            ext[:, a0:a0 + S] *= np.exp(-1j * th)[:, None].astype(np.complex64)
            if not live.any():
                continue
            Z = np.zeros((n, S), dtype=np.complex128)
            for j in range(K):
                Z += W[:, j:j + 1] * ext[:, a0 - j:a0 - j + S]
            tgt = dd[b * S:(b + 1) * S] - Z.sum(axis=0)
            for k in np.nonzero(live)[0]:
                r = tgt + Z[k]
                c = np.vdot(Z[k], r)
                coh = abs(c) / (np.linalg.norm(Z[k]) * np.linalg.norm(r) + 1e-30)
                if coh > 0.3:
                    e = np.angle(c)
                    th[k] -= self.fast_gain * e
                    jit.append(abs(e))
        self.theta = np.mod(th + np.pi, 2 * np.pi) - np.pi
        if jit:
            self.jitter_rms[:] = 0.9 * self.jitter_rms + 0.1 * float(np.sqrt(np.mean(np.square(jit))))
        return ext[:, K - 1:]

    # ---------------------------------------------------------------- readout
    @staticmethod
    def knob(h: complex) -> tuple[float, float]:
        return 20 * np.log10(abs(h) + 1e-12), float(np.rad2deg(np.angle(h)))

    def channel_knob(self, k: int) -> tuple[float, float]:
        """Equivalent single gain (dB) / phase (deg) of channel k as applied."""
        return self.knob(self.w[self._sl(k)].sum())
