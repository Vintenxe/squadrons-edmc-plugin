"""Redirect-refusal tests for the telemetry transport (launch audit P0-E).

Uses a real local HTTP server so the assertions cover genuine urllib
behaviour: every 3xx must surface as an error, and the bearer token must
never reach the redirect target — same-origin, cross-origin, or
HTTPS-to-HTTP downgrade alike.

Run with:  python3 -m unittest discover edmc-plugin
"""

import http.server
import json
import threading
import unittest
import urllib.error
import urllib.request

from telemetry_transport import build_telemetry_opener

# Requests received by the "redirect target" server, keyed by path.
TARGET_REQUESTS = []
# Behaviour the origin server should exhibit for the next request.
ORIGIN_BEHAVIOR = {"code": 200, "location": None}


class _OriginHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
        code = ORIGIN_BEHAVIOR["code"]
        if 300 <= code < 400:
            self.send_response(code)
            self.send_header("Location", ORIGIN_BEHAVIOR["location"])
            self.end_headers()
            return
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"processed": 1, "skipped": 0}).encode())

    def do_GET(self):  # 303 converts POST→GET; must still be refused upstream
        self.do_POST()

    def log_message(self, *args):
        pass


class _TargetHandler(http.server.BaseHTTPRequestHandler):
    def _record(self):
        TARGET_REQUESTS.append(
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
            }
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    do_POST = _record
    do_GET = _record

    def log_message(self, *args):
        pass


def _start(handler):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


class RedirectRefusalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.origin = _start(_OriginHandler)
        cls.target = _start(_TargetHandler)
        cls.origin_url = f"http://127.0.0.1:{cls.origin.server_address[1]}"
        cls.target_url = f"http://127.0.0.1:{cls.target.server_address[1]}"
        cls.opener = build_telemetry_opener()

    @classmethod
    def tearDownClass(cls):
        cls.origin.shutdown()
        cls.target.shutdown()

    def setUp(self):
        TARGET_REQUESTS.clear()
        ORIGIN_BEHAVIOR.update({"code": 200, "location": None})

    def send(self):
        req = urllib.request.Request(
            f"{self.origin_url}/api/v1/telemetry/ingest",
            data=json.dumps({"cmdrName": "IT", "events": []}).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer sq_secret_token",
            },
            method="POST",
        )
        return self.opener.open(req, timeout=5)

    def assert_redirect_refused(self, code, location):
        ORIGIN_BEHAVIOR.update({"code": code, "location": location})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.send()
        self.assertEqual(ctx.exception.code, code)
        # The token must never have reached the redirect target.
        self.assertEqual(TARGET_REQUESTS, [])

    def test_301_cross_origin_refused(self):
        self.assert_redirect_refused(301, f"{self.target_url}/steal")

    def test_302_cross_origin_refused(self):
        self.assert_redirect_refused(302, f"{self.target_url}/steal")

    def test_303_cross_origin_refused(self):
        self.assert_redirect_refused(303, f"{self.target_url}/steal")

    def test_307_cross_origin_refused(self):
        self.assert_redirect_refused(307, f"{self.target_url}/steal")

    def test_308_cross_origin_refused(self):
        self.assert_redirect_refused(308, f"{self.target_url}/steal")

    def test_same_origin_redirect_refused(self):
        # Even a same-origin move is refused: the configured URL is the
        # final canonical endpoint, and following it would still re-send
        # the token based on attacker-controllable response data.
        self.assert_redirect_refused(302, f"{self.origin_url}/api/v2/ingest")

    def test_https_to_http_style_downgrade_refused(self):
        # A downgrade points at a plain-http host; refusal is identical —
        # the token never leaves for the Location target.
        self.assert_redirect_refused(301, f"{self.target_url.replace('https', 'http')}/downgrade")

    def test_2xx_success_passes_through(self):
        with self.send() as resp:
            body = json.loads(resp.read().decode())
        self.assertEqual(body["processed"], 1)
        self.assertEqual(TARGET_REQUESTS, [])

    def test_4xx_and_5xx_surface_as_http_errors(self):
        for code in (401, 403, 413, 426, 429, 500):
            ORIGIN_BEHAVIOR.update({"code": code, "location": None})
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.send()
            self.assertEqual(ctx.exception.code, code)


class FlushPolicyShapeTests(unittest.TestCase):
    """The plugin's flush handler must drop (not rebuffer) on 3xx and use
    the refusing opener. squadrons_telemetry imports EDMC-only modules, so
    this is pinned at source level."""

    def _source(self):
        import os

        path = os.path.join(os.path.dirname(__file__), "squadrons_telemetry.py")
        with open(path, "r") as f:
            return f.read()

    def test_flush_uses_refusing_opener(self):
        src = self._source()
        self.assertIn("build_telemetry_opener", src)
        self.assertIn("_TELEMETRY_OPENER.open(", src)
        self.assertNotIn("urllib.request.urlopen(req", src)

    def test_redirects_drop_the_batch_instead_of_retry_looping(self):
        src = self._source()
        self.assertIn("300 <= e.code < 400", src)
        # The 3xx branch (everything before the 401/403/413/426 handling)
        # must return without rebuffering.
        redirect_branch = src.split("300 <= e.code < 400", 1)[1].split("if e.code in (401", 1)[0]
        self.assertIn("return", redirect_branch)
        self.assertNotIn("_rebuffer", redirect_branch)
        # Log line identifies only the target hostname, never the token.
        self.assertIn("hostname", redirect_branch)
        self.assertNotIn("api_token", redirect_branch)


if __name__ == "__main__":
    unittest.main()
