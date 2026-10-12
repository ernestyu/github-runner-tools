#!/usr/bin/env python3
"""V2 one-shot token handoff for an already authenticated Executor request.

The token is carried in an inherited pipe FD, not argv, environment, disk or
logs. This module does not grant privilege or start the registration process.
The caller must authenticate its socket peer and select a fixed CLI before use.
"""
from __future__ import annotations

import os

from v2_executor_contract import AdmissionError, validate_token_payload

TOKEN_READ_FD = 3


class TokenTransportError(RuntimeError):
    pass


def make_token_pipe(secret: bytes) -> int:
    """Return a single-use read descriptor containing a line-delimited token.

    Caller must close the returned descriptor after spawning the fixed CLI.
    A child must consume it exactly once and close it before external commands.
    """
    token = validate_token_payload(secret)
    read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
    try:
        view = memoryview(token + b"\n")
        while view:
            n = os.write(write_fd, view)
            if n <= 0:
                raise TokenTransportError("token handoff failed")
            view = view[n:]
        return read_fd
    except BaseException:
        os.close(read_fd)
        raise
    finally:
        os.close(write_fd)


def consume_token_fd(fd: int) -> bytes:
    """Read a single line without exposing the token in error text.

    Does not close FD on success; caller must close on every path.
    """
    if type(fd) is not int or fd < 0:
        raise TokenTransportError("invalid token descriptor")
    data = bytearray()
    while len(data) <= 1025:
        fragment = os.read(fd, min(1026 - len(data), 256))
        if not fragment:
            break
        data.extend(fragment)
        if b"\n" in fragment:
            break
    if not data.endswith(b"\n") or data.count(b"\n") != 1:
        raise TokenTransportError("malformed token descriptor")
    try:
        return validate_token_payload(bytes(data[:-1]))
    except AdmissionError as exc:
        raise TokenTransportError("invalid token descriptor") from exc


def close_quietly(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass
