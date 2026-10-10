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
from runner_lifecycle_authority import (_key, _safe_directory, ROOT, ACTIVE_INSTANCES,
                                         CREATE_STATES, REMOVE_STATES, ATTESTATIONS,
                                         UNIT_ATTESTATIONS)
from v2_executor_contract import Request
from v2_remove_guard import check_remove_target, RemoveGuardError

CONFIG = "/etc/github-runner-tools/web.conf"

def _read_active_without_mutation(repo: str, name: str, directory: str):
    """Open existing root state only; Status must never mkdir a state store."""
    import stat
    state_dir = ROOT + "/" + ACTIVE_INSTANCES
    try:
        fd = _safe_directory(state_dir)
    except FileNotFoundError:
        return None
    try:
        opened = os.open(_key(repo, name, directory), os.O_RDONLY | os.O_CLOEXEC |
                         os.O_NOFOLLOW, dir_fd=fd)
        try:
            st = os.fstat(opened)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o077:
                return None
            raw = os.read(opened, 16385)
            if len(raw) > 16384:
                return None
            active = json.loads(raw)
            if (active.get("repository"), active.get("runner"),
                    active.get("runner_dir")) != (repo, name, directory):
                return None
            return active
        finally:
            os.close(opened)
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return None
    finally:
        os.close(fd)



def annotate_inventory(result: dict) -> dict:
    if not result.get("ok") or not isinstance(result.get("data"), list):
        return result
    rows = result["data"]
    # Preserve read-only Status if Executor lacks root authority or config.
    try:
        runtime = Runtime(CONFIG)
    except (OSError, KeyError, RuntimeError, ValueError):
        runtime = None
    # Existing authority helpers use mkdir(exist_ok) on each read. Refuse
    # attestation enrichment if any required root store is absent, so a pure
    # Status request can never initialize new persistent directories.
    stores_ready = all(os.path.isdir(os.path.join(ROOT, subdir)) for subdir in (
        ACTIVE_INSTANCES, CREATE_STATES, REMOVE_STATES, ATTESTATIONS,
        UNIT_ATTESTATIONS,
    ))
    enriched = []
    for raw in rows:
        if not isinstance(raw, dict):
            return {"ok": False, "error": "invalid_cli_inventory"}
        row = dict(raw)
        row["can_remove"] = False
        row["can_recover_local"] = False
        row.pop("runner_id", None)
        row.pop("instance", None)
        if runtime is not None and stores_ready and row.get("configured") is True:
            try:
                repo, name = row["repository"], row["runner_name"]
                owner, project = repo.split("/", 1)
                directory = os.path.join(
                    runtime.runner_home, "actions-runner-" + make_local_id(owner, project)
                )
                # Root-owned active record is immutable across an individual
                # cycle; the final Remove still independently rechecks it.
                active = _read_active_without_mutation(repo, name, directory)
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
