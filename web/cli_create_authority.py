#!/usr/bin/env python3
"""Fixed installed root authority for non-Web interactive CLI Create.

This entrypoint must be root-owned at the fixed installed path and is invoked
through sudo, never executed from the untrusted checked-out repository.
"""
import os
import pwd
import re
import stat
import subprocess
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


def validate_official_unit(lines: list[str], user: str, directory: str) -> None:
    """Only the pinned official systemd service template schema is accepted."""
    expected = {
        "[Unit]": {"After": "network.target"},
        "[Service]": {
            "ExecStart": directory + "/runsvc.sh",
            "User": user, "WorkingDirectory": directory,
            "KillMode": "process", "KillSignal": "SIGTERM",
            "TimeoutStopSec": "5min"},
        "[Install]": {"WantedBy": "multi-user.target"},
    }
    parsed: dict[str, dict[str, str]] = {}
    section = None
    for line in lines:
        if not line:
            continue
        if line in expected:
            if line in parsed:
                raise AuthorityError("duplicate Unit section")
            parsed[line] = {}
            section = line
            continue
        if line.startswith("[") or section is None or "=" not in line:
            raise AuthorityError("unexpected Unit section or directive")
        key, value = line.split("=", 1)
        if key in parsed[section] or (
                key not in expected[section] and
                not (section == "[Unit]" and key == "Description")):
            raise AuthorityError("unsupported or duplicated Unit directive")
        parsed[section][key] = value
    if set(parsed) != set(expected):
        raise AuthorityError("incomplete Unit definition")
    for section, keys in expected.items():
        for key, val in keys.items():
            if parsed[section].get(key) != val:
                raise AuthorityError("Unit identity/schema mismatch")
        if set(parsed[section]) != (set(keys) | ({"Description"} if section == "[Unit]" else set())):
            raise AuthorityError("Unit schema mismatch")
    description = parsed["[Unit]"].get("Description", "")
    if not description.startswith("GitHub Actions Runner") or len(description) > 200:
        raise AuthorityError("unknown official Unit description")


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
                validate_official_unit(lines, user, directory)
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
        result = subprocess.run(
            ["/usr/bin/systemctl", "show", service, "--no-pager",
             "-p", "LoadState", "-p", "FragmentPath", "-p", "DropInPaths",
             "-p", "User", "-p", "WorkingDirectory", "-p", "ExecStart"],
            capture_output=True, text=True, timeout=15, check=False)
        if result.returncode != 0:
            raise AuthorityError("systemd Unit state cannot be checked")
        fields = {}
        for line in result.stdout.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                fields[key] = value
        runsvc = directory + "/runsvc.sh"
        loaded_exec = fields.get("ExecStart", "")
        if (fields.get("LoadState") != "loaded" or
                fields.get("FragmentPath") != str(unit) or
                fields.get("DropInPaths", "").strip() or
                fields.get("User") != user or
                fields.get("WorkingDirectory") != directory or
                loaded_exec.count("path=") != 1 or
                loaded_exec.count("argv[]=") != 1 or
                "path=" + runsvc not in loaded_exec or
                "argv[]=" + runsvc not in loaded_exec):
            raise AuthorityError("systemd resolved Unit identity mismatch")
        create_unit_attestation(repo, runner_name, directory, service, str(unit))


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except Exception:
        print("CLI lifecycle authority refused; registration may be incomplete", file=sys.stderr)
        sys.exit(1)
