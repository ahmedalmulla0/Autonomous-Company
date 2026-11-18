"""A per-invocation public-network proxy; never an arbitrary host socket bridge."""
from __future__ import annotations

import http.client
import ipaddress
import json
import os
import select
import socket
import socketserver
from urllib.parse import urlsplit


def public_address(host, port):
    if not host or port not in (80, 443):
        raise ValueError("Only public HTTP(S) destinations are available")
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise ValueError("Local, private and special-use destinations are blocked")
    # Resolve once, validate every answer, then connect to the pinned IP.
    return addresses[0][4][0]


def relay(left, right):
    while True:
        ready, _, _ = select.select([left, right], [], [], 120)
        if not ready:
            return
        for source in ready:
            data = source.recv(65536)
            if not data:
                return
            (right if source is left else left).sendall(data)


def header(sock):
    value = bytearray()
    while not value.endswith(b"\r\n\r\n"):
        item = sock.recv(1)
        if not item or len(value) >= 65536:
            raise ValueError("Invalid proxy request")
        value.extend(item)
    return bytes(value)


def connect_public(host, port, upstream=None):
    address = public_address(host, port)
    if not upstream:
        return socket.create_connection((address, port), timeout=30)
    parsed = urlsplit(upstream)
    if parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Upstream proxy must be an unauthenticated HTTP proxy")
    connection = socket.create_connection((parsed.hostname, parsed.port or 80), timeout=30)
    authority = f"[{address}]:{port}" if ":" in address else f"{address}:{port}"
    connection.sendall(f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode())
    response = header(connection)
    if response.split(b" ", 2)[1] != b"200":
        connection.close()
        raise ValueError("Upstream proxy refused public destination")
    return connection


class PublicProxy(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, path):
        self.upstream = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        super().__init__(str(path), ProxyRequest)


class ProxyRequest(socketserver.BaseRequestHandler):
    def handle(self):
        remote = None
        try:
            self.request.settimeout(30)
            raw = header(self.request)
            method, target, version = raw.split(b"\r\n", 1)[0].decode("ascii").split(" ")
            if method == "CONNECT":
                parsed = urlsplit("//" + target)
                host, port = parsed.hostname, parsed.port or 443
                if parsed.path or parsed.username or parsed.password or port != 443:
                    raise ValueError("Invalid CONNECT destination")
            else:
                parsed = urlsplit(target)
                if parsed.scheme != "http" or parsed.username or parsed.password:
                    raise ValueError("Invalid HTTP destination")
                host, port = parsed.hostname, parsed.port or 80
                if port != 80:
                    raise ValueError("Invalid HTTP port")
            remote = connect_public(host, port, self.server.upstream)
            if method == "CONNECT":
                self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            else:
                path = parsed.path or "/"
                if parsed.query:
                    path += "?" + parsed.query
                lines = raw.split(b"\r\n")[1:-2]
                lines = [line for line in lines if line.split(b":", 1)[0].lower() not in
                         (b"proxy-authorization", b"proxy-connection", b"connection", b"host")]
                lines += [f"Host: {host}".encode(), b"Connection: close"]
                remote.sendall(f"{method} {path} {version}\r\n".encode() + b"\r\n".join(lines) + b"\r\n\r\n")
            relay(self.request, remote)
        except (OSError, ValueError, UnicodeError):
            try:
                self.request.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
        finally:
            if remote:
                remote.close()


MODEL_PATH = "/v1/chat/completions"
MODEL_BODY_LIMIT = 8 * 1024 * 1024


def loopback_upstream(endpoint):
    """Validate a host-local model router URL (for example FreeLLMAPI)."""
    parsed = urlsplit(endpoint or "")
    if (parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or any(c.isspace() or ord(c) < 32 for c in endpoint)):
        raise ValueError("Model broker upstream must be a plain http:// loopback URL")
    host = parsed.hostname
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host.lower().rstrip(".") == "localhost"
    if not loopback:
        raise ValueError("Model broker upstream must be a loopback address")
    return host, parsed.port or 80, parsed.path or MODEL_PATH


