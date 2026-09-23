"""Small CONNECT proxy for networked harness containers.

The proxy runs in a separate Docker container. Its allowlist is supplied as
arguments by the runner; the harness container only joins the internal network.
"""

import selectors
import socket
import socketserver
import sys


ALLOWED = set(sys.argv[1:])


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        line = self.rfile.readline(8192).decode("ascii", "replace").strip()
        parts = line.split()
        while self.rfile.readline(8192) not in (b"\r\n", b"\n", b""):
            pass
        if len(parts) != 3 or parts[0] != "CONNECT" or ":" not in parts[1]:
            self.wfile.write(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            return
        host, port = parts[1].rsplit(":", 1)
        if host not in ALLOWED or port != "443":
            self.wfile.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            return
        try:
            upstream = socket.create_connection((host, 443), timeout=15)
        except OSError:
            self.wfile.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            return
        with upstream:
            self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            self.wfile.flush()
            selector = selectors.DefaultSelector()
            selector.register(self.connection, selectors.EVENT_READ, upstream)
            selector.register(upstream, selectors.EVENT_READ, self.connection)
            with selector:
                while True:
                    for key, _ in selector.select(60):
                        data = key.fileobj.recv(65536)
                        if not data:
                            return
                        key.data.sendall(data)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    with Server(("0.0.0.0", 8888), Handler) as server:
        server.serve_forever()
