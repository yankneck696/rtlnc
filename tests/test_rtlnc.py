import numpy as np
import pytest

from rtlnc.metrics import run_sim, band_sinr
from rtlnc.pipeline import Pipeline
from rtlnc.resample import StreamResampler
from rtlnc.sim import SimSource
from rtlnc.sync import envelope_lag, estimate_cfo


def test_resampler_identity_and_ratio():
    rng = np.random.default_rng(0)
    x = (rng.normal(size=20000) + 1j * rng.normal(size=20000)).astype(np.complex64)
    from scipy import signal
    x = signal.lfilter(signal.firwin(255, 0.8), [1.0], x).astype(np.complex64)
    rs = StreamResampler(); rs.push(x)
    y = rs.pull(10000, 1.0)
    err = np.mean(np.abs(y[300:] - x[300:10000]) ** 2) / np.mean(np.abs(x) ** 2)
    assert 10 * np.log10(err) < -50
    # a slow tone survives a 3 ppm ratio change with a tiny error
    n = np.arange(200000)
    tone = np.exp(2j * np.pi * 0.35 * n).astype(np.complex64)
    rs = StreamResampler(); rs.push(tone)
    r = 1 + 3e-6
    y = rs.pull(150000, r)
    ref = np.exp(2j * np.pi * 0.35 * (np.arange(150000) * r))
    assert np.max(np.abs(y[100:] - ref[100:])) < 1e-3     # better than -60 dB


def test_acquisition_finds_offsets():
    src = SimSource(n_aux=2, seed=5, ppm_spread=3)
    pl = Pipeline(src.fs, src.f_tune, 2)
    while pl.state == "acquire":
        pl.feed(src.read()); pl.step()
    est = np.array([s["ppm"] for s in pl.status()])
    assert np.allclose(est, src.eps[1:] * 1e6, atol=0.05)


@pytest.mark.parametrize("n_aux,min_gain", [(1, 18), (2, 16), (3, 14)])
def test_cancellation_independent_clocks(n_aux, min_gain):
    r = run_sim(n_aux=n_aux, ppm_spread=3)
    assert r["gain_db"] > min_gain, r["gain_db"]


@pytest.mark.parametrize("f", [1.85e6, 7.1e6, 28.4e6])
def test_hf_bands(f):
    assert run_sim(n_aux=2, f_tune=f, ppm_spread=3)["gain_db"] > 16


def test_exclusion_protects_strong_station():
    a = run_sim(n_aux=2, aux_leak_db=-3, noise_to_signal_db=-5)
    b = run_sim(n_aux=2, aux_leak_db=-3, noise_to_signal_db=-5, exclude_bw=0)
    assert a["gain_db"] > b["gain_db"] + 10


def test_recovers_from_usb_dropout():
    src = SimSource(n_aux=2, seed=7)
    pl = Pipeline(src.fs, src.f_tune, 2, listen_offset=src.listen_offset)
    outs = []
    for i in range(int(src.fs * 16 / src.chunk)):
        ch = src.read()
        if i == 60:                      # aux 1 loses 3000 samples
            ch[1] = ch[1][3000:]
        pl.feed(ch)
        while (o := pl.step()) is not None:
            outs.append(o)
    assert any("realigned" in l for l in pl.log), pl.log
    tail = outs[-int(4 * src.fs / pl.L):]
    i0 = tail[0]["index"]
    y = np.concatenate([o["y"] for o in tail]); d = np.concatenate([o["d"] for o in tail])
    t = src.main_truth()[i0:i0 + len(y)]
    lo, hi = src.listen_offset + 300, src.listen_offset + 2700
    assert band_sinr(y, t, src.fs, lo, hi) - band_sinr(d, t, src.fs, lo, hi) > 15


def test_constrained_solve_other_channel_compensates():
    """Fix aux1 by hand at a bad setting: auto aux2 must still beat no-cancel."""
    from rtlnc.canceller import Canceller
    rng = np.random.default_rng(2)
    L = 8192
    s1 = rng.normal(size=(40, L)) + 1j * rng.normal(size=(40, L))
    s2 = rng.normal(size=(40, L)) + 1j * rng.normal(size=(40, L))
    c = Canceller(2, taps=3)
    res = {}
    for mode1 in ("auto", "manual"):
        c.reset(); c.ch_mode = [mode1, "auto"]; c.manual[0] = 0.0
        for b in range(40):
            main = (s1[b] * (0.8 - 0.3j) + s2[b] * (0.5 + 0.6j)).astype(np.complex64)
            aux = np.stack((s1[b] + 0.01 * s2[b], s2[b])).astype(np.complex64)
            r = c.process(main, aux)
        res[mode1] = 10 * np.log10(np.mean(np.abs(r["d"]) ** 2) / np.mean(np.abs(r["y"]) ** 2))
    assert res["auto"] > 30          # both nulls
    assert 2 < res["manual"] < 10    # aux1 forced to zero: only source 2 removed


def test_manual_takeover_is_seamless():
    """Touching a knob must not lose the auto null; a 1 degree nudge costs little."""
    from rtlnc.metrics import run_sim
    import rtlnc.metrics as M
    r = run_sim(n_aux=1, seconds=6)
    pl, src = r["pipeline"], r["src"]

    def supp(nblocks=20):
        pin = pout = 0.0; n = 0
        while n < nblocks:
            pl.feed(src.read())
            while (o := pl.step()) is not None:
                pin += np.mean(np.abs(o["d"]) ** 2); pout += np.mean(np.abs(o["y"]) ** 2); n += 1
        return 10 * np.log10(pin / pout)
    auto = supp()
    pl.canc.set_manual(0, phase_deg=pl.canc.channel_knob(0)[1])   # touch, no change
    same = supp()
    g, p = pl.canc.knob(pl.canc.manual[0])
    pl.canc.set_manual(0, phase_deg=p + 1.0)
    nudged = supp()
    assert abs(same - auto) < 0.5, (auto, same)
    assert nudged > auto - 3, (auto, nudged)
