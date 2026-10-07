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
import dispatcher
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
        self.assertIn("Secure; HttpOnly; SameSite=Strict", source)
        self.assertIn('Cache-Control", "no-store"', source)
        self.assertIn('bind != "127.0.0.1"', source)


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


class DispatcherAuthorityTests(unittest.TestCase):
    def runtime(self):
        rt = object.__new__(dispatcher.Runtime)
        rt.runner_user = "actions"
        rt.runner_home = "/home/actions"
        return rt

    @staticmethod
    def regular_root_stat():
        obj = mock.Mock()
        obj.st_mode = stat.S_IFREG | 0o644
        obj.st_uid = 0
        return obj

    def base_props(self):
        return {
            "LoadState": "loaded",
            "FragmentPath": "/etc/systemd/system/actions.runner.owner-repo.runner.service",
            "DropInPaths": "",
            "User": "actions",
            "WorkingDirectory": "/home/actions/actions-runner-owner--repo",
            "ExecStart": "{ path=/home/actions/actions-runner-owner--repo/runsvc.sh ; argv[]=/home/actions/actions-runner-owner--repo/runsvc.sh ; }",
            "ExecStartPre": "",
            "ExecStartPost": "",
            "ExecStop": "",
            "ExecStopPost": "",
            "ExecReload": "",
        }

    def validate_with(self, props):
        rt = self.runtime()
        rt.runner_group = "actions"
        rt._systemctl_show = mock.Mock(return_value=props)
        service = "actions.runner.owner-repo.runner.service"
        unit = (
            "[Service]\n"
            "User=actions\n"
            "WorkingDirectory=/home/actions/actions-runner-owner--repo\n"
            "ExecStart=/home/actions/actions-runner-owner--repo/runsvc.sh\n"
        )
        with mock.patch("dispatcher.os.path.realpath", side_effect=lambda p: p), \
             mock.patch("dispatcher.os.path.islink", return_value=False), \
             mock.patch("dispatcher.os.stat", return_value=self.regular_root_stat()), \
             mock.patch("builtins.open", mock.mock_open(read_data=unit)):
            return rt.validate_unit(
                "owner/repo",
                "/home/actions/actions-runner-owner--repo",
                "runner",
                service,
            )

    def test_canonical_unit_passes(self):
        state, _ = self.validate_with(self.base_props())
        self.assertEqual(state, "present")

    def test_dropin_fails_closed(self):
        props = self.base_props()
        props["DropInPaths"] = "/etc/systemd/system/x.d/evil.conf"
        with self.assertRaises(dispatcher.DispatchError):
            self.validate_with(props)

    def test_extra_exec_fails_closed(self):
        props = self.base_props()
        props["ExecStartPre"] = "{ path=/bin/sh ; argv[]=/bin/sh -c evil ; }"
        with self.assertRaises(dispatcher.DispatchError):
            self.validate_with(props)

    def test_wrong_user_fails_closed(self):
        props = self.base_props()
        props["User"] = "root"
        with self.assertRaises(dispatcher.DispatchError):
            self.validate_with(props)

    def test_wrong_fragment_fails_closed(self):
        props = self.base_props()
        props["FragmentPath"] = "/tmp/unmanaged.service"
        with self.assertRaises(dispatcher.DispatchError):
            self.validate_with(props)

    def test_wrong_working_directory_fails_closed(self):
        props = self.base_props()
        props["WorkingDirectory"] = "/home/actions/actions-runner-other--repo"
        with self.assertRaises(dispatcher.DispatchError):
            self.validate_with(props)

    def test_wrong_execstart_fails_closed(self):
        props = self.base_props()
        props["ExecStart"] = "{ path=/tmp/evil ; argv[]=/tmp/evil ; }"
        with self.assertRaises(dispatcher.DispatchError):
            self.validate_with(props)

    def test_privileged_schema_rejects_unknown_extra_malformed_and_injection(self):
        rt = self.runtime()
        rt.runner_group = "actions"
        self.assertEqual(
            rt.privileged({"op": "unknown"}),
            {"ok": False, "error": "unknown_privileged_operation"},
        )
        self.assertEqual(
            rt.privileged({"op": "context_check", "extra": "x"}),
            {"ok": False, "error": "invalid_privileged_fields"},
        )
        for repository in ("bad", "owner/repo;rm", "../owner/repo"):
            result = rt.privileged(
                {
                    "op": "service_state",
                    "repository": repository,
                    "runner_dir": "/home/actions/actions-runner-owner--repo",
                    "runner_name": "runner",
                    "service": "actions.runner.owner-repo.runner.service",
                }
            )
            self.assertEqual(result, {"ok": False, "error": "privileged_validation_failed"})

    def test_public_dispatch_rejects_unknown_fields_before_execute(self):
        sent = []
        fake_request = mock.Mock()
        fake_request.sendall.side_effect = sent.append
        runtime = SimpleNamespace(web_pw=SimpleNamespace(pw_uid=4242))
        server = SimpleNamespace(runtime=runtime, execute=mock.Mock())
        handler = object.__new__(dispatcher.DispatchHandler)
        handler.request = fake_request
        handler.server = server
        with mock.patch("dispatcher.unix_peer_uid", return_value=4242), \
             mock.patch(
                 "dispatcher.recv_json_line",
                 return_value={"op": "create", "repository": "owner/repo", "token": "x", "command": "id"},
             ):
            handler.handle()
        self.assertFalse(server.execute.called)
        reply = json.loads(sent[0].decode("utf-8").strip())
        self.assertEqual(reply, {"ok": False, "error": "invalid_request_fields"})

    def test_repository_directory_binding_fails_closed(self):
        rt = self.runtime()
        with mock.patch("dispatcher.os.path.realpath", side_effect=lambda p: p), \
             mock.patch("dispatcher.os.path.islink", return_value=False):
            with self.assertRaises(dispatcher.DispatchError):
                rt.validate_runner_dir(
                    "owner/repo",
                    "/home/actions/actions-runner-someone--else",
                )

    def test_public_schema_has_no_command_path_fields(self):
        forbidden = {"exec", "shell", "command", "path", "script", "argv", "uid", "gid", "service_name", "systemd_unit"}
        for fields in dispatcher.ALLOWED_PUBLIC.values():
            self.assertTrue(forbidden.isdisjoint(fields))

    def test_worker_hardens_same_uid_fd_boundary(self):
        source = (WEB / "lifecycle_worker.py").read_text(encoding="utf-8")
        self.assertIn("PR_SET_DUMPABLE", source)
        self.assertIn("disable_ptrace_dumpability()", source)



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
            "WEB_BIND_ADDRESS=127.0.0.1\n"
            "WEB_PORT=0\n"
            "DISPATCH_SOCKET=/nonexistent/web-dispatch.sock\n"
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

        with mock.patch("app.dispatch", return_value={"ok": True}):
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
                {"repository": "owner/repo", "can_remove": True, "can_recover_local": False}
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


    def test_valid_login_establishes_opaque_secure_cookie(self):
        status, headers, _body = self.request(
            "POST", "/login", {"password": "correct-password"}
        )
        self.assertEqual(status, 303)
        cookie = headers.get("Set-Cookie", "")
        self.assertIn(f"{web_app.COOKIE_NAME}=", cookie)
        self.assertIn("Secure", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        sid = cookie.split(";", 1)[0].split("=", 1)[1]
        self.assertIn(sid, web_app.SESSIONS)
        self.assertNotIn("authenticated", sid)

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


class DispatcherAndWorkerAuthorityTests(unittest.TestCase):
    def test_same_uid_fake_socketpair_cannot_authorize_web_context(self):
        if os.geteuid() == 0:
            self.skipTest("negative same-UID test requires non-root test runner")
        left, right = socketpair = __import__("socket").socketpair()
        try:
            # A fake actions-side peer has the current non-root uid.
            self.assertEqual(lifecycle_worker.privileged_peer_uid(left.fileno()), os.geteuid())
            with self.assertRaises(RuntimeError):
                lifecycle_worker.privileged_context_check(left.fileno())
        finally:
            left.close()
            right.close()

    def test_worker_identity_requires_exact_uid_gid_no_groups_no_caps_path(self):
        uid, gid = 1234, 2345
        with mock.patch("lifecycle_worker.os.geteuid", return_value=uid), \
             mock.patch("lifecycle_worker.os.getegid", return_value=gid), \
             mock.patch("lifecycle_worker.os.getresuid", return_value=(uid, uid, uid)), \
             mock.patch("lifecycle_worker.os.getresgid", return_value=(gid, gid, gid)), \
             mock.patch("lifecycle_worker.os.getgroups", return_value=[]), \
             mock.patch("builtins.open", mock.mock_open(read_data=(
                 "CapInh:\t0000000000000000\n"
                 "CapPrm:\t0000000000000000\n"
                 "CapEff:\t0000000000000000\n"
                 "CapAmb:\t0000000000000000\n"
             ))):
            self.assertTrue(lifecycle_worker.verify_unprivileged_identity(uid, gid))

        with mock.patch("lifecycle_worker.os.geteuid", return_value=uid), \
             mock.patch("lifecycle_worker.os.getegid", return_value=gid), \
             mock.patch("lifecycle_worker.os.getresuid", return_value=(uid, uid, uid)), \
             mock.patch("lifecycle_worker.os.getresgid", return_value=(gid, gid, gid)), \
             mock.patch("lifecycle_worker.os.getgroups", return_value=[999]):
            self.assertFalse(lifecycle_worker.verify_unprivileged_identity(uid, gid))

    def test_dispatcher_peer_uid_helper_uses_unix_peer_credentials(self):
        import socket
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.assertEqual(dispatcher.unix_peer_uid(left), os.geteuid())
        finally:
            left.close()
            right.close()

    def test_dispatch_handler_rejects_wrong_peer_uid_before_request_parse(self):
        sent = []
        fake_request = mock.Mock()
        fake_request.sendall.side_effect = sent.append
        runtime = SimpleNamespace(web_pw=SimpleNamespace(pw_uid=4242))
        handler = object.__new__(dispatcher.DispatchHandler)
        handler.request = fake_request
        handler.server = SimpleNamespace(runtime=runtime)
        with mock.patch("dispatcher.unix_peer_uid", return_value=31337):
            handler.handle()
        self.assertTrue(sent)
        reply = json.loads(sent[0].decode("utf-8").strip())
        self.assertEqual(reply, {"ok": False, "error": "peer_not_authorized"})

    def test_dispatcher_launches_worker_with_exact_runner_identity_drop(self):
        class FakeProc:
            returncode = 0
            pid = 999999

            def communicate(self, timeout=None):
                return ('{"ok":true}', "")

        with tempfile.TemporaryDirectory() as td:
            lock_path = pathlib.Path(td) / "mutation.lock"
            lock_path.touch()
            runtime = SimpleNamespace(
                lock_file=str(lock_path),
                timeout=5,
                runner_user="actions",
                runner_home="/home/actions",
                worker_path="/root-owned/lifecycle_worker.py",
                runner_pw=SimpleNamespace(pw_uid=1234, pw_gid=2345),
                validate_fixed_worker=mock.Mock(),
                privileged=mock.Mock(return_value={"ok": True, "context": "dispatcher"}),
            )
            server = object.__new__(dispatcher.DispatchServer)
            server.runtime = runtime
            captured = {}

            def fake_popen(args, **kwargs):
                captured["args"] = args
                captured["kwargs"] = kwargs
                return FakeProc()

            with mock.patch("dispatcher.subprocess.Popen", side_effect=fake_popen):
                result = dispatcher.DispatchServer.execute(server, {"op": "list"})

            self.assertEqual(result, {"ok": True})
            self.assertEqual(captured["args"], ["/usr/bin/python3", runtime.worker_path])
            self.assertEqual(captured["kwargs"]["user"], 1234)
            self.assertEqual(captured["kwargs"]["group"], 2345)
            self.assertEqual(captured["kwargs"]["extra_groups"], [])
            self.assertTrue(captured["kwargs"]["start_new_session"])

    def test_web_dispatcher_held_lock_blocks_cli_register_and_remove(self):
        class BlockingProc:
            returncode = 0
            pid = 999998

            def __init__(self, entered, release):
                self.entered = entered
                self.release = release

            def communicate(self, timeout=None):
                self.entered.set()
                if not self.release.wait(timeout=5):
                    raise subprocess.TimeoutExpired("worker", timeout)
                return ('{"ok":true}', "")

        with tempfile.TemporaryDirectory() as td:
            lock_path = pathlib.Path(td) / "mutation.lock"
            lock_path.touch()
            entered = threading.Event()
            release = threading.Event()
            runtime = SimpleNamespace(
                lock_file=str(lock_path),
                timeout=5,
                runner_user="actions",
                runner_home="/home/actions",
                worker_path="/root-owned/lifecycle_worker.py",
                runner_pw=SimpleNamespace(pw_uid=os.geteuid(), pw_gid=os.getegid()),
                validate_fixed_worker=mock.Mock(),
                privileged=mock.Mock(return_value={"ok": True, "context": "dispatcher"}),
            )
            server = object.__new__(dispatcher.DispatchServer)
            server.runtime = runtime
            result_box = {}

            def run_web():
                result_box["result"] = dispatcher.DispatchServer.execute(
                    server, {"op": "create", "repository": "owner/repo", "token": "temporary"}
                )

            with mock.patch(
                "dispatcher.subprocess.Popen",
                side_effect=lambda *a, **kw: BlockingProc(entered, release),
            ):
                thread = threading.Thread(target=run_web)
                thread.start()
                self.assertTrue(entered.wait(timeout=2))
                for script in ("register-runner.sh", "remove-runner.sh"):
                    command = (
                        f'export GRT_TEST_MODE=1; export GRT_TEST_LOCK_DIR="{td}"; '
                        f'RUNNER_TOOLS_LIB_ONLY=1 source "{ROOT}/scripts/{script}"; '
                        "acquire_mutation_lock"
                    )
                    proc = subprocess.run(
                        ["bash", "-c", command],
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertNotEqual(proc.returncode, 0, script)
                release.set()
                thread.join(timeout=5)
            self.assertEqual(result_box.get("result"), {"ok": True})

    def test_cli_held_lock_makes_web_dispatcher_return_busy_before_worker_launch(self):
        import fcntl
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as td:
            lock_path = pathlib.Path(td) / "mutation.lock"
            lock_path.touch()
            holder = open(lock_path, "r+", encoding="utf-8")
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                server = object.__new__(dispatcher.DispatchServer)
                server.runtime = SimpleNamespace(lock_file=str(lock_path), timeout=10)
                result = dispatcher.DispatchServer.execute(
                    server,
                    {"op": "create", "repository": "owner/repo", "token": "temporary"},
                )
                self.assertEqual(result, {"ok": False, "error": "operation_in_progress"})
            finally:
                fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
                holder.close()


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
