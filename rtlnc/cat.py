"""Read the frequency and TX state of a CAT-controlled ham radio. Read only.

Backends:
  icom      CI-V over the radio's USB/serial port (IC-7300, 705, 9700, 7610, ...)
  yaesu     newer Yaesu ASCII CAT (FT-991A, FTdx10, FTdx101, FT-710, FT-891, ...)
  kenwood   Kenwood ASCII CAT (TS-590, TS-890, TS-2000, TS-480, ...)
  elecraft  Elecraft K3/K3S/K4/KX2/KX3 (Kenwood-style)
  flrig     FLRig's XML-RPC server (default port 12345). Shareable with other programs.
  rigctld   Hamlib rigctld (default port 4532). Shareable with other programs.

Only queries are ever sent; nothing is set on the radio. On Kenwood the bare
"TX;" command *keys the transmitter*, so this module never sends it: Kenwood TX
state comes from the IF; status answer, Elecraft's from TQ;, and Yaesu's
from TX; (on Yaesu a bare TX; is a read-only query).

A COM port can only be opened by one program. If WSJT-X, a logger or another
program also needs CAT, run FLRig or rigctld and choose that backend here.
"""
from __future__ import annotations

import re
import socket
import time
import xmlrpc.client
from dataclasses import dataclass

# common CI-V default addresses
ICOM_ADDR = {"IC-7300": 0x94, "IC-705": 0xA4, "IC-9700": 0xA2, "IC-7610": 0x98,
             "IC-7100": 0x88, "IC-7851": 0x8E, "IC-7600": 0x7A, "IC-9100": 0x7C,
             "IC-7410": 0x80, "IC-905": 0xAC, "IC-7760": 0xB2}
DEFAULT_BAUD = {"icom": 19200, "yaesu": 38400, "kenwood": 115200, "elecraft": 38400}
BACKENDS = ("icom", "yaesu", "kenwood", "elecraft", "flrig", "rigctld")


class CatError(Exception):
    pass


@dataclass
class RigState:
    freq: float
    ptt: bool | None          # None = this backend/radio can't report it


# ---------------------------------------------------------------- serial ASCII
class _Serial:
    def __init__(self, port: str, baud: int, timeout: float = 0.3):
        import serial
        try:
            self.s = serial.Serial(port, baud, timeout=timeout, write_timeout=1)
        except serial.SerialException as e:
            raise CatError(f"cannot open {port}: {e} (in use by another program? "
                           f"then use FLRig or rigctld)") from e

    def close(self):
        try:
            self.s.close()
        except Exception:
            pass


class AsciiCat(_Serial):
    """Kenwood / Elecraft / newer Yaesu: commands and answers end with ';'."""

    def __init__(self, family: str, port: str, baud: int | None = None):
        super().__init__(port, baud or DEFAULT_BAUD[family])
        self.family = family

    def _ask(self, cmd: str) -> str:
        self.s.reset_input_buffer()
        self.s.write(cmd.encode())
        buf = b""
        t0 = time.time()
        while time.time() - t0 < 0.6:
            b = self.s.read(1)
            if not b:
                continue
            buf += b
            if b == b";":
                txt = buf.decode(errors="replace")
                if txt.startswith(cmd[:2]):
                    return txt
                buf = b""                       # unsolicited/other answer: skip
        raise CatError(f"no answer to {cmd} (baud rate? CAT enabled on the radio?)")

    def read(self) -> RigState:
        if self.family == "kenwood":
            info = self._ask("IF;")             # IF + 11-digit freq ... TX flag at [28]
            m = re.match(r"IF(\d{11})", info)
            if not m:
                raise CatError(f"odd IF answer {info!r}")
            ptt = info[28] == "1" if len(info) > 28 else None
            return RigState(float(m.group(1)), ptt)
        fa = self._ask("FA;")
        m = re.match(r"FA(\d+);", fa)
        if not m:
            raise CatError(f"odd FA answer {fa!r}")
        if self.family == "elecraft":
            tq = self._ask("TQ;")
            ptt = tq.startswith("TQ1")
        else:                                   # yaesu: TX0 rx, TX1 cat-tx, TX2 ptt-tx
            tx = self._ask("TX;")
            ptt = tx[2:3] in ("1", "2")
        return RigState(float(m.group(1)), ptt)


