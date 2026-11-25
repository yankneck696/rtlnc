"""Stateful demodulator: channel select, decimate to audio rate, then
AM, narrow FM (2 m / 70 cm voice), SSB (USB/LSB) or CW.

Default filter widths: SSB 2.6 kHz, CW 500 Hz, AM 9 kHz, FM 16 kHz (12.5/25 kHz
channels, +/-5 kHz deviation). Optional squelch (FM and AM): audio is muted
while the channel power is below the threshold (dBFS).
"""
from __future__ import annotations

import numpy as np
from scipy import signal

MODES = ("am", "fm", "usb", "lsb", "cw")
EXCLUDE_HZ = {"am": 12e3, "fm": 20e3, "usb": 8e3, "lsb": 8e3, "cw": 4e3}


class Demodulator:
    def __init__(self, fs: float, offset: float, mode: str = "usb",
                 audio_rate: float = 48000, cw_pitch: float = 700.0):
        self.dec = int(round(fs / audio_rate))
        if self.dec < 1 or abs(fs / self.dec - audio_rate) > 1:
            raise ValueError(f"fs {fs} must be an integer multiple of {audio_rate}")
        self.fs, self.ar = fs, fs / self.dec
        self.offset, self.cw_pitch = offset, cw_pitch
        self.sos_dec = signal.ellip(8, 0.5, 80, 0.85 * (self.ar / 2) / (fs / 2), output="sos")
        self.zi_dec = np.zeros((self.sos_dec.shape[0], 2), dtype=np.complex128)
        self.n_in = 0
        self.n_out = 0
        self.agc = 1e-3
        self.squelch_db = None            # None = open
        self.level_db = -120.0            # channel power (dBFS), for a meter / squelch
        self.sq_open = True
        self.set_mode(mode)

    def set_mode(self, mode: str) -> None:
        mode = mode.lower()
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        self.mode = mode
        bw = {"usb": 1300, "lsb": 1300, "cw": 250, "am": 4500, "fm": 8000}[mode]
        self.sos_ch = signal.butter(6, bw / (self.ar / 2), output="sos")
        self.zi_ch = np.zeros((self.sos_ch.shape[0], 2), dtype=np.complex128)
        self.dc = 0.0
        # FM: discriminator state, 750 us de-emphasis, 300-3000 Hz voice band
        self.fm_last = 1 + 0j
        a = np.exp(-1 / (self.ar * 750e-6))
        self.deemph = ([1 - a], [1, -a])
        self.zi_de = np.zeros(1)
        self.sos_voice = signal.butter(4, [300 / (self.ar / 2), 3000 / (self.ar / 2)],
                                       btype="band", output="sos")
        self.zi_voice = np.zeros((self.sos_voice.shape[0], 2))

    def process(self, x: np.ndarray) -> np.ndarray:
        n = self.n_in + np.arange(len(x))
        self.n_in += len(x)
        bb = x * np.exp(-2j * np.pi * self.offset * n / self.fs)
        bb, self.zi_dec = signal.sosfilt(self.sos_dec, bb, zi=self.zi_dec)
        # keep the decimation phase continuous across blocks
        start = (-n[0]) % self.dec
        z = bb[start::self.dec]
        t = (self.n_out + np.arange(len(z))) / self.ar
        self.n_out += len(z)
        if self.mode in ("usb", "lsb"):
            s = 1 if self.mode == "usb" else -1
            sh = np.exp(-2j * np.pi * s * 1500 * t)
            z, self.zi_ch = signal.sosfilt(self.sos_ch, z * sh, zi=self.zi_ch)
            a = np.real(z * np.conj(sh))
        elif self.mode == "cw":
            z, self.zi_ch = signal.sosfilt(self.sos_ch, z, zi=self.zi_ch)
            a = np.real(z * np.exp(2j * np.pi * self.cw_pitch * t))
        elif self.mode == "fm":
            z, self.zi_ch = signal.sosfilt(self.sos_ch, z, zi=self.zi_ch)
            prev = np.concatenate(([self.fm_last], z[:-1]))
            self.fm_last = z[-1] if len(z) else self.fm_last
            f = np.angle(z * np.conj(prev)) * self.ar / (2 * np.pi * 5000.0)   # 1.0 = 5 kHz dev
            f, self.zi_de = signal.lfilter(*self.deemph, f, zi=self.zi_de)
            a, self.zi_voice = signal.sosfilt(self.sos_voice, f, zi=self.zi_voice)
            a = a * 3.0
        else:
            z, self.zi_ch = signal.sosfilt(self.sos_ch, z, zi=self.zi_ch)
            e = np.abs(z)
            self.dc = 0.999 * self.dc + 0.001 * float(np.mean(e)) if self.dc else float(np.mean(e))
            a = e - self.dc
        if len(z):
            self.level_db = 0.8 * self.level_db + 0.2 * float(10 * np.log10(np.mean(np.abs(z) ** 2) + 1e-20))
        if self.squelch_db is not None:
            hyst = 2.0 if self.sq_open else 0.0
            self.sq_open = self.level_db > self.squelch_db - hyst
            if not self.sq_open:
                return np.zeros(len(a), dtype=np.float32)
        else:
            self.sq_open = True
        if self.mode == "fm":                      # FM is already level-independent
            return np.clip(a, -1, 1).astype(np.float32)
        # block AGC (fast attack, slow decay)
        pk = float(np.max(np.abs(a))) + 1e-12
        self.agc = pk if pk > self.agc else 0.995 * self.agc + 0.005 * pk
        return np.clip(0.5 * a / self.agc, -1, 1).astype(np.float32)
