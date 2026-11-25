"""No rtlnc process may outlive its window or its launcher (0.3.2)."""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

import rtlnc.launcher as Lm

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX process checks")


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # zombie?
    try:
        return open(f"/proc/{pid}/stat").read().split()[2] != "Z"
    except OSError:
        return False


def _children(pid):
    out = subprocess.run(["ps", "-o", "pid=", "--ppid", str(pid)], capture_output=True, text=True).stdout
    return [int(x) for x in out.split()]


def test_window_closed_rules(tmp_path, monkeypatch):
    monkeypatch.setenv("RTLNC_CONFIG_DIR", str(tmp_path))
    L = Lm.Launcher()
    t = 1000.0
    L.last_seen, L.bye_at = t, None
    assert not Lm.window_closed(L, t + 60)                 # minimised: slow polls are fine
    assert Lm.window_closed(L, t + Lm.HEARTBEAT_TIMEOUT_S + 1)
    L.bye_at = t + 1                                       # window said goodbye
    assert not Lm.window_closed(L, t + 2)                  # grace period (could be a reload)
    assert Lm.window_closed(L, t + 1 + Lm.BYE_GRACE_S + 0.5)
    L.last_seen, L.bye_at = t + 3, None                    # reload polled again -> stays
    assert not Lm.window_closed(L, t + 10)


def test_closing_window_stops_everything(tmp_path):
    port, bus, panel = _free_port(), _free_port(), _free_port()
    cfg = tmp_path / "cfg"; cfg.mkdir()
    (cfg / "launcher.json").write_text(json.dumps(
        {"demo": True, "local_audio": False, "bus_port": bus, "panel_port": panel}))
    env = dict(os.environ, RTLNC_CONFIG_DIR=str(cfg), BROWSER="true")
    p = subprocess.Popen([sys.executable, "-m", "rtlnc.winapp", "--browser", "--port", str(port)],
                         env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    B = f"http://127.0.0.1:{port}"
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(B + "/api/state"); break
            except OSError:
                time.sleep(0.2)
        post = lambda path: urllib.request.urlopen(urllib.request.Request(
            B + path, data=b"{}", headers={"Content-Type": "application/json"}))
        post("/api/start")
        time.sleep(3)
        kids = _children(p.pid)
        assert len(kids) >= 2                               # core + panel running
        post("/api/bye")                                    # the window closes
        p.wait(timeout=Lm.BYE_GRACE_S + 15)
        time.sleep(1)
        assert not any(_alive(k) for k in kids), "helper processes left running"
    finally:
        if p.poll() is None:
            p.kill()


def test_helpers_exit_when_launcher_vanishes(tmp_path):
    parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(2)"])
    env = dict(os.environ, RTLNC_PARENT_PID=str(parent.pid), RTLNC_LOG=str(tmp_path / "p.log"),
               RTLNC_CONFIG_DIR=str(tmp_path))
    child = subprocess.Popen([sys.executable, "-m", "rtlnc.winapp", "panel", "--http",
                              f"127.0.0.1:{_free_port()}", "--bus", f"127.0.0.1:{_free_port()}"],
                             env=env)
    parent.wait()
    child.wait(timeout=10)                                  # the watchdog ends it
    assert "launcher is gone" in (tmp_path / "p.log").read_text()
