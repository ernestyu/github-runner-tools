#!/usr/bin/env python3
"""Minimal Tailscale-only Web frontend for github-runner-tools."""
from __future__ import annotations

import argparse
import html
import http.cookies
import json
import os
import socket
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from grt_web_common import (
    DEFAULT_AUTH_CONFIG,
    DEFAULT_CONFIG,
    MAX_REQUEST_BYTES,
    MAX_REPOSITORY_LEN,
    MAX_TOKEN_LEN,
    encode_json_line,
    load_key_value,
    recv_json_line,
    secure_random_token,
    validate_repository,
    validate_temporary_token,
    verify_password,
)

SESSIONS: dict[str, dict[str, Any]] = {}
LOGIN_ATTEMPTS: dict[str, list[float]] = {}
SESSION_IDLE = 30 * 60
SESSION_ABSOLUTE = 8 * 60 * 60
CONFIRM_TTL = 5 * 60
COOKIE_NAME = "grt_session"


def now() -> float:
    return time.time()


def session_is_valid(sess: dict[str, Any], at: float | None = None) -> bool:
    t = now() if at is None else at
    return (
        t - float(sess["last"]) <= SESSION_IDLE
        and t - float(sess["created"]) <= SESSION_ABSOLUTE
    )


def login_attempt_allowed(source: str, at: float | None = None) -> bool:
    t = now() if at is None else at
    attempts = [x for x in LOGIN_ATTEMPTS.get(source, []) if t - x < 300]
    if attempts:
        LOGIN_ATTEMPTS[source] = attempts
    else:
        LOGIN_ATTEMPTS.pop(source, None)
    return len(attempts) < 8


def record_login_attempt(source: str, at: float | None = None) -> None:
    LOGIN_ATTEMPTS.setdefault(source, []).append(now() if at is None else at)


def consume_confirmation(
    sess: dict[str, Any], nonce: str, expected_op: str, at: float | None = None
) -> dict[str, Any] | None:
    pending = sess["confirm"].pop(nonce, None)
    t = now() if at is None else at
    if (
        not isinstance(pending, dict)
        or pending.get("op") != expected_op
        or pending.get("expires", 0) < t
    ):
        return None
    return pending


def form_fields_within_bounds(form: dict[str, str]) -> bool:
    limits = {
        "repository": MAX_REPOSITORY_LEN,
        "token": MAX_TOKEN_LEN,
        "password": 256,
        "csrf": 256,
        "nonce": 256,
    }
    return all(len(value) <= limits.get(key, 4096) for key, value in form.items())


def dispatch(config: dict[str, str], request: dict[str, Any]) -> dict[str, Any]:
    path = config.get("DISPATCH_SOCKET", "/run/github-runner-tools/web-dispatch.sock")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    timeout = 20
    if request.get("op") != "list":
        timeout = int(config.get("MUTATION_TIMEOUT_SECONDS", "900")) + 30
    sock.settimeout(timeout)
    try:
        sock.connect(path)
        sock.sendall(encode_json_line(request))
        response = recv_json_line(sock)
        if not isinstance(response, dict):
            return {"ok": False, "error": "dispatcher_invalid_response"}
        return response
    except (OSError, ValueError):
        return {"ok": False, "error": "dispatcher_unavailable"}
    finally:
        sock.close()


def page(title: str, body: str) -> bytes:
    doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>
