"""Unit tests for the telemetry server-URL policy (launch audit P1-F).

Run with:  python3 -m unittest discover edmc-plugin
"""

import unittest

from url_policy import DEFAULT_SERVER_URL, describe_destination, normalize_server_url


class NormalizeServerUrlTests(unittest.TestCase):
    def assert_ok(self, raw, expected):
        normalized, error = normalize_server_url(raw)
        self.assertIsNone(error, f"expected {raw!r} to be accepted, got: {error}")
        self.assertEqual(normalized, expected)

    def assert_rejected(self, raw, fragment):
        normalized, error = normalize_server_url(raw)
        self.assertIsNone(normalized, f"expected {raw!r} to be rejected")
        self.assertIn(fragment, error or "")

    # --- accepted -------------------------------------------------------

    def test_empty_defaults_to_production(self):
        self.assert_ok("", DEFAULT_SERVER_URL)
        self.assert_ok("   ", DEFAULT_SERVER_URL)

    def test_default_production_url(self):
        self.assert_ok("https://elitesquadrons.com", "https://elitesquadrons.com")

    def test_trailing_slash_is_normalized(self):
        self.assert_ok("https://elitesquadrons.com/", "https://elitesquadrons.com")
        self.assert_ok("https://squadrons.example.org/base/", "https://squadrons.example.org/base")

    def test_https_custom_host_allowed(self):
        self.assert_ok("https://squadrons.example.org", "https://squadrons.example.org")

    def test_bare_host_upgrades_to_https(self):
        self.assert_ok("squadrons.example.org", "https://squadrons.example.org")

    def test_hostname_is_lowercased(self):
        self.assert_ok("https://SQUADRONS.Example.ORG", "https://squadrons.example.org")

    def test_localhost_http_allowed_for_development(self):
        self.assert_ok("http://localhost:3000", "http://localhost:3000")
        self.assert_ok("http://127.0.0.1:3000", "http://127.0.0.1:3000")
        self.assert_ok("http://127.5.4.3", "http://127.5.4.3")
        self.assert_ok("http://[::1]:3000", "http://[::1]:3000")

    def test_unicode_hostname_is_idna_normalized(self):
        normalized, error = normalize_server_url("https://élite-squadrons.com")
        self.assertIsNone(error)
        self.assertTrue(normalized.startswith("https://xn--"), normalized)

    # --- rejected -------------------------------------------------------

    def test_http_production_host_rejected(self):
        self.assert_rejected("http://elitesquadrons.com", "localhost")

    def test_http_arbitrary_host_rejected(self):
        self.assert_rejected("http://evil.example.net", "localhost")

    def test_embedded_credentials_rejected(self):
        self.assert_rejected("https://user:pass@evil.example.net", "credentials")
        self.assert_rejected("https://user@evil.example.net", "credentials")

    def test_query_string_rejected(self):
        self.assert_rejected("https://example.net/?next=1", "query string")

    def test_fragment_rejected(self):
        self.assert_rejected("https://example.net/#frag", "#fragment")

    def test_non_http_schemes_rejected(self):
        self.assert_rejected("file:///etc/passwd", "scheme")
        self.assert_rejected("ftp://example.net", "scheme")

    def test_missing_hostname_rejected(self):
        self.assert_rejected("https://", "hostname")

    def test_invalid_port_rejected(self):
        self.assert_rejected("https://example.net:notaport", "port")

    def test_loopback_lookalike_is_not_loopback(self):
        # 127.0.0.1.evil.net is a normal (sub)domain — https is required.
        self.assert_rejected("http://127.0.0.1.evil.net", "localhost")


class DescribeDestinationTests(unittest.TestCase):
    def test_returns_hostname(self):
        self.assertEqual(describe_destination("https://squadrons.example.org/base"), "squadrons.example.org")


if __name__ == "__main__":
    unittest.main()
