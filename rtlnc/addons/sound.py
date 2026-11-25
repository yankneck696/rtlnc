"""rtlnc-sound: cancelled signal to sound devices.

Two independent outputs; use one or both:

  IQ to a (virtual) sound card, stereo float, left = I, right = Q:
      rtlnc-sound --iq-device "CABLE Input" --iq-rate 192000
    Then choose that cable's *recording* side as a soundcard IQ source in
    HDSDR, SDR Console, Quisk, Linrad and similar. Set the program's LO to
    the core's centre frequency, shown in the status line.

  Demodulated audio for digital-mode and decoder software:
      rtlnc-sound --audio-device "CABLE-B Input" --mode usb
    WSJT-X / JS8Call / fldigi then use the cable's recording side as input.
    The demodulator follows the core's listen frequency, so run rtlnc-rig to
    follow the radio, or set `listen` in the core.

Virtual cables: VB-Audio Cable (Windows), BlackHole (macOS), a PipeWire or
PulseAudio null sink (Linux). Use `--list` to see device names.
"""
from __future__ import annotations

import argparse

import numpy as np

from ..bus import DEFAULT_PORT, BusClient
from ..demod import Demodulator
from .common import Converter, Leveler, SoundOut, iq_to_stereo


def main(argv=None):
    ap = argparse.ArgumentParser(prog="rtlnc-sound", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bus", default=f"127.0.0.1:{DEFAULT_PORT}")
    ap.add_argument("--list", action="store_true", help="list sound devices and exit")
    ap.add_argument("--iq-device", help="sound device for stereo IQ output")
    ap.add_argument("--iq-rate", type=float, default=192000)
    ap.add_argument("--iq-swap", action="store_true", help="swap I/Q (spectrum inverted)")
    ap.add_argument("--audio-device", help="sound device for demodulated audio")
    ap.add_argument("--mode", default=None, choices=["am", "fm", "usb", "lsb", "cw"],
                    help="demodulator (default: follow the mode chosen in the core/panel)")
    ap.add_argument("--no-ptt-mute", action="store_true", help="keep audio during PTT")
    a = ap.parse_args(argv)
    if a.list:
        import sounddevice as sd
        print(sd.query_devices())
        return
    if not (a.iq_device or a.audio_device):
        ap.error("give --iq-device and/or --audio-device (see --list)")

    bus = BusClient(a.bus, name="rtlnc-sound")
    iq_out = SoundOut(a.iq_device, a.iq_rate, 2) if a.iq_device else None
    iq_conv = Converter(a.iq_rate, Leveler(-20.0)) if iq_out else None
    au_out = SoundOut(a.audio_device, 48000, 1) if a.audio_device else None
    dem, dem_key = None, None
    last_fc = None
    try:
        for hdr, iq in bus.frames():
            if hdr.get("type") != "iq" or iq is None:
                continue
            if hdr["fc"] != last_fc:
                last_fc = hdr["fc"]
                print(f"[rtlnc-sound] core centre {last_fc/1e6:.4f} MHz "
                      f"(set this as the LO in your IQ program)")
            mute = hdr.get("ptt") and not a.no_ptt_mute
            if iq_out:
                y = iq_conv.process(hdr, iq)
                if len(y):
                    iq_out.put(iq_to_stereo(y * (0 if mute else 1), a.iq_swap))
            if au_out:
                off = (hdr["listen"] - hdr["fc"]) if hdr.get("listen") else 0.0
                mode = a.mode or hdr.get("demod", "usb")
                key = (hdr["gen"], hdr["fs"])
                if key != dem_key:
                    dem, dem_key = Demodulator(hdr["fs"], off, mode), key
                if dem.mode != mode:
                    dem.set_mode(mode)
                dem.offset = off
                aud = dem.process(iq)
                au_out.put(aud * (0 if mute else 1))
    except KeyboardInterrupt:
        pass
    finally:
        for o in (iq_out, au_out):
            if o:
                o.close()


if __name__ == "__main__":
    main()
