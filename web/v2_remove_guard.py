#!/usr/bin/env python3
"""Read-only root attestation gate for V2 Remove.

Bind the browser's exact tuple to the existing durable active cycle, signed
registration provenance and on-disk GitHub runner identity *before* a CLI
can stop a service or contact GitHub. No legacy adoption in Web.
"""
from __future__ import annotations
import json
import os
import stat
from pathlib import Path

from grt_web_common import canonical_service_name, make_local_id
from runner_lifecycle_authority import (
    AuthorityError, _current, _cycle_record, ATTESTATIONS, CREATE_STATES,
    REMOVE_STATES,
)


class RemoveGuardError(ValueError):
    pass


def check_remove_target(runtime, request) -> str:
    if request.op != "remove":
        raise RemoveGuardError("not a Remove request")
    repo = request.repository
    owner, name = repo.split("/", 1)
    local_id = make_local_id(owner, name)
    runner_dir = os.path.join(runtime.runner_home, "actions-runner-" + local_id)
    try:
        runner_dir = runtime.validate_runner_dir(repo, runner_dir, allow_legacy=False)
        active = _current(repo, request.runner_name, runner_dir)
        if active is None or active["instance"] != request.instance:
            raise RemoveGuardError("active instance not attested")
        created = _cycle_record(CREATE_STATES, repo, request.runner_name,
                                runner_dir, request.instance)
        if created is None or created.get("stage") != "CREATE_COMPLETE":
            raise RemoveGuardError("creation not complete")
        removed = _cycle_record(REMOVE_STATES, repo, request.runner_name,
                                runner_dir, request.instance)
        if removed is not None:
            raise RemoveGuardError("previous Remove requires reconciliation")
        att = _cycle_record(ATTESTATIONS, repo, request.runner_name,
                            runner_dir, request.instance)
        if (att is None or att.get("instance") != request.instance or
                att.get("github_runner_id") != request.runner_id or
                att.get("capture_stage") != "registered-secure-before-service"):
            raise RemoveGuardError("registration attestation mismatch")
        dir_fd = os.open(runner_dir, os.O_RDONLY | os.O_DIRECTORY |
                        os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            st = os.fstat(dir_fd)
            if ((st.st_dev, st.st_ino) !=
                    (created.get("directory_device"), created.get("directory_inode"))):
                raise RemoveGuardError("runner inode mismatch")
            fd = os.open(".runner", os.O_RDONLY | os.O_CLOEXEC |
                         os.O_NOFOLLOW, dir_fd=dir_fd)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:
                    raise RemoveGuardError("unsafe registration metadata")
                raw = os.read(fd, 16385)
                if len(raw) > 16384:
                    raise RemoveGuardError("registration metadata oversized")
                metadata = json.loads(raw)
            finally:
                os.close(fd)
        finally:
            os.close(dir_fd)
        if (metadata.get("agentName") != request.runner_name or
                type(metadata.get("agentId")) is not int or
                metadata["agentId"] != request.runner_id or
                str(metadata.get("gitHubUrl", "")).rstrip("/").lower()
                != ("https://github.com/" + repo).lower()):
            raise RemoveGuardError("GitHub runner identity changed")
        service = canonical_service_name(repo, request.runner_name)
        runtime.validate_unit(repo, runner_dir, request.runner_name,
                              service, allow_absent=False)
        return runner_dir
    except (AuthorityError, OSError, ValueError, KeyError, TypeError) as exc:
        if isinstance(exc, RemoveGuardError):
            raise
        raise RemoveGuardError("Remove identity verification failed") from exc
