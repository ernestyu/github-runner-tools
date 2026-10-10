#!/usr/bin/env python3
"""Read-only V2 CLI Status execution as the runner owner.

No shell interpretation, no sudo and no state mutation. This module is a
building block for the future bounded Executor, not a running server.
"""
from __future__ import annotations

import json
import os
import pwd
import subprocess
from typing import Any

from v2_executor_contract import CLI_ARGV


class StatusError(RuntimeError):
    pass


def runner_identity(runner_user: str) -> tuple[int, int, str]:
    if runner_user != "actions":
        raise StatusError("unsupported runner user")
    try:
        pw = pwd.getpwnam(runner_user)
    except KeyError as exc:
        raise StatusError("runner user unavailable") from exc
    return pw.pw_uid, pw.pw_gid, pw.pw_dir


def status_as_runner(
    runner_user: str = "actions",
    *,
    timeout: int = 15,
) -> dict[str, Any]:
    """Execute the installed read-only CLI under the configured runner UID.

    This must never be invoked with a privileged CLI subcommand. Requires an
    Executor already running as root; the subprocess itself has no root UID.
    """
    if os.geteuid() != 0:
        raise StatusError("executor must be root")
    if type(timeout) is not int or not 1 <= timeout <= 60:
        raise StatusError("invalid status timeout")
    uid, gid, home = runner_identity(runner_user)

    # subprocess user/group/extra_groups are applied before exec; no shell.
    try:
        result = subprocess.run(
            list(CLI_ARGV["status"]),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=timeout,
            env={
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "HOME": home,
                "USER": runner_user,
                "LOGNAME": runner_user,
            },
            user=uid,
            group=gid,
            extra_groups=[],
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StatusError("status CLI unavailable") from exc
    if result.returncode != 0:
        raise StatusError("status CLI failed")
    if len(result.stdout) > 1048576:
        raise StatusError("status CLI output too large")
    try:
        payload = json.loads(result.stdout.decode("utf-8", "strict"))
    except (ValueError, UnicodeError) as exc:
        raise StatusError("invalid status CLI output") from exc
    # Do not alter or synthesize the CLI's runner information.
    if not isinstance(payload, (dict, list)):
        raise StatusError("invalid status CLI output")
    return {"ok": True, "data": payload}
