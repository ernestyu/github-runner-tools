#!/usr/bin/env python3
"""Allowlisted journal diagnostics for bounded lifecycle CLI output.

Never persist subprocess output, user strings, argv, tokens, URLs or paths.
Only fixed, explicitly audited diagnostic categories enter systemd journal.
"""
from __future__ import annotations

import os
import sys

# Entire categories are static literals, not substrings copied from output.
SIGNATURES = (
    (b"v2_cli_launcher: restricted launch rejected capability_drop_failed", "launcher_capability_drop_failed"),
    (b"v2_cli_launcher: restricted launch rejected identity_drop_failed", "launcher_identity_drop_failed"),
    (b"v2_cli_launcher: restricted launch rejected fd_missing", "launcher_fd_missing"),
    (b"v2_cli_launcher: restricted launch rejected peer_rejected", "launcher_peer_rejected"),
    (b"v2_cli_launcher: restricted launch rejected runner_identity_invalid", "launcher_runner_identity_invalid"),
    (b"v2_cli_launcher: restricted launch rejected", "launcher_rejected"),
    (b"Local archive platform is not installed", "archive_library_missing"),
    (b"Local archive config is missing", "archive_config_missing"),
    (b"Local archive config is invalid", "archive_config_invalid"),
    (b"Local archive completed hook is missing", "archive_hook_missing"),
    (b"Archive root is not accessible", "archive_root_unavailable"),
    (b"Archive root must not be world-writable", "archive_root_unsafe"),
    (b"Root-owned lifecycle", "lifecycle_authority_rejected"),
    (b"lifecycle checkpoint failed", "lifecycle_checkpoint_failed"),
    (b"Another runner lifecycle operation is already in progress", "lock_conflict"),
    (b"Runner directory requires independent lifecycle review", "existing_runner_directory"),
    (b"Runner registration outcome is uncertain", "remote_registration_uncertain"),
    (b"Runner registration removal failed or remains uncertain", "remote_remove_uncertain"),
    (b"Could not fetch GitHub Actions Runner release metadata", "github_release_metadata_failed"),
    (b"Runner download failed", "github_runner_download_failed"),
    (b"Runner dependencies are missing", "runner_dependencies_missing"),
    (b"Privileged canonical service installation failed", "systemd_install_failed"),
    (b"Privileged runner service start failed", "systemd_start_failed"),
    (b"Post-start service health verification failed", "systemd_health_check_failed"),
    (b"ERROR: This version requires a systemd-based Linux host", "systemd_unavailable"),
    (b"internal Web lifecycle context", "context_invalid"),
    (b"root privileged socket", "privileged_socket_failure"),
)

def classify_output(raw: bytes) -> str:
    if not isinstance(raw, bytes):
        return "invalid_capture"
    for signature, category in SIGNATURES:
        if signature in raw:
            return category
    return "unclassified_cli_failure"

def log_cli_exit(operation: str, status: int | None, output: bytes) -> None:
    """Only fixed vocabulary appears in journal, no external output."""
    if operation not in ("create", "remove"):
        return
    if status is not None and os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0:
        return
    code = str(os.WEXITSTATUS(status)) if status is not None and os.WIFEXITED(status) else "unknown"
    category = classify_output(output)
    print(f"v2_cli_failure op={operation} exit={code} cause={category}",
          file=sys.stderr, flush=True)
