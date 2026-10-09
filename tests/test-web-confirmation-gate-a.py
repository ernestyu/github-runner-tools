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


    def test_d1_all_routes_methods_status_and_flush(self):
        import io
        handler = object.__new__(web.Handler)
        handler.client_address = ("192.0.2.9", 1234)
        cases = [
            ("/", "index"), ("/login", "login"), ("/create", "create"),
            ("/logout", "logout"), ("/remove/prepare", "remove_prepare"),
            ("/remove/confirm", "remove_confirm"),
            ("/recover/prepare", "recover_prepare"),
            ("/recover/confirm", "recover_confirm"),
        ]
        for path, label in cases:
            for method in ("GET", "POST"):
                with self.subTest(path=path, method=method):
                    handler.path, handler.command = path, method
                    out = io.StringIO()
                    with mock.patch("sys.stdout", out):
                        handler.log_message("malicious %s", "PRIVATE_TOKEN", "403")
                    self.assertEqual(out.getvalue(), f"web_access method={method} route={label} status=403\n")
                    self.assertEqual(out.getvalue().count("\n"), 1)

        for path in ("/unknown", "/remove/confirm/extra", "/%72emove/confirm",
                     "/remove/%0Aconfirm", "/bad\\r\nCOOKIE_PRIVATE", "/?token=PASSWORD"):
            handler.path, handler.command = path, "BOGUS\nPRIVATE"
            out = io.StringIO()
            with mock.patch("sys.stdout", out):
                handler.log_message("%s %s", "PRIVATE_CSRF", "3\n99")
            self.assertEqual(out.getvalue(), "web_access method=OTHER route=other status=000\n")
            for forbidden in ("192.0.2.9", "PRIVATE", "unknown", "%0A", "PASSWORD"):
                self.assertNotIn(forbidden, out.getvalue())
        handler.path, handler.command = "/remove/confirm?token=PRIVATE_TOKEN", "GET"
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            handler.log_message("%s", "PRIVATE_NONCE", "200")
        self.assertEqual(out.getvalue(), "web_access method=GET route=remove_confirm status=200\n")

        with mock.patch("builtins.print") as printer:
            handler.log_message("%s", "SECRET_COOKIE", "200")
            self.assertEqual(printer.call_count, 1)
            self.assertIs(printer.call_args.kwargs["flush"], True)
            self.assertEqual(printer.call_args.args[0], "web_access method=GET route=remove_confirm status=200")

    def test_d2_single_pop_single_get_per_guard_and_precedence(self):
        class Pending(dict):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.lookups = []
            def get(self, key, default=None):
                self.lookups.append(key)
                return super().get(key, default)
        class Confirms(dict):
            def __init__(self, data):
                super().__init__(data)
                self.pops = 0
            def pop(self, *args):
                self.pops += 1
                return super().pop(*args)

        cases = [
            ("missing", None, "nonce_missing_or_used", []),
            ("wrong", {"op": "recover_local", "expires": 101}, "operation_mismatch", ["op"]),
            ("expired", {"op": "remove", "expires": 99}, "nonce_expired", ["op", "expires"]),
            ("wrong-expired", {"op": "recover_local", "expires": 99}, "operation_mismatch", ["op"]),
            ("valid", {"op": "remove", "expires": 101, "repository": REPO}, None, ["op", "expires"]),
        ]
        for name, data, expected, lookups in cases:
            with self.subTest(name=name):
                obj = Pending(**data) if data else None
                confirms = Confirms({"key": obj} if obj is not None else {})
                with mock.patch.object(web, "diagnostic_event") as event, mock.patch.object(web, "now", side_effect=AssertionError("extra clock")):
                    result = web.consume_confirmation({"confirm": confirms}, "key", "remove", at=100)
                self.assertEqual(confirms.pops, 1)
                self.assertEqual(obj.lookups if obj is not None else [], lookups)
                self.assertEqual(result is not None, name == "valid")
                self.assertEqual(event.call_args_list, [] if expected is None else [mock.call(expected)])

    def test_d2_http_events_results_and_no_unexpected_dispatch(self):
        from copy import deepcopy
        cookie = self.login()
        csrf, nonce = self.prepare(cookie)
        scenarios = [
            ("/remove/confirm", {"csrf": csrf, "nonce": "absent", "token": TOKEN}, 403, "nonce_missing_or_used"),
            ("/recover/confirm", {"csrf": csrf, "nonce": nonce}, 403, "operation_mismatch"),
            ("/remove/confirm", {"csrf": "incorrect", "nonce": nonce, "token": TOKEN}, 403, "invalid_csrf"),
            ("/remove/confirm", {"csrf": csrf, "nonce": nonce, "token": ""}, 400, "invalid_token_format"),
        ]
        for path, form, status_expected, category in scenarios:
            with self.subTest(category=category):
                # Each scenario starts from the same server-side pending confirmation.
                sid = cookie.split("=", 1)[1]
                original = web.SESSIONS[sid]
                saved = deepcopy(original["confirm"])
                before_mutations = len([x for x in self.calls if x["op"] == "remove"])
                with mock.patch.object(web, "diagnostic_event") as spy:
                    status, _, body = self.request("POST", path, form, cookie=cookie)
                self.assertEqual(status, status_expected)
                self.assertTrue(body)
                self.assertIn(mock.call(category), spy.call_args_list)
                if category != "invalid_csrf":
                    self.assertIn(mock.call("pre_dispatch_rejected"), spy.call_args_list)
                self.assertEqual(len([x for x in self.calls if x["op"] == "remove"]), before_mutations)
                original["confirm"] = saved

        # Explicitly verify the expiry branch via the real HTTP endpoint.
        sid = cookie.split("=", 1)[1]
        web.SESSIONS[sid]["confirm"][nonce]["expires"] = 0
        with mock.patch.object(web, "diagnostic_event") as spy:
            status, _, _ = self.request("POST", "/remove/confirm",
                {"csrf": csrf, "nonce": nonce, "token": TOKEN}, cookie=cookie)
        self.assertEqual(status, 403)
        self.assertIn(mock.call("nonce_expired"), spy.call_args_list)
        self.assertIn(mock.call("pre_dispatch_rejected"), spy.call_args_list)
        self.assertEqual(len([x for x in self.calls if x["op"] == "remove"]), 0)

    def test_noninterference_on_write_and_flush_failure(self):
        import io
        from copy import deepcopy

        class BrokenStream(io.StringIO):
            def __init__(self, failure):
                super().__init__()
                self.failure = failure
            def write(self, value):
                if self.failure == "write":
                    raise OSError("simulated write failure")
                return super().write(value)
            def flush(self):
                if self.failure == "flush":
                    raise OSError("simulated flush failure")
                return super().flush()

        cookie = self.login()
        csrf, nonce = self.prepare(cookie)
        sid = cookie.split("=", 1)[1]
        original = deepcopy(web.SESSIONS[sid])
        payload = {"csrf": csrf, "nonce": nonce, "token": TOKEN}
        observed = []
        for failure in (None, "write", "flush"):
            web.SESSIONS[sid] = deepcopy(original)
            self.calls.clear()
            stream = io.StringIO() if failure is None else BrokenStream(failure)
            with mock.patch("sys.stdout", stream):
                status, _headers, body = self.request("POST", "/remove/confirm", payload, cookie=cookie)
            observed.append((status, body, deepcopy(web.SESSIONS[sid]["confirm"]),
                             deepcopy(self.calls), web.SESSIONS[sid]["csrf"],
                             web.SESSIONS[sid]["created"]))
        self.assertEqual(observed[0], observed[1])
        self.assertEqual(observed[0], observed[2])
        self.assertEqual(observed[0][0], 303)
        self.assertEqual([x["op"] for x in observed[0][3]], ["remove"])


if __name__ == "__main__":
    unittest.main()
