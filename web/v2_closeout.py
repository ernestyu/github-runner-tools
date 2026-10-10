#!/usr/bin/env python3
"""Confirm successful CLI exit against the existing durable lifecycle journal.

Exit code 0 alone is not completion evidence. This module does not create,
reset, repair or advance any lifecycle record; it only verifies the journal
created by the shared CLI authority.
"""
from __future__ import annotations

import os
from runner_lifecycle_authority import _current, _cycle_record, CREATE_STATES, REMOVE_STATES, AuthorityError
from grt_web_common import make_local_id


def completed_mutation(runtime, request, *, remove_instance: str | None = None) -> bool:
    try:
        owner, name = request.repository.split("/", 1)
        local_id = make_local_id(owner, name)
        directory = os.path.join(runtime.runner_home, "actions-runner-" + local_id)
        runner = "local-ci-" + local_id if request.op == "create" else request.runner_name
        if request.op == "create":
            active = _current(request.repository, runner, directory)
            if not active:
                return False
            instance = active.get("instance")
            if not isinstance(instance, str):
                return False
            record = _cycle_record(CREATE_STATES, request.repository, runner, directory, instance)
            return bool(record and record.get("instance") == instance and
                        record.get("stage") == "CREATE_COMPLETE" and os.path.isdir(directory))
        if request.op == "remove":
            if remove_instance != request.instance:
                return False
            record = _cycle_record(REMOVE_STATES, request.repository, runner,
                                   directory, request.instance)
            return bool(record and record.get("instance") == request.instance and
                        record.get("stage") == "REMOVE_COMPLETE" and
                        not os.path.lexists(directory))
        return False
    except (AuthorityError, OSError, ValueError, TypeError, KeyError):
        return False
