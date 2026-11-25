"""CAT backends against fake radios on pseudo-terminals (and a fake FLRig)."""
import os
import threading
from xmlrpc.server import SimpleXMLRPCServer

import sys

import pytest

from rtlnc import cat

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="fake radios use POSIX pseudo-terminals")


class FakeRadio:
    def __init__(self, kind, freq=14074000, tx=0, addr=0x94):
        self.kind, self.freq, self.tx, self.addr = kind, freq, tx, addr
        self.m, s = os.openpty()
        self.port = os.ttyname(s)
        self.seen = []
        threading.Thread(target=self._loop, daemon=True).start()

    def _reply(self, b):
        os.write(self.m, b)

    def _loop(self):
        buf = b""
        while True:
            try:
                buf += os.read(self.m, 256)
            except OSError:
                return
            if self.kind == "icom":
                while b"\xfd" in buf:
                    fr, buf = buf[:buf.index(b"\xfd") + 1], buf[buf.index(b"\xfd") + 1:]
                    self._reply(fr)                                   # CI-V echo
                    body = fr[fr.index(b"\xfe\xfe") + 2:-1]
                    dst, src, cmd = body[0], body[1], body[2:]
                    self.seen.append(cmd)
                    if dst != self.addr:
                        continue
                    if cmd == b"\x03":
                        d, f = b"", self.freq
                        for _ in range(5):
                            d += bytes([(f % 10) | ((f // 10 % 10) << 4)]); f //= 100
                        self._reply(b"\xfe\xfe\xe0" + bytes([self.addr]) + b"\x03" + d + b"\xfd")
                    elif cmd == b"\x1c\x00":
                        self._reply(b"\xfe\xfe\xe0" + bytes([self.addr]) + b"\x1c\x00" + bytes([self.tx]) + b"\xfd")
            else:
                while b";" in buf:
                    c, buf = buf[:buf.index(b";") + 1].decode(), buf[buf.index(b";") + 1:]
                    self.seen.append(c)
                    if c == "FA;":
                        n = 11 if self.kind in ("kenwood", "elecraft") else 9
                        self._reply(f"FA{self.freq:0{n}d};".encode())
                    elif c == "IF;" and self.kind == "kenwood":
                        self._reply(("IF" + f"{self.freq:011d}" + "     " + "+0000" + "000" + "00"
                                     + str(self.tx) + "2" + "0000000;").encode())
                    elif c == "TX;" and self.kind == "yaesu":
                        self._reply(f"TX{2 if self.tx else 0};".encode())
                    elif c == "TQ;" and self.kind == "elecraft":
                        self._reply(f"TQ{self.tx};".encode())


@pytest.mark.parametrize("kind", ["icom", "yaesu", "kenwood", "elecraft"])
def test_serial_backends(kind):
    r = FakeRadio(kind, freq=145520000 if kind == "icom" else 7074000)
    rig = cat.open_backend(kind, r.port, 19200, 0x94)
    st = rig.read()
    assert st.freq == r.freq and st.ptt is False
    r.tx, r.freq = 1, 432100000 if kind == "icom" else 14200000
    st = rig.read()
    assert st.freq == r.freq and st.ptt is True
    rig.close()
    if kind == "kenwood":
        assert "TX;" not in r.seen          # would key a Kenwood transmitter


def test_icom_wrong_address_reports_clearly():
    r = FakeRadio("icom", addr=0xA4)
    rig = cat.open_backend("icom", r.port, 19200, 0x94)
    with pytest.raises(cat.CatError, match="CI-V address"):
        rig.read()
    rig.close()


def test_flrig_backend():
    srv = SimpleXMLRPCServer(("127.0.0.1", 0), logRequests=False, allow_none=True)
    st = {"f": "50313000", "p": 0}
    srv.register_function(lambda: st["f"], "rig.get_vfo")
    srv.register_function(lambda: st["p"], "rig.get_ptt")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    rig = cat.open_backend("flrig", addr=f"127.0.0.1:{srv.server_address[1]}")
    assert rig.read().freq == 50313000 and rig.read().ptt is False
    st["p"] = 1
    assert rig.read().ptt is True
    srv.shutdown()


def test_busy_port_message():
    with pytest.raises(cat.CatError, match="in use"):
        cat.open_backend("yaesu", "/dev/does-not-exist", 38400)
