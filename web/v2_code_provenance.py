#!/usr/bin/env python3
"""Root-owned V2 installed executable provenance, checked before root launch.

The root Executor must not launch mutable code from a checked-out Git repo or
accept a symlink in the pinned executable path. These checks complement the
installer's own root-owned mode assertions.
"""
from __future__ import annotations
import os
import stat
from v2_executor_contract import CLI_ARGV

PINNED_ROOT = "/usr/local/lib/github-runner-tools"
TRUSTED_FILES = {
    "/usr/local/lib/github-runner-tools/web/v2_cli_launcher.py",
    "/usr/local/lib/github-runner-tools/web/lifecycle_worker.py",
    "/usr/local/lib/github-runner-tools/web/pty_token_adapter.py",
    "/usr/local/lib/github-runner-tools/cli/register-runner.sh",
    "/usr/local/lib/github-runner-tools/cli/remove-runner.sh",
    "/usr/local/lib/github-runner-tools/cli/status-runners.sh",
}


class CodeProvenanceError(ValueError):
    pass


def verify_fixed_file(path: str) -> None:
    if path not in TRUSTED_FILES:
        raise CodeProvenanceError("unapproved executable")
    parent = "/"
    for part in path.strip("/").split("/")[:-1]:
        parent = os.path.join(parent, part)
        st = os.lstat(parent)
        if (not stat.S_ISDIR(st.st_mode) or st.st_uid != 0 or
                st.st_mode & 0o022):
            raise CodeProvenanceError("untrusted executable parent")
    st = os.lstat(path)
    if (not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or
            st.st_mode & 0o022 or st.st_nlink != 1):
        raise CodeProvenanceError("untrusted executable")
    if path.endswith(".sh") and not st.st_mode & stat.S_IXUSR:
        raise CodeProvenanceError("non-executable pinned CLI")


def verify_launch(op: str) -> None:
    if op not in ("create", "remove", "status"):
        raise CodeProvenanceError("unapproved operation")
    verify_fixed_file(CLI_ARGV[op][0])
    if op in ("create", "remove"):
        verify_fixed_file("/usr/local/lib/github-runner-tools/web/v2_cli_launcher.py")
        verify_fixed_file("/usr/local/lib/github-runner-tools/web/pty_token_adapter.py")
        verify_fixed_file("/usr/local/lib/github-runner-tools/web/lifecycle_worker.py")
