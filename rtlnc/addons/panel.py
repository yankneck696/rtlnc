"""rtlnc-panel: browser control panel for the phasing canceller.

    rtlnc-panel                      then open http://127.0.0.1:8073
    rtlnc-panel --http 0.0.0.0:8073  reach it from a tablet or phone on your LAN

Per aux channel: PHASE and GAIN knobs, the same as on a hardware phasing
unit. A ghost marker on each knob shows the canceller's own auto solution.
Each channel has Auto / Manual / Hold / Off modes and a "knobs <- auto"
button to start hand-tuning from the auto null. The panel also shows the
live spectrum before and after cancelling, a suppression meter, an A/B
bypass, the adaptation speed, the training exclusion, and click-to-listen
on the spectrum.

The panel holds no state of its own; everything lives in the core. Several
panels can be open at once, and closing one changes nothing.
"""
from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..bus import DEFAULT_PORT, BusClient, parse_addr

ALLOWED = {"chan", "mode", "lam", "exclude", "listen", "tune", "reacquire",
           "demod", "squelch", "fast"}


class State:
    def __init__(self):
        self.cond = threading.Condition()
        self.status: dict = {"type": "disconnected"}
        self.ver = 0

    def update(self, hdr: dict):
        with self.cond:
            self.status = hdr
            self.ver += 1
            self.cond.notify_all()


def _bus_loop(bus: BusClient, st: State):
    for hdr, _iq in bus.frames():
        t = hdr.get("type")
        if t in ("status", "disconnected"):
            st.update(hdr)


def make_handler(bus: BusClient, st: State, page: bytes, token: str | None):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _auth(self) -> bool:
            if not token:
                return True
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            return q.get("t", [""])[0] == token or self.headers.get("X-Token") == token

        def do_GET(self):
            if not self._auth():
                self.send_error(403); return
            path = self.path.split("?")[0]
            if path == "/":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)
            elif path == "/favicon.ico":
                self.send_response(204); self.end_headers()
            elif path == "/events":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                seen = -1
                try:
                    while True:
                        with st.cond:
                            st.cond.wait_for(lambda: st.ver != seen, timeout=2.0)
                            seen, s = st.ver, st.status
                        self.wfile.write(b"data: " + json.dumps(s).encode() + b"\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
            else:
                self.send_error(404)

        def do_POST(self):
            if not self._auth():
                self.send_error(403); return
            if self.path.split("?")[0] != "/cmd":
                self.send_error(404); return
            n = int(self.headers.get("Content-Length", 0))
            try:
                cmd = json.loads(self.rfile.read(n))
                ok = isinstance(cmd, dict) and cmd.get("cmd") in ALLOWED and bus.send(cmd)
            except (json.JSONDecodeError, ValueError):
                ok = False
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}' if ok else b'{"ok":false}')

    return H


def main(argv=None):
    ap = argparse.ArgumentParser(prog="rtlnc-panel", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bus", default=f"127.0.0.1:{DEFAULT_PORT}")
    ap.add_argument("--http", default="127.0.0.1:8073", help="address to serve the panel on")
    ap.add_argument("--token", default=None,
                    help="require ?t=TOKEN in the URL (use when serving on a LAN)")
    a = ap.parse_args(argv)
    page = (Path(__file__).parent / "panel.html").read_bytes()
    bus = BusClient(a.bus, name="rtlnc-panel", iq=False)
    st = State()
    threading.Thread(target=_bus_loop, args=(bus, st), daemon=True).start()
    host, port = parse_addr(a.http)
    srv = ThreadingHTTPServer((host, port), make_handler(bus, st, page, a.token))
    url = f"http://{'127.0.0.1' if host == '0.0.0.0' else host}:{srv.server_address[1]}/"
    if a.token:
        url += f"?t={a.token}"
    print(f"[rtlnc-panel] open {url}")
    if host != "127.0.0.1" and not a.token:
        print("[rtlnc-panel] warning: panel is reachable from the network without a --token")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
