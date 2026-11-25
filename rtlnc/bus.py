"""The rtlnc IQ bus: how the core and the separate add-on programs talk.

The core (``rtlnc run``) listens on TCP, by default 127.0.0.1:7355. Each
add-on is its own process and connects as a client.

Core -> client, repeated frames:
    b"RNC1" | u32 json_len | u32 payload_len | JSON header | complex64 IQ (LE)

    header: {"type": "iq"|"status", "seq", "index", "fs", "fc", "gen",
             "listen", "ptt", "mode", ...}
    * index: sample number of payload[0] in the main stream (gaps = dropped)
    * gen:   increments on every retune or re-acquire; reset filters
    * fc:    the dongles' tuned centre; payload is complex baseband around it
    * a "status" frame (no payload) is sent about once a second

Client -> core: one JSON command per line:
    {"cmd": "hello", "name": "rtlnc-tcp", "iq": true}   iq=false: status only
    {"cmd": "tune", "fc": 7.13e6}         dongle centre frequency
    {"cmd": "listen", "freq": 7.160e6}    where you listen (retunes if outside span)
    {"cmd": "ptt", "on": true}            freeze canceller, mute RF/audio outputs
    {"cmd": "mode", "mode": "auto"|"freeze"|"bypass"}
    {"cmd": "exclude", "bw": "auto" | 8000 | 0}

The protocol is small enough to drive from GNU Radio, a script or another
language.
"""
from __future__ import annotations

import json
import queue
import socket
import struct
import threading
import time

import numpy as np

MAGIC = b"RNC1"
DEFAULT_PORT = 7355
_HDR = struct.Struct("<4sII")


def parse_addr(s: str, default_host: str = "127.0.0.1") -> tuple[str, int]:
    if ":" in s:
        h, p = s.rsplit(":", 1)
        return h or default_host, int(p)
    return default_host, int(s)


def encode(header: dict, iq: np.ndarray | None = None) -> bytes:
    j = json.dumps(header, separators=(",", ":")).encode()
    payload = b"" if iq is None else np.ascontiguousarray(iq, dtype="<c8").tobytes()
    return _HDR.pack(MAGIC, len(j), len(payload)) + j + payload


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("bus closed")
        buf += chunk
    return bytes(buf)


# ---------------------------------------------------------------------------
class _Peer:
    def __init__(self, sock: socket.socket, addr, server: "BusServer"):
        self.sock, self.addr, self.server = sock, addr, server
        self.name, self.want_iq = f"{addr[0]}:{addr[1]}", True
        self.q: queue.Queue = queue.Queue(maxsize=256)    # ~9 s of IQ at 240k
        self.alive = True
        self.dropped = 0
        threading.Thread(target=self._writer, daemon=True).start()
        threading.Thread(target=self._reader, daemon=True).start()

    def send(self, frame: bytes, is_iq: bool) -> None:
        if not self.alive or (is_iq and not self.want_iq):
            return
        try:
            self.q.put_nowait(frame)
        except queue.Full:
            self.dropped += 1        # slow client: drop; it sees the index gap

    def _writer(self) -> None:
        try:
            while self.alive:
                self.sock.sendall(self.q.get())
        except OSError:
            pass
        self.close()

    def _reader(self) -> None:
        f = self.sock.makefile("r", encoding="utf-8", newline="\n")
        try:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    cmd = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if cmd.get("cmd") == "hello":
                    self.name = str(cmd.get("name", self.name))
                    self.want_iq = bool(cmd.get("iq", True))
                    self.server.log(f"bus: {self.name} connected ({'iq' if self.want_iq else 'control'})")
                    continue
                cmd["_from"] = self.name
                self.server.commands.put(cmd)
        except (OSError, ValueError):
            pass
        self.close()

    def close(self) -> None:
        if self.alive:
            self.alive = False
            try:
                self.sock.close()
            except OSError:
                pass
            self.server.log(f"bus: {self.name} disconnected")


class BusServer:
    """Runs inside the core. publish() never blocks the DSP loop."""

    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_PORT):
        self.commands: queue.Queue = queue.Queue()
        self.peers: list[_Peer] = []
        self._log: queue.Queue = queue.Queue()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        from .netutil import exclusive, port_in_use_message
        exclusive(self.sock)
        try:
            self.sock.bind((host, port))
        except OSError as e:
            raise SystemExit(port_in_use_message("the rtlnc core", port)) from e
        self.sock.listen(8)
        self.addr = self.sock.getsockname()
        self.seq = 0
        threading.Thread(target=self._accept, daemon=True).start()

    def log(self, msg: str) -> None:
        self._log.put(msg)

    def drain_log(self) -> list[str]:
        out = []
        while not self._log.empty():
            out.append(self._log.get_nowait())
        return out

    def _accept(self) -> None:
        while True:
            try:
                s, a = self.sock.accept()
            except OSError:
                return
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.peers.append(_Peer(s, a, self))

    def publish(self, header: dict, iq: np.ndarray | None = None) -> None:
        self.peers = [p for p in self.peers if p.alive]
        if not self.peers:
            return
        header = dict(header, seq=self.seq)
        self.seq += 1
        frame = encode(header, iq)
        for p in self.peers:
            p.send(frame, iq is not None)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass
        for p in self.peers:
            p.close()


# ---------------------------------------------------------------------------
class BusClient:
    """Used by the add-ons. Reconnects automatically while the core restarts."""

    def __init__(self, addr: str = f"127.0.0.1:{DEFAULT_PORT}", name: str = "client",
                 iq: bool = True, retry: float = 2.0, verbose: bool = True):
        self.host, self.port = parse_addr(addr)
        self.name, self.iq, self.retry, self.verbose = name, iq, retry, verbose
        self.sock: socket.socket | None = None
        self._lock = threading.Lock()
        self.last_status: dict = {}

    def _connect(self) -> None:
        while True:
            try:
                s = socket.create_connection((self.host, self.port), timeout=5)
                s.settimeout(None)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self.sock = s
                self._send_raw({"cmd": "hello", "name": self.name, "iq": self.iq})
                if self.verbose:
                    print(f"[{self.name}] connected to rtlnc core at {self.host}:{self.port}")
                return
            except OSError:
                if self.verbose:
                    print(f"[{self.name}] waiting for rtlnc core at {self.host}:{self.port} ...")
                time.sleep(self.retry)

    def _send_raw(self, cmd: dict) -> None:
        with self._lock:
            if self.sock is None:
                raise OSError("not connected to the rtlnc core")
            self.sock.sendall((json.dumps(cmd) + "\n").encode())

    def send(self, cmd: dict) -> bool:
        try:
            self._send_raw(cmd)
            return True
        except OSError:
            return False

    def frames(self):
        """Yield (header, iq-or-None) forever, reconnecting as needed."""
        while True:
            if self.sock is None:
                self._connect()
            try:
                magic, jl, pl = _HDR.unpack(_recv_exact(self.sock, _HDR.size))
                if magic != MAGIC:
                    raise ConnectionError("bad magic - not an rtlnc core?")
                hdr = json.loads(_recv_exact(self.sock, jl))
                iq = np.frombuffer(_recv_exact(self.sock, pl), dtype="<c8") if pl else None
                if hdr.get("type") == "status":
                    self.last_status = hdr
                yield hdr, iq
            except (OSError, ConnectionError, struct.error, json.JSONDecodeError):
                try:
                    if self.sock:
                        self.sock.close()
                except OSError:
                    pass
                self.sock = None
                yield {"type": "disconnected"}, None

    def close(self) -> None:
        if self.sock:
            self.sock.close()
