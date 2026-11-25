"""rtlnc-tcp: present the cancelled signal as an rtl_tcp server.

Point any SDR program's "RTL-SDR (TCP)" / rtl_tcp source at this machine,
port 1234, and it receives the noise-cancelled IQ as if it came from one
ordinary RTL-SDR. It speaks the standard rtl_tcp protocol used by
SDR#, SDR++, GQRX (rtl_tcp=host:port), SDR Console, HDSDR (ExtIO_RTL_TCP),
CubicSDR (SoapyRTLTCP) and OpenWebRX.

  * Centre-frequency changes from the program retune the dongles (debounced),
    unless --no-tune is given (use that when rtlnc-rig controls tuning).
  * If the core sits on a different centre (another client, the radio, or an
    HF-only refusal), the IQ is shifted so the program's frequency scale is
    still correct. Only the core's ~240 kHz around its own centre has signal.
  * Sample-rate requests are honoured by resampling; content stays ~240 kHz wide.
    Choose the lowest rate the program offers (250k / 0.25 MSPS) to save CPU.
  * The gain slider becomes a digital output gain; "auto gain" = slow AGC.
"""
from __future__ import annotations

import argparse
import queue
import socket
import struct
import threading
import time

import numpy as np

from ..bus import DEFAULT_PORT, BusClient
from .common import Converter, Leveler

TUNER_R828D = 6
# gain table that RTL clients expect (tenths of dB), 29 steps
GAINS = [0, 9, 14, 27, 37, 77, 87, 125, 144, 157, 166, 197, 207, 229, 254,
         280, 297, 328, 338, 364, 372, 386, 402, 421, 434, 439, 445, 480, 496]


class TcpClient:
    def __init__(self, sock, addr, srv: "RtlTcpServer"):
        self.sock, self.addr, self.srv = sock, addr, srv
        self.rate = 2.4e6 if srv.default_rate is None else srv.default_rate
        self.freq = None                       # centre the program asked for
        self.conv = Converter(self.rate, Leveler())
        self.q: queue.Queue = queue.Queue(maxsize=128)
        self.alive = True
        sock.sendall(b"RTL0" + struct.pack(">II", TUNER_R828D, len(GAINS)))
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._writer, daemon=True).start()
        print(f"[rtlnc-tcp] client {addr[0]}:{addr[1]} connected")

    def _reader(self):
        try:
            while self.alive:
                b = b""
                while len(b) < 5:
                    c = self.sock.recv(5 - len(b))
                    if not c:
                        raise ConnectionError
                    b += c
                cmd, val = b[0], struct.unpack(">I", b[1:])[0]
                self.srv.command(self, cmd, val)
        except (OSError, ConnectionError):
            pass
        self.close()

    def _writer(self):
        try:
            while self.alive:
                self.sock.sendall(self.q.get())
        except OSError:
            pass
        self.close()

    def push(self, hdr: dict, iq: np.ndarray):
        shift = 0.0 if self.freq is None else hdr["fc"] - self.freq
        y = self.conv.process(hdr, iq, shift)
        if not len(y):
            return
        u = np.empty(2 * len(y), dtype=np.uint8)
        u[0::2] = np.clip(np.rint(127.5 + 127.5 * y.real), 0, 255)
        u[1::2] = np.clip(np.rint(127.5 + 127.5 * y.imag), 0, 255)
        try:
            self.q.put_nowait(u.tobytes())
        except queue.Full:
            pass                               # client too slow: drop

    def close(self):
        if self.alive:
            self.alive = False
            try:
                self.sock.close()
            except OSError:
                pass
            print(f"[rtlnc-tcp] client {self.addr[0]}:{self.addr[1]} disconnected")


class RtlTcpServer:
    def __init__(self, bus: BusClient, host: str, port: int, tune: bool,
                 default_rate: float | None):
        self.bus, self.tune, self.default_rate = bus, tune, default_rate
        self.clients: list[TcpClient] = []
        self.pending_tune: tuple[float, float] | None = None
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        from ..netutil import exclusive, port_in_use_message
        exclusive(self.sock)
        try:
            self.sock.bind((host, port))
        except OSError as e:
            raise SystemExit(port_in_use_message("the rtl_tcp server", port)) from e
        self.sock.listen(4)
        self.addr = self.sock.getsockname()
        threading.Thread(target=self._accept, daemon=True).start()
        threading.Thread(target=self._debounce, daemon=True).start()

    def _accept(self):
        while True:
            try:
                s, a = self.sock.accept()
            except OSError:
                return
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.clients.append(TcpClient(s, a, self))

    def command(self, c: TcpClient, cmd: int, val: int):
        if cmd == 0x01:                        # centre frequency
            c.freq = float(val)
            if self.tune:
                self.pending_tune = (float(val), time.time())
        elif cmd == 0x02:                      # sample rate
            c.rate = float(val)
            c.conv = Converter(c.rate, c.conv.lev)
        elif cmd == 0x03:                      # gain mode: 0 = auto
            if val == 0:
                c.conv.lev.fixed_db = None
        elif cmd == 0x04:                      # gain, tenths of dB -> digital gain
            c.conv.lev.fixed_db = val / 10.0 - 25.0
        elif cmd == 0x0D and val < len(GAINS):  # gain by index
            c.conv.lev.fixed_db = GAINS[val] / 10.0 - 25.0
        # 0x05 ppm, 0x08 AGC, 0x09 direct sampling, 0x0e bias tee: ignored on
        # purpose (the core owns the hardware; ppm must stay 0 on every dongle)

    def _debounce(self):
        while True:
            time.sleep(0.1)
            p = self.pending_tune
            if p and time.time() - p[1] > 0.4:
                self.pending_tune = None
                self.bus.send({"cmd": "tune", "fc": p[0]})

    def push(self, hdr, iq):
        self.clients = [c for c in self.clients if c.alive]
        for c in self.clients:
            c.push(hdr, iq)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="rtlnc-tcp", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bus", default=f"127.0.0.1:{DEFAULT_PORT}", help="rtlnc core bus address")
    ap.add_argument("--listen", default="0.0.0.0:1234", help="rtl_tcp address to serve on")
    ap.add_argument("--no-tune", action="store_true",
                    help="don't let SDR programs retune the dongles (radio/rig controls it)")
    ap.add_argument("--rate", type=float, default=None,
                    help="rate to send before the program asks for one (default 2.4e6)")
    a = ap.parse_args(argv)
    host, port = a.listen.rsplit(":", 1)
    bus = BusClient(a.bus, name="rtlnc-tcp")
    srv = RtlTcpServer(bus, host, int(port), tune=not a.no_tune, default_rate=a.rate)
    print(f"[rtlnc-tcp] rtl_tcp server on {srv.addr[0]}:{srv.addr[1]}")
    for hdr, iq in bus.frames():
        if hdr.get("type") == "iq" and iq is not None:
            srv.push(hdr, iq)


if __name__ == "__main__":
    main()
