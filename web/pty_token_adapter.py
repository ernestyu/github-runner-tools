#!/usr/bin/env python3
"""Feed a temporary GitHub token to config.sh through a PTY, never argv/env."""
from __future__ import annotations

import argparse
import os
import pty
import select
import subprocess
import sys
import termios
import time


def read_token(fd: int) -> bytes:
    data = bytearray()
    while len(data) <= 2048:
        chunk = os.read(fd, 2048)
        if not chunk:
            break
        data.extend(chunk)
        if b"\n" in data:
            break
    token = bytes(data).split(b"\n", 1)[0].rstrip(b"\r")
    if not token or len(token) > 1024:
        raise RuntimeError("invalid token input")
    return token


def redact(data: bytes, token: bytes) -> bytes:
    return data.replace(token, b"[REDACTED]")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--token-fd", type=int, required=True)
    parser.add_argument("--mode", choices=("create", "remove"), required=True)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise SystemExit("missing command")

    token = read_token(args.token_fd)
    try:
        os.close(args.token_fd)
    except OSError:
        pass

    master, slave = pty.openpty()
    attrs = termios.tcgetattr(slave)
    attrs[3] &= ~termios.ECHO
    termios.tcsetattr(slave, termios.TCSANOW, attrs)

    proc = subprocess.Popen(
        command,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        close_fds=True,
        env=os.environ.copy(),
    )
    os.close(slave)

    deadline = time.monotonic() + args.timeout
    transcript = bytearray()
    token_sent = False
    try:
        while proc.poll() is None:
            if time.monotonic() >= deadline:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                return 124

            ready, _, _ = select.select([master], [], [], 0.2)
            if not ready:
                continue
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            transcript.extend(chunk)
            if len(transcript) > 32768:
                del transcript[:-16384]

            lowered = bytes(transcript).lower()
            if not token_sent and b"token" in lowered:
                os.write(master, token + b"\n")
                token_sent = True
                transcript.clear()

            # Non-secret arguments are supplied on the command line. These
            # defaults handle only known harmless optional prompts if a runner
            # version still asks them interactively.
            if args.mode == "create" and token_sent:
                low = chunk.lower()
                safe_prompts = (
                    b"enter the name of runner group",
                    b"enter the name of runner",
                    b"enter any additional labels",
                    b"enter name of work folder",
                )
                if any(p in low for p in safe_prompts):
                    os.write(master, b"\n")

            # Raw PTY output is intentionally not forwarded. The Web path
            # needs only the exit status; suppressing the transcript prevents
            # an echoed or transformed secret from reaching logs/responses.

        rc = proc.wait(timeout=5)
        if not token_sent:
            print("ERROR: runner never requested a token on PTY", file=sys.stderr)
            return 65
        return rc
    finally:
        try:
            os.close(master)
        except OSError:
            pass
        token = b"\x00" * len(token)


if __name__ == "__main__":
    raise SystemExit(main())
