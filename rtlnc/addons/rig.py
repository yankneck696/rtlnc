"""rtlnc-rig: make the canceller follow your radio (CAT).

    rtlnc-rig --cat icom --port COM5 --civ 0x94          IC-7300 (USB CAT)
    rtlnc-rig --cat yaesu --port COM7 --baud 38400        FT-991A / FTdx10 / FT-710 ...
    rtlnc-rig --cat kenwood --port COM3 --baud 115200     TS-590 / TS-890 ...
    rtlnc-rig --cat elecraft --port /dev/ttyUSB0          K3 / K4 / KX3
    rtlnc-rig --cat flrig                                 via FLRig (shareable)
    rtlnc-rig --cat rigctld --addr 127.0.0.1:4532         via Hamlib rigctld (shareable)
    rtlnc-rig --list-ports

  * Follows the radio's VFO A frequency. The listen frequency moves with it,
    the dongles retune when you leave the 240 kHz span, and the protected
    band and the demodulator move too.
  * Follows TX: while you transmit, the canceller weights are frozen and
    adaptation and tracking are paused, so your own signal doesn't wreck
    them. rtlnc-sound mutes its audio. Everything resumes on receive.
  * Read-only: it never changes anything on the radio.

Only one program can open a COM port. If WSJT-X or a logger also uses CAT,
run FLRig or rigctld and point both at it.
"""
from __future__ import annotations

import argparse
import socket
import threading
import time

from ..bus import DEFAULT_PORT, BusClient, parse_addr


class Rigctld:
    def __init__(self, addr: str):
        self.host, self.port = parse_addr(addr, "127.0.0.1")
        self.sock = None
        self.f = None

    def _cmd(self, c: str) -> str:
        if self.sock is None:
            self.sock = socket.create_connection((self.host, self.port), timeout=2)
            self.f = self.sock.makefile("r")
        self.sock.sendall((c + "\n").encode())
        line = self.f.readline()
        if not line:
            raise ConnectionError("rigctld closed")
        line = line.strip()
        if line.startswith("RPRT"):
            raise ValueError(line)
        return line

    def freq(self) -> float:
        return float(self._cmd("f"))

    def ptt(self) -> bool:
        return self._cmd("t").strip() not in ("0", "")

    def close(self):
        if self.sock:
            self.sock.close()
        self.sock = None


class _RigctldAdapter:
    def __init__(self, r):
        self.r = r

    def read(self):
        from ..cat import RigState
        return RigState(self.r.freq(), self.r.ptt())

    def close(self):
        self.r.close()


def run(bus: BusClient, rig, poll: float = 0.2, offset: float = 0.0,
        stop: threading.Event | None = None, verbose: bool = True):
    """Follow `rig` (any CAT backend with read(), or a Rigctld) until stopped."""
    from ..cat import CatError
    if isinstance(rig, Rigctld):
        rig = _RigctldAdapter(rig)
    sent_f = sent_ptt = None        # what the core was last told
    shown_f = shown_ptt = None      # what was last printed
    last_err = None
    last_sync = time.time()
    # drain bus frames on a thread, so a dead core is noticed and the client reconnects
    threading.Thread(target=lambda: [None for _ in bus.frames()], daemon=True).start()
    while not (stop and stop.is_set()):
        try:
            st = rig.read()
            f, p = st.freq + offset, bool(st.ptt)
        except (CatError, OSError, ValueError, ConnectionError) as e:
            if verbose and str(e) != last_err:          # say it once, not every second
                print(f"[rtlnc-rig] radio: {e}; retrying")
            last_err = str(e)
            time.sleep(1.0)
            continue
        if last_err is not None:
            if verbose:
                print("[rtlnc-rig] radio connected")
            last_err = None
        if time.time() - last_sync > 5.0:   # periodic resync: the core may have restarted
            sent_f = sent_ptt = None
            last_sync = time.time()
        if p != sent_ptt and bus.send({"cmd": "ptt", "on": p}):
            sent_ptt = p
        if (sent_f is None or abs(f - sent_f) > 5) and bus.send({"cmd": "listen", "freq": f}):
            sent_f = f
        if verbose and p != shown_ptt:
            print(f"[rtlnc-rig] PTT {'ON' if p else 'off'}")
            shown_ptt = p
        if verbose and (shown_f is None or abs(f - shown_f) > 5):
            print(f"[rtlnc-rig] radio at {f/1e6:.5f} MHz")
            shown_f = f
        time.sleep(poll)


def main(argv=None):
    from .. import cat
    ap = argparse.ArgumentParser(prog="rtlnc-rig", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bus", default=f"127.0.0.1:{DEFAULT_PORT}")
    ap.add_argument("--cat", choices=cat.BACKENDS, default="rigctld", help="radio CAT type")
    ap.add_argument("--port", default="", help="serial port (COM5, /dev/ttyUSB0)")
    ap.add_argument("--baud", type=int, default=None, help="baud rate (default by type)")
    ap.add_argument("--civ", default=None,
                    help="Icom CI-V address, e.g. 0x94, or a model: " + ", ".join(cat.ICOM_ADDR))
    ap.add_argument("--addr", default="", help="FLRig / rigctld address host:port")
    ap.add_argument("--rig", default=None, help="(old) rigctld address; same as --cat rigctld --addr")
    ap.add_argument("--poll", type=float, default=0.2, help="seconds between polls")
    ap.add_argument("--offset", type=float, default=0.0,
                    help="Hz added to the radio's frequency (transverter/IF quirks)")
    ap.add_argument("--list-ports", action="store_true", help="list serial ports and exit")
    a = ap.parse_args(argv)
    if a.list_ports:
        for dev, desc in cat.list_ports():
            print(f"{dev:12s} {desc}")
        return
    if a.rig:
        a.cat, a.addr = "rigctld", a.rig
    civ = None
    if a.civ:
        civ = cat.ICOM_ADDR.get(a.civ.upper()) or int(a.civ, 0)
    if a.cat in ("icom", "yaesu", "kenwood", "elecraft") and not a.port:
        ap.error("--port is required for direct serial CAT (see --list-ports)")
    try:
        rig = cat.open_backend(a.cat, a.port, a.baud, civ, a.addr)
    except cat.CatError as e:
        raise SystemExit(f"[rtlnc-rig] {e}")
    print(f"[rtlnc-rig] following {a.cat} {a.port or a.addr}")
    bus = BusClient(a.bus, name="rtlnc-rig", iq=False)
    bus._connect()
    try:
        run(bus, rig, a.poll, a.offset)
    except KeyboardInterrupt:
        pass
    finally:
        rig.close()


if __name__ == "__main__":
    main()
