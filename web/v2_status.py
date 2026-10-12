#!/usr/bin/env python3
"""Read-only CLI Status execution as the configured runner owner.

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
from grt_web_common import DEFAULT_CONFIG, load_key_value


class StatusError(RuntimeError):
    pass


def runner_identity(runner_user: str) -> tuple[int, int, str]:
    try:
        pw = pwd.getpwnam(runner_user)
    except KeyError as exc:
        raise StatusError("runner user unavailable") from exc
    if pw.pw_uid <= 0 or pw.pw_gid <= 0 or not pw.pw_dir.startswith("/"):
        raise StatusError("invalid runner identity")
    return pw.pw_uid, pw.pw_gid, pw.pw_dir


def configured_runner_user(config_path: str = DEFAULT_CONFIG) -> str:
    try:
        value = load_key_value(config_path)["RUNNER_USER"]
    except (OSError, KeyError, ValueError) as exc:
        raise StatusError("runner configuration unavailable") from exc
    if not value:
        raise StatusError("runner user unavailable")
    return value


def status_as_runner(
    runner_user: str | None = None,
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
    if runner_user is None:
        runner_user = configured_runner_user()
    uid, gid, home = runner_identity(runner_user)
    from v2_code_provenance import verify_launch, CodeProvenanceError
    try:
        verify_launch("status")
    except CodeProvenanceError as exc:
        raise StatusError("pinned Status CLI provenance rejected") from exc

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
