"""End-to-end: a real core process (simulator mode) plus add-ons over sockets."""
import json
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import numpy as np
import pytest

from rtlnc.bus import BusClient


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


@pytest.fixture(scope="module")
def core():
    port = _free_port()
    p = subprocess.Popen([sys.executable, "-m", "rtlnc", "run", "--sim", "2", "--no-audio",
                          "--freq", "7.160e6", "--bus", f"127.0.0.1:{port}"],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True)
    addr = f"127.0.0.1:{port}"
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close(); break
        except OSError:
            time.sleep(0.1)
    yield addr
    p.terminate(); p.wait(5)


def wait_status(addr, pred, timeout=20):
    bus = BusClient(addr, name="test", iq=False, verbose=False)
    t0 = time.time()
    for hdr, _ in bus.frames():
        if hdr.get("type") == "status" and pred(hdr):
            bus.close(); return hdr
        if time.time() - t0 > timeout:
            bus.close(); raise TimeoutError("condition not reached")


def test_core_tracks_and_publishes_spectra(core):
    h = wait_status(core, lambda h: h["state"] == "track" and "spec_in" in h)
    assert len(h["spec_in"]) == 512 and len(h["channels"]) == 2
    h = wait_status(core, lambda h: h.get("cancel_db", 0) > 6)
    assert h["cancel_db"] > 6


def test_rtl_tcp_bridge(core):
    from rtlnc.addons.tcp import RtlTcpServer
    bus = BusClient(core, name="rtlnc-tcp", verbose=False)
    srv = RtlTcpServer(bus, "127.0.0.1", 0, tune=False, default_rate=None)
    threading.Thread(target=lambda: [srv.push(h, iq) for h, iq in bus.frames()
                                     if h.get("type") == "iq" and iq is not None],
                     daemon=True).start()
    c = socket.create_connection(srv.addr)
    hdr = c.recv(12)
    assert hdr[:4] == b"RTL0" and struct.unpack(">II", hdr[4:]) == (6, 29)
    c.sendall(struct.pack(">BI", 0x02, 1024000))           # sample rate
    c.sendall(struct.pack(">BI", 0x01, 7130000))           # centre = core centre
    time.sleep(1.0)
    c.settimeout(5)
    buf = bytearray(); t0 = time.time()
    while time.time() - t0 < 2.0:
        buf += c.recv(1 << 20)
    rate = len(buf) / 2 / (time.time() - t0)
    # ~1.024 MS/s of uint8 IQ; a loaded test machine can deliver queued data in a burst
    assert 0.5e6 < rate < 4e6, rate
    u = np.frombuffer(bytes(buf[: len(buf) // 2 * 2]), np.uint8).astype(np.float32)
    iq = (u[0::2] - 127.5) + 1j * (u[1::2] - 127.5)
    P = np.abs(np.fft.fftshift(np.fft.fft(iq[:262144]))) ** 2
    f = np.fft.fftshift(np.fft.fftfreq(262144, 1 / 1024000))
    inside = P[np.abs(f) < 100e3].mean(); outside = P[np.abs(f) > 200e3].mean()
    assert inside > 30 * outside                           # content only in the core's span
    c.close()


def test_rig_follow_and_ptt(core):
    from rtlnc.addons.rig import Rigctld, run
    state = {"f": 7.175e6, "t": 0}
    ls = socket.socket(); ls.bind(("127.0.0.1", 0)); ls.listen(1)

    def fake_rigctld():
        s, _ = ls.accept(); f = s.makefile("r")
        for line in f:
            cmd = line.strip()
            s.sendall((f"{int(state['f'])}\n" if cmd == "f" else f"{state['t']}\n").encode())
    threading.Thread(target=fake_rigctld, daemon=True).start()
    bus = BusClient(core, name="rtlnc-rig", iq=False, verbose=False); bus._connect()
    stop = threading.Event()
    threading.Thread(target=run, args=(bus, Rigctld(f"127.0.0.1:{ls.getsockname()[1]}")),
                     kwargs={"poll": 0.05, "stop": stop, "verbose": False}, daemon=True).start()
    wait_status(core, lambda h: abs(h["listen"] - 7.175e6) < 1)
    state["t"] = 1
    wait_status(core, lambda h: h["ptt"] is True)
    state["t"] = 0
    wait_status(core, lambda h: h["ptt"] is False)
    state["f"] = 7.300e6                                     # outside span -> core retunes
    h = wait_status(core, lambda h: abs(h["listen"] - 7.3e6) < 1 and abs(h["fc"] - 7.27e6) < 1)
    stop.set()


def test_panel_controls(core):
    from http.server import ThreadingHTTPServer
    from importlib import resources
    from rtlnc.addons.panel import State, _bus_loop, make_handler
    bus = BusClient(core, name="rtlnc-panel", iq=False, verbose=False)
    st = State()
    threading.Thread(target=_bus_loop, args=(bus, st), daemon=True).start()
    page = resources.files("rtlnc.addons").joinpath("panel.html").read_bytes()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(bus, st, page, None))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    assert b"PHASE" in urllib.request.urlopen(base + "/").read()
    time.sleep(1.0)

    def cmd(c):
        r = urllib.request.Request(base + "/cmd", data=json.dumps(c).encode(),
                                   headers={"Content-Type": "application/json"})
        try:
            return json.loads(urllib.request.urlopen(r).read())
        except urllib.error.HTTPError as e:
            return json.loads(e.read())
    assert cmd({"cmd": "chan", "ch": 1, "phase_deg": 45.0, "gain_db": -12.0})["ok"]
    h = wait_status(core, lambda h: h["channels"][0]["mode"] == "manual")
    c = h["channels"][0]
    assert abs(c["man_phase_deg"] - 45) < 0.01 and abs(c["man_gain_db"] + 12) < 0.01
    assert cmd({"cmd": "chan", "ch": 2, "mode": "off"})["ok"]
    wait_status(core, lambda h: h["channels"][1]["mode"] == "off")
    assert cmd({"cmd": "mode", "mode": "bypass"})["ok"]
    wait_status(core, lambda h: h["bypass"] is True)
    assert cmd({"cmd": "mode", "mode": "auto"})["ok"]
    assert cmd({"cmd": "chan", "ch": 2, "mode": "auto"})["ok"]
    wait_status(core, lambda h: [c["mode"] for c in h["channels"]] == ["auto", "auto"]
                and not h["bypass"])
    assert not cmd({"cmd": "shutdown"})["ok"]             # not on the allow-list
    # SSE stream delivers status JSON
    with urllib.request.urlopen(base + "/events", timeout=5) as r:
        line = r.readline()
        assert line.startswith(b"data: ") and json.loads(line[6:])["type"] in ("status", "disconnected")
    srv.shutdown()
