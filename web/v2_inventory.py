#!/usr/bin/env python3
"""V2 read-only inventory enrichment using existing root-owned attestation.

Only the root Executor may emit a Web-eligible Remove tuple. The terminal
Status CLI remains the single source of inventory and state labels. This
module only attaches a checked identity; it never creates lifecycle state.
"""
from __future__ import annotations

import json
import os

from dispatcher import Runtime
from grt_web_common import make_local_id
from runner_lifecycle_authority import _current
from v2_executor_contract import Request
from v2_remove_guard import check_remove_target, RemoveGuardError

CONFIG = "/etc/github-runner-tools/web.conf"


def annotate_inventory(result: dict) -> dict:
    if not result.get("ok") or not isinstance(result.get("data"), list):
        return result
    rows = result["data"]
    # Preserve read-only Status if Executor lacks root authority or config.
    try:
        runtime = Runtime(CONFIG)
    except (OSError, KeyError, RuntimeError, ValueError):
        runtime = None
    enriched = []
    for raw in rows:
        if not isinstance(raw, dict):
            return {"ok": False, "error": "invalid_cli_inventory"}
        row = dict(raw)
        row["can_remove"] = False
        row["can_recover_local"] = False
        row.pop("runner_id", None)
        row.pop("instance", None)
        if runtime is not None and row.get("configured") is True:
            try:
                repo, name = row["repository"], row["runner_name"]
                owner, project = repo.split("/", 1)
                directory = os.path.join(
                    runtime.runner_home, "actions-runner-" + make_local_id(owner, project)
                )
                # Root-owned active record is immutable across an individual
                # cycle; the final Remove still independently rechecks it.
                active = _current(repo, name, directory)
                if active is None:
                    raise RemoveGuardError("untracked runner")
                dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY |
                                 os.O_CLOEXEC | os.O_NOFOLLOW)
                try:
                    reg_fd = os.open(".runner", os.O_RDONLY | os.O_CLOEXEC |
                                     os.O_NOFOLLOW, dir_fd=dir_fd)
                    try:
                        raw = os.read(reg_fd, 16385)
                        if len(raw) > 16384:
                            raise RemoveGuardError("metadata too large")
                        metadata = json.loads(raw)
                    finally:
                        os.close(reg_fd)
                finally:
                    os.close(dir_fd)
                runner_id = metadata.get("agentId")
                if type(runner_id) is not int or runner_id <= 0:
                    raise RemoveGuardError("runner ID missing")
                request = Request(op="remove", repository=repo, runner_name=name,
                                  runner_id=runner_id, instance=active["instance"])
                check_remove_target(runtime, request)
                row.update(can_remove=True, runner_id=runner_id,
                           instance=active["instance"])
            except (OSError, KeyError, TypeError, ValueError, RemoveGuardError):
                pass
        enriched.append(row)
    return {"ok": True, "data": enriched}
