import json
import threading
import urllib.error
import urllib.request

import pytest

from rtlnc import devconfig, eeprom


@pytest.fixture(autouse=True)
def tmp_config(tmp_path, monkeypatch):
    monkeypatch.setenv("RTLNC_CONFIG_DIR", str(tmp_path))
    yield tmp_path


def test_parse_factory_v4_image():
    info = eeprom.parse(eeprom.make_image())
    assert (info.manufacturer, info.product, info.serial) == ("RTLSDRBlog", "Blog V4", "00000001")
    assert info.is_v4 and info.max_serial_len() == 14


def test_with_serial_changes_only_the_serial():
    img = bytearray(eeprom.make_image())
    img[100:110] = b"IRDATA-XYZ"                          # data past the strings must survive
    new = eeprom.with_serial(bytes(img), "RTLNC-AUX1")
    info = eeprom.parse(new)
    assert info.serial == "RTLNC-AUX1" and info.is_v4
    assert new[100:110] == b"IRDATA-XYZ" and new[:9] == bytes(img[:9])
    # shorter serial afterwards: leftover bytes cleared, still parses
    info2 = eeprom.parse(eeprom.with_serial(new, "M1"))
    assert info2.serial == "M1" and info2.manufacturer == "RTLSDRBlog"


@pytest.mark.parametrize("bad", ["", " lead", "trail ", "ÄUX", "x" * 15, "tab\there"])
def test_bad_serials_rejected(bad):
    with pytest.raises(eeprom.EepromError):
        eeprom.with_serial(eeprom.make_image(), bad)


def test_set_serial_backup_verify_and_replug():
    io = eeprom.FakeIO(["00000001", "00000001"])
    r = eeprom.set_serial(io, 1, "RTLNC-MAIN")
    assert r["changed"] and r["backup"]
    assert eeprom.parse(open(r["backup"], "rb").read()).serial == "00000001"
    assert io.list()[1].serial == "00000001"              # like USB: needs a replug
    io.replug(1)
    assert io.list()[1].serial == "RTLNC-MAIN"
    eeprom.restore(io, 1, r["backup"])
    io.replug(1)
    assert io.list()[1].serial == "00000001"


def test_verify_catches_bad_write():
    class Flaky(eeprom.FakeIO):
        def write(self, i, data):
            super().write(i, data[:20] + b"\xff" + data[21:])
    with pytest.raises(eeprom.EepromError, match="verify failed"):
        eeprom.set_serial(Flaky(["00000001"]), 0, "RTLNC-AUX2")


def test_labels_roles_and_resolution():
    devconfig.set_label("RTLNC-MAIN", name="Main dipole", role="main", gain=30)
    devconfig.set_label("RTLNC-AUX2", name="Neighbour LED", role="aux2")
    devconfig.set_label("RTLNC-AUX1", name="Solar inverter", role="aux1", bias_tee=True)
    assert devconfig.from_roles() == ["RTLNC-MAIN", "RTLNC-AUX1", "RTLNC-AUX2"]
    assert devconfig.resolve("solar inverter") == "RTLNC-AUX1"
    assert devconfig.resolve("aux2") == "RTLNC-AUX2"
    assert devconfig.resolve("idx:3") == "idx:3"
    devconfig.set_label("XYZ", role="aux1")                # roles are unique
    assert devconfig.load()["devices"]["RTLNC-AUX1"]["role"] == "unused"
    devconfig.rename_serial("RTLNC-AUX2", "LED-AUX")
    assert devconfig.label("LED-AUX") == "Neighbour LED"


def test_web_api_demo_flow():
    from http.server import ThreadingHTTPServer
    from importlib import resources
    from rtlnc.addons.devices import make_handler
    io = eeprom.FakeIO(["00000001", "00000001"])
    page = resources.files("rtlnc.addons").joinpath("devices.html").read_bytes()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(io, page, True))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def get(p):
        return json.loads(urllib.request.urlopen(base + p).read())

    def post(p, body):
        r = urllib.request.Request(base + p, data=json.dumps(body).encode(),
                                   headers={"Content-Type": "application/json"})
        try:
            return 200, json.loads(urllib.request.urlopen(r).read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    assert b"Numbering wizard" in urllib.request.urlopen(base + "/").read()
    d = get("/api/devices")
    assert all(x["duplicate"] for x in d["devices"]) and d["suggest"]["serial"] == "RTLNC-MAIN"
    code, r = post("/api/serial", {"index": 0, "serial": "RTLNC-MAIN",
                                   "label": {"name": "Main dipole", "role": "main"}})
    assert code == 200 and r["changed"]
    post("/api/demo/replug", {"index": 0})
    d = get("/api/devices")
    assert d["devices"][0]["serial"] == "RTLNC-MAIN" and d["devices"][0]["label"]["role"] == "main"
    assert d["suggest"]["serial"] == "RTLNC-AUX1"
    code, r = post("/api/serial", {"index": 1, "serial": "way-too-long-serial"})
    assert code == 400 and "too long" in r["error"]
    code, r = post("/api/label", {"serial": "RTLNC-MAIN", "gain": "28.5", "bias_tee": False,
                                  "name": "Main", "role": "main", "notes": ""})
    assert code == 200 and r["gain"] == 28.5
    srv.shutdown()


def test_core_uses_roles(tmp_config, monkeypatch):
    """rtlnc run with no --devices opens main + aux in role order, with saved gains."""
    import rtlnc.app as app
    devconfig.set_label("S-MAIN", name="Main", role="main", gain=33)
    devconfig.set_label("S-AUX1", name="Inverter", role="aux1", gain=21, bias_tee=True)
    seen = {}

    class FakeMulti:
        def __init__(self, devs, fs, fc, gains, bias_tee=None):
            seen.update(devs=devs, gains=gains, bt=bias_tee)
        def start(self): pass
    monkeypatch.setattr(app, "MultiRTL", FakeMulti)
    monkeypatch.setattr("sys.argv", ["rtlnc", "run", "--no-audio", "--bus", "127.0.0.1:0"])
    core = app.Core(app._parse())
    assert seen == {"devs": ["S-MAIN", "S-AUX1"], "gains": [33.0, 21.0], "bt": [False, True]}
    assert core.names == ["Main", "Inverter"]
    core.bus.close()