body{{font-family:system-ui,-apple-system,sans-serif;max-width:760px;margin:0 auto;padding:20px;background:#f6f7f9;color:#171717}}
.card{{background:white;border:1px solid #ddd;border-radius:10px;padding:16px;margin:12px 0}}
.row{{display:flex;gap:10px;flex-wrap:wrap;align-items:center}}
input,button{{font:inherit;padding:10px;border-radius:8px;border:1px solid #aaa}}
input{{width:min(100%,430px);box-sizing:border-box}}
button{{cursor:pointer;background:#111;color:white}}
.danger{{background:#9d1c1c}} .muted{{color:#666;font-size:.92rem}}
.ok{{color:#126b2d}} .bad{{color:#9d1c1c}} code{{word-break:break-all}}
</style>
</head><body>{body}</body></html>"""
    return doc.encode("utf-8")


class App:
    def __init__(self, config_path: str, auth_path: str):
        self.config = load_key_value(config_path)
        self.auth = load_key_value(auth_path)
        self.password_hash = self.auth["PASSWORD_HASH"]

    def prune(self) -> None:
        t = now()
        stale = [
            sid
            for sid, sess in SESSIONS.items()
            if t - sess["last"] > SESSION_IDLE or t - sess["created"] > SESSION_ABSOLUTE
        ]
        for sid in stale:
            SESSIONS.pop(sid, None)
        for source, attempts in list(LOGIN_ATTEMPTS.items()):
            LOGIN_ATTEMPTS[source] = [x for x in attempts if t - x < 300]
            if not LOGIN_ATTEMPTS[source]:
                LOGIN_ATTEMPTS.pop(source, None)

    def new_session(self) -> tuple[str, dict[str, Any]]:
        sid = secure_random_token(32)
        sess = {
            "created": now(),
            "last": now(),
            "csrf": secure_random_token(24),
            "confirm": {},
        }
        SESSIONS[sid] = sess
        return sid, sess


class Handler(BaseHTTPRequestHandler):
    server_version = "github-runner-tools-web/1"
    app: App

    def log_message(self, fmt: str, *args: Any) -> None:
        # Never log bodies/forms. Method/path/status metadata only.
        safe_path = self.path.split("?", 1)[0]
        print(f'{self.client_address[0]} {self.command} {safe_path} {args[1] if len(args)>1 else ""}')

    def _source(self) -> str:
        # The backend is loopback-only. Do not trust forwarding headers for
        # rate-limit identity because a local process can spoof them.
        return self.client_address[0]

    def _send(self, status: int, body: bytes, *, cookie: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
        )
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _cookie_sid(self) -> str | None:
        raw = self.headers.get("Cookie", "")
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(raw)
        except http.cookies.CookieError:
            return None
        morsel = jar.get(COOKIE_NAME)
        return morsel.value if morsel else None

    def _session(self) -> tuple[str, dict[str, Any]] | None:
        self.app.prune()
        sid = self._cookie_sid()
        if not sid:
            return None
        sess = SESSIONS.get(sid)
        if not sess:
            return None
        t = now()
        if not session_is_valid(sess, t):
            SESSIONS.pop(sid, None)
            return None
        sess["last"] = t
        return sid, sess

    def _require_session(self) -> tuple[str, dict[str, Any]] | None:
        current = self._session()
        if current is None:
            self._redirect("/login")
            return None
        return current

    def _read_form(self) -> dict[str, str] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_REQUEST_BYTES:
            self._send(413, page("Request rejected", "<h1>Request too large or empty</h1>"))
            return None
        raw = self.rfile.read(length)
        try:
            parsed = urllib.parse.parse_qs(raw.decode("utf-8"), keep_blank_values=True, max_num_fields=10)
        except (UnicodeDecodeError, ValueError):
            self._send(400, page("Bad request", "<h1>Invalid form</h1>"))
            return None
        form = {key: values[-1] for key, values in parsed.items()}
        if not form_fields_within_bounds(form):
            self._send(413, page("Request rejected", "<h1>Request field too large</h1>"))
            return None
        return form

    @staticmethod
    def _csrf_ok(form: dict[str, str], sess: dict[str, Any]) -> bool:
        import hmac
        return hmac.compare_digest(form.get("csrf", ""), sess["csrf"])

    def _listed_eligible(self, repository: str, operation: str) -> bool:
        response = dispatch(self.app.config, {"op": "list"})
        if not response.get("ok") or not isinstance(response.get("runners"), list):
            return False
        flag = "can_remove" if operation == "remove" else "can_recover_local"
        for item in response["runners"]:
            if (
                isinstance(item, dict)
                and item.get("repository") == repository
                and item.get(flag) is True
            ):
                return True
        return False

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/login":
            body = page(
                "Login",
                """<div class="card"><h1>github-runner-tools</h1>
<form method="post" action="/login">
<label>Administrator password</label><br><input type="password" name="password" autocomplete="current-password" maxlength="256" required>
<br><br><button type="submit">Log in</button></form></div>""",
            )
            self._send(200, body)
            return
        if path == "/":
            current = self._require_session()
            if not current:
                return
            _, sess = current
            response = dispatch(self.app.config, {"op": "list"})
            cards = []
            if response.get("ok") and isinstance(response.get("runners"), list):
                for item in response["runners"]:
                    if not isinstance(item, dict):
                        continue
                    repo = item.get("repository")
                    repo_text = html.escape(repo or "Unknown repository")
                    name = html.escape(str(item.get("runner_name") or "unknown"))
                    service = html.escape(str(item.get("service_state") or "unknown"))
                    management = html.escape(str(item.get("management_state") or "ambiguous"))
                    actions = ""
                    if repo and item.get("can_remove") is True:
                        actions += f"""<form method="post" action="/remove/prepare"><input type="hidden" name="csrf" value="{sess['csrf']}"><input type="hidden" name="repository" value="{html.escape(repo)}"><button class="danger" type="submit">Remove</button></form>"""
                    if repo and item.get("can_recover_local") is True:
                        actions += f"""<form method="post" action="/recover/prepare"><input type="hidden" name="csrf" value="{sess['csrf']}"><input type="hidden" name="repository" value="{html.escape(repo)}"><button class="danger" type="submit">Recover local residue</button></form>"""
                    cards.append(
                        f"""<div class="card"><strong>{repo_text}</strong><div class="muted">Runner: {name}<br>Service: {service}<br>State: {management}</div><div class="row">{actions}</div></div>"""
                    )
            else:
                cards.append('<div class="card bad">Runner status is unavailable.</div>')
            body = page(
                "Runner management",
                f"""<h1>Runner management</h1>
<div class="card"><h2>Add runner</h2>
<form method="post" action="/create">
<input type="hidden" name="csrf" value="{sess['csrf']}">
<label>Repository (OWNER/REPO)</label><br><input name="repository" maxlength="200" required>
<br><br><label>Temporary registration token</label><br><input type="password" name="token" maxlength="1024" autocomplete="off" required>
<br><br><button type="submit">Create runner</button>
</form><p class="muted">Use only with trusted workflows. Public/untrusted workflows are outside the Web V1 security model.</p></div>
<h2>Current runners</h2>{''.join(cards)}
<form method="post" action="/logout"><input type="hidden" name="csrf" value="{sess['csrf']}"><button type="submit">Log out</button></form>""",
            )
            self._send(200, body)
            return
        self._send(404, page("Not found", "<h1>Not found</h1>"))

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        form = self._read_form()
        if form is None:
            return

        if path == "/login":
            source = self._source()
            self.app.prune()
            if not login_attempt_allowed(source):
                self._send(429, page("Too many attempts", "<h1>Try again later</h1>"))
                return
            password = form.get("password", "")
            record_login_attempt(source)
            if len(password) > 256 or not verify_password(password, self.app.password_hash):
                self._send(403, page("Login failed", "<h1>Login failed</h1>"))
                return
            LOGIN_ATTEMPTS.pop(source, None)
            sid, _ = self.app.new_session()
            cookie = f"{COOKIE_NAME}={sid}; Path=/; Secure; HttpOnly; SameSite=Strict"
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie", cookie)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return

        current = self._session()
        if current is None:
            self._redirect("/login")
            return
        sid, sess = current
        if not self._csrf_ok(form, sess):
            self._send(403, page("Rejected", "<h1>CSRF validation failed</h1>"))
            return

        if path == "/logout":
            SESSIONS.pop(sid, None)
            cookie = f"{COOKIE_NAME}=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Strict"
            self.send_response(303)
            self.send_header("Location", "/login")
            self.send_header("Set-Cookie", cookie)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return

        if path == "/create":
            repo = form.get("repository", "")
            token = form.get("token", "")
            try:
                validate_repository(repo)
            except ValueError:
                self._send(400, page("Invalid repository", "<h1>Invalid repository</h1>"))
                return
            try:
                validate_temporary_token(token)
            except ValueError:
                self._send(400, page("Invalid token", "<h1>Invalid registration token</h1>"))
                return
            result = dispatch(self.app.config, {"op": "create", "repository": repo, "token": token})
            token = ""
            if result.get("ok"):
                self._redirect("/")
            elif result.get("error") == "operation_in_progress":
                self._send(409, page("Busy", "<h1>Another lifecycle operation is already in progress.</h1>"))
            else:
                self._send(500, page("Create failed", "<h1>Runner creation failed.</h1>"))
            return

        if path in ("/remove/prepare", "/recover/prepare"):
            repo = form.get("repository", "")
            try:
                validate_repository(repo)
            except ValueError:
                self._send(400, page("Invalid repository", "<h1>Invalid repository</h1>"))
                return
            operation = "remove" if path.startswith("/remove") else "recover_local"
            if not self._listed_eligible(repo, operation):
                self._send(409, page("State changed", "<h1>This runner is not currently eligible for that operation.</h1>"))
                return
            nonce = secure_random_token(24)
            sess["confirm"][nonce] = {"op": operation, "repository": repo, "expires": now() + CONFIRM_TTL}
            if operation == "remove":
                extra = '<label>Temporary removal token</label><br><input type="password" name="token" maxlength="1024" autocomplete="off" required><br><br>'
                label = "Remove runner"
                target = "/remove/confirm"
            else:
                extra = ""
                label = "Recover local residue"
                target = "/recover/confirm"
            body = page(
                "Confirm",
                f"""<div class="card"><h1>Confirm {html.escape(label)}</h1><p><strong>{html.escape(repo)}</strong></p>
<form method="post" action="{target}">
<input type="hidden" name="csrf" value="{sess['csrf']}">
<input type="hidden" name="nonce" value="{nonce}">
{extra}<button class="danger" type="submit">{html.escape(label)}</button>
</form><p><a href="/">Cancel</a></p></div>""",
            )
            self._send(200, body)
            return

        if path in ("/remove/confirm", "/recover/confirm"):
            nonce = form.get("nonce", "")
            expected_op = "remove" if path.startswith("/remove") else "recover_local"
            pending = consume_confirmation(sess, nonce, expected_op)
            if pending is None:
                self._send(403, page("Rejected", "<h1>Confirmation expired or invalid.</h1>"))
                return
            repo = pending["repository"]
            request: dict[str, Any] = {"op": expected_op, "repository": repo}
            token = ""
            if expected_op == "remove":
                token = form.get("token", "")
                try:
                    validate_temporary_token(token)
                except ValueError:
                    self._send(400, page("Invalid token", "<h1>Invalid removal token</h1>"))
                    return
                request["token"] = token
            result = dispatch(self.app.config, request)
            token = ""
            if result.get("ok"):
                self._redirect("/")
            elif result.get("error") == "operation_in_progress":
                self._send(409, page("Busy", "<h1>Another lifecycle operation is already in progress.</h1>"))
            else:
                self._send(500, page("Operation failed", "<h1>Runner operation failed.</h1>"))
            return

        self._send(404, page("Not found", "<h1>Not found</h1>"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--auth-config", default=DEFAULT_AUTH_CONFIG)
    args = parser.parse_args()
    app = App(args.config, args.auth_config)
    bind = app.config.get("WEB_BIND_ADDRESS", "127.0.0.1")
    port = int(app.config.get("WEB_PORT", "8765"))
    if bind != "127.0.0.1":
        raise SystemExit("Web V1 must bind only to 127.0.0.1")
    Handler.app = app
    server = ThreadingHTTPServer((bind, port), Handler)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
