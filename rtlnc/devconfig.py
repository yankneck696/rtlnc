"""Friendly names and roles for dongles, keyed by EEPROM serial.

Stored in  ~/.config/rtlnc/devices.json  (on Windows: %APPDATA%\\rtlnc\\devices.json;
override with the RTLNC_CONFIG_DIR environment variable).

    {"devices": {
        "RTLNC-MAIN": {"name": "Main dipole", "role": "main", "gain": 30, "bias_tee": false,
                       "notes": "80m dipole, feedline via preselector"},
        "RTLNC-AUX1": {"name": "Solar inverter", "role": "aux1", "gain": 20, "bias_tee": true,
                       "notes": "1 m active loop on the garage wall"}}}

With this file, `rtlnc run` needs no --devices: it opens the "main" dongle and
then aux1, aux2, ... in order. --devices also accepts these names.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROLES = ["main"] + [f"aux{i}" for i in range(1, 9)] + ["unused"]


def config_dir() -> Path:
    if os.environ.get("RTLNC_CONFIG_DIR"):
        d = Path(os.environ["RTLNC_CONFIG_DIR"])
    elif sys.platform == "win32":
        d = Path(os.environ.get("APPDATA", Path.home())) / "rtlnc"
    else:
        d = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "rtlnc"
    d.mkdir(parents=True, exist_ok=True)
    return d


def path() -> Path:
    return config_dir() / "devices.json"


def load() -> dict:
    p = path()
    if not p.exists():
        return {"devices": {}}
    try:
        d = json.loads(p.read_text())
        d.setdefault("devices", {})
        return d
    except json.JSONDecodeError as e:
        raise SystemExit(f"{p} is not valid JSON: {e}")


def save(cfg: dict) -> None:
    p = path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2, sort_keys=True))
    tmp.replace(p)


def set_label(serial: str, **fields) -> dict:
    cfg = load()
    entry = cfg["devices"].setdefault(serial, {})
    for k, v in fields.items():
        if v is None:
            continue
        if k == "role":
            if v not in ROLES:
                raise ValueError(f"role must be one of {ROLES}")
            if v != "unused":                       # a role belongs to one dongle only
                for s, e in cfg["devices"].items():
                    if s != serial and e.get("role") == v:
                        e["role"] = "unused"
        entry[k] = v
    save(cfg)
    return entry


def rename_serial(old: str, new: str) -> None:
    """Carry the label across after the EEPROM serial is changed."""
    cfg = load()
    if old in cfg["devices"] and old != new:
        cfg["devices"][new] = {**cfg["devices"].pop(old), **cfg["devices"].get(new, {})}
        save(cfg)


def resolve(spec: str, cfg: dict | None = None) -> str:
    """Turn a --devices entry (serial, friendly name, role or idx:N) into a serial or idx:N."""
    cfg = cfg or load()
    if spec.startswith("idx:") or spec in cfg["devices"]:
        return spec
    for s, e in cfg["devices"].items():
        if e.get("name", "").lower() == spec.lower() or e.get("role") == spec.lower():
            return s
    return spec


def from_roles(cfg: dict | None = None) -> list[str]:
    """Serials in order: main, aux1, aux2, ... (gaps are skipped)."""
    cfg = cfg or load()
    by_role = {e.get("role"): s for s, e in cfg["devices"].items()}
    if "main" not in by_role:
        return []
    out = [by_role["main"]]
    for r in sorted((r for r in by_role if re.fullmatch(r"aux\d+", r or "")),
                    key=lambda r: int(r[3:])):
        out.append(by_role[r])
    return out


def label(serial: str, cfg: dict | None = None) -> str:
    cfg = cfg or load()
    return cfg["devices"].get(serial, {}).get("name") or serial
