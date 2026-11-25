"""Multi-dongle capture with pyrtlsdr async readers (one thread per dongle).

RTL-SDR Blog V4 only: every dongle is checked by its USB strings
("RTLSDRBlog" / "Blog V4") before it is opened. Coverage is 0.5-450 MHz,
HF through 70 cm: below 28.8 MHz through the V4's upconverter, above it
directly.

The dongles are deliberately NOT clock-locked. All of them must use the same
settings, including freq_correction = 0: with identical PLL words, each
dongle's frequency error is exactly proportional to its own crystal error,
which the tracker relies on.
"""
from __future__ import annotations

import queue
import threading
import time

import numpy as np

FREQ_MIN = 0.5e6
FREQ_MAX = 450e6
V4_STRINGS = ("RTLSDRBlog", "Blog V4")


def freq_ok(f: float) -> bool:
    return FREQ_MIN <= f <= FREQ_MAX


def check_v4(specs: list[str]) -> None:
    """Refuse anything that isn't an RTL-SDR Blog V4."""
    from .eeprom import RtlIO
    devs = RtlIO().list()
    if not devs:
        raise RuntimeError("no RTL-SDR found. On Windows, install the WinUSB driver "
                           "for each dongle with Zadig (one time).")
    for spec in specs:
        if spec.startswith("idx:"):
            i = int(spec[4:])
            d = devs[i] if i < len(devs) else None
        else:
            m = [d for d in devs if d.serial == spec]
            if len(m) > 1:
                raise RuntimeError(f"{len(m)} dongles share serial {spec!r}: give each its own "
                                   f"serial with the dongle editor first")
            d = m[0] if m else None
        if d is None:
            raise RuntimeError(f"dongle {spec!r} not found (plugged in? see the dongle editor)")
        if (d.manufacturer, d.product) != V4_STRINGS:
            raise RuntimeError(f"dongle {spec!r} is '{d.manufacturer} {d.product}': "
                               f"only RTL-SDR Blog V4 dongles are supported")


def _open(spec: str):
    from rtlsdr import RtlSdr
    if spec.startswith("idx:"):
        return RtlSdr(device_index=int(spec[4:]))
    return RtlSdr(serial_number=spec)


class MultiRTL:
    def __init__(self, devices: list[str], fs: float, fc: float, gain: list[float],
                 bias_tee: list[bool] | None = None, chunk: int = 16384):
        if not freq_ok(fc):
            raise ValueError(f"frequency must be {FREQ_MIN/1e6:g}-{FREQ_MAX/1e6:g} MHz")
        check_v4(devices)
        self.fs, self.fc, self.chunk = fs, fc, chunk
        self.devs = []
        for i, spec in enumerate(devices):
            d = _open(spec)
            d.sample_rate = fs
            d.center_freq = fc
            try:
                d.set_agc_mode(False)
            except Exception:
                pass
            d.gain = gain[i] if i < len(gain) else gain[-1]   # manual, AGC off
            if bias_tee and i < len(bias_tee) and hasattr(d, "set_bias_tee"):
                d.set_bias_tee(bool(bias_tee[i]))
            self.devs.append(d)
        self.q = [queue.Queue(maxsize=512) for _ in self.devs]
        self.threads: list[threading.Thread] = []
        self.errors: list[str] = []
        self.overflows = [0] * len(self.devs)

    def _reader(self, i: int) -> None:
        def cb(samples, _ctx):
            try:
                self.q[i].put_nowait(np.asarray(samples, dtype=np.complex64))
            except queue.Full:
                self.overflows[i] += 1        # consumer too slow: data lost
        try:
            self.devs[i].read_samples_async(cb, num_samples=self.chunk)
        except Exception as e:                # pragma: no cover - hardware
            self.errors.append(f"dongle {i}: {e}")

    def start(self) -> None:
        for i in range(len(self.devs)):
            t = threading.Thread(target=self._reader, args=(i,), daemon=True)
            t.start()
            self.threads.append(t)

    def read(self, timeout: float = 0.2) -> list[np.ndarray]:
        out = []
        deadline = time.time() + timeout
        for qi in self.q:
            parts = []
            try:
                parts.append(qi.get(timeout=max(0.0, deadline - time.time())))
                while True:
                    parts.append(qi.get_nowait())
            except queue.Empty:
                pass
            out.append(np.concatenate(parts) if parts else np.zeros(0, np.complex64))
        return out

    def retune(self, fc: float) -> None:
        if not freq_ok(fc):
            raise ValueError(f"frequency must be {FREQ_MIN/1e6:g}-{FREQ_MAX/1e6:g} MHz")
        for d in self.devs:
            d.center_freq = fc
        self.fc = fc
        for qi in self.q:                     # drop stale samples
            while not qi.empty():
                qi.get_nowait()

    def close(self) -> None:
        for d in self.devs:
            try:
                d.cancel_read_async()
            except Exception:
                pass
        time.sleep(0.2)
        for d in self.devs:
            try:
                d.close()
            except Exception:
                pass