class ModelBroker(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """Host-side broker for exactly one local model router.

    The namespace may only send ``POST /v1/chat/completions`` with a JSON body.
    The upstream is fixed by the host, the API key is injected here and never
    enters the namespace, and every other method, path or header is dropped.
    """
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, path, endpoint, api_key="", timeout=600, log=None):
        self.target = loopback_upstream(endpoint)
        self.api_key = api_key
        self.timeout = timeout
        self.log = log
        super().__init__(str(path), ModelRequest)


def reply(sock, status, reason, body=b"", content_type="application/json"):
    sock.sendall(f"HTTP/1.1 {status} {reason}\r\nContent-Type: {content_type}\r\n"
                 f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)


class ModelRequest(socketserver.BaseRequestHandler):
    def handle(self):
        server = self.server
        status = 403
        try:
            self.request.settimeout(server.timeout)
            raw = header(self.request)
            method, target, _ = raw.split(b"\r\n", 1)[0].decode("ascii").split(" ")
            path = urlsplit(target).path if target.startswith("http://") else target
            if method != "POST" or path != MODEL_PATH:
                raise PermissionError("Only POST /v1/chat/completions is brokered")
            fields = {}
            for line in raw.split(b"\r\n")[1:-2]:
                name, _, value = line.decode("latin-1").partition(":")
                fields[name.strip().lower()] = value.strip()
            if "transfer-encoding" in fields:
                raise PermissionError("Chunked request bodies are not brokered")
            length = int(fields.get("content-length", "-1"))
            if length < 0 or length > MODEL_BODY_LIMIT:
                raise PermissionError("Request body is missing or too large")
            body = bytearray()
            while len(body) < length:
                chunk = self.request.recv(min(65536, length - len(body)))
                if not chunk:
                    raise PermissionError("Truncated request body")
                body.extend(chunk)
            if not isinstance(json.loads(body), dict):
                raise PermissionError("Request body must be a JSON object")
            host, port, upstream_path = server.target
            headers = {"Content-Type": "application/json", "Accept": "application/json"}
            if server.api_key:
                headers["Authorization"] = f"Bearer {server.api_key}"
            connection = http.client.HTTPConnection(host, port, timeout=server.timeout)
            try:
                connection.request("POST", upstream_path, body=bytes(body), headers=headers)
                response = connection.getresponse()
                status = response.status
                payload = response.read()
                reply(self.request, status, response.reason or "OK", payload,
                      response.getheader("Content-Type", "application/json"))
            finally:
                connection.close()
        except (PermissionError, ValueError, UnicodeError):
            status = 403
            try:
                reply(self.request, 403, "Forbidden", b'{"error":"model broker refused request"}')
            except OSError:
                pass
        except OSError:
            status = 502
            try:
                reply(self.request, 502, "Bad Gateway", b'{"error":"model router unavailable"}')
            except OSError:
                pass
        finally:
            try:
                # Half-close, then drain unread input so the client sees the
                # reply instead of a connection reset.
                self.request.shutdown(socket.SHUT_WR)
                self.request.settimeout(1)
                while self.request.recv(65536):
                    pass
            except OSError:
                pass
            if server.log:
                server.log(status)


class LocalBridge(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


def bridge(path):
    class BridgeRequest(socketserver.BaseRequestHandler):
        def handle(self):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as remote:
                remote.connect(path)
                relay(self.request, remote)
    return BridgeRequest


MODEL_SOCKET = "/run/autonomous-company/model.sock"
MODEL_BRIDGE_PORT = 18081


if __name__ == "__main__":
    import threading
    if os.path.exists(MODEL_SOCKET):
        model = LocalBridge(("127.0.0.1", MODEL_BRIDGE_PORT), bridge(MODEL_SOCKET))
        threading.Thread(target=model.serve_forever, daemon=True).start()
    LocalBridge(("127.0.0.1", 18080), bridge("/run/autonomous-company/egress.sock")).serve_forever()
