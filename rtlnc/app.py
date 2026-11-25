"""rtlnc core: dongles -> noise canceller -> IQ bus (+ optional local audio).

    rtlnc run   --devices MAIN AUX1 AUX2 --freq 7.160e6 --mode lsb
    rtlnc check --devices MAIN AUX1 AUX2 --freq 7.160e6
    rtlnc run   --sim 2                           (no hardware needed)
    rtlnc list

The core always publishes the cancelled IQ on the bus (default
127.0.0.1:7355). The add-ons are separate programs that connect to it:
    rtlnc-tcp    rtl_tcp server for SDR#, SDR++, GQRX, SDR Console, HDSDR...
    rtlnc-sound  IQ and/or demodulated audio to a sound card or virtual cable
    rtlnc-rig    follow the radio's frequency and PTT through Hamlib rigctld

Console commands (type and press Enter):
    auto | freeze | bypass | manual        canceller mode
    g <ch> <dB>   p <ch> <deg>             manual gain/phase per aux channel
    off <ch> | on <ch>                     drop/restore an aux channel
    tune <Hz>                              dongle centre frequency
    listen <Hz>                            listen frequency (retunes if needed)
    mode usb|lsb|cw|am                     local demodulator
    ab                                     local audio: cancelled <-> raw main
    x <Hz>|auto                            training exclusion (0 = off)
    ptt on|off                             simulate transmit hold
    q                                      quit
"""
from __future__ import annotations

import argparse
import os
import queue
import sys
import threading
import time
import wave

import numpy as np

from .bus import DEFAULT_PORT, BusServer, parse_addr
from .capture import FREQ_MAX, FREQ_MIN, MultiRTL, freq_ok
from .demod import EXCLUDE_HZ, MODES, Demodulator
from .pipeline import Pipeline


def _parse() -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="rtlnc", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["run", "check", "list"])
    ap.add_argument("--devices", nargs="+", default=[],
                    help="serials, names from rtlnc-devices, or idx:N; FIRST = main, rest = aux. "
                         "Omit to use the roles set with rtlnc-devices (main, aux1, aux2...)")
    ap.add_argument("--sim", type=int, default=0, metavar="N_AUX",
                    help="simulator with N aux dongles instead of hardware")
    ap.add_argument("--freq", type=float, default=7.160e6, help="listen frequency, Hz")
    ap.add_argument("--offset", type=float, default=30e3,
                    help="dongles tune this far below the listen freq (keeps DC spike out)")
    ap.add_argument("--fs", type=float, default=240e3, help="sample rate (240k recommended)")
    ap.add_argument("--gain", type=float, nargs="+", default=None,
                    help="tuner gain dB, one for all or one per device (main first); "
                         "default: per-dongle gain from rtlnc-devices, else 30")
    ap.add_argument("--bias-tee", default=None,
                    help="comma list of device positions to power (0 = main); "
                         "default: per-dongle setting from rtlnc-devices")
    ap.add_argument("--mode", default="usb", choices=list(MODES),
                    help="am, fm (narrow, 2 m / 70 cm voice), usb, lsb, cw")
    ap.add_argument("--squelch", type=float, default=None, help="squelch level dBFS (default off)")
    ap.add_argument("--fast-phase", default="auto", choices=["auto", "on", "off"],
                    help="fast per-channel phase lock (auto: on above 30 MHz)")
    ap.add_argument("--taps", type=int, default=5, help="FIR taps per aux channel (odd)")
    ap.add_argument("--lam", type=float, default=0.9, help="LS forgetting factor per block")
    ap.add_argument("--block", type=int, default=8192)
    ap.add_argument("--exclude", default="mode",
                    help="training exclusion: 'mode' (width suits the mode), Hz around the "
                         "listen freq, 0=off, or 'auto' (skip strong carriers)")
    ap.add_argument("--bus", default=f"127.0.0.1:{DEFAULT_PORT}",
                    help="IQ bus address for add-ons ([host]:port; 0.0.0.0:7355 = LAN)")
    ap.add_argument("--no-audio", action="store_true", help="no local demodulated audio")
    ap.add_argument("--audio-device", default=None, help="sound device name/index for local audio")
    ap.add_argument("--wav", help="also record demodulated audio to this WAV file")
    ap.add_argument("--iq-out", help="write cancelled IQ (complex64, fs) to this file")
    ap.add_argument("--seconds", type=float, default=0, help="stop after this long (0 = run)")
    a = ap.parse_args()
    if a.cmd == "check":
        a.no_audio = True
        a.seconds = a.seconds or 20
    return a


