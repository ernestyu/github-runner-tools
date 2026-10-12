#!/usr/bin/env python3
"""Transaction-specific authorization for internal CLI root operations.

The private socket is not a general privileged API: every request must match
the exact already-authenticated Create/Remove transaction, its derived runner
identity, and the operation appropriate for the transaction's direction.
"""
from __future__ import annotations

import os

from grt_web_common import canonical_service_name, make_local_id
from runner_lifecycle_authority import (AuthorityError, _current, _cycle_record,
                                         CREATE_STATES, REMOVE_STATES)
from v2_executor_contract import Request
from v2_privileged import admitted_operation


class TransactionBindingError(ValueError):
    pass


CREATE_OPS = frozenset({
    "create_state", "registration_attestation_create",
    "registration_permission_normalize", "service_install",
    "service_start", "service_state",
})
REMOVE_OPS = frozenset({
    "remove_state", "service_stop", "service_state",
    "service_uninstall",
})


def authorize_private_request(runtime, transaction: Request, payload) -> dict:
    """Validate fixed tuple; Runtime still validates paths and durable stages."""
    try:
        op = admitted_operation(payload)
        kind = transaction.op
        if kind not in ("create", "remove"):
            raise TransactionBindingError("no active mutation")
        if op["op"] not in (CREATE_OPS if kind == "create" else REMOVE_OPS):
            raise TransactionBindingError("privileged action not allowed in transaction")
        if op["repository"] != transaction.repository:
            raise TransactionBindingError("repository mismatch")
        owner, repo = transaction.repository.split("/", 1)
        path = os.path.join(runtime.runner_home, "actions-runner-" + make_local_id(owner, repo))
        if op["runner_dir"] != path or os.path.islink(path):
            raise TransactionBindingError("runner directory mismatch")
        expected_runner_name = (
            "local-ci-" + make_local_id(owner, repo)
            if kind == "create" else transaction.runner_name
        )
        if op["runner_name"] != expected_runner_name:
            raise TransactionBindingError("runner name mismatch")
        if "service" in op and op["service"] != canonical_service_name(
            transaction.repository, expected_runner_name
        ):
            raise TransactionBindingError("service mismatch")
        if kind == "remove":
            active = _current(transaction.repository, expected_runner_name, path)
            if active is None or active.get("instance") != transaction.instance:
                raise TransactionBindingError("instance changed")
        # For Create, PRE_REGISTRATION is the sole stage that creates the
        # instance. Afterward _current is the durable CLI journal pointer.
        elif not (op["op"] == "create_state" and op["stage"] == "PRE_REGISTRATION"):
            active = _current(transaction.repository, expected_runner_name, path)
            if active is None:
                raise TransactionBindingError("missing Create instance")
        # Existing root authority alone decides whether PRE_REGISTRATION may
        # start a fresh cycle. Do not inspect or mutate a journal beforehand.
        if (kind == "create" and op["op"] == "create_state"
                and op["stage"] == "PRE_REGISTRATION"):
            return op
        # Reject privileged service operations before the corresponding
        # durable CLI stage exists.
        active = _current(transaction.repository, expected_runner_name, path)
        if kind == "create":
            if active is None:
                if op["op"] == "create_state" and op["stage"] == "PRE_REGISTRATION":
                    return op
                raise TransactionBindingError("missing Create checkpoint")
            record = _cycle_record(CREATE_STATES, transaction.repository,
                                   expected_runner_name, path, active["instance"])
            stage = record.get("stage") if record else None
            allowed_stage = {
                "registration_permission_normalize": "REGISTERED_PERMISSION_INCOMPLETE",
                "registration_attestation_create": "REGISTERED_PERMISSION_INCOMPLETE",
                "service_install": "REGISTERED_UNIT_INCOMPLETE",
                "service_start": "REGISTERED_START_INCOMPLETE",
                "service_state": "REGISTERED_HEALTH_UNKNOWN",
            }.get(op["op"])
            if allowed_stage is not None and stage != allowed_stage:
                raise TransactionBindingError("Create checkpoint mismatch")
        else:
            ledger = _cycle_record(REMOVE_STATES, transaction.repository,
                                   expected_runner_name, path, transaction.instance)
            current = ledger.get("stage") if ledger else None
            if op["op"] == "remove_state":
                stage = op["stage"]
                expected = {"BEGIN": None,
                            "CONFIRM_REMOTE_REMOVED": "REMOTE_REMOVE_OUTCOME_UNKNOWN",
                            "COMPLETE": "REGISTERED_REMOVED_LOCAL_CLEANUP_PENDING"}[stage]
                if current != expected:
                    raise TransactionBindingError("Remove checkpoint mismatch")
            elif current != "REMOTE_REMOVE_OUTCOME_UNKNOWN":
                raise TransactionBindingError("Remove service operation before BEGIN")
        return op
    except (AuthorityError, ValueError, OSError, TypeError, KeyError) as exc:
        if isinstance(exc, TransactionBindingError):
            raise
        raise TransactionBindingError("privileged transaction binding rejected") from exc
