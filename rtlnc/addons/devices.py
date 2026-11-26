"""rtlnc-devices: give each RTL-SDR a unique serial and a friendly name/role.

Run this while the rtlnc core is NOT running (it needs the dongles free).

    rtlnc-devices web                     editor + plug-in wizard: http://127.0.0.1:8074
    rtlnc-devices list
    rtlnc-devices set-serial 0 RTLNC-AUX1   write a serial to dongle index 0 (EEPROM)
    rtlnc-devices name RTLNC-AUX1 --name "Solar inverter" --role aux1 --gain 20 --bias-tee on
    rtlnc-devices info 0                  decode the EEPROM of dongle 0
    rtlnc-devices backup 0                save its EEPROM image
    rtlnc-devices restore 0 FILE          write a saved image back

Serial (stored in the dongle): up to 14 printable ASCII characters on a V4,
13 on a V4L (its longer product name takes the space).
That is enough for a readable tag like RTLNC-AUX1 or AUX1-INVERTER. It goes
wherever the dongle goes. A new serial shows up after you unplug and replug.

Name, role, gain, bias-tee and notes are stored on this PC in
devices.json and keyed by serial. The core uses them: `rtlnc run` with no
--devices opens main + aux1, aux2, ..., and the panel labels each strip.

New dongles all share the factory serial (00000001), so you can't tell
which is which by software. The wizard in `web` therefore has you plug them
in one at a time.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from .. import devconfig, eeprom
from ..eeprom import model_of
from ..netutil import HTTPServer, port_in_use_message
from ..bus import parse_addr


def _rows(io) -> list[dict]:
    cfg = devconfig.load()
    devs = io.list()
    counts: dict[str, int] = {}
    for d in devs:
        counts[d.serial] = counts.get(d.serial, 0) + 1
    rows = []
    for d in devs:
        rows.append({"index": d.index, "manufacturer": d.manufacturer, "product": d.product,
                     "serial": d.serial, "duplicate": counts[d.serial] > 1,
                     "factory": d.serial in eeprom.DEFAULT_SERIALS,
                     "is_v4": model_of(d.manufacturer, d.product) is not None,
                     "model": model_of(d.manufacturer, d.product) or d.product,
                     "label": cfg["devices"].get(d.serial, {})})
    return rows


def suggest(io) -> dict:
    """Next sensible serial + role for a new dongle."""
    cfg = devconfig.load()
    used_roles = {e.get("role") for e in cfg["devices"].values()}
    serials = {d.serial for d in io.list()} | set(cfg["devices"])
    if "main" not in used_roles and "RTLNC-MAIN" not in serials:
        return {"serial": "RTLNC-MAIN", "role": "main", "name": "Main antenna"}
    for i in range(1, 9):
        if f"aux{i}" not in used_roles and f"RTLNC-AUX{i}" not in serials:
            return {"serial": f"RTLNC-AUX{i}", "role": f"aux{i}", "name": f"Aux {i}"}
    return {"serial": "", "role": "unused", "name": ""}


def write_serial(io, index: int, serial: str) -> dict:
    devs = {d.index: d for d in io.list()}
    if index not in devs:
        raise eeprom.EepromError(f"no dongle at index {index}")
    old_usb = devs[index].serial
    res = eeprom.set_serial(io, index, serial)
    clash = [d for d in devs.values() if d.serial == serial and d.index != index]
    if clash:
        res["warning"] = f"another connected dongle already uses {serial}"
    if not devs[index].serial in eeprom.DEFAULT_SERIALS and not [
            d for d in devs.values() if d.serial == old_usb and d.index != index]:
        devconfig.rename_serial(old_usb, serial)       # keep its name/role
    return res


# ---------------------------------------------------------------------------
def make_handler(io, page: bytes, demo: bool):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, code: int, obj):
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
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)
            elif p == "/favicon.ico":
                self.send_response(204); self.end_headers()
            elif p == "/api/devices":
                try:
                    self._json(200, {"devices": _rows(io), "suggest": suggest(io),
                                     "roles": devconfig.ROLES, "config": str(devconfig.path()),
                                     "demo": demo})
                except Exception as e:
                    self._json(500, {"error": str(e)})
            else:
                self.send_error(404)

        def do_POST(self):
            p = self.path.split("?")[0]
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                if p == "/api/serial":
                    res = write_serial(io, int(body["index"]), str(body["serial"]).strip())
                    if body.get("label"):
                        devconfig.set_label(str(body["serial"]).strip(), **body["label"])
                    self._json(200, res)
                elif p == "/api/label":
                    lab = {k: body.get(k) for k in ("name", "role", "gain", "bias_tee", "notes")}
                    if lab["gain"] in ("", None):
                        lab["gain"] = None
                    else:
                        lab["gain"] = float(lab["gain"])
                    self._json(200, devconfig.set_label(str(body["serial"]), **lab))
                elif p == "/api/info":
                    info = eeprom.parse(io.read(int(body["index"])))
                    self._json(200, {**info.__dict__, "max_serial_len": info.max_serial_len(),
                                     "is_v4": info.is_v4, "model": info.model})
                elif p.startswith("/api/demo/") and demo:
                    act = p.rsplit("/", 1)[1]
                    i = int(body.get("index", 0))
                    if act == "replug":
                        io.replug(i)
                    elif act == "unplug":
                        io.unplug(i)
                    elif act == "plug":
                        io.plug()
                    self._json(200, {"ok": True})
                else:
                    self.send_error(404)
            except (eeprom.EepromError, ValueError, KeyError) as e:
                self._json(400, {"error": str(e)})
    return H


# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(prog="rtlnc-devices", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("web", help="editor + wizard in the browser")
    w.add_argument("--http", default="127.0.0.1:8074")
    w.add_argument("--demo", action="store_true", help="pretend dongles (try it without hardware)")
    sub.add_parser("list")
    s = sub.add_parser("set-serial"); s.add_argument("index", type=int); s.add_argument("serial")
    s.add_argument("--yes", action="store_true")
    n = sub.add_parser("name"); n.add_argument("serial")
    n.add_argument("--name"); n.add_argument("--role", choices=devconfig.ROLES)
    n.add_argument("--gain", type=float); n.add_argument("--bias-tee", choices=["on", "off"])
    n.add_argument("--notes")
    i = sub.add_parser("info"); i.add_argument("index", type=int)
    b = sub.add_parser("backup"); b.add_argument("index", type=int)
    r = sub.add_parser("restore"); r.add_argument("index", type=int); r.add_argument("file")
    r.add_argument("--yes", action="store_true")
    a = ap.parse_args(argv)

    if a.cmd == "name":
        e = devconfig.set_label(a.serial, name=a.name, role=a.role, gain=a.gain,
                                bias_tee=None if a.bias_tee is None else a.bias_tee == "on",
                                notes=a.notes)
        print(f"{a.serial}: {e}  ({devconfig.path()})")
        return

    try:
        io = eeprom.FakeIO() if getattr(a, "demo", False) else eeprom.RtlIO()
        if a.cmd == "list":
            rows = _rows(io)
            if not rows:
                print("no RTL-SDR dongles found")
            for d in rows:
                lab = d["label"]
                flag = " DUPLICATE" if d["duplicate"] else ""
                flag += " factory-serial" if d["factory"] else ""
                print(f"idx {d['index']}: {d['serial']:<15} {d['manufacturer']} / {d['product']}"
                      f"   {lab.get('name', '')} [{lab.get('role', '-')}]{flag}")
            if any(d["duplicate"] for d in rows):
                print("\nDuplicate serials: run `rtlnc-devices web` and use the wizard.")
        elif a.cmd == "info":
            info = eeprom.parse(io.read(a.index))
            for k, v in info.__dict__.items():
                print(f"{k:14s} {v if not isinstance(v, int) or isinstance(v, bool) else hex(v)}")
            print(f"{'max serial':14s} {info.max_serial_len()} chars   model: {info.model or 'unsupported'}")
        elif a.cmd == "backup":
            data = io.read(a.index)
            info = eeprom.parse(data)
            p = eeprom.backup_dir() / f"{info.serial or 'noserial'}-idx{a.index}-{time.strftime('%Y%m%d-%H%M%S')}.bin"
            p.write_bytes(data)
            print("saved", p)
        elif a.cmd == "set-serial":
            d = {x.index: x for x in io.list()}.get(a.index)
            if not d:
                sys.exit(f"no dongle at index {a.index}")
            if not a.yes:
                ok = input(f"Write serial {a.serial!r} to dongle {a.index} (now {d.serial!r})? [y/N] ")
                if ok.strip().lower() != "y":
                    sys.exit("cancelled")
            res = write_serial(io, a.index, a.serial)
            print(("written and verified" if res["changed"] else "unchanged") +
                  f"; backup: {res['backup']}" + (f"\nwarning: {res['warning']}" if res.get("warning") else ""))
            print("Unplug and replug the dongle to see the new serial.")
        elif a.cmd == "restore":
            if not a.yes and input(f"Write {a.file} to dongle {a.index}? [y/N] ").strip().lower() != "y":
                sys.exit("cancelled")
            eeprom.restore(io, a.index, a.file)
            print("restored and verified; replug the dongle")
        elif a.cmd == "web":
            page = (Path(__file__).parent / "devices.html").read_bytes()
            host, port = parse_addr(a.http)
            try:
                srv = HTTPServer((host, port), make_handler(io, page, a.demo))
            except OSError as e:
                raise SystemExit(port_in_use_message("the dongle editor", port)) from e
            print(f"[rtlnc-devices] open http://{host}:{srv.server_address[1]}/"
                  + ("   (demo: pretend dongles)" if a.demo else ""))
            try:
                srv.serve_forever()
            except KeyboardInterrupt:
                pass
    except eeprom.EepromError as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
