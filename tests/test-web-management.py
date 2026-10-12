#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import http.client
import importlib.util
import io
import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from types import SimpleNamespace
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
sys.path.insert(0, str(WEB))

import grt_web_common as common
import app as web_app
import lifecycle_worker


class CommonTests(unittest.TestCase):
    def test_password_hash_roundtrip(self):
        encoded = common.password_hash("correct horse battery staple", n=2**10)
        self.assertTrue(encoded.startswith("scrypt$"))
        self.assertTrue(common.verify_password("correct horse battery staple", encoded))
        self.assertFalse(common.verify_password("wrong", encoded))

    def test_repository_and_local_identity(self):
        self.assertEqual(common.validate_repository("ernestyu/HyperGrid"), "ernestyu/HyperGrid")
        with self.assertRaises(ValueError):
            common.validate_repository("ernestyu/HyperGrid;rm -rf")
        self.assertEqual(common.make_local_id("ErnestYu", "HyperGrid"), "ernestyu--hypergrid")

    def test_service_name_is_complete_or_fails(self):
        name = common.canonical_service_name("ernestyu/repo", "custom-runner")
        self.assertEqual(name, "actions.runner.ernestyu-repo.custom-runner.service")
        with self.assertRaises(ValueError):
            common.canonical_service_name("owner/" + "r" * 100, "n" * 100)


class SessionAndBrowserTests(unittest.TestCase):
    def setUp(self):
        web_app.SESSIONS.clear()
        web_app.LOGIN_ATTEMPTS.clear()

    def test_session_is_opaque_and_server_side(self):
        app_obj = object.__new__(web_app.App)
        sid, sess = app_obj.new_session()
        self.assertGreaterEqual(len(sid), 32)
        self.assertIn(sid, web_app.SESSIONS)
        self.assertNotIn("authenticated", sid)
        self.assertIn("csrf", sess)
        self.assertEqual(sess["confirm"], {})

    def test_csrf_compare(self):
        sess = {"csrf": "abc"}
        self.assertTrue(web_app.Handler._csrf_ok({"csrf": "abc"}, sess))
        self.assertFalse(web_app.Handler._csrf_ok({"csrf": "def"}, sess))

    def test_cookie_and_cache_security_are_present(self):
        source = (WEB / "app.py").read_text(encoding="utf-8")
        self.assertIn("HttpOnly; SameSite=Strict", source)
        self.assertIn('WEB_COOKIE_SECURE', source)
        self.assertIn('Cache-Control", "no-store"', source)
        self.assertIn("RFC1918", source)
        self.assertNotIn("V2_EXECUTOR_MODE", source)
        self.assertNotIn("web-dispatch.sock", source)


class TokenAdapterTests(unittest.TestCase):
    def test_token_never_enters_child_argv_or_env(self):
        token = "SECRET_WEB_TOKEN_123456"
        with tempfile.TemporaryDirectory() as td:
            child = pathlib.Path(td) / "child.py"
            result_file = pathlib.Path(td) / "result.json"
            child.write_text(
                "import json,os,sys\n"
                "print('Enter token:', flush=True)\n"
                "value=input()\n"
                "result={\n"
                " 'token_ok': value.startswith('SECRET_'),\n"
                " 'argv_has': any('SECRET_WEB_TOKEN' in x for x in sys.argv),\n"
                " 'env_has': any('SECRET_WEB_TOKEN' in v for v in os.environ.values()),\n"
                "}\n"
                "open(sys.argv[1], 'w', encoding='utf-8').write(json.dumps(result))\n",
                encoding="utf-8",
            )
            read_fd, write_fd = os.pipe()
            try:
                os.write(write_fd, (token + "\n").encode())
                os.close(write_fd)
                write_fd = -1
                proc = subprocess.run(
                    [
                        sys.executable,
                        str(WEB / "pty_token_adapter.py"),
                        "--token-fd",
                        str(read_fd),
                        "--mode",
                        "remove",
                        "--",
                        sys.executable,
                        str(child),
                        str(result_file),
                    ],
                    pass_fds=(read_fd,),
                    text=True,
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
            finally:
                os.close(read_fd)
                if write_fd >= 0:
                    os.close(write_fd)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertNotIn(token, proc.stdout)
            self.assertNotIn(token, proc.stderr)
            self.assertEqual(proc.stdout, "")
            result = json.loads(result_file.read_text(encoding="utf-8"))
            self.assertTrue(result["token_ok"])
            self.assertFalse(result["argv_has"])
            self.assertFalse(result["env_has"])


class WorkerProtocolTests(unittest.TestCase):
    def test_worker_reads_dispatch_request_from_pipe(self):
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, b'{"op":"list"}\n')
            os.close(write_fd)
            write_fd = -1
            self.assertEqual(lifecycle_worker.read_request(read_fd), {"op": "list"})
        finally:
            os.close(read_fd)
            if write_fd >= 0:
                os.close(write_fd)


