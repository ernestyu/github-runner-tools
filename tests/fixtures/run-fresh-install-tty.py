#!/usr/bin/env python3
"""Run the production fresh installer under a real controlling TTY.

The fixture password is read from this helper's ordinary stdin before the
child PTY is created. It is sent only after the installer's no-echo password
prompts appear, so CI output never races ahead of terminal echo suppression.
"""
from __future__ import annotations

import os
import pty
import select
import sys

if len(sys.argv) < 2:
    raise SystemExit("usage: run-fresh-install-tty.py COMMAND [ARG...]")

password = sys.stdin.readline().rstrip("\r\n")
if not password:
    raise SystemExit("fixture password missing")

argv = sys.argv[1:]
pid, master = pty.fork()
if pid == 0:
    os.execvpe(argv[0], argv, os.environ)

prompts = [
    b"Web administrator password: ",
    b"Confirm Web administrator password: ",
]
sent = [False, False]
seen = bytearray()
status = None

try:
    while True:
        ready, _, _ = select.select([master], [], [], 0.1)
        if ready:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                chunk = b""
            if chunk:
                seen.extend(chunk)
                # Never echo the fixture password even if a regression in
                # terminal state causes the child to reflect it.
                safe = chunk.replace(password.encode(), b"[REDACTED]")
                sys.stdout.buffer.write(safe)
                sys.stdout.buffer.flush()
                for idx, prompt in enumerate(prompts):
                    if not sent[idx] and prompt in seen:
                        os.write(master, password.encode() + b"\n")
                        sent[idx] = True
                if len(seen) > 16384:
                    del seen[:-8192]
            else:
                break
        waited, raw = os.waitpid(pid, os.WNOHANG)
        if waited == pid:
            status = raw
            break
finally:
    try:
        os.close(master)
    except OSError:
        pass

if status is None:
    _, status = os.waitpid(pid, 0)
if os.WIFEXITED(status):
    raise SystemExit(os.WEXITSTATUS(status))
if os.WIFSIGNALED(status):
    raise SystemExit(128 + os.WTERMSIG(status))
raise SystemExit(125)
