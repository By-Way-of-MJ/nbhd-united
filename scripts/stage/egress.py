"""CONNECT-only runtime egress: only TLS to OpenRouter can leave the network."""

import select
import socket
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def relay(left, right):
    sockets = (left, right)
    while ready := select.select(sockets, [], [], 360)[0]:
        for source in ready:
            chunk = source.recv(65536)
            if not chunk:
                return
            (right if source is left else left).sendall(chunk)


class LocalIngress(socketserver.BaseRequestHandler):
    def handle(self):
        # Fixed destination/port: never accept a caller-supplied proxy target.
        try:
            with socket.create_connection(("django", self.server.server_address[1]), timeout=10) as remote:
                relay(self.request, remote)
        except OSError:
            return


class Proxy(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Neither headers nor URLs belong in logs.

    def do_CONNECT(self):
        if self.path != "openrouter.ai:443":
            self.send_error(403, "Stage egress denied")
            return
        try:
            remote = socket.create_connection(("openrouter.ai", 443), timeout=15)
        except OSError:
            self.send_error(502)
            return
        with remote:
            self.send_response(200, "Connection established")
            self.end_headers()
            relay(self.connection, remote)


if __name__ == "__main__":
    for port in (8000, 8080):
        server = socketserver.ThreadingTCPServer(("0.0.0.0", port), LocalIngress)
        threading.Thread(target=server.serve_forever, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8888), Proxy).serve_forever()
