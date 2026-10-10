#!/usr/bin/env python3
"""Bounded diagnostics from existing durable CLI lifecycle journals.

No subprocess transcripts, token bytes or registration metadata are returned.
A correlation ID is request-local; it is not a second persistent state store.
"""
from __future__ import annotations

import os
import secrets

from grt_web_common import make_local_id
from runner_lifecycle_authority import (
    AuthorityError, _current, _cycle_record, CREATE_STATES, REMOVE_STATES,
)

SAFE_STAGES = frozenset({
    "PRE_REGISTRATION", "REGISTRATION_OUTCOME_UNKNOWN",
    "REGISTERED_PERMISSION_INCOMPLETE", "REGISTERED_UNIT_INCOMPLETE",
    "REGISTERED_START_INCOMPLETE", "REGISTERED_HEALTH_UNKNOWN",
    "CREATE_COMPLETE", "REMOTE_REMOVE_OUTCOME_UNKNOWN",
    "REGISTERED_REMOVED_LOCAL_CLEANUP_PENDING", "REMOVE_COMPLETE",
})


def diagnostic(runtime, request, error: str, *,
               exit_code: int | None = None) -> dict:
    result = {"ok": False, "error": error,
              "correlation_id": secrets.token_hex(6)}
    if type(exit_code) is int and 0 < exit_code <= 255:
        result["exit_code"] = exit_code
    try:
        repo = request.repository
        owner, project = repo.split("/", 1)
        directory = os.path.join(runtime.runner_home,
                                 "actions-runner-" + make_local_id(owner, project))
        runner = ("local-ci-" + make_local_id(owner, project)
                  if request.op == "create" else request.runner_name)
        active = _current(repo, runner, directory)
        if active is not None:
            store = CREATE_STATES if request.op == "create" else REMOVE_STATES
            record = _cycle_record(store, repo, runner, directory, active["instance"])
            if record and record.get("stage") in SAFE_STAGES:
                result["stage"] = record["stage"]
    except (AuthorityError, OSError, TypeError, ValueError, KeyError):
        pass
    return result
