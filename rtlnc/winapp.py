"""Single entry point for the packaged app (rtlnc.exe).

    rtlnc.exe                    the launcher window (normal use)
    rtlnc.exe core  [args]       the canceller core        (= rtlnc run ...)
    rtlnc.exe check [args]       20 s health check         (= rtlnc check ...)
    rtlnc.exe tcp|sound|rig|panel|devices [args]   the add-ons

The launcher runs the other parts as hidden child processes of this same
executable, so nothing ever opens a console window. When RTLNC_LOG is set, a
child writes its output to that file and the launcher shows it.
"""
from __future__ import annotations

import os
import sys


def app_dir() -> str:
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _setup_dlls() -> None:
    """Let pyrtlsdr / ctypes find the bundled RTL-SDR Blog driver (rtlsdr.dll, V4 + V4L)."""
    if sys.platform != "win32":
        return
    for d in (app_dir(), os.path.join(app_dir(), "rtlsdr")):
        if os.path.isdir(d):
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            try:
                os.add_dll_directory(d)
            except (AttributeError, OSError):
                pass


def _setup_log() -> None:
    path = os.environ.get("RTLNC_LOG")
    if path:
        f = open(path, "a", buffering=1, encoding="utf-8", errors="replace")
        sys.stdout = sys.stderr = f
    elif sys.stdout is None:                 # windowed exe started by hand with args
        sys.stdout = sys.stderr = open(os.devnull, "w")


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x00100000, False, pid)        # SYNCHRONIZE
        if not h:
            return False
        try:
            return k.WaitForSingleObject(h, 0) == 0x102    # WAIT_TIMEOUT = still running
        finally:
            k.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _watch_parent() -> None:
    """Started by the launcher? Then quit if the launcher disappears, so no
    hidden rtlnc.exe is ever left running (and holding the dongles)."""
    pid = os.environ.get("RTLNC_PARENT_PID")
    if not pid:
        return
    import threading
    import time

    def loop():
        while _pid_alive(int(pid)):
            time.sleep(2)
        print("launcher is gone; exiting")
        os._exit(0)
    threading.Thread(target=loop, daemon=True).start()


SUBCOMMANDS = {
    "core": ("rtlnc.app", "run"), "run": ("rtlnc.app", "run"),
    "check": ("rtlnc.app", "check"), "list": ("rtlnc.app", "list"),
    "tcp": ("rtlnc.addons.tcp", None), "sound": ("rtlnc.addons.sound", None),
    "rig": ("rtlnc.addons.rig", None), "panel": ("rtlnc.addons.panel", None),
    "devices": ("rtlnc.addons.devices", None),
}


def main() -> None:
    _setup_dlls()
    if len(sys.argv) > 1 and sys.argv[1] in SUBCOMMANDS:
        _setup_log()
        _watch_parent()
        mod, first = SUBCOMMANDS[sys.argv[1]]
        rest = sys.argv[2:]
        import importlib
        m = importlib.import_module(mod)
        if mod == "rtlnc.app":
            sys.argv = ["rtlnc", first] + rest
            m.main()
        else:
            m.main(rest)
        return
    from .launcher import main as launcher_main
    launcher_main()


if __name__ == "__main__":
    main()