def _console(cmds: queue.Queue) -> None:
    if sys.stdin is None or getattr(sys.stdin, "closed", False):
        return
    for line in sys.stdin:
        line = line.strip()
        if line:
            cmds.put({"cmd": "_text", "text": line})


class Audio:
    def __init__(self, rate: float, enabled: bool, device=None):
        self.q: queue.Queue = queue.Queue(maxsize=64)
        self.stream = None
        self.buf = np.zeros(0, np.float32)
        if not enabled:
            return
        try:
            import sounddevice as sd
            dev = int(device) if device is not None and str(device).isdigit() else device
            self.stream = sd.OutputStream(samplerate=int(rate), channels=1, dtype="float32",
                                          blocksize=1024, callback=self._cb, device=dev)
            self.stream.start()
        except Exception as e:      # pragma: no cover
            print(f"[audio] unavailable ({e}); running without local audio")
            self.stream = None

    def _cb(self, out, frames, _t, _s):
        while len(self.buf) < frames:
            try:
                self.buf = np.concatenate((self.buf, self.q.get_nowait()))
            except queue.Empty:
                break
        n = min(frames, len(self.buf))
        out[:n, 0] = self.buf[:n]
        out[n:, 0] = 0
        self.buf = self.buf[n:]

    def put(self, a: np.ndarray) -> None:
        if self.stream is not None:
            try:
                self.q.put_nowait(a)
            except queue.Full:
                pass

    def close(self):
        if self.stream is not None:
            self.stream.stop(); self.stream.close()


def _db(x: float) -> float:
    return 10 * np.log10(x + 1e-30)


