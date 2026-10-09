#!/usr/bin/env python3
"""Gate A ONLY: baseline HTTP reproduction; no runtime mutation and no GitHub calls."""
from __future__ import annotations

import http.client
import html.parser
import pathlib
import sys
import threading
import unittest
import urllib.parse
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "web"))
import app as web

PASSWORD = "gate-a-test-password"
TOKEN = "SYNTHETIC_REMOVAL_TOKEN_123456"
REPO = "example/disposable"


class HiddenFields(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.fields = {}

    def handle_starttag(self, tag, attrs):
        if tag == "input":
            item = dict(attrs)
            if item.get("name") in ("csrf", "nonce"):
                self.fields[item["name"]] = item.get("value", "")


class BaselineHTTPGateA(unittest.TestCase):
    def setUp(self):
        web.SESSIONS.clear()
        web.LOGIN_ATTEMPTS.clear()
        self.calls = []

        def fake_dispatch(_cfg, request):
            self.calls.append(dict(request))
            if request["op"] == "list":
                return {"ok": True, "runners": [
                    {"repository": REPO, "runner_name": "test-runner", "service_state": "active",
                     "management_state": "configured", "can_remove": True, "can_recover_local": False}
                ]}
            return {"ok": True}

        self.dispatch_patch = mock.patch.object(web, "dispatch", side_effect=fake_dispatch)
        self.verify_patch = mock.patch.object(web, "verify_password", side_effect=lambda p, _h: p == PASSWORD)
        self.dispatch_patch.start()
        self.verify_patch.start()
        app_obj = object.__new__(web.App)
        app_obj.config = {}
        app_obj.password_hash = "test-only-hash"
        web.Handler.app = app_obj
        # Avoid patching live server state: bind only an ephemeral loopback test socket.
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.verify_patch.stop()
        self.dispatch_patch.stop()
        web.SESSIONS.clear()
        web.LOGIN_ATTEMPTS.clear()

    def request(self, method, path, form=None, cookie=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        payload = urllib.parse.urlencode(form).encode() if form is not None else None
        h = dict(headers or {})
        if payload is not None:
            h["Content-Type"] = "application/x-www-form-urlencoded"
        if cookie:
            h["Cookie"] = cookie
        conn.request(method, path, body=payload, headers=h)
        response = conn.getresponse()
        status, headers = response.status, dict(response.getheaders())
        body = response.read().decode()
        conn.close()
        return status, headers, body

    def login(self):
        status, headers, _ = self.request("POST", "/login", {"password": PASSWORD})
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/")
        policy = headers["Set-Cookie"]
        for attr in ("Path=/", "Secure", "HttpOnly", "SameSite=Strict"):
            self.assertIn(attr, policy)
        return policy.split(";", 1)[0]

    def prepare(self, cookie):
        status, _, body = self.request("GET", "/", cookie=cookie)
        self.assertEqual(status, 200)
        h = HiddenFields()
        h.feed(body)
        csrf = h.fields["csrf"]
        status, _, body = self.request("POST", "/remove/prepare",
            {"csrf": csrf, "repository": REPO}, cookie=cookie)
        self.assertEqual(status, 200)
        h = HiddenFields()
        h.feed(body)
        return csrf, h.fields["nonce"]

    def test_normal_full_http_login_cookie_prepare_confirm(self):
        cookie = self.login()
        csrf, nonce = self.prepare(cookie)
        status, headers, _ = self.request("POST", "/remove/confirm",
            {"csrf": csrf, "nonce": nonce, "token": TOKEN}, cookie=cookie)
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/")
        self.assertEqual([r["op"] for r in self.calls], ["list", "list", "remove"])
        self.assertEqual(self.calls[-1]["repository"], REPO)

    def test_immediate_invalid_token_consumes_confirmation_baseline_observation(self):
        cookie = self.login()
        csrf, nonce = self.prepare(cookie)
        status, _, body = self.request("POST", "/remove/confirm",
            {"csrf": csrf, "nonce": nonce, "token": ""}, cookie=cookie)
        self.assertEqual(status, 400)
        self.assertIn("Invalid removal token", body)
        status, _, body = self.request("POST", "/remove/confirm",
            {"csrf": csrf, "nonce": nonce, "token": TOKEN}, cookie=cookie)
        self.assertEqual(status, 403)
        self.assertIn("Confirmation expired or invalid.", body)
        self.assertEqual([x for x in self.calls if x["op"] == "remove"], [])

    def test_wrong_operation_consumes_original_nonce_baseline_observation(self):
        cookie = self.login()
        csrf, nonce = self.prepare(cookie)
        status, _, _ = self.request("POST", "/recover/confirm",
            {"csrf": csrf, "nonce": nonce}, cookie=cookie)
        self.assertEqual(status, 403)
        status, _, body = self.request("POST", "/remove/confirm",
            {"csrf": csrf, "nonce": nonce, "token": TOKEN}, cookie=cookie)
        self.assertEqual(status, 403)
        self.assertIn("Confirmation expired or invalid.", body)
        self.assertEqual([x for x in self.calls if x["op"] != "list"], [])

    def test_stale_cookie_and_cross_session_nonce_fail_closed(self):
        cookie = self.login()
        csrf, nonce = self.prepare(cookie)
        other_cookie = self.login()
        status, _, _ = self.request("POST", "/remove/confirm",
            {"csrf": csrf, "nonce": nonce, "token": TOKEN}, cookie=other_cookie)
        self.assertEqual(status, 403)
        status, _, _ = self.request("POST", "/remove/confirm",
            {"csrf": csrf, "nonce": nonce, "token": TOKEN}, cookie=cookie)
        self.assertEqual(status, 303)
        self.assertEqual(len([x for x in self.calls if x["op"] == "remove"]), 1)


    def test_d1_log_allowlist_and_fail_open_diagnostics(self):
        import io
        handler = object.__new__(web.Handler)
        handler.path = "/untrusted/%0Asecret?token=PRIVATE_QUERY"
        handler.command = "POST"
        handler.client_address = ("192.0.2.9", 4567)
        output = io.StringIO()
        with mock.patch("builtins.print", side_effect=lambda *a, **k: output.write(a[0] + "\\n")):
            handler.log_message('"%s" %s %s', "RAW_PATH", "403", "PRIVATE")
        self.assertEqual(output.getvalue(), "web_access method=POST route=other status=403\\n")
        for secret in ("PRIVATE", "192.0.2.9", "%0A", "RAW_PATH"):
            self.assertNotIn(secret, output.getvalue())
        handler.path = "/remove/confirm?token=LEAK"
        handler.command = "UNTRUSTED_METHOD"
        output = io.StringIO()
        with mock.patch("builtins.print", side_effect=lambda *a, **k: output.write(a[0] + "\\n")):
            handler.log_message("%s", "raw", "3\\n99")
        self.assertEqual(output.getvalue(), "web_access method=OTHER route=remove_confirm status=000\\n")
        with mock.patch("builtins.print", side_effect=OSError("journal unavailable")):
            handler.log_message("%s", "raw", "200")
            web.diagnostic_event("invalid_csrf")

    def test_d2_classifications_and_diagnostic_failure_noninterference(self):
        cookie = self.login()
        csrf, nonce = self.prepare(cookie)
        with mock.patch("builtins.print", side_effect=OSError("journal unavailable")):
            status, _, _ = self.request("POST", "/remove/confirm",
                {"csrf": csrf, "nonce": nonce, "token": TOKEN}, cookie=cookie)
        self.assertEqual(status, 303)
        self.assertEqual(len([x for x in self.calls if x["op"] == "remove"]), 1)
        with mock.patch("app.diagnostic_event") as spy:
            status, _, _ = self.request("POST", "/remove/confirm",
                {"csrf": csrf, "nonce": nonce, "token": TOKEN}, cookie=cookie)
        self.assertEqual(status, 403)
        self.assertIn(mock.call("nonce_missing_or_used"), spy.call_args_list)
        self.assertEqual(len([x for x in self.calls if x["op"] == "remove"]), 1)


if __name__ == "__main__":
    unittest.main()
