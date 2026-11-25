"""Listening sockets that refuse to share a port.

On Windows, SO_REUSEADDR lets a second program bind a port that is already in
use, so two copies of the panel or the core can end up answering on the same
port. These helpers use SO_EXCLUSIVEADDRUSE on Windows and SO_REUSEADDR
elsewhere. There it only allows a quick restart and never shares a live port.
"""
from __future__ import annotations

import socket
import sys
from http.server import ThreadingHTTPServer


def exclusive(sock: socket.socket) -> None:
    if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)


class HTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False          # we set the right option ourselves
    daemon_threads = True

    def server_bind(self):
        exclusive(self.socket)
        super().server_bind()


def port_in_use_message(name: str, port: int) -> str:
    return (f"port {port} is already in use, so {name} can't start. Is rtlnc already "
            f"running in another window (see Task Manager), or is another program using it?")