class IcomCat(_Serial):
    """Icom CI-V. Handles the echo of single-wire CI-V and transceive broadcasts."""

    def __init__(self, port: str, baud: int | None = None, addr: int = 0x94):
        super().__init__(port, baud or DEFAULT_BAUD["icom"], timeout=0.05)
        self.addr = addr
        self.buf = b""

    @staticmethod
    def bcd_freq(b: bytes) -> float:
        f, mul = 0, 1
        for byte in b:                          # little-endian packed BCD
            f += ((byte & 0x0F) + 10 * (byte >> 4)) * mul
            mul *= 100
        return float(f)

    def _frames(self):
        self.buf += self.s.read(256)
        while True:
            i = self.buf.find(b"\xfe\xfe")
            if i < 0:
                self.buf = b""
                return
            j = self.buf.find(b"\xfd", i)
            if j < 0:
                self.buf = self.buf[i:]
                return
            fr = self.buf[i + 2:j]
            self.buf = self.buf[j + 1:]
            if len(fr) >= 3:
                yield fr                        # dst, src, cmd, data...

    def _ask(self, cmd: bytes) -> bytes:
        self.s.reset_input_buffer()
        self.buf = b""
        self.s.write(b"\xfe\xfe" + bytes([self.addr, 0xE0]) + cmd + b"\xfd")
        t0 = time.time()
        while time.time() - t0 < 0.6:
            for fr in self._frames():
                dst, src, data = fr[0], fr[1], fr[2:]
                if dst == 0xE0 and src == self.addr and data[:len(cmd)] == cmd:
                    return data[len(cmd):]
                if dst == 0xE0 and src == self.addr and data[:1] == b"\xfa":
                    raise CatError("radio answered NG (command not supported)")
        raise CatError("no CI-V answer: check the CI-V address, baud rate and "
                       "'CI-V USB Echo Back' / 'CI-V Transceive' settings")

    def read(self) -> RigState:
        f = self.bcd_freq(self._ask(b"\x03")[:5])
        try:
            ptt = self._ask(b"\x1c\x00")[:1] == b"\x01"
        except CatError:
            ptt = None
        return RigState(f, ptt)


# ---------------------------------------------------------------- network
class FlrigCat:
    def __init__(self, addr: str = "127.0.0.1:12345"):
        host, port = addr.rsplit(":", 1) if ":" in addr else (addr, "12345")
        self.p = xmlrpc.client.ServerProxy(f"http://{host or '127.0.0.1'}:{port}/",
                                           allow_none=True)
        socket.setdefaulttimeout(1.0)

    def read(self) -> RigState:
        try:
            f = float(self.p.rig.get_vfo())
            try:
                ptt = bool(int(self.p.rig.get_ptt()))
            except Exception:
                ptt = None
            return RigState(f, ptt)
        except (OSError, xmlrpc.client.Error) as e:
            raise CatError(f"FLRig not reachable: {e}") from e

    def close(self):
        pass


class RigctldCat:
    def __init__(self, addr: str = "127.0.0.1:4532"):
        from .addons.rig import Rigctld
        self.r = Rigctld(addr)

    def read(self) -> RigState:
        try:
            return RigState(self.r.freq(), self.r.ptt())
        except (OSError, ValueError, ConnectionError) as e:
            self.r.close()
            raise CatError(f"rigctld: {e}") from e

    def close(self):
        self.r.close()


def open_backend(kind: str, port: str = "", baud: int | None = None, civ: int | None = None,
                 addr: str = ""):
    kind = kind.lower()
    if kind in ("kenwood", "yaesu", "elecraft"):
        return AsciiCat(kind, port, baud)
    if kind == "icom":
        return IcomCat(port, baud, civ if civ is not None else 0x94)
    if kind == "flrig":
        return FlrigCat(addr or "127.0.0.1:12345")
    if kind == "rigctld":
        return RigctldCat(addr or "127.0.0.1:4532")
    raise CatError(f"unknown CAT type {kind!r}; choose from {BACKENDS}")


def list_ports() -> list[tuple[str, str]]:
    try:
        from serial.tools import list_ports
        return [(p.device, p.description or "") for p in list_ports.comports()]
    except Exception:
        return []
