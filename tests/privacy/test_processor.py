"""Actual loopback processor HTTP checks; no replacement HTTP transport."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
import time
import unittest
from unittest.mock import patch

import httpx

from enterprise_ai_lab.privacy_proxy.api import LocalProcessor


class _Loopback:
    def __init__(self, *, body=b'{"text":"returned"}', status=200, headers=None, stall=False):
        self.body, self.status = body, status
        self.headers = headers if headers is not None else [("Content-Type", "application/json")]
        self.stall = stall
        self.release = threading.Event()
        self.requests = []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def _respond(self):
                self.connection.settimeout(1)
                length = int(self.headers.get("content-length", "0"))
                if not 0 <= length <= 65536:
                    self.close_connection = True
                    return
                raw = self.rfile.read(length)
                fixture.requests.append({"method": self.command, "path": self.path,
                                         "headers": {k.lower(): v for k, v in self.headers.items()}, "body": raw})
                body, status, headers = fixture.body, fixture.status, list(fixture.headers)
                if fixture.stall:
                    fixture.release.wait(timeout=3)
                try:
                    self.send_response(status)
                    for key, value in headers:
                        self.send_header(key, value)
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # An intentionally rejected response may close its peer.
                finally:
                    self.close_connection = True

            do_POST = _respond
            do_GET = _respond

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = False
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=.01))

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        if self.thread.is_alive():
            raise AssertionError("Processor fixture listener was not reaped")


class LocalProcessorTests(unittest.TestCase):
    def test_exact_local_request_has_no_credentials_and_ignores_environment_proxy(self):
        text = "redacted [[PII:" + "a"*32 + "]]"
        with _Loopback() as target, _Loopback() as proxy:
            inherited = {"HTTP_PROXY": proxy.origin, "HTTPS_PROXY": proxy.origin,
                         "ALL_PROXY": proxy.origin, "NO_PROXY": ""}
            with patch.dict(os.environ, inherited):
                self.assertEqual(LocalProcessor(target.origin).process(text), "returned")
            self.assertEqual(proxy.requests, [])
            self.assertEqual(len(target.requests), 1)
            request = target.requests[0]
            self.assertEqual((request["method"], request["path"]), ("POST", "/process"))
            self.assertEqual(json.loads(request["body"]), {"text": text})
            for name in ["authorization", "cookie", "proxy-authorization", "x-tenant-id", "x-role", "x-roles", "x-client-id"]:
                self.assertNotIn(name, request["headers"])
            self.assertEqual(request["headers"]["accept-encoding"], "identity")

    def test_redirect_is_refused_without_any_request_to_location(self):
        with _Loopback() as destination, _Loopback(status=307, body=b"", headers=[
                ("Location", destination.origin + "/redirected"), ("Content-Type", "application/json")]) as redirect:
            with self.assertRaises(ValueError):
                LocalProcessor(redirect.origin).process("redacted fixture")
            self.assertEqual(len(redirect.requests), 1)
            self.assertEqual(destination.requests, [])

    def test_response_byte_json_shape_and_string_limits(self):
        examples = [
            ("oversized_wire", b" "*65537),
            ("oversized_text", json.dumps({"text": "x"*32769}).encode()),
            ("oversized_unicode_bytes", json.dumps({"text": "é"*16385}, ensure_ascii=False).encode()),
            ("duplicate", b'{"text":"first","text":"second"}'),
            ("nonfinite_constant", b'{"text":NaN}'),
            ("nonfinite_exponent", b'{"text":1e999}'),
            ("malformed", b'{"text":'),
            ("list", b'[]'), ("missing", b'{}'),
            ("extra", b'{"text":"value","extra":true}'),
            ("null", b'{"text":null}'), ("bool", b'{"text":true}'),
            ("surrogate", b'{"text":"\\ud800"}'),
        ]
        with _Loopback() as target:
            processor = LocalProcessor(target.origin)
            for name, raw in examples:
                with self.subTest(name=name):
                    target.body = raw
                    with self.assertRaises((ValueError, UnicodeError)):
                        processor.process("redacted fixture")
            self.assertEqual(len(target.requests), len(examples))

    def test_status_content_type_and_duplicate_content_type_are_rejected(self):
        with _Loopback() as target:
            processor = LocalProcessor(target.origin)
            for status, headers in [(503, [("Content-Type", "application/json")]),
                                    (200, [("Content-Type", "text/html")]),
                                    (200, [("Content-Type", "application/json"), ("Content-Type", "application/json")])]:
                with self.subTest(status=status, headers=headers):
                    target.status, target.headers = status, headers
                    with self.assertRaises(ValueError):
                        processor.process("redacted fixture")

    def test_literal_loopback_origin_only_and_no_embedded_credentials(self):
        for origin in ["https://127.0.0.1:8080", "http://localhost:8080", "http://[::1]:8080",
                       "http://127.1:8080", "http://192.0.2.1:8080", "file:///tmp/processor",
                       "http://127.0.0.1", "http://127.0.0.1:0", "http://127.0.0.1:65536",
                       "http://127.0.0.1:8080/other", "http://127.0.0.1:8080?url=x",
                       "http://127.0.0.1:8080#fragment", "http://user:password@127.0.0.1:8080"]:
            with self.subTest(origin=origin):
                with self.assertRaises(ValueError):
                    LocalProcessor(origin)
        self.assertEqual(LocalProcessor("http://127.0.0.1:8080/").origin, "http://127.0.0.1:8080")

    def test_stalled_real_response_reaches_read_timeout_and_fixture_closes(self):
        with _Loopback(stall=True) as target:
            started = time.monotonic()
            with self.assertRaises(httpx.ReadTimeout):
                LocalProcessor(target.origin).process("redacted fixture")
            elapsed = time.monotonic()-started
            self.assertLess(elapsed, 3.5)
            self.assertEqual(len(target.requests), 1)
        self.assertFalse(target.thread.is_alive())

    def test_placeholder_semantics_remain_the_scoped_vaults_responsibility(self):
        # The HTTP boundary accepts bounded text. Mapping validity is checked by
        # Vault.restore after the caller's credential is revalidated, not here.
        text = "unknown [[PII:" + "f"*32 + "]] and malformed [[PII:bad]]"
        with _Loopback(body=json.dumps({"text": text}).encode()) as target:
            self.assertEqual(LocalProcessor(target.origin).process("redacted fixture"), text)


if __name__ == "__main__":
    unittest.main()
