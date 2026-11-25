"""v0.3: VHF/UHF with fast phase lock, AM/FM/SSB/CW demod, launcher."""
import json
import time
import urllib.request

import numpy as np
import pytest

from rtlnc.demod import Demodulator
from rtlnc.metrics import run_sim


@pytest.mark.parametrize("f,min_gain", [(145e6, 16), (435e6, 14)])
def test_vhf_uhf_with_fast_phase_lock(f, min_gain):
    pn = 0.02 * f / 7.1e6                       # oscillator phase noise grows with frequency
    on = run_sim(n_aux=2, f_tune=f, ppm_spread=3, phase_noise=pn, fast="on")
    off = run_sim(n_aux=2, f_tune=f, ppm_spread=3, phase_noise=pn, fast="off")
    assert on["gain_db"] > min_gain and on["gain_db"] > off["gain_db"] + 3
    est = [s["ppm"] for s in on["pipeline"].status()]
    assert np.allclose(est, on["src"].eps[1:] * 1e6, atol=0.02)


def test_fast_lock_keeps_manual_knob_meaning():
    r = run_sim(n_aux=1, f_tune=435e6, phase_noise=1.2, fast="on", seconds=6)
    pl, src = r["pipeline"], r["src"]

    def supp(n=15):
        a = b = 0.0; k = 0
        while k < n:
            pl.feed(src.read())
            while (o := pl.step()) is not None:
                a += np.mean(np.abs(o["d"]) ** 2); b += np.mean(np.abs(o["y"]) ** 2); k += 1
        return 10 * np.log10(a / b)
    base = supp()
    g, p = pl.canc.channel_knob(0)
    pl.canc.set_manual(0, phase_deg=p + 40)      # deliberately detune by hand
    detuned = supp()
    assert detuned < base - 4                     # the phase lock didn't "fix" the knob


def _tone(mode, fs=240e3, off=30e3, n=240000):
    t = np.arange(n) / fs
    msg = np.sin(2 * np.pi * 1000 * t)
    if mode == "fm":
        x = np.exp(1j * (2 * np.pi * off * t + 2 * np.pi * 3000 * np.cumsum(msg) / fs))
    elif mode == "am":
        x = (1 + 0.5 * msg) * np.exp(2j * np.pi * off * t)
    elif mode == "usb":
        x = np.exp(2j * np.pi * (off + 1000) * t)
    elif mode == "lsb":
        x = np.exp(2j * np.pi * (off - 1000) * t)
    else:  # cw: carrier 0 Hz offset -> 700 Hz tone
        x = np.exp(2j * np.pi * off * t)
    return (0.3 * x).astype(np.complex64)


@pytest.mark.parametrize("mode,expect", [("am", 1000), ("fm", 1000), ("usb", 1000),
                                         ("lsb", 1000), ("cw", 700)])
def test_demod_modes(mode, expect):
    d = Demodulator(240e3, 30e3, mode)
    x = _tone(mode)
    a = np.concatenate([d.process(x[i:i + 8192]) for i in range(0, len(x), 8192)])[8000:]
    f = np.fft.rfftfreq(len(a), 1 / 48000)
    assert abs(f[np.argmax(np.abs(np.fft.rfft(a)))] - expect) < 5
    assert a.std() > 0.05


def test_usb_rejects_lsb_side():
    d = Demodulator(240e3, 30e3, "usb")
    x = _tone("lsb")
    a = np.concatenate([d.process(x[i:i + 8192]) for i in range(0, len(x), 8192)])[8000:]
    d2 = Demodulator(240e3, 30e3, "lsb")
    b = np.concatenate([d2.process(x[i:i + 8192]) for i in range(0, len(x), 8192)])[8000:]
    # AGC normalises level, so compare the 1 kHz content against the right sideband
    assert np.abs(np.fft.rfft(b)).max() > 10 * np.abs(np.fft.rfft(a)).max() or a.std() < 0.05


def test_launcher_demo_start_stop(tmp_path, monkeypatch):
    monkeypatch.setenv("RTLNC_CONFIG_DIR", str(tmp_path))
    import threading
    from http.server import ThreadingHTTPServer
    from pathlib import Path
    import rtlnc.launcher as Lm
    L = Lm.Launcher()
    L.s.update(demo=True, local_audio=False, freq_mhz=146.52, mode="fm",
               bus_port=7411, panel_port=8411, cat="none")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Lm.make_handler(
        L, (Path(Lm.__file__).parent / "launcher.html").read_bytes()))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    B = f"http://127.0.0.1:{srv.server_address[1]}"
    post = lambda p, b: urllib.request.urlopen(urllib.request.Request(
        B + p, data=json.dumps(b).encode(), headers={"Content-Type": "application/json"}))
    post("/api/start", {})
    st = {}
    for _ in range(30):
        time.sleep(1)
        st = json.loads(urllib.request.urlopen(B + "/api/state").read())
        if st["status"].get("state") == "track" and st["status"].get("cancel_db") is not None:
            break
    assert st["running"] and st["status"]["demod"] == "fm" and st["status"]["fast_on"]
    post("/api/settings", {"mode": "usb"})                 # live change
    for _ in range(10):
        time.sleep(0.5)
        if json.loads(urllib.request.urlopen(B + "/api/state").read())["status"]["demod"] == "usb":
            break
    else:
        pytest.fail("mode did not change live")
    post("/api/stop", {})
    assert not json.loads(urllib.request.urlopen(B + "/api/state").read())["running"]
    srv.shutdown()
