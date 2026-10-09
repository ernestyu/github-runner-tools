# Changelog

All notable changes to this project are documented here.

Changes intended for the next release are collected under **Unreleased**.

## Unreleased

### Added

- Added optional Web Management V1 for repository-level runner management from a browser over Tailscale-only HTTPS.
- Added explicit `setup-web-management.sh --dry-run|--apply`; Web Management remains opt-in and CLI-only operation remains fully supported.
- Added a dedicated non-root `grt-web` frontend, root-owned dispatcher, fixed lifecycle worker, Unix-socket peer authentication, shared CLI/Web mutation locking, CSRF/session/confirmation protections, and request-scoped token transport.
- Added compact responsive runner inventory UI with Create, Remove, and Recover actions; normal configured runners now display a concise user-facing status instead of internal management-state text.
- Added deterministic Web security/integration coverage, including dispatcher peer credentials, UID/GID/capability drop, lock exclusion, token transport/redaction, systemd unit provenance, explicit Web installation, and failure-path tests.

### Changed

- Web registration-token input is now blank by default with a non-secret placeholder.
- Web runner inventory now uses a compact table on larger screens and a responsive row layout on phones.
- Dispatcher worker identity transition now explicitly handles inherited Linux capabilities before dropping to the runner owner.
- Dispatcher systemd installation now provisions only the explicit `CAP_SETUID` / `CAP_SETGID` authority required for the fixed worker identity transition while retaining `NoNewPrivileges=yes` and the existing sandbox.
- Dispatcher startup now fails early with bounded diagnostics when required identity-transition capabilities are missing.
- Web and dispatcher diagnostics remain bounded and avoid logging request bodies, credentials, tokens, cookies, or exception messages containing secrets.

### Validated

- Full repository test suite passes on GitHub-hosted CI and the Debian CI host.
- Web Management has been installed on the real Debian host and validated for Tailscale-only HTTPS access, authentication, persistent systemd startup, dispatcher/worker privilege transition, and listing the existing active/configured runner inventory.
- Existing runner registrations and services remained unchanged during Web Management list/status validation.

### Pending before release

- Complete live Debian create/remove/recover acceptance with a disposable repository runner.
- ARM64 remains implemented but unvalidated on real ARM64 hardware.

## v1.0.1 - 2026-10-05

Maintenance release adding safe local recovery cleanup for runners that were already removed from GitHub.

### Added

- Added `remove-runner.sh --recover-local OWNER/REPO` for safely cleaning verified local service/directory residue after the GitHub-side runner was already removed.
- Added fail-closed recovery identity checks for absent-only `.runner` state, exact owner+repository directory identity, normalized non-truncated service scope, custom runner names, and systemd post-uninstall state.
- Added deterministic recovery-removal tests covering configured/malformed residue, legacy ambiguity, service failure states, truncated identity, and archive preservation.

### Changed

- Documented the difference between normal GitHub unregister/removal and local-only recovery cleanup.

### Validated

- Full `bash tests/run-all.sh` suite passed on the Debian CI host after the recovery implementation.

## v1.0.0 - 2026-10-05

First stable public release. The release covers repository-level self-hosted runner provisioning and management on systemd Linux, plus the local artifact archive workflow validated on a real Debian x86_64 host.

### Added

- Open-source contribution guide in `CONTRIBUTING.md`.
- Security reporting policy in `SECURITY.md`.
- Structured bug report and feature request issue forms.
- Pull request template covering tests, live validation, documentation, and safety impact.
- Issue-template configuration that directs security-sensitive reports to the security policy.
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
