#!/usr/bin/env python3
"""Fixed sudo authority for interactive CLI Remove checkpoint transitions.

Delegates to the exact existing root Remove journal used by V2 Executor.
No generic root execution, shell or alternate removal state machine.
"""
from __future__ import annotations

import os
import pwd
import re
import stat
import sys
from grt_web_common import make_local_id, validate_repository
from runner_lifecycle_authority import AuthorityError, remove_stage, _safe_directory


class RemoveCLIError(ValueError):
    pass


def checkpoint(argv: list[str], *, sudo_user: str | None = None) -> None:
    if os.geteuid() != 0:
        raise RemoveCLIError("root checkpoint authority required")
    user = sudo_user if sudo_user is not None else os.environ.get("SUDO_USER")
    if not user or user == "root":
        raise RemoveCLIError("interactive Runner owner required")
    try:
        account = pwd.getpwnam(user)
    except KeyError as exc:
        raise RemoveCLIError("unknown Runner owner") from exc
    if len(argv) != 4:
        raise RemoveCLIError("invalid root checkpoint fields")
    repo, runner, directory, stage = argv
    repo = validate_repository(repo)
    if not isinstance(runner, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", runner):
        raise RemoveCLIError("invalid runner identity")
    if stage not in ("BEGIN", "CONFIRM_REMOTE_REMOVED", "COMPLETE"):
        raise RemoveCLIError("invalid removal stage")
    owner, name = repo.split("/", 1)
    canonical = os.path.join(os.path.realpath(account.pw_dir),
                             "actions-runner-" + make_local_id(owner, name))
    if directory != canonical:
        raise RemoveCLIError("noncanonical runner target")
    if stage != "COMPLETE":
        fd = _safe_directory(directory)
        try:
            st = os.fstat(fd)
            if st.st_uid != account.pw_uid or st.st_gid != account.pw_gid or st.st_mode & 0o022:
                raise RemoveCLIError("unsafe runner directory")
        finally:
            os.close(fd)
    elif os.path.lexists(directory):
        raise RemoveCLIError("runner still exists after cleanup")
    remove_stage(repo, runner, directory, user, stage)


def main() -> int:
    try:
        checkpoint(sys.argv[1:])
    except (AuthorityError, RemoveCLIError, OSError, ValueError, TypeError):
        print("CLI Remove checkpoint rejected; reconcile before retry", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
