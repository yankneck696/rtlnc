"""The launcher window: settings, Start/Stop, status, logs. No command lines.

It starts these as hidden child processes, all of them this same program:
    core (dongles + canceller)   always
    panel (phasing knobs)        always; opened in its own window on demand
    tcp   (SDR programs)         if "SDR programs (rtl_tcp)" is ticked
    sound (IQ / audio out)       if a sound output is ticked
    rig   (follow radio by CAT)  if a radio is chosen
The dongle editor runs only while the core is stopped; it needs the dongles.

The window is a local web page shown as an app window: Microsoft Edge in app
mode on Windows (no address bar; Edge is part of Windows 10/11), pywebview if
installed elsewhere, otherwise your browser. Closing the main window stops
everything.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from . import devconfig
from .bus import BusClient
from .netutil import HTTPServer

DEFAULTS = {
    "freq_mhz": 7.160, "mode": "lsb", "squelch": None, "demo": False,
    "local_audio": True, "audio_device": "",
    "tcp_on": False, "tcp_port": 1234, "tcp_tune": True, "tcp_lan": False,
    "iq_on": False, "iq_device": "", "iq_rate": 192000,
    "aud_on": False, "aud_device": "",
    "cat": "none", "cat_port": "", "cat_baud": "", "cat_civ": "IC-7300", "cat_addr": "",
    "panel_lan": False, "panel_token": "",
    "bus_port": 7355, "panel_port": 8073, "devices_port": 8074,
}
CREATE_NO_WINDOW = 0x08000000


class LauncherError(Exception):
    """A problem to show the user instead of starting."""


def settings_path():
    return devconfig.config_dir() / "launcher.json"


def load_settings() -> dict:
    s = dict(DEFAULTS)
    try:
        s.update(json.loads(settings_path().read_text()))
    except (OSError, json.JSONDecodeError):
        pass
    return s


def save_settings(s: dict) -> None:
    settings_path().write_text(json.dumps(s, indent=2))


def _self_cmd(sub: str, *args: str) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, sub, *args]
    return [sys.executable, "-m", "rtlnc.winapp", sub, *args]


class Proc:
    def __init__(self, name: str, cmd: list[str], logdir):
        self.name, self.cmd = name, cmd
        self.log = logdir / f"{name}.log"
        self.log.write_text("")
        env = dict(os.environ, RTLNC_LOG=str(self.log), PYTHONUNBUFFERED="1",
                   RTLNC_PARENT_PID=str(os.getpid()))
        kw = {"creationflags": CREATE_NO_WINDOW} if sys.platform == "win32" else {}
        self.p = subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
        self.pos = 0

    def alive(self) -> bool:
        return self.p.poll() is None

    def new_lines(self) -> list[str]:
        try:
            with open(self.log, "r", encoding="utf-8", errors="replace") as f:
                f.seek(self.pos)
                data = f.read()
                self.pos = f.tell()
            return [l for l in data.splitlines() if l.strip()]
        except OSError:
            return []

    def stop(self, timeout: float = 4.0) -> None:
        if not self.alive():
            return
        try:
            self.p.wait(timeout)
        except subprocess.TimeoutExpired:
            self.p.terminate()
            try:
                self.p.wait(2)
            except subprocess.TimeoutExpired:
                self.p.kill()


class Launcher:
    def __init__(self):
        self.s = load_settings()
        self.logdir = devconfig.config_dir() / "logs"
        self.logdir.mkdir(exist_ok=True)
        self.procs: dict[str, Proc] = {}
        self.logs: deque = deque(maxlen=400)
        self.status: dict = {}
        self.lock = threading.Lock()
        self.bus: BusClient | None = None
        self.windows = None                  # pywebview module when available
        self.edge = None                     # msedge.exe path (Windows app windows)
        self.edge_profile = devconfig.config_dir() / "window-profile"
        self.last_seen = time.time()
        self.bye_at: float | None = None      # window said goodbye at this time
        self.rig_err: str | None = None
        self.rig_freq: str | None = None
        threading.Thread(target=self._pump_logs, daemon=True).start()

    # ------------------------------------------------------------ helpers
    def log(self, msg: str) -> None:
        self.logs.append(time.strftime("%H:%M:%S ") + msg)

    def _pump_logs(self):
        while True:
            for name, p in list(self.procs.items()):
                for line in p.new_lines():
                    self.logs.append(f"{time.strftime('%H:%M:%S')} [{name}] {line}")
                    if name == "rig":
                        if "radio:" in line:
                            self.rig_err = line.split("radio:", 1)[1].replace("; retrying", "").strip()
                        elif "radio at" in line or "radio connected" in line:
                            self.rig_err = None
                            if "radio at" in line:
                                self.rig_freq = line.split("radio at", 1)[1].strip()
                if not p.alive() and not getattr(p, "reported", False):
                    p.reported = True
                    code = p.p.returncode
                    if code not in (0, None) and not getattr(p, "stopping", False):
                        self.log(f"{name} stopped unexpectedly (exit {code}); see its messages above")
                    if name == "core" and not getattr(p, "stopping", False):
                        threading.Thread(target=self._core_died, daemon=True).start()
            time.sleep(0.3)

    def running(self) -> bool:
        return "core" in self.procs and self.procs["core"].alive()

    def _status_loop(self, bus: BusClient):
        for hdr, _ in bus.frames():
            if bus is not self.bus:
                return
            if hdr.get("type") == "status":
                self.status = hdr
            elif hdr.get("type") == "disconnected":
                self.status = {}

    # ------------------------------------------------------------ start / stop
    def preflight(self) -> None:
        """Catch the common set-up problems before anything starts."""
        if self.s["demo"]:
            return
        roles = devconfig.from_roles()
        try:
            from .eeprom import RtlIO
            present = RtlIO().list()
        except Exception as e:
            raise LauncherError(f"Can't access RTL-SDR dongles: {e}")
        if not present:
            raise LauncherError("No RTL-SDR dongles found. Plug them in (powered hub). On a new PC, "
                                "install the WinUSB driver for each one with Zadig first. "
                                "To try without hardware, tick Demo mode.")
        if len(roles) < 2:
            raise LauncherError("Your dongles don't have roles yet. Click 'Dongles & serials…', "
                                "give one dongle the role 'main' and at least one 'aux1' "
                                "(the wizard does this for you). To try without hardware, tick Demo mode.")
        serials = [d.serial for d in present]
        cfg = devconfig.load()["devices"]
        for s in roles:
            if s not in serials:
                role = cfg.get(s, {}).get("role", "?")
                name = cfg.get(s, {}).get("name") or s
                raise LauncherError(f"The {role} dongle '{name}' ({s}) isn't plugged in.")

    def start(self) -> None:
        with self.lock:
            if self.running():
                return
            self.preflight()
            self._kill_all()                 # leftovers from a failed start
            self.stop_devices()
            s = self.s
            bus = f"127.0.0.1:{s['bus_port']}"
            core = ["--freq", str(float(s["freq_mhz"]) * 1e6), "--mode", s["mode"], "--bus", bus]
            if s.get("squelch") is not None:
                core += ["--squelch", str(s["squelch"])]
            if s["demo"]:
                core += ["--sim", "2"]
            if not s["local_audio"]:
                core += ["--no-audio"]
            elif s["audio_device"]:
                core += ["--audio-device", s["audio_device"]]
            self.procs["core"] = Proc("core", _self_cmd("core", *core), self.logdir)
            http = f"{'0.0.0.0' if s['panel_lan'] else '127.0.0.1'}:{s['panel_port']}"
            pargs = ["--bus", bus, "--http", http]
            if s["panel_lan"] and s["panel_token"]:
                pargs += ["--token", s["panel_token"]]
            self.procs["panel"] = Proc("panel", _self_cmd("panel", *pargs), self.logdir)
            if s["tcp_on"]:
                host = "0.0.0.0" if s["tcp_lan"] else "127.0.0.1"
                targs = ["--bus", bus, "--listen", f"{host}:{s['tcp_port']}"]
                if not s["tcp_tune"] or s["cat"] != "none":
                    targs.append("--no-tune")
                self.procs["tcp"] = Proc("tcp", _self_cmd("tcp", *targs), self.logdir)
            if s["iq_on"] or s["aud_on"]:
                sargs = ["--bus", bus]
                if s["iq_on"] and s["iq_device"]:
                    sargs += ["--iq-device", s["iq_device"], "--iq-rate", str(s["iq_rate"])]
                if s["aud_on"] and s["aud_device"]:
                    sargs += ["--audio-device", s["aud_device"]]
                if len(sargs) > 2:
                    self.procs["sound"] = Proc("sound", _self_cmd("sound", *sargs), self.logdir)
            if s["cat"] != "none":
                rargs = ["--bus", bus, "--cat", s["cat"]]
                if s["cat"] in ("icom", "yaesu", "kenwood", "elecraft"):
                    rargs += ["--port", s["cat_port"]]
                    if str(s["cat_baud"]).strip():
                        rargs += ["--baud", str(s["cat_baud"])]
                    if s["cat"] == "icom" and s["cat_civ"]:
                        rargs += ["--civ", str(s["cat_civ"])]
                elif s["cat_addr"]:
                    rargs += ["--addr", s["cat_addr"]]
                self.procs["rig"] = Proc("rig", _self_cmd("rig", *rargs), self.logdir)
            self.rig_err, self.rig_freq = None, None
            self.bus = BusClient(bus, name="launcher", iq=False, verbose=False, retry=0.5)
            threading.Thread(target=self._status_loop, args=(self.bus,), daemon=True).start()
            self.log("started: " + ", ".join(self.procs))

    def _kill_all(self) -> None:
        for name, p in list(self.procs.items()):
            if name == "devices":
                continue
            p.stopping = True
            if p.alive():
                p.p.terminate()
                p.stop(2)
            self.procs.pop(name, None)
        if self.bus:
            b, self.bus = self.bus, None
            b.close()

    def _core_died(self) -> None:
        with self.lock:
            self._kill_all()
            self.status = {}
        self.log("Stopped: the canceller core ended. Fix the problem shown above, then press Start.")

    def stop(self) -> None:
        with self.lock:
            for p in self.procs.values():   # mark first: a clean exit is not a crash
                p.stopping = True
            if self.bus and self.bus.sock:
                self.bus.send({"cmd": "quit"})      # let the core release the dongles cleanly
            for name in ["rig", "sound", "tcp", "panel", "core"]:
                p = self.procs.get(name)
                if p:
                    p.stopping = True
                    if name != "core":
                        p.p.terminate()
                    p.stop()
            for name in ["rig", "sound", "tcp", "panel", "core"]:
                self.procs.pop(name, None)
            if self.bus:
                b, self.bus = self.bus, None
                b.close()
            self.status = {}
            self.log("stopped")

    def start_devices(self) -> str:
        if self.running():
            self.stop()
        p = self.procs.get("devices")
        if not (p and p.alive()):
            args = ["web", "--http", f"127.0.0.1:{self.s['devices_port']}"]
            if self.s["demo"]:
                args.append("--demo")
            self.procs["devices"] = Proc("devices", _self_cmd("devices", *args), self.logdir)
            _wait_port(self.s["devices_port"])
        return f"http://127.0.0.1:{self.s['devices_port']}/"

    def stop_devices(self) -> None:
        p = self.procs.pop("devices", None)
        if p:
            p.stopping = True
            p.p.terminate()
            p.stop(2)

    # ------------------------------------------------------------ info
    def dongles(self) -> list[dict]:
        if self.running() or self.s["demo"]:
            return []
        try:
            from .eeprom import RtlIO
            cfg = devconfig.load()
            from .eeprom import model_of
            return [{"index": d.index, "serial": d.serial,
                     "v4": model_of(d.manufacturer, d.product) is not None,
                     "model": model_of(d.manufacturer, d.product) or d.product,
                     "name": cfg["devices"].get(d.serial, {}).get("name", ""),
                     "role": cfg["devices"].get(d.serial, {}).get("role", "")}
                    for d in RtlIO().list()]
        except Exception as e:
            return [{"error": str(e)}]

    def options(self) -> dict:
        from .cat import BACKENDS, ICOM_ADDR, list_ports
        try:
            import sounddevice as sd
            outs = [d["name"] for d in sd.query_devices() if d["max_output_channels"] > 0]
        except Exception:
            outs = []
        return {"ports": list_ports(), "sound_out": outs, "cat_types": list(BACKENDS),
                "icom_models": list(ICOM_ADDR), "roles": devconfig.from_roles()}

    def open_window(self, title: str, url: str) -> None:
        if self.edge:
            subprocess.Popen([self.edge, f"--app={url}", f"--user-data-dir={self.edge_profile}",
                              "--window-size=1300,920"])
        elif self.windows is not None:
            self.windows.create_window(title, url, width=1280, height=900)
        else:
            webbrowser.open(url)


def _wait_port(port: int, timeout: float = 6.0) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return
        except OSError:
            time.sleep(0.15)


def make_handler(L: Launcher, page: bytes):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, obj, code=200):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            p = self.path.split("?")[0]
            if p == "/":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(page)
            elif p == "/api/state":
                L.last_seen = time.time()
                L.bye_at = None
                st = L.status
                self._json({"settings": L.s, "running": L.running(),
                            "rig": {"err": L.rig_err, "freq": L.rig_freq},
                            "procs": {n: p.alive() for n, p in L.procs.items()},
                            "status": {k: st.get(k) for k in
                                       ("state", "fc", "listen", "demod", "cancel_db", "ptt",
                                        "channels", "main_name", "fast_on")},
                            "logs": list(L.logs)[-200:]})
            elif p == "/api/options":
                self._json(L.options())
            elif p == "/api/dongles":
                self._json(L.dongles())
            elif p == "/favicon.ico":
                self.send_response(204); self.end_headers()
            else:
                self.send_error(404)

        def do_POST(self):
            p = self.path.split("?")[0]
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            try:
                if p == "/api/settings":
                    for k, v in body.items():
                        if k in DEFAULTS:
                            L.s[k] = v
                    save_settings(L.s)
                    if L.running() and L.bus:          # live-apply what can change live
                        if "freq_mhz" in body:
                            L.bus.send({"cmd": "listen", "freq": float(body["freq_mhz"]) * 1e6})
                        if "mode" in body:
                            L.bus.send({"cmd": "demod", "mode": body["mode"]})
                    self._json({"ok": True})
                elif p == "/api/bye":                 # window closing (or reloading)
                    L.bye_at = time.time()
                    self._json({"ok": True})
                elif p == "/api/start":
                    try:
                        L.start()
                    except LauncherError as e:
                        L.log(str(e))
                        return self._json({"error": str(e)}, 400)
                    self._json({"ok": True})
                elif p == "/api/stop":
                    L.stop(); self._json({"ok": True})
                elif p == "/api/open":
                    what = body.get("what")
                    if what == "panel":
                        if not L.running():
                            return self._json({"error": "Start the canceller first"}, 400)
                        _wait_port(L.s["panel_port"])
                        L.open_window("rtlnc — phasing panel",
                                      f"http://127.0.0.1:{L.s['panel_port']}/")
                    elif what == "devices":
                        url = L.start_devices()
                        L.open_window("rtlnc — dongles", url)
                    elif what == "logs":
                        L.open_window("rtlnc — log folder", L.logdir.as_uri())
                    self._json({"ok": True})
                else:
                    self.send_error(404)
            except Exception as e:                    # surface errors in the UI
                self._json({"error": str(e)}, 500)
    return H


def find_edge() -> str | None:
    import shutil
    if sys.platform != "win32":
        return None
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                 os.environ.get("LOCALAPPDATA")):
        if base:
            p = os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe")
            if os.path.exists(p):
                return p
    return shutil.which("msedge")


# How long the launcher waits after the window closes before shutting down
# everything. "bye" is sent when the window closes; a reload sends a new poll
# within the grace time. The heartbeat timeout covers a window that vanished
# without saying goodbye. It is long because browsers poll only about once a
# minute in a minimised window.
BYE_GRACE_S = 4.0
HEARTBEAT_TIMEOUT_S = 150.0


def window_closed(L: "Launcher", now: float | None = None) -> bool:
    now = time.time() if now is None else now
    if L.bye_at is not None and now - L.bye_at > BYE_GRACE_S and L.last_seen <= L.bye_at:
        return True
    return now - L.last_seen > HEARTBEAT_TIMEOUT_S


def main(argv=None) -> None:
    import argparse
    ap = argparse.ArgumentParser(prog="rtlnc-launcher")
    ap.add_argument("--browser", action="store_true", help="use the web browser, not an app window")
    ap.add_argument("--port", type=int, default=0, help="launcher HTTP port (0 = any free port)")
    a, _ = ap.parse_known_args(argv if argv is not None else sys.argv[1:])
    L = Launcher()
    page = (Path(__file__).parent / "launcher.html").read_bytes()
    srv = HTTPServer(("127.0.0.1", a.port), make_handler(L, page))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    try:
        L.edge = None if a.browser else find_edge()
        if L.edge:
            L.edge_profile.mkdir(parents=True, exist_ok=True)
            # Don't wait for the Edge process: with Edge's background mode or
            # startup boost it can outlive the window. Watch the page instead.
            subprocess.Popen([L.edge, f"--app={url}", f"--user-data-dir={L.edge_profile}",
                              "--window-size=1120,860", "--no-first-run",
                              "--disable-features=Translate", "--disable-background-mode"])
            L.last_seen = time.time() + 30          # allow time for the window to appear
            while not window_closed(L):
                time.sleep(1)
        else:
            webview = None
            if not a.browser:
                try:
                    import webview  # pywebview
                except Exception:
                    webview = None
            if webview is not None:
                L.windows = webview
                webview.create_window("rtlnc — noise canceller", url, width=1100, height=820,
                                      min_size=(820, 600))
                webview.start()
            else:
                print(f"rtlnc launcher: {url}")
                webbrowser.open(url)
                L.last_seen = time.time() + 60
                while not window_closed(L):
                    time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        L.stop()
        L.stop_devices()
        srv.shutdown()


if __name__ == "__main__":
    main()
