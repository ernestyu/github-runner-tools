#!/usr/bin/env python3
"""Transaction-specific authorization for internal CLI root operations.

The private socket is not a general privileged API: every request must match
the exact already-authenticated Create/Remove transaction, its derived runner
identity, and the operation appropriate for the transaction's direction.
"""
from __future__ import annotations

import os

from grt_web_common import canonical_service_name, make_local_id
from runner_lifecycle_authority import _current
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
        return op
    except (ValueError, OSError, TypeError, KeyError) as exc:
        if isinstance(exc, TransactionBindingError):
            raise
        raise TransactionBindingError("privileged transaction binding rejected") from exc
