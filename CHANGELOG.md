# Changelog

## 0.3.2
- Fix: closing the launcher window could leave rtlnc running invisibly in
  the background (Edge keeps its process alive after the window closes),
  which then blocked updating ("rtlnc is still running"). The launcher now
  follows its window, not the Edge process, and every helper process exits
  by itself if the launcher is gone.
- Installer and uninstaller now close any running rtlnc themselves.

## 0.3.1
- Launcher: pressing Start without dongles set up now explains what to do
  (plug in / Zadig / set roles / Demo mode) instead of failing in the log.
- Launcher: if the core stops, the panel and other parts stop with it, and
  a second Start no longer leaves a duplicate panel running.
- Windows: a port that is already in use can't be shared by a second copy;
  you get a clear message instead.
- GitHub build now rehearses the launcher (Demo-mode Start/Stop) on Windows
  and publishes a Release automatically when the version changes.
- README: how to get past browser/SmartScreen download warnings.

## 0.3.0
- **RTL-SDR Blog V4 only**; other dongles are refused.
- **0.5–450 MHz**: HF, 6 m, 2 m and 70 cm. A fast per-channel phase lock
  (default above 30 MHz) handles the higher oscillator phase noise of
  independent dongles at VHF/UHF.
- **Modes**: AM, FM (narrow), USB, LSB, CW, with squelch.
- **CAT radio follow**: Icom CI-V, Yaesu, Kenwood, Elecraft (direct serial),
  FLRig and Hamlib rigctld. Read-only, with TX-state hold.
- **Windows app**: launcher window, no console windows, bundled V4 driver
  library, installer, GitHub Actions build.
- Removed RF re-injection into the radio.

## 0.2.0
- Split into a core plus separate add-ons over a local IQ bus.
- Browser control panel with per-channel phase/gain knobs (Auto / Manual / Hold / Off).
- rtl_tcp server for SDR programs, sound-card IQ/audio output, Hamlib follow.
- Dongle serial/name editor with numbering wizard (EEPROM write with backup + verify).

## 0.1.0
- Multi-dongle joint least-squares canceller with independent-clock tracking
  (acquisition, FLL, sample-rate tracking, slip repair) and simulator.
