#!/usr/bin/env python3
"""Single-threaded V2 CLI execution and private privileged channel pump.

No lifecycle worker and no separately running privileged-loop thread. The
root Executor serializes a request under the existing host mutation lock;
this event pump services only that child's private socket and bounded output.
"""
from __future__ import annotations

import json
import os
import selectors
import socket
import time

from v2_binding import authorize_private_request, TransactionBindingError
from v2_cli_spawn import terminate_group
from v2_privileged import execute_privileged, PrivilegedProtocolError

MAX_PRIVATE_FRAME = 16384
MAX_CAPTURE = 131072


class PumpError(RuntimeError):
    pass


def handle_root_request(runtime, transaction, raw: bytes, deadline: float) -> bytes:
    try:
        incoming = json.loads(raw.decode("utf-8", "strict"))
        authorized = authorize_private_request(runtime, transaction, incoming)
        answer = execute_privileged(runtime, authorized, deadline=deadline)
    except (ValueError, UnicodeError, OSError, TransactionBindingError,
            PrivilegedProtocolError):
        answer = {"ok": False, "error": "privileged_request_rejected"}
    return json.dumps(answer, separators=(",", ":")).encode("utf-8") + b"\n"


def pump_transaction(runtime, transaction, pid: int, control: socket.socket,
                     output_fd: int, deadline: float) -> tuple[int | None, bool]:
    """Return (waitpid status, ambiguous/timeout). Reap and close output FD.

    The caller keeps the shared host lock until this function returns.
    """
    output = bytearray()
    input_buffer = bytearray()
    exited = False
    status = None
    output_eof = False
    control_eof = False

    control.setblocking(False)
    os.set_blocking(output_fd, False)
    poll = selectors.DefaultSelector()
    poll.register(control, selectors.EVENT_READ, "privilege")
    poll.register(output_fd, selectors.EVENT_READ, "output")
    try:
        while time.monotonic() < deadline:
            if not exited:
                try:
                    got, code = os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    return None, True
                if got == pid:
                    exited, status = True, code
            if exited and (control_eof or not input_buffer) and output_eof:
                from v2_cli_diagnostics import log_cli_exit
                log_cli_exit(transaction.op, status, bytes(output))
                return status, False
            events = poll.select(min(max(deadline - time.monotonic(), 0), 0.15))
            for key, _mask in events:
                if key.data == "output":
                    try:
                        chunk = os.read(output_fd, 8192)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        poll.unregister(output_fd)
                        output_eof = True
                    elif len(output) < MAX_CAPTURE:
                        output.extend(chunk[:MAX_CAPTURE - len(output)])
                else:
                    try:
                        part = control.recv(4096)
                    except BlockingIOError:
                        continue
                    if not part:
                        poll.unregister(control)
                        control_eof = True
                        continue
                    input_buffer.extend(part)
                    if len(input_buffer) > MAX_PRIVATE_FRAME:
                        return None, True
                    while b"\n" in input_buffer:
                        raw, _, remaining = input_buffer.partition(b"\n")
                        input_buffer = bytearray(remaining)
                        response = handle_root_request(runtime, transaction, raw, deadline)
                        # Temporary blocking mode only during bounded reply.
                        control.settimeout(max(min(deadline-time.monotonic(), 5.0), 0.01))
                        try:
                            control.sendall(response)
                        except OSError:
                            return None, True
                        finally:
                            control.setblocking(False)
        return None, True
    finally:
        poll.close()
        os.close(output_fd)
        if not exited:
            terminate_group(pid)
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass
