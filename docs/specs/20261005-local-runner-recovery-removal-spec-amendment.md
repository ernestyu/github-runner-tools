# Local Runner Recovery Removal — SPEC Amendment

Date: 2026-10-05  
Repository: `ernestyu/github-runner-tools`  
Baseline: `eb5eddfff77830631182a9ef694bc6fd24326970`

Status: SPEC ONLY / NO IMPLEMENTATION

## 1. Background

The current runner removal flow assumes that the GitHub-side runner registration still exists when local cleanup begins.

The normal path is:

```text
GitHub runner exists
→ local .runner metadata exists
→ obtain GitHub removal token
→ stop systemd service
→ uninstall systemd service
→ config.sh remove --token ...
→ delete local runner directory
```

A real recovery case exposed one missing lifecycle state.

For the public repository `ernestyu/github-runner-tools`, the self-hosted runner was intentionally removed from GitHub first. The local Runner.Listener later detected that the runner no longer existed on the server and automatically removed:

```text
.runner
.credentials
```

The listener then exited successfully.

However, the local host still retained:

```text
runner directory
.service metadata
installed systemd unit
```

The existing `scripts/remove-runner.sh` cannot safely complete this cleanup because it currently requires configured runner metadata and a GitHub removal token.

This amendment adds an explicit local recovery path for that state.

## 2. Goal

Add a conservative recovery mode to the existing removal tool:

```bash
bash scripts/remove-runner.sh --recover-local OWNER/REPO
```

This mode exists only for the case where the GitHub-side registration has already been removed or invalidated, while local runner/service residue remains.

The normal removal mode remains unchanged.

## 3. Non-goals

This amendment does not:

- redesign runner registration;
- change normal `remove-runner.sh OWNER/REPO` behavior;
- add remote GitHub API discovery;
- add automatic detection that silently switches into recovery mode;
- add bulk cleanup;
- delete local artifact archives;
- delete unrelated runner workspaces;
- change archive retention;
- change runner naming;
- change repository identity rules;
- change systemd service installation semantics.

Recovery must always be explicit.

## 4. CLI contract

New explicit option:

```bash
bash scripts/remove-runner.sh --recover-local OWNER/REPO
```

The mode must be mutually exclusive with the normal GitHub-unregister flow.

In recovery mode:

- no GitHub removal token is requested;
- `config.sh remove --token ...` is not executed;
- no remote runner replacement or deletion is attempted;
- only verified local residue may be removed.

The script must clearly print that it is operating in local recovery mode.

## 5. Recovery eligibility

Recovery mode must not treat a missing `.runner` file as sufficient proof of repository identity.

The tool must first resolve the expected local runner directory using the existing owner+repository local identity rules.

A candidate recovery directory is eligible only when all of the following are true:

1. the path is exactly the expected runner path under the resolved `RUNNER_BASE_DIR`;
2. the path passes the existing base-directory safety boundary;
3. `.runner` is absent, or the runner is otherwise in an explicitly unconfigured local residue state;
4. `.service` exists and contains a non-empty service name;
5. the service name is structurally consistent with the requested `OWNER/REPO` and the expected local runner identity, using existing naming rules;
6. required runner service-management files, especially `svc.sh`, exist before service cleanup is attempted.

If repository identity cannot be established from trusted local information, recovery must stop.

No fallback may guess identity from repo-only legacy directory names.

## 6. Configured-runner guard

If `.runner` still exists and identifies a configured runner, `--recover-local` must refuse to proceed.

The user must use the normal removal flow instead.

This prevents recovery mode from becoming a token-bypass path for a still-configured GitHub runner.

Required behavior:

```text
.runner exists and configured
→ fail closed
→ tell user to use normal removal
→ do not uninstall service
→ do not delete directory
```

## 7. Systemd cleanup contract

Recovery cleanup order is:

```text
validate local identity
→ determine service state
→ stop service when present/running
→ uninstall service
→ verify service is absent
→ delete local runner directory
```

The existing conservative service-state model must be reused.

Required behavior:

### 7.1 Service already absent

If systemd confirms the service is already absent:

```text
continue
→ delete verified local runner directory
```

### 7.2 Service present

If the service is present:

```text
attempt stop
→ attempt svc.sh uninstall
→ re-check systemd state
```

Directory deletion is allowed only after the service is confirmed absent.

### 7.3 Unknown service state

If the service state is unknown:

```text
stop
→ preserve directory
```

### 7.4 Uninstall failure

If `svc.sh uninstall` returns non-zero:

- re-check systemd state;
- if the unit is confirmed absent, recovery may continue;
- if the unit is still present or state is unknown, recovery must stop;
- the runner directory must remain intact.

## 8. Local directory deletion

Local deletion must preserve the current safety boundary.

Before `rm -rf`, the script must verify that:

- `RUNNER_DIR` is non-empty;
- `RUNNER_DIR` is inside the canonical `RUNNER_BASE_DIR`;
- the basename begins with the expected runner-directory prefix;
- the directory is the verified recovery target for the requested repository.

Only that runner directory may be deleted.

The archive tree under:

```text
/srv/github-actions-archive
```

must not be touched.

## 9. Confirmation behavior

Recovery mode must require an explicit human confirmation before destructive local deletion.

Suggested confirmation text:

```text
Type RECOVER-REMOVE to uninstall the local service residue and delete this runner directory:
```

The exact wording may change, but it must be distinct from normal removal confirmation so the user can see that this is a local-only recovery path.

## 10. Legacy runner handling

This amendment must not weaken existing legacy-runner identity protections.

For legacy repo-only directories:

- directory name alone is insufficient;
- missing `.runner` metadata makes repository identity ambiguous;
- `--recover-local` must not delete an ambiguous legacy directory automatically.

Legacy recovery with insufficient identity evidence remains a manual administrative case and is out of scope for this amendment.

The safety rule remains:

```text
uncertain identity
→ stop
```

## 11. Error behavior

Recovery mode must fail closed.

Stable errors should distinguish at least:

```text
configured runner still present
recovery target not found
identity cannot be verified
.service missing or invalid
service state unknown
service uninstall failed
unexpected local path
```

No failure path may silently continue to `rm -rf`.

## 12. Tests

Add deterministic tests covering at least the following cases.

### A. Unconfigured residue + service present

```text
.runner absent
.service valid
service present
uninstall succeeds
service becomes absent
→ directory deleted
→ PASS
```

### B. Unconfigured residue + service already absent

```text
.runner absent
.service valid
service already absent
→ no remote token requested
→ directory deleted
→ PASS
```

### C. Uninstall returns non-zero but service becomes absent

```text
service present
svc.sh uninstall returns non-zero
post-check = absent
→ continue
→ directory deleted
→ PASS
```

### D. Uninstall failure with service still present

```text
svc.sh uninstall fails
post-check = present
→ FAIL
→ directory preserved
```

### E. Unknown service state

```text
.service missing/empty/unreadable
or systemctl cannot establish state
→ FAIL
→ directory preserved
```

### F. Configured runner guard

```text
.runner exists and is configured
--recover-local used
→ FAIL
→ no service uninstall
→ no deletion
```

### G. Identity mismatch

```text
requested OWNER/REPO
local service metadata does not match expected identity
→ FAIL
→ no deletion
```

### H. Ambiguous legacy directory

```text
legacy repo-only directory
.runner absent
identity cannot be independently verified
→ FAIL
→ no deletion
```

### I. Archive preservation

```text
local runner residue removed
existing /srv/github-actions-archive/OWNER/REPO remains untouched
```

## 13. Documentation changes

After implementation:

- update `README.md`;
- update `README.zh-CN.md`;
- update `CHANGELOG.md`.

Documentation must clearly distinguish:

```text
normal removal
vs
local recovery cleanup
```

Example:

```bash
# Normal removal: GitHub runner still exists
bash scripts/remove-runner.sh OWNER/REPO

# Recovery only: GitHub runner was already removed remotely
bash scripts/remove-runner.sh --recover-local OWNER/REPO
```

## 14. Acceptance criteria

Implementation is complete only when all of the following are true:

1. `--recover-local OWNER/REPO` exists.
2. Recovery mode never requests a GitHub removal token.
3. Recovery mode never calls `config.sh remove --token`.
4. Normal removal behavior remains unchanged.
5. A still-configured runner cannot be removed through recovery mode.
6. Missing `.runner` alone is not treated as proof of identity.
7. The new owner+repo runner path can be recovered only after local identity validation.
8. Ambiguous legacy directories are never automatically deleted.
9. Service state must be known before directory deletion.
10. Service uninstall failure blocks deletion unless the service is subsequently confirmed absent.
11. An already-absent service is a valid recovery state.
12. Directory deletion remains constrained to the canonical runner base.
13. Local artifact archives are never deleted by recovery mode.
14. Recovery requires explicit destructive confirmation.
15. Deterministic tests cover cases A–I.
16. `bash tests/run-all.sh` passes on a suitable test host.
17. README, Chinese README, and CHANGELOG describe the new recovery path.

## 15. Implementation boundary

This is a narrow lifecycle amendment.

Do not add:

- remote runner discovery;
- GitHub API calls for recovery detection;
- auto-cleanup daemons;
- cron jobs;
- bulk runner garbage collection;
- archive cleanup;
- new runner naming schemes;
- new service-management abstractions.

The intended change is limited to:

```text
one explicit CLI mode
+ conservative local identity validation
+ safe systemd cleanup
+ local directory cleanup
+ deterministic tests
+ documentation
```

## 16. Validation sequence

Because the repository's own self-hosted runner has now been removed, implementation validation is split into two stages.

### Stage 1 — deterministic local test suite

On the Debian host:

```bash
cd ~/github-runner-tools
git pull --ff-only
bash tests/run-all.sh
```

This does not require the repository to have an active GitHub self-hosted runner.

### Stage 2 — optional live recovery smoke

Only after code audit and only on a deliberately prepared disposable/test runner residue:

```text
remove GitHub registration first
→ allow local .runner/.credentials cleanup
→ run --recover-local
→ verify service absent
→ verify runner directory removed
→ verify archive preserved
```

Do not use an unrelated production runner as the first live recovery test.

---

End of SPEC amendment.