class Core:
    def __init__(self, a: argparse.Namespace):
        self.a = a
        self.fc = a.freq - a.offset
        if not freq_ok(self.fc):
            sys.exit(f"frequency must be within {FREQ_MIN/1e6:g}-{FREQ_MAX/1e6:g} MHz")
        if a.sim:
            from .sim import SimSource
            self.src = SimSource(n_aux=a.sim, fs=a.fs, f_tune=self.fc, listen_offset=a.offset)
            self.n_aux = a.sim
            self.names = ["SIM-MAIN"] + [f"Sim noise {k}" for k in range(1, a.sim + 1)]
        else:
            from . import devconfig
            cfg = devconfig.load()
            devs = [devconfig.resolve(d, cfg) for d in a.devices] or devconfig.from_roles(cfg)
            if len(devs) < 2:
                sys.exit("No dongles assigned: rtlnc needs a 'main' dongle and at least one "
                         "'aux1'. Set the roles in 'Dongles & serials...' (rtlnc-devices web), "
                         "or give --devices. To try without hardware use Demo mode (--sim 2).")
            labels = [cfg["devices"].get(d, {}) for d in devs]
            gains = a.gain or [float(l.get("gain", 30.0)) for l in labels]
            if a.bias_tee is not None:
                bt = [False] * len(devs)
                for i in filter(None, a.bias_tee.split(",")):
                    bt[int(i)] = True
            else:
                bt = [bool(l.get("bias_tee", False)) for l in labels]
            self.names = [devconfig.label(d, cfg) for d in devs]
            print("dongles: " + ", ".join(f"{'MAIN' if i == 0 else f'AUX{i}'}={n} ({d}, {g:g} dB"
                                          f"{', bias-T' if b else ''})"
                                          for i, (n, d, g, b) in enumerate(zip(
                                              self.names, devs,
                                              gains + [gains[-1]] * len(devs), bt))))
            self.src = MultiRTL(devs, a.fs, self.fc, gains, bias_tee=bt)
            self.src.start()
            self.n_aux = len(devs) - 1
        self.pl = Pipeline(a.fs, self.fc, self.n_aux, block=a.block, taps=a.taps,
                           lam=a.lam, listen_offset=a.offset)
        self.excl_by_mode = a.exclude == "mode"
        self.pl.set_exclusion("auto" if a.exclude == "auto" else
                              EXCLUDE_HZ[a.mode] if self.excl_by_mode else float(a.exclude))
        self.pl.set_fast(a.fast_phase)
        self.listen = a.freq
        self.dem = Demodulator(a.fs, a.offset, a.mode)
        self.dem.squelch_db = a.squelch
        self.audio = Audio(self.dem.ar, not a.no_audio, a.audio_device)
        self.bus = BusServer(*parse_addr(a.bus))
        self.cmds: queue.Queue = queue.Queue()
        self.ptt = False
        self.ab_raw = False
        self.quit = False
        self.events: list[str] = []
        # under the launcher the status shows in its window; log a line only every 30 s
        self.stat_every = 30.0 if os.environ.get("RTLNC_LOG") else 1.0
        self.win = np.zeros(0)

    # ---------------------------------------------------------------- control
    def retune(self, fc: float) -> None:
        if not freq_ok(fc):
            print(f"  ignored tune to {fc/1e6:.4f} MHz: outside {FREQ_MIN/1e6:g}-{FREQ_MAX/1e6:g} MHz")
            return
        self.fc = fc
        if self.a.sim:
            self.src.f_tune = fc
            self.src.cfo = -fc * self.src.eps
        else:
            self.src.retune(fc)
        self.pl.retune(fc, listen_offset=self.listen - fc if self.listen else None)
        sq = self.dem.squelch_db
        self.dem = Demodulator(self.a.fs, self.listen - fc if self.listen else self.a.offset,
                               self.dem.mode)
        self.dem.squelch_db = sq
        print(f"  tuned dongles to {fc/1e6:.4f} MHz (re-acquiring)")

    def set_listen(self, f: float) -> None:
        self.listen = f
        off = f - self.fc
        edge = self.a.fs / 2 - 15e3
        if abs(off) > edge or abs(off) < 3e3:
            self.retune(f - self.a.offset)
            return
        self.pl.set_listen(off)
        self.dem.offset = off

    def handle(self, c: dict) -> None:
        cmd = c.get("cmd")
        if cmd == "_text":
            return self.handle_text(c["text"])
        try:
            if cmd == "tune":
                fc = float(c["fc"])
                if abs(fc - self.fc) > 1:
                    self.retune(fc)
            elif cmd == "listen":
                self.set_listen(float(c["freq"]))
            elif cmd == "ptt":
                self.set_ptt(bool(c.get("on")))
            elif cmd == "mode":
                self.set_mode(str(c["mode"]))
            elif cmd == "demod":
                self.set_demod(str(c["mode"]))
            elif cmd == "squelch":
                v = c.get("db")
                self.dem.squelch_db = None if v is None else float(v)
            elif cmd == "fast":
                self.pl.set_fast(str(c["mode"]))
            elif cmd == "quit":
                self.quit = True
            elif cmd == "chan":
                self.set_chan(c)
            elif cmd == "lam":
                self.pl.canc.lam = float(np.clip(float(c["value"]), 0.5, 0.999))
            elif cmd == "reacquire":
                self.pl.reset_streams()
            elif cmd == "exclude":
                bw = c["bw"]
                self.excl_by_mode = bw == "mode"
                self.pl.set_exclusion("auto" if bw == "auto" else
                                      EXCLUDE_HZ[self.dem.mode] if self.excl_by_mode else float(bw))
        except (KeyError, ValueError, TypeError) as e:
            print(f"  bad bus command {c}: {e}")

    def set_demod(self, m: str) -> None:
        self.dem.set_mode(m)
        if self.excl_by_mode and not self.pl.auto_exclude:
            self.pl.set_exclusion(EXCLUDE_HZ[m])
        print(f"  mode {m.upper()}")

    def set_mode(self, m: str) -> None:
        if m in ("auto", "bypass", "manual", "freeze", "hold"):
            self.pl.canc.mode = m
        elif m == "active":                 # leave bypass, keep per-channel modes
            self.pl.canc.bypass = False

    def set_chan(self, c: dict) -> None:
        canc = self.pl.canc
        k = int(c["ch"]) - 1
        if not 0 <= k < self.n_aux:
            raise ValueError(f"no aux channel {k + 1}")
        if c.get("copy_auto"):
            canc.copy_auto(k)
        if "gain_db" in c or "phase_deg" in c:
            canc.set_manual(k, c.get("gain_db"), c.get("phase_deg"))
        if "mode" in c:
            canc.set_channel(k, str(c["mode"]))

    def set_ptt(self, on: bool) -> None:
        if on == self.ptt:
            return
        self.ptt = on
        # Hold: no adaptation and no clock tracking. The weights in use stay as
        # they were, because R/p stop updating; knob changes still apply.
        self.pl.hold = on
        print("  PTT", "ON - canceller held, outputs muted" if on else "off - resuming")

    def handle_text(self, line: str) -> None:
        c = line.split()
        canc = self.pl.canc
        try:
            if c[0] == "q":
                self.quit = True
            elif c[0] in ("auto", "bypass", "manual", "freeze", "hold"):
                self.set_mode(c[0])
            elif c[0] == "g":
                canc.set_manual(int(c[1]) - 1, gain_db=float(c[2]))
            elif c[0] == "p":
                canc.set_manual(int(c[1]) - 1, phase_deg=float(c[2]))
            elif c[0] in ("off", "on"):
                canc.set_channel(int(c[1]) - 1, "off" if c[0] == "off" else "auto")
            elif c[0] == "tune":
                self.retune(float(c[1]))
            elif c[0] == "listen":
                self.set_listen(float(c[1]))
            elif c[0] == "mode":
                self.set_demod(c[1])
            elif c[0] == "sq":
                self.dem.squelch_db = None if c[1] == "off" else float(c[1])
            elif c[0] == "fast":
                self.pl.set_fast(c[1])
            elif c[0] == "ab":
                self.ab_raw = not self.ab_raw
                print("[A/B] local audio:", "RAW main" if self.ab_raw else "CANCELLED")
            elif c[0] == "x":
                self.excl_by_mode = c[1] == "mode"
                self.pl.set_exclusion("auto" if c[1] == "auto" else
                                      EXCLUDE_HZ[self.dem.mode] if self.excl_by_mode else float(c[1]))
            elif c[0] == "ptt":
                self.set_ptt(c[1] == "on")
            else:
                print("?", line)
        except (IndexError, ValueError) as e:
            print("bad command:", e)

    # ---------------------------------------------------------------- status
    SPEC_BINS = 512

    @staticmethod
    def _fresh() -> dict:
        return dict(pin=0.0, pout=0.0, sd=0.0, sy=0.0, n=0)

    def publish_status(self, fast: dict) -> None:
        pl = self.pl
        st = pl.status()
        for s_, m, nm in zip(st, pl.canc.ch_mode, self.names[1:]):
            s_["mode"] = m
            s_["name"] = nm
        hdr = {"channels": [{k: (round(float(v), 3) if not isinstance(v, str) else v)
                             for k, v in s_.items()} for s_ in st],
               "lam": pl.canc.lam, "taps": pl.canc.K, "block": pl.L,
               "exclude": "auto" if pl.auto_exclude else pl.exclude_bw,
               "bypass": pl.canc.bypass, "events": self.events[-20:],
               "main_name": self.names[0],
               "squelch": self.dem.squelch_db, "sq_open": self.dem.sq_open,
               "level_db": round(self.dem.level_db, 1),
               "fast": self.pl.fast_mode, "fast_on": self.pl.canc.fast_track,
               "fmin": FREQ_MIN, "fmax": FREQ_MAX, "excl_mode": self.excl_by_mode}
        if fast["n"]:
            hdr["cancel_db"] = round(_db(fast["pin"] / fast["pout"]), 2)
            nb = self.SPEC_BINS
            for key, name in (("sd", "spec_in"), ("sy", "spec_out")):
                sp = np.fft.fftshift(fast[key] / fast["n"])
                sp = sp[: len(sp) // nb * nb].reshape(nb, -1).mean(axis=1)
                hdr[name] = np.round(10 * np.log10(sp + 1e-20), 1).tolist()
        self.bus.publish(self.header("status", **hdr))

    # ---------------------------------------------------------------- loop
    def header(self, kind: str, **kw) -> dict:
        return {"type": kind, "fs": self.a.fs, "fc": self.fc, "gen": self.pl.gen,
                "listen": self.listen, "ptt": self.ptt, "mode": self.pl.canc.mode,
                "demod": self.dem.mode, "state": self.pl.state, **kw}

    def run(self) -> None:
        a, pl = self.a, self.pl
        threading.Thread(target=_console, args=(self.cmds,), daemon=True).start()
        wav = None
        if a.wav:
            wav = wave.open(a.wav, "wb"); wav.setnchannels(1); wav.setsampwidth(2)
            wav.setframerate(int(self.dem.ar))
        iqf = open(a.iq_out, "wb") if a.iq_out else None
        print(f"rtlnc core: {self.n_aux} aux, listen {self.listen/1e6:.4f} MHz, "
              f"fs {a.fs/1e3:.0f} kS/s, bus {self.bus.addr[0]}:{self.bus.addr[1]}. Acquiring...")
        t0 = t_stat = t_fast = time.time()
        acc = dict(pin=0.0, pout=0.0, n=0)
        fast = self._fresh()
        shown = 0
        try:
            while not self.quit:
                for q in (self.cmds, self.bus.commands):
                    while not q.empty():
                        self.handle(q.get_nowait())
                chunks = self.src.read()
                if a.sim:
                    time.sleep(len(chunks[0]) / a.fs * 0.95)
                pl.feed(chunks)
                while (o := pl.step()) is not None:
                    y, d = o["y"], o["d"]
                    self.bus.publish(self.header("iq", index=int(o["index"])), y)
                    aud = self.dem.process(d if self.ab_raw else y)
                    if self.ptt:
                        aud = aud * 0
                    self.audio.put(aud)
                    if wav:
                        wav.writeframes((aud * 32767).astype("<i2").tobytes())
                    if iqf:
                        y.astype(np.complex64).tofile(iqf)
                    pd, py = float(np.mean(np.abs(d) ** 2)), float(np.mean(np.abs(y) ** 2))
                    acc["pin"] += pd; acc["pout"] += py; acc["n"] += 1
                    fast["pin"] += pd; fast["pout"] += py
                    win = self.win if len(self.win) == len(d) else np.hanning(len(d))
                    self.win = win
                    fast["sd"] = fast["sd"] + np.abs(np.fft.fft(d * win)) ** 2
                    fast["sy"] = fast["sy"] + np.abs(np.fft.fft(y * win)) ** 2
                    fast["n"] += 1
                while shown < len(pl.log):
                    print("  " + pl.log[shown]); self.events.append(pl.log[shown]); shown += 1
                for m in self.bus.drain_log():
                    print("  " + m); self.events.append(m)
                if not a.sim and self.src.errors:
                    sys.exit("\n".join(self.src.errors))
                now = time.time()
                if now - t_fast > 0.1:
                    self.publish_status(fast)
                    fast = self._fresh()
                    t_fast = now
                if now - t_stat > self.stat_every:
                    st = pl.status()
                    if acc["n"]:
                        canc_db = _db(acc["pin"] / acc["pout"])
                        ch = "  ".join(f"[{s['ch']}] {pl.canc.ch_mode[s['ch']-1]:6s} {s['ppm']:+.2f}ppm "
                                       f"coh {s['coh']:.2f} {s['gain_db']:+.1f}dB/{s['phase_deg']:+.0f}°"
                                       for s in st)
                        print(f"{pl.canc.mode:6s}{' PTT' if self.ptt else ''} "
                              f"suppression {canc_db:5.1f} dB | {ch}")
                    acc = dict(pin=0.0, pout=0.0, n=0)
                    t_stat = now
                if a.seconds and now - t0 > a.seconds:
                    break
        except KeyboardInterrupt:
            pass
        finally:
            self.audio.close()
            self.bus.close()
            if wav:
                wav.close()
            if iqf:
                iqf.close()
            if not a.sim:
                self.src.close()
        if a.cmd == "check":
            print("\nSummary (aux relative to main):")
            for s in pl.status():
                ok = "OK" if s["coh"] > 0.2 else "WEAK - aux does not hear the noise main hears"
                print(f"  ch{s['ch']}: {s['ppm']:+.2f} ppm ({s['cfo_hz']:+.2f} Hz), "
                      f"coherence {s['coh']:.2f}  {ok}")
            if not a.sim and any(self.src.overflows):
                print("  USB overflows:", self.src.overflows, "- use a faster host or lower --fs")


def main() -> None:
    a = _parse()
    if a.cmd == "list":
        from rtlsdr import RtlSdr
        for i, s in enumerate(RtlSdr.get_device_serial_addresses()):
            print(f"idx:{i}  serial {s}")
        return
    Core(a).run()


if __name__ == "__main__":
    main()