class FrozenSessionContractTests(unittest.TestCase):
    def setUp(self):
        web_app.SESSIONS.clear()
        web_app.LOGIN_ATTEMPTS.clear()

    def test_expired_idle_and_absolute_sessions_rejected(self):
        base = 10_000.0
        active = {"created": base, "last": base + 10, "csrf": "x", "confirm": {}}
        self.assertTrue(web_app.session_is_valid(active, base + 20))
        self.assertFalse(
            web_app.session_is_valid(
                {"created": base, "last": base, "csrf": "x", "confirm": {}},
                base + web_app.SESSION_IDLE + 1,
            )
        )
        self.assertFalse(
            web_app.session_is_valid(
                {
                    "created": base,
                    "last": base + web_app.SESSION_ABSOLUTE,
                    "csrf": "x",
                    "confirm": {},
                },
                base + web_app.SESSION_ABSOLUTE + 1,
            )
        )

    def test_old_cookie_state_dies_on_restart(self):
        app_obj = object.__new__(web_app.App)
        sid, _sess = app_obj.new_session()
        self.assertIn(sid, web_app.SESSIONS)
        web_app.SESSIONS.clear()  # process restart semantics: memory is gone
        self.assertNotIn(sid, web_app.SESSIONS)

    def test_login_rate_limit_actual_state_transition(self):
        source = "127.0.0.1"
        t = 1000.0
        for i in range(8):
            self.assertTrue(web_app.login_attempt_allowed(source, t + i))
            web_app.record_login_attempt(source, t + i)
        self.assertFalse(web_app.login_attempt_allowed(source, t + 9))
        # Window expiry restores eligibility.
        self.assertTrue(web_app.login_attempt_allowed(source, t + 400))

    def test_confirmation_nonce_missing_wrong_expired_reused_and_bound(self):
        t = 5000.0
        sess = {
            "confirm": {
                "good": {"op": "remove", "repository": "owner/repo", "expires": t + 10},
                "wrongop": {"op": "recover_local", "repository": "owner/repo", "expires": t + 10},
                "expired": {"op": "remove", "repository": "owner/repo", "expires": t - 1},
            }
        }
        self.assertIsNone(web_app.consume_confirmation(sess, "missing", "remove", t))
        self.assertIsNone(web_app.consume_confirmation(sess, "wrongop", "remove", t))
        self.assertIsNone(web_app.consume_confirmation(sess, "expired", "remove", t))

        pending = web_app.consume_confirmation(sess, "good", "remove", t)
        self.assertEqual(pending["repository"], "owner/repo")
        self.assertIsNone(web_app.consume_confirmation(sess, "good", "remove", t))  # single use

        # Repository binding is server-side state, not a client field.
        self.assertEqual(pending, {"op": "remove", "repository": "owner/repo", "expires": t + 10})

    def test_oversized_fields_rejected_by_shared_bounds(self):
        self.assertFalse(
            web_app.form_fields_within_bounds(
                {"repository": "o/" + "r" * common.MAX_REPOSITORY_LEN}
            )
        )
        self.assertFalse(
            web_app.form_fields_within_bounds({"token": "x" * (common.MAX_TOKEN_LEN + 1)})
        )
        self.assertFalse(web_app.form_fields_within_bounds({"password": "x" * 257}))
        self.assertTrue(
            web_app.form_fields_within_bounds(
                {"repository": "owner/repo", "token": "short", "csrf": "x", "nonce": "y"}
            )
        )

    def test_list_eligibility_rechecks_current_dispatch_state(self):
        handler = object.__new__(web_app.Handler)
        handler.app = mock.Mock()
        handler.app.config = {}
        with mock.patch(
            "app.dispatch",
            return_value={
                "ok": True,
                "runners": [
                    {
                        "repository": "owner/repo",
                        "can_remove": False,
                        "can_recover_local": False,
                    }
                ],
            },
        ):
            self.assertFalse(handler._listed_eligible("owner/repo", "remove"))
            self.assertFalse(handler._listed_eligible("owner/repo", "recover_local"))


