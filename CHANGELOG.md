# Changelog

All notable changes to this project are documented here.

The project has not published its first tagged release yet. Until then, changes are collected under **Unreleased**.

## Unreleased

### Added

- One-line repository-level self-hosted runner bootstrap with `curl | bash`.
- Interactive registration-token input through `/dev/tty`, so token entry does not conflict with piped script input.
- Deterministic owner+repository runner identity, including collision-safe handling for long names.
- x86_64 and ARM64 runner asset selection.
- Optional GitHub Actions Runner version pinning.
- Conservative runner removal with legacy metadata verification and retry-safe systemd cleanup.
- Local artifact archive platform with default root:
  `/srv/github-actions-archive`.
- Root-owned archive configuration and shared `ACTIONS_RUNNER_HOOK_JOB_COMPLETED` hook.
- Automatic completed-hook configuration for newly registered runners.
- Dry-run/apply migration for existing runners.
- Final-workspace local archival with default exclusions for reproducible dependency/cache directories.
- Symlink-safe archival that does not dereference external targets.
- Collision-safe per-job archive directories for matrix/repeated executions.
- Atomic PASS publication using `manifest.json` and `manifest.sha256`.
- Failure manifests with stable `LOCAL_ARTIFACT_*` error codes.
- Disk guard and bounded copy/finalization timeout.
- Timeout-race protection that preserves an already published, hash-valid authoritative PASS archive.
- GitHub Step Summary output from the completed hook.
- Reusable fallback local-artifact summary action.
- Local archive retention cleanup with 90-day default and run-level `.keep` protection.
- Local archive health information in `status-runners.sh`.
- Safe GitHub Actions artifact migration with local verification before optional remote deletion.
- ZIP traversal/symlink protection for migrated GitHub artifacts.
- Full test runner at `tests/run-all.sh`, including syntax, unit, integration, and failure-path coverage.
- English and Chinese documentation for setup, runner registration, archive migration, cleanup, and safety boundaries.

### Changed

- Runner registration now requires the one-time local archive platform setup before new runners are registered.
- `RUNNER_BASE_DIR` is normalized to an absolute path before runner operations.
- Runner registration no longer silently replaces a remote runner with `--replace`.
- Library-only sourcing of `register-runner.sh` no longer installs a caller-visible global EXIT trap.
- Expected-failure tests now execute failure cases in subshells so production `die()/exit` behavior does not terminate the parent test process.
- Core archive test prerequisites are checked centrally; missing dependencies now fail the full suite instead of producing a misleading overall PASS.

### Validated

- Full automated test suite passed on the real Debian CI host.
- One-line self-hosted runner registration was validated against a private GitHub repository.
- Existing runner status and systemd service behavior were validated on the live host.
- Completed-job local artifact archival was exercised successfully with a real self-hosted runner.
- `manifest.json` / `manifest.sha256` publication was validated in the live flow.
- Completed-hook GitHub Step Summary output was validated on the real Debian runner deployment.
- Existing-runner local-archive migration and real workflow execution were exercised successfully.

### Not yet validated

- ARM64 support is implemented but has not yet been validated on a real ARM64 runner host.
