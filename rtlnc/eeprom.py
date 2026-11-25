"""RTL2832U EEPROM: read, edit the serial number, write back safely.

The layout follows osmocom rtl_eeprom.c:
    0x00  0x28 0x32           signature
    0x02  vendor id (LE)      0x04 product id (LE)
    0x06  0xA5 = has serial   0x07 flags (remote wake-up, IR)   0x08 misc
    0x09  USB string descriptors, back to back:
          [len][0x03][UTF-16LE text] x 3: manufacturer, product, serial
    rtl_eeprom keeps every descriptor below offset 78; everything after that
    is left untouched here.

Guards:
  * A backup of the full 256-byte image is saved before every write.
  * Only the serial is changed. Manufacturer and product are kept as they
    are, because the RTL-SDR Blog V4 driver recognises the dongle by
    "RTLSDRBlog" / "Blog V4" and switches on its HF upconverter only then.
  * The image is read back and compared after writing.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import time
from dataclasses import dataclass
from pathlib import Path

EEPROM_SIZE = 256
STR_OFFSET = 0x09
STR_LIMIT = 78                    # same limit rtl_eeprom enforces
DEFAULT_SERIALS = {"00000001", "00000000", ""}


class EepromError(Exception):
    pass


@dataclass
class EepromInfo:
    vendor_id: int
    product_id: int
    have_serial: bool
    manufacturer: str
    product: str
    serial: str
    strings_end: int

    @property
    def is_v4(self) -> bool:
        return self.manufacturer == "RTLSDRBlog" and self.product == "Blog V4"

    def max_serial_len(self) -> int:
        used = STR_OFFSET + (2 + 2 * len(self.manufacturer)) + (2 + 2 * len(self.product))
        return max(0, (STR_LIMIT - 1 - used - 2) // 2)


def _get_str(dat: bytes, pos: int) -> tuple[str, int]:
    n = dat[pos]
    if pos + 1 >= len(dat) or dat[pos + 1] != 0x03 or n < 2:
        raise EepromError(f"no string descriptor at offset {pos}")
    chars = []
    i = 2
    while i < n and pos + i < EEPROM_SIZE:
        chars.append(chr(dat[pos + i]))
        i += 2
    return "".join(chars), pos + i


def parse(dat: bytes) -> EepromInfo:
    if len(dat) < EEPROM_SIZE:
        raise EepromError("short EEPROM image")
    if dat[0] != 0x28 or dat[1] != 0x32:
        raise EepromError("not an RTL2832U EEPROM image (bad signature)")
    m, p1 = _get_str(dat, STR_OFFSET)
    p, p2 = _get_str(dat, p1)
    s, p3 = _get_str(dat, p2)
    return EepromInfo(dat[2] | dat[3] << 8, dat[4] | dat[5] << 8, dat[6] == 0xA5,
                      m, p, s, p3)


def validate_serial(serial: str, info: EepromInfo) -> None:
    if not serial or serial != serial.strip():
        raise EepromError("serial must not be empty or start/end with spaces")
    bad = [c for c in serial if not (0x20 <= ord(c) <= 0x7E)]
    if bad:
        raise EepromError(f"serial may only contain printable ASCII (bad: {''.join(bad)!r})")
    mx = info.max_serial_len()
    if len(serial) > mx:
        raise EepromError(f"serial too long: {len(serial)} chars, this dongle allows {mx}")


def with_serial(dat: bytes, serial: str) -> bytes:
    """Return a new 256-byte image that differs from ``dat`` only in the serial."""
    info = parse(dat)
    validate_serial(serial, info)
    out = bytearray(dat)
    pos = STR_OFFSET + (2 + 2 * len(info.manufacturer)) + (2 + 2 * len(info.product))
    desc = bytearray([2 + 2 * len(serial), 0x03])
    for ch in serial:
        desc += bytes([ord(ch), 0x00])
    end = pos + len(desc)
    if end >= STR_LIMIT:
        raise EepromError("serial does not fit")
    out[pos:end] = desc
    # clear what is left of a longer old serial (only inside the string area)
    for i in range(end, min(max(end, info.strings_end), STR_LIMIT)):
        out[i] = 0
    out[6] = 0xA5
    chk = parse(bytes(out))
    if (chk.serial, chk.manufacturer, chk.product) != (serial, info.manufacturer, info.product):
        raise EepromError("internal check failed; nothing written")
    return bytes(out)


def make_image(manufacturer="RTLSDRBlog", product="Blog V4", serial="00000001",
               vid=0x0BDA, pid=0x2838) -> bytes:
    """A factory-like image (used by the simulator and the tests)."""
    d = bytearray(EEPROM_SIZE)
    d[0:9] = bytes([0x28, 0x32, vid & 0xFF, vid >> 8, pid & 0xFF, pid >> 8, 0xA5, 0x16, 0x02])
    pos = STR_OFFSET
    for s in (manufacturer, product, serial):
        d[pos] = 2 + 2 * len(s); d[pos + 1] = 0x03
        for i, ch in enumerate(s):
            d[pos + 2 + 2 * i] = ord(ch)
        pos += 2 + 2 * len(s)
    return bytes(d)


# ---------------------------------------------------------------------------
@dataclass
class DongleInfo:
    index: int
    manufacturer: str
    product: str
    serial: str


class RtlIO:
    """Direct librtlsdr access through ctypes (EEPROM and USB strings)."""

    def __init__(self):
        path = None
        try:
            from rtlsdr.librtlsdr import librtlsdr as _l
            path = _l._name
        except Exception:
            path = ctypes.util.find_library("rtlsdr") or ctypes.util.find_library("librtlsdr")
        if not path:
            raise EepromError("librtlsdr not found; install the RTL-SDR driver/library")
        L = ctypes.CDLL(path)
        L.rtlsdr_get_device_count.restype = ctypes.c_uint32
        L.rtlsdr_get_device_usb_strings.argtypes = [ctypes.c_uint32, ctypes.c_char_p,
                                                    ctypes.c_char_p, ctypes.c_char_p]
        L.rtlsdr_open.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint32]
        L.rtlsdr_close.argtypes = [ctypes.c_void_p]
        for fn in (L.rtlsdr_read_eeprom, L.rtlsdr_write_eeprom):
            fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint8), ctypes.c_uint8,
                           ctypes.c_uint16]
            fn.restype = ctypes.c_int
        self.L = L

    def list(self) -> list[DongleInfo]:
        out = []
        for i in range(self.L.rtlsdr_get_device_count()):
            m, p, s = (ctypes.create_string_buffer(256) for _ in range(3))
            self.L.rtlsdr_get_device_usb_strings(i, m, p, s)
            out.append(DongleInfo(i, m.value.decode(errors="replace"),
                                  p.value.decode(errors="replace"),
                                  s.value.decode(errors="replace")))
        return out

    def _open(self, index: int):
        n = self.L.rtlsdr_get_device_count()
        if index >= n:
            raise EepromError(f"no dongle at index {index} ({n} connected)")
        dev = ctypes.c_void_p()
        r = self.L.rtlsdr_open(ctypes.byref(dev), index)
        if r < 0 or not dev:
            raise EepromError(f"cannot open dongle {index} (in use by rtlnc or another "
                              f"program? stop it first) [code {r}]")
        return dev

    def read(self, index: int) -> bytes:
        dev = self._open(index)
        try:
            buf = (ctypes.c_uint8 * EEPROM_SIZE)()
            r = self.L.rtlsdr_read_eeprom(dev, buf, 0, EEPROM_SIZE)
            if r < 0:
                raise EepromError({-3: "dongle has no EEPROM"}.get(r, f"EEPROM read failed [{r}]"))
            return bytes(buf)
        finally:
            self.L.rtlsdr_close(dev)

    def write(self, index: int, data: bytes) -> None:
        dev = self._open(index)
        try:
            buf = (ctypes.c_uint8 * EEPROM_SIZE).from_buffer_copy(data)
            r = self.L.rtlsdr_write_eeprom(dev, buf, 0, EEPROM_SIZE)
            if r < 0:
                raise EepromError(f"EEPROM write failed [{r}]")
            time.sleep(0.1)
        finally:
            self.L.rtlsdr_close(dev)


class FakeIO:
    """In-memory dongles, for --demo and the tests. Like real hardware, the USB
    serial seen by list() only changes after a 'replug'."""

    def __init__(self, serials=("00000001", "00000001", "00000001")):
        self.images = [make_image(serial=s) for s in serials]
        self.usb = list(serials)

    def list(self):
        return [DongleInfo(i, "RTLSDRBlog", "Blog V4", s) for i, s in enumerate(self.usb)]

    def read(self, i):
        return self.images[i]

    def write(self, i, data):
        self.images[i] = bytes(data)

    def replug(self, i):
        self.usb[i] = parse(self.images[i]).serial

    def unplug(self, i):
        del self.images[i]; del self.usb[i]

    def plug(self, serial="00000001"):
        self.images.append(make_image(serial=serial)); self.usb.append(serial)


# ---------------------------------------------------------------------------
def backup_dir() -> Path:
    from .devconfig import config_dir
    d = config_dir() / "eeprom-backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def set_serial(io, index: int, serial: str, backup: bool = True) -> dict:
    """Backup -> modify -> write -> verify. Returns details for the UI."""
    old = io.read(index)
    info = parse(old)
    new = with_serial(old, serial)
    bpath = None
    if backup:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in info.serial) or "noserial"
        bpath = backup_dir() / f"{safe}-idx{index}-{stamp}.bin"
        bpath.write_bytes(old)
    if new == old:
        return {"changed": False, "backup": str(bpath) if bpath else None, "old": info.serial}
    io.write(index, new)
    back = io.read(index)
    if back != new:
        raise EepromError("verify failed: EEPROM read-back differs. Restore with: "
                          f"rtlnc-devices restore {index} {bpath}")
    return {"changed": True, "backup": str(bpath) if bpath else None, "old": info.serial,
            "new": serial, "replug": True}


def restore(io, index: int, path: str) -> None:
    data = Path(path).read_bytes()
    parse(data)                                     # refuse garbage
    io.write(index, data)
    if io.read(index) != data:
        raise EepromError("verify failed after restore")
