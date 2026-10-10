#!/usr/bin/env python3
"""Fixed installed root authority for non-Web interactive CLI Create.

This entrypoint must be root-owned at the fixed installed path and is invoked
through sudo, never executed from the untrusted checked-out repository.
"""
import os
import pwd
import re
import stat
import sys
from pathlib import Path

from grt_web_common import canonical_service_name, make_local_id, validate_repository
from runner_lifecycle_authority import (
    AuthorityError, create_attestation, create_stage, create_unit_attestation, normalize_new_registration,
    _safe_directory, _current, _cycle_record, CREATE_STATES,
)

INSTALL = "/usr/local/lib/github-runner-tools/web/cli_create_authority.py"
ALLOWED = frozenset({"PRE_REGISTRATION", "REGISTRATION_OUTCOME_UNKNOWN",
                     "REGISTERED_PERMISSION_INCOMPLETE", "REGISTERED_UNIT_INCOMPLETE",
                     "REGISTERED_START_INCOMPLETE", "REGISTERED_HEALTH_UNKNOWN",
                     "CREATE_COMPLETE"})


def main(argv: list[str]) -> None:
    if os.geteuid() != 0 or not os.environ.get("SUDO_USER"):
        raise AuthorityError("requires interactive sudo authority")
    user = os.environ["SUDO_USER"]
    if user == "root":
        raise AuthorityError("non-root Runner owner required")
    account = pwd.getpwnam(user)
    if len(argv) != 6 or argv[0] not in ("stage", "attest", "unit", "normalize"):
        raise AuthorityError("invalid operation")
    op, repo, runner_name, directory, action, version = argv
    repo = validate_repository(repo)
    if not runner_name or any(c in runner_name for c in "/\r\n"):
        raise AuthorityError("invalid runner name")
    home = os.path.realpath(account.pw_dir)
    owner, project = repo.split("/", 1)
    expected_dir = os.path.join(home, "actions-runner-" + make_local_id(owner, project))
    if directory != expected_dir:
        raise AuthorityError("noncanonical CLI Runner target")
    df = _safe_directory(directory)
    try:
        ds = os.fstat(df)
        if ds.st_uid != account.pw_uid or ds.st_gid != account.pw_gid or ds.st_mode & 0o022:
            raise AuthorityError("untrusted runner directory")
    finally:
        os.close(df)
    if op == "stage":
        if action not in ALLOWED or version != "-":
            raise AuthorityError("invalid CLI stage")
        create_stage(repo, runner_name, directory, action)
    elif op == "normalize":
        if action != "-" or version != "-":
            raise AuthorityError("invalid CLI normalize invocation")
        normalize_new_registration(repo, runner_name, directory, user)
    elif op == "attest":
        if action != "-" or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
            raise AuthorityError("invalid attestation request")
        create_attestation(repo, runner_name, directory, user, version)
    else:
        if action != "-" or version != "-":
            raise AuthorityError("invalid Unit verification")
        service = canonical_service_name(repo, runner_name)
        unit = Path("/etc/systemd/system") / service
        fd_dir = _safe_directory("/etc/systemd/system")
        try:
            fd = os.open(service, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK |
                         os.O_CLOEXEC, dir_fd=fd_dir)
            try:
                st = os.fstat(fd)
                if (not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_gid != 0
                        or st.st_nlink != 1 or stat.S_IMODE(st.st_mode) != 0o644):
                    raise AuthorityError("official CLI Unit needs administrative review")
                content = os.read(fd, 16385)
                if len(content) > 16384:
                    raise AuthorityError("Unit too large")
                lines = content.decode("utf-8").splitlines()
                allowed_keys = {"User", "WorkingDirectory", "ExecStart"}
                for field, expected in (
                    ("User", user), ("WorkingDirectory", directory),
                    ("ExecStart", directory + "/runsvc.sh")):
                    if lines.count(field + "=" + expected) != 1:
                        raise AuthorityError("CLI Unit identity mismatch")
                if any(line.startswith((
                        "ExecStartPre=", "ExecStartPost=", "ExecStop=",
                        "ExecStopPost=", "ExecReload=")) for line in lines):
                    raise AuthorityError("unsupported unit exec hook")
                path_st = os.stat(service, dir_fd=fd_dir, follow_symlinks=False)
                after = os.fstat(fd)
                if ((st.st_dev, st.st_ino, st.st_ctime_ns) !=
                    (path_st.st_dev, path_st.st_ino, path_st.st_ctime_ns) or
                    (st.st_dev, st.st_ino, st.st_mtime_ns) !=
                    (after.st_dev, after.st_ino, after.st_mtime_ns)):
                    raise AuthorityError("CLI Unit changed during validation")
            finally:
                os.close(fd)
        finally:
            os.close(fd_dir)
        create_unit_attestation(repo, runner_name, directory, service, str(unit))


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except Exception:
        print("CLI lifecycle authority refused; registration may be incomplete", file=sys.stderr)
        sys.exit(1)