class WebHTTPContractTests(unittest.TestCase):
    def setUp(self):
        web_app.SESSIONS.clear()
        web_app.LOGIN_ATTEMPTS.clear()
        self.tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.tmp.name)
        config = base / "web.conf"
        auth = base / "web-auth.conf"
        config.write_text(
            "WEB_BIND_ADDRESS=192.168.1.10\n"
            "WEB_PORT=0\n"
            "WEB_COOKIE_SECURE=0\n"
            "MUTATION_TIMEOUT_SECONDS=1\n",
            encoding="utf-8",
        )
        auth.write_text(
            "PASSWORD_HASH=" + common.password_hash("correct-password", n=2**10) + "\n",
            encoding="utf-8",
        )
        self.app = web_app.App(str(config), str(auth))
        web_app.Handler.app = self.app
        self.server = web_app.ThreadingHTTPServer(("127.0.0.1", 0), web_app.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()
        web_app.SESSIONS.clear()
        web_app.LOGIN_ATTEMPTS.clear()

    def request(self, method, path, fields=None, cookie=None, raw_body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        if cookie:
            headers["Cookie"] = cookie
        if raw_body is None and fields is not None:
            raw_body = urllib.parse.urlencode(fields).encode()
        if raw_body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            headers["Content-Length"] = str(len(raw_body))
        conn.request(method, path, body=raw_body, headers=headers)
        response = conn.getresponse()
        body = response.read()
        result = (response.status, dict(response.getheaders()), body)
        conn.close()
        return result

    def new_cookie_session(self):
        sid, sess = self.app.new_session()
        return f"{web_app.COOKIE_NAME}={sid}", sess

    def test_unauthenticated_management_redirects_to_login(self):
        status, headers, _body = self.request("GET", "/")
        self.assertEqual(status, 303)
        self.assertEqual(headers.get("Location"), "/login")

    def test_actual_login_rate_limit(self):
        for _ in range(8):
            status, _headers, _body = self.request(
                "POST", "/login", {"password": "wrong-password"}
            )
            self.assertEqual(status, 403)
        status, _headers, _body = self.request(
            "POST", "/login", {"password": "wrong-password"}
        )
        self.assertEqual(status, 429)

    def test_expired_and_restart_invalidated_cookie_rejected(self):
        cookie, sess = self.new_cookie_session()
        sess["last"] = web_app.now() - web_app.SESSION_IDLE - 1
        status, headers, _body = self.request("GET", "/", cookie=cookie)
        self.assertEqual(status, 303)
        self.assertEqual(headers.get("Location"), "/login")

        cookie2, _sess2 = self.new_cookie_session()
        web_app.SESSIONS.clear()  # restart semantics
        status, headers, _body = self.request("GET", "/", cookie=cookie2)
        self.assertEqual(status, 303)
        self.assertEqual(headers.get("Location"), "/login")

    def test_state_change_requires_csrf(self):
        cookie, _sess = self.new_cookie_session()
        status, _headers, _body = self.request(
            "POST", "/logout", {"csrf": "wrong"}, cookie=cookie
        )
        self.assertEqual(status, 403)

    def test_confirmation_nonce_missing_expired_wrong_operation_and_reuse(self):
        cookie, sess = self.new_cookie_session()
        eligible = {
            "ok": True,
            "runners": [
                {
                    "repository": "owner/repo",
                    "runner_name": "local-ci-owner--repo",
                    "runner_id": 123,
                    "instance": "0123456789abcdef0123456789abcdef",
                    "can_remove": True,
                    "can_recover_local": True,
                }
            ],
        }
        with mock.patch("app.dispatch", return_value=eligible):
            status, _headers, body = self.request(
                "POST",
                "/remove/prepare",
                {"csrf": sess["csrf"], "repository": "owner/repo"},
                cookie=cookie,
            )
        self.assertEqual(status, 200)
        self.assertNotIn(b"temporary-secret", body)
        nonce = next(iter(sess["confirm"]))

        status, _headers, _body = self.request(
            "POST",
            "/remove/confirm",
            {"csrf": sess["csrf"], "nonce": "missing", "token": "temporary-secret"},
            cookie=cookie,
        )
        self.assertEqual(status, 403)

        # Wrong operation consumes and rejects its own nonce.
        sess["confirm"]["recover-only"] = {
            "op": "recover_local",
            "repository": "owner/repo",
            "expires": web_app.now() + 60,
        }
        status, _headers, _body = self.request(
            "POST",
            "/remove/confirm",
            {"csrf": sess["csrf"], "nonce": "recover-only", "token": "temporary-secret"},
            cookie=cookie,
        )
        self.assertEqual(status, 403)

        sess["confirm"]["expired"] = {
            "op": "remove",
            "repository": "owner/repo",
            "expires": web_app.now() - 1,
        }
        status, _headers, _body = self.request(
            "POST",
            "/remove/confirm",
            {"csrf": sess["csrf"], "nonce": "expired", "token": "temporary-secret"},
            cookie=cookie,
        )
        self.assertEqual(status, 403)

        with mock.patch("app.dispatch", side_effect=[eligible, {"ok": True}]):
            status, _headers, _body = self.request(
                "POST",
                "/remove/confirm",
                {"csrf": sess["csrf"], "nonce": nonce, "token": "temporary-secret"},
                cookie=cookie,
            )
        self.assertEqual(status, 303)
        self.assertNotIn(nonce, sess["confirm"])
        status, _headers, _body = self.request(
            "POST",
            "/remove/confirm",
            {"csrf": sess["csrf"], "nonce": nonce, "token": "temporary-secret"},
            cookie=cookie,
        )
        self.assertEqual(status, 403)

    def test_confirmation_is_repository_bound_server_side(self):
        cookie, sess = self.new_cookie_session()
        eligible = {
            "ok": True,
            "runners": [
                {"repository": "owner/repo", "runner_name": "local-ci-owner--repo",
                 "runner_id": 123, "instance": "0123456789abcdef0123456789abcdef",
                 "can_remove": True, "can_recover_local": False}
            ],
        }
        with mock.patch("app.dispatch", return_value=eligible):
            status, _headers, body = self.request(
                "POST",
                "/remove/prepare",
                {"csrf": sess["csrf"], "repository": "owner/repo"},
                cookie=cookie,
            )
        self.assertEqual(status, 200)
        nonce, pending = next(iter(sess["confirm"].items()))
        self.assertEqual(pending["repository"], "owner/repo")
        html_text = body.decode("utf-8")
        self.assertIn(f'name="nonce" value="{nonce}"', html_text)
        self.assertNotIn('name="repository"', html_text)

    def test_stale_list_eligibility_rejects_prepare(self):
        cookie, sess = self.new_cookie_session()
        stale = {
            "ok": True,
            "runners": [
                {
                    "repository": "owner/repo",
                    "can_remove": False,
                    "can_recover_local": False,
                }
            ],
        }
        with mock.patch("app.dispatch", return_value=stale):
            status, _headers, _body = self.request(
                "POST",
                "/remove/prepare",
                {"csrf": sess["csrf"], "repository": "owner/repo"},
                cookie=cookie,
            )
            self.assertEqual(status, 409)
            status, _headers, _body = self.request(
                "POST",
                "/recover/prepare",
                {"csrf": sess["csrf"], "repository": "owner/repo"},
                cookie=cookie,
            )
            self.assertEqual(status, 409)

    def test_oversized_body_repository_and_token_rejected_before_dispatch(self):
        cookie, sess = self.new_cookie_session()

        status, _headers, _body = self.request(
            "POST", "/create", cookie=cookie, raw_body=b"x" * (common.MAX_REQUEST_BYTES + 1)
        )
        self.assertEqual(status, 413)

        with mock.patch("app.dispatch") as call:
            status, _headers, _body = self.request(
                "POST",
                "/create",
                {
                    "csrf": sess["csrf"],
                    "repository": "o/" + "r" * common.MAX_REPOSITORY_LEN,
                    "token": "short",
                },
                cookie=cookie,
            )
            self.assertEqual(status, 413)
            self.assertFalse(call.called)

            status, _headers, _body = self.request(
                "POST",
                "/create",
                {
                    "csrf": sess["csrf"],
                    "repository": "owner/repo",
                    "token": "x" * (common.MAX_TOKEN_LEN + 1),
                },
                cookie=cookie,
            )
            self.assertEqual(status, 413)
            self.assertFalse(call.called)

    def test_secret_not_reflected_in_error_response_log_or_confirmation_state(self):
        cookie, sess = self.new_cookie_session()
        token = "DO_NOT_LEAK_TEMP_TOKEN"
        log = io.StringIO()
        with mock.patch("app.dispatch", return_value={"ok": False, "error": "lifecycle_failed"}), \
             contextlib.redirect_stdout(log):
            status, _headers, body = self.request(
                "POST",
                "/create",
                {"csrf": sess["csrf"], "repository": "owner/repo", "token": token},
                cookie=cookie,
            )
        self.assertEqual(status, 500)
        self.assertNotIn(token, body.decode("utf-8"))
        self.assertNotIn(token, log.getvalue())
        self.assertNotIn(token, json.dumps(sess))


    def test_valid_login_establishes_opaque_http_cookie(self):
        status, headers, _body = self.request(
            "POST", "/login", {"password": "correct-password"}
        )
        self.assertEqual(status, 303)
        cookie = headers.get("Set-Cookie", "")
        self.assertIn(f"{web_app.COOKIE_NAME}=", cookie)
        self.assertNotIn("Secure", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        sid = cookie.split(";", 1)[0].split("=", 1)[1]
        self.assertIn(sid, web_app.SESSIONS)
        self.assertNotIn("authenticated", sid)

    def test_https_cookie_mode_can_explicitly_add_secure(self):
        self.app.config["WEB_COOKIE_SECURE"] = "1"
        status, headers, _body = self.request(
            "POST", "/login", {"password": "correct-password"}
        )
        self.assertEqual(status, 303)
        cookie = headers.get("Set-Cookie", "")
        self.assertIn("; Secure;", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)

    def test_create_validation_and_typed_dispatch(self):
        cookie, sess = self.new_cookie_session()
        with mock.patch("app.dispatch", return_value={"ok": True}) as call:
            status, _headers, _body = self.request(
                "POST",
                "/create",
                {
                    "csrf": sess["csrf"],
                    "repository": "owner/repo",
                    "token": "temporary-registration-token",
                },
                cookie=cookie,
            )
        self.assertEqual(status, 303)
        call.assert_called_once_with(
            self.app.config,
            {
                "op": "create",
                "repository": "owner/repo",
                "token": "temporary-registration-token",
            },
        )

        with mock.patch("app.dispatch") as call:
            status, _headers, _body = self.request(
                "POST",
                "/create",
                {
                    "csrf": sess["csrf"],
                    "repository": "owner/repo;evil",
                    "token": "temporary-registration-token",
                },
                cookie=cookie,
            )
            self.assertEqual(status, 400)
            self.assertFalse(call.called)

            status, _headers, _body = self.request(
                "POST",
                "/create",
                {
                    "csrf": sess["csrf"],
                    "repository": "owner/repo",
                    "token": "",
                },
                cookie=cookie,
            )
            self.assertEqual(status, 400)
            self.assertFalse(call.called)

    def test_management_page_uses_compact_table_and_blank_registration_token(self):
        cookie, _sess = self.new_cookie_session()
        response = {
            "ok": True,
            "runners": [
                {
                    "repository": "owner/repo",
                    "runner_name": "local-ci-owner--repo",
                    "service_state": "active",
                    "management_state": "configured",
                    "can_remove": True,
                    "can_recover_local": False,
                }
            ],
        }
        with mock.patch("app.dispatch", return_value=response):
            status, _headers, body = self.request("GET", "/", cookie=cookie)

        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertIn('class="runner-table"', text)
        self.assertIn("<th>Repository</th>", text)
        self.assertIn("<th>Runner</th>", text)
        self.assertIn("<th>Status</th>", text)
        self.assertIn("owner/repo", text)
        self.assertIn("local-ci-owner--repo", text)
        self.assertIn("Active", text)
        self.assertNotIn("State: configured", text)
        self.assertNotIn("Service: active", text)
        self.assertIn('action="/remove/prepare"', text)

        token_input = (
            '<input type="password" name="token" value="" maxlength="1024" '
            'autocomplete="new-password" placeholder="Paste temporary registration token" required>'
        )
        self.assertIn(token_input, text)

    def test_recoverable_runner_uses_user_facing_status_and_recover_action(self):
        cookie, _sess = self.new_cookie_session()
        response = {
            "ok": True,
            "runners": [
                {
                    "repository": "owner/repo",
                    "runner_name": "local-ci-owner--repo",
                    "service_state": "absent",
                    "management_state": "recoverable_residue",
                    "can_remove": False,
                    "can_recover_local": True,
                }
            ],
        }
        with mock.patch("app.dispatch", return_value=response):
            status, _headers, body = self.request("GET", "/", cookie=cookie)

        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertIn("Needs recovery", text)
        self.assertIn('action="/recover/prepare"', text)
        self.assertIn(">Recover</button>", text)
        self.assertNotIn("recoverable_residue", text)

    def test_runner_status_view_hides_internal_configured_state(self):
        self.assertEqual(
            web_app.runner_status_view(
                {"service_state": "active", "management_state": "configured"}
            ),
            ("Active", "ok"),
        )
        self.assertEqual(
            web_app.runner_status_view(
                {"service_state": "inactive", "management_state": "configured"}
            ),
            ("Inactive", "muted"),
        )
        self.assertEqual(
            web_app.runner_status_view(
                {
                    "service_state": "absent",
                    "management_state": "recoverable_residue",
                    "can_recover_local": True,
                }
            ),
            ("Needs recovery", "status-warn"),
        )

    def test_ambiguous_runner_renders_no_destructive_action(self):
        cookie, _sess = self.new_cookie_session()
        response = {
            "ok": True,
            "runners": [
                {
                    "repository": None,
                    "runner_name": None,
                    "service_state": "unknown",
                    "management_state": "ambiguous",
                    "can_remove": False,
                    "can_recover_local": False,
                }
            ],
        }
        with mock.patch("app.dispatch", return_value=response):
            status, _headers, body = self.request("GET", "/", cookie=cookie)
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertNotIn('action="/remove/prepare"', text)
        self.assertNotIn('action="/recover/prepare"', text)

    def test_confirmation_nonce_cannot_cross_sessions(self):
        cookie_a, sess_a = self.new_cookie_session()
        cookie_b, sess_b = self.new_cookie_session()
        nonce = "session-a-only"
        sess_a["confirm"][nonce] = {
            "op": "remove",
            "repository": "owner/repo",
            "expires": web_app.now() + 60,
        }
        status, _headers, _body = self.request(
            "POST",
            "/remove/confirm",
            {"csrf": sess_b["csrf"], "nonce": nonce, "token": "temporary"},
            cookie=cookie_b,
        )
        self.assertEqual(status, 403)
        self.assertIn(nonce, sess_a["confirm"])


class FinalConsumerTokenTests(unittest.TestCase):
    def _run_config_fixture(self, mode: str):
        token = "TEMPORARY_SECRET_TOKEN_987654"
        with tempfile.TemporaryDirectory() as td:
            td_path = pathlib.Path(td)
            config = td_path / "config.sh"
            result_file = td_path / "result.json"
            config.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "print('Enter token:', flush=True)\n"
                "value=input()\n"
                "result={\n"
                " 'token_ok': bool(value),\n"
                " 'argv': sys.argv,\n"
                " 'env_values': list(os.environ.values()),\n"
                "}\n"
                "open(os.environ['RESULT_FILE'], 'w', encoding='utf-8').write(json.dumps(result))\n",
                encoding="utf-8",
            )
            config.chmod(0o755)
            read_fd, write_fd = os.pipe()
            env = os.environ.copy()
            env["RESULT_FILE"] = str(result_file)
            try:
                os.write(write_fd, (token + "\n").encode())
                os.close(write_fd)
                write_fd = -1
                proc = subprocess.run(
                    [
                        sys.executable,
                        str(WEB / "pty_token_adapter.py"),
                        "--token-fd",
                        str(read_fd),
                        "--mode",
                        mode,
                        "--",
                        str(config),
                    ],
                    pass_fds=(read_fd,),
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
            finally:
                os.close(read_fd)
                if write_fd >= 0:
                    os.close(write_fd)

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertNotIn(token, proc.stdout)
            self.assertNotIn(token, proc.stderr)
            result = json.loads(result_file.read_text(encoding="utf-8"))
            self.assertTrue(result["token_ok"])
            self.assertFalse(any(token in arg for arg in result["argv"]))
            self.assertFalse(any(token in value for value in result["env_values"]))
            for candidate in td_path.iterdir():
                if candidate.is_file():
                    self.assertNotIn(token, candidate.read_text(encoding="utf-8"))

    def test_final_config_consumer_create_has_no_argv_or_env_secret(self):
        self._run_config_fixture("create")

    def test_final_config_consumer_remove_has_no_argv_or_env_secret(self):
        self._run_config_fixture("remove")


class OfficialRemoveDiagnosticsTests(unittest.TestCase):
    def test_strict_marker_parser(self):
        good = b"GRT_REMOVE_RESULT_V1 stage=config_remove_failed exit=9\n"
        expected = {"ok": False, "error": "lifecycle_failed", "stage": "config_remove_failed", "exit_code": 9}
        self.assertEqual(lifecycle_worker.parse_remove_marker(good, 1), expected)
        permission = b"GRT_REMOVE_RESULT_V1 stage=permission_reconcile_failed exit=unknown\n"
        self.assertEqual(lifecycle_worker.parse_remove_marker(permission, 1), {
            "ok": False, "error": "lifecycle_failed",
            "stage": "permission_reconcile_failed", "exit_code": None})
        self.assertEqual(web_app.remove_failure_message({
            "ok": False, "error": "lifecycle_failed", "stage": "permission_reconcile_failed",
            "exit_code": None}),
            "Runner permission integrity could not be verified; no GitHub unregister was attempted. Administrator review required.")

        reconciled = b"GRT_REMOVE_RESULT_V1 stage=service_record_reconcile_failed exit=unknown\n"
        self.assertEqual(lifecycle_worker.parse_remove_marker(reconciled, 1),
                         {"ok": False, "error": "lifecycle_failed",
                          "stage": "service_record_reconcile_failed", "exit_code": None})
        self.assertEqual(web_app.remove_failure_message({
            "ok": False, "error": "lifecycle_failed",
            "stage": "service_record_reconcile_failed", "exit_code": None}),
            "Runner service record reconciliation failed; GitHub removal was not attempted. Inspect local service state.")
        fallback = {"ok": False, "error": "lifecycle_failed", "stage": "unknown_failed", "exit_code": None}
        for raw in (b"", good * 2, b"noise" + good, good + b"noise", good[:-1],
                    good.replace(b"9", b"999"), good.replace(b"9", b"-1"),
                    good.replace(b"config_remove_failed", b"arbitrary"),
                    b"GRT_REMOVE_RESULT_V1 stage=preflight_failed exit=0\n", b"X" * 129):
            with self.subTest(raw=raw[:70]):
                self.assertEqual(lifecycle_worker.parse_remove_marker(raw, 1), fallback)
        self.assertEqual(lifecycle_worker.parse_remove_marker(good, -9), fallback)
        self.assertEqual(lifecycle_worker.parse_remove_marker(good, 0), fallback)

    def test_remove_unknown_dispatcher_outcomes_are_explicit(self):
        expected = web_app.REMOVE_FAILURE_TEXT["unknown_failed"]
        for error in ("operation_timed_out", "internal_error",
                      "invalid_worker_result", "worker_error"):
            with self.subTest(error=error):
                self.assertEqual(web_app.remove_failure_message({"ok": False, "error": error}), expected)

    def test_web_static_failure_mapping_is_not_injectable(self):
        item = {"ok": False, "error": "lifecycle_failed", "stage": "service_uninstall_failed", "exit_code": 3}
        self.assertEqual(web_app.remove_failure_message(item),
                         "Runner service uninstall failed; check systemd state before retrying. Exit code: 3.")
        for stage, code in (("<script>alert(1)</script>", 1), ("config_remove_failed", "SECRET_TOKEN"),
                            ("config_remove_failed", True), ("config_remove_failed", 400)):
            self.assertEqual(web_app.remove_failure_message(
                {"error": "lifecycle_failed", "stage": stage, "exit_code": code}), "Runner operation failed.")

    def test_official_remove_source_contract(self):
        source = (ROOT / "scripts" / "remove-runner.sh").read_text()
        self.assertIn('./config.sh remove --token "$TOKEN"', source)
        self.assertIn('exec {RESULT_FD}>&-', source)
        # V1 remains on its original official --token path, while V2
        # exclusively uses the private FD/PTY adapter. The two contracts
        # are selected by the internal executor mode.
        self.assertIn('--mode remove -- ./config.sh remove', source)
        start = source.index('if [[ "$EXECUTOR_MODE" == "1" ]]; then\n    # V2 consumes')
        end = source.index('elif ( exec {RESULT_FD}>&-;', start)
        self.assertNotIn('--token "$TOKEN"', source[start:end])
        self.assertIn('./config.sh remove --token "$TOKEN"', source[end:])


if __name__ == "__main__":
    unittest.main(verbosity=2)
