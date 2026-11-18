"""Host-side model broker: one fixed loopback router, one path, host-held key."""
import http.server
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest

ROOT = Path(os.environ.get("AUTONOMOUS_COMPANY_TEST_SOURCE", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(ROOT / "scripts/core"))
from isolation_proxy import ModelBroker, loopback_upstream


class FakeRouter(http.server.BaseHTTPRequestHandler):
    seen = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        FakeRouter.seen.append({"path": self.path, "auth": self.headers.get("Authorization"),
                                "cookie": self.headers.get("Cookie"), "body": json.loads(body)})
        payload = json.dumps({"choices": [{"message": {"role": "assistant", "content": "ok"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_):
        pass


def unix_request(path, raw):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(10)
        client.connect(str(path))
        client.sendall(raw)
        data = b""
        while chunk := client.recv(65536):
            data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    return int(head.split(b" ", 2)[1]), body


def post(path, body, extra=b""):
    return (b"POST " + path + b" HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n" + extra
            + b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)


class ModelBrokerTests(unittest.TestCase):
    def setUp(self):
        FakeRouter.seen = []
        self.router = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeRouter)
        threading.Thread(target=self.router.serve_forever, daemon=True).start()
        self.temporary = tempfile.TemporaryDirectory()
        self.socket = Path(self.temporary.name) / "model.sock"
        endpoint = f"http://127.0.0.1:{self.router.server_address[1]}/v1/chat/completions"
        self.broker = ModelBroker(self.socket, endpoint, "host-secret", timeout=10)
        threading.Thread(target=self.broker.serve_forever, daemon=True).start()

    def tearDown(self):
        self.broker.shutdown()
        self.broker.server_close()
        self.router.shutdown()
        self.router.server_close()
        self.temporary.cleanup()

    def test_forwards_chat_with_host_key_only(self):
        body = json.dumps({"model": "auto:coding", "messages": []}).encode()
        status, reply = unix_request(self.socket, post(
            b"/v1/chat/completions", body, b"Authorization: Bearer inner-guess\r\nCookie: a=b\r\n"))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(reply)["choices"][0]["message"]["content"], "ok")
        self.assertEqual(FakeRouter.seen[0]["auth"], "Bearer host-secret")
        self.assertIsNone(FakeRouter.seen[0]["cookie"])
        self.assertEqual(FakeRouter.seen[0]["body"]["model"], "auto:coding")

    def test_refuses_other_paths_methods_and_bodies(self):
        cases = [post(b"/v1/admin/keys", b"{}"),
                 post(b"/v1/chat/completions/../../dashboard", b"{}"),
                 b"GET /v1/chat/completions HTTP/1.1\r\nHost: x\r\n\r\n",
                 post(b"/v1/chat/completions", b"[1]"),
                 post(b"/v1/chat/completions", b"not json"),
                 b"POST /v1/chat/completions HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n",
                 b"CONNECT 127.0.0.1:3001 HTTP/1.1\r\n\r\n"]
        for raw in cases:
            with self.subTest(raw=raw[:40]):
                self.assertEqual(unix_request(self.socket, raw)[0], 403)
        self.assertEqual(FakeRouter.seen, [])

    def test_unavailable_router_is_bad_gateway(self):
        self.router.shutdown()
        self.router.server_close()
        status, _ = unix_request(self.socket, post(b"/v1/chat/completions", b"{}"))
        self.assertEqual(status, 502)

    def test_upstream_must_be_plain_loopback(self):
        self.assertEqual(loopback_upstream("http://localhost:3001/v1/chat/completions"),
                         ("localhost", 3001, "/v1/chat/completions"))
        for bad in ("https://127.0.0.1/v1/chat/completions", "http://10.0.0.5:3001/v1/chat/completions",
                    "http://example.com/v1/chat/completions", "http://u:p@127.0.0.1/v1/chat/completions",
                    "http://127.0.0.1/v1/chat/completions?x=1", ""):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                loopback_upstream(bad)


if __name__ == "__main__":
    unittest.main()
