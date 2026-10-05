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
3. `.runner` is completely absent; if any `.runner` filesystem object exists, recovery is not eligible;
4. `.service` exists as a regular readable file and contains exactly one non-empty service name;
5. the service name passes the anchored repository-scope predicate defined in §5.1;
6. required runner service-management files, especially `svc.sh`, exist before service cleanup is attempted.

If repository identity cannot be established from trusted local information, recovery must stop.

No fallback may guess identity from repo-only legacy directory names.

### 5.1 Frozen recovery identity predicate

For this amendment, recovery identity is established only for the new owner+repository runner directory:

```text
RUNNER_DIR =
<canonical RUNNER_BASE_DIR>/actions-runner-<LOCAL_ID>
```

where `LOCAL_ID` is derived from the requested `OWNER/REPO` by the existing `make_local_id` rules.

The second required identity fact is the service name stored in `.service`.

GitHub Actions Runner service names are not defined by simple raw `OWNER/REPO` concatenation in every case. The official runner normalizes the repository/org scope by replacing characters outside:

```text
[0-9a-zA-Z._-]
```

with:

```text
-
```

and may truncate long service names to satisfy the platform service-name length limit.

This amendment intentionally supports only service names whose repository scope remains fully present and can therefore be matched exactly.

For a non-truncated service name, define:

```text
REPO_SCOPE_RAW =
OWNER + "-" + REPO

REPO_SCOPE_NORMALIZED =
replace every character in REPO_SCOPE_RAW
that is not [0-9a-zA-Z._-]
with "-"

SERVICE_NAME =
"actions.runner."
+ REPO_SCOPE_NORMALIZED
+ "."
+ RUNNER_NAME
+ ".service"
```

Repository-scope comparison is ASCII case-insensitive because GitHub repository identity is case-insensitive for this purpose.

The validator must therefore require all of the following:

```text
service name begins exactly with:
actions.runner.<REPO_SCOPE_NORMALIZED>.

service name ends exactly with:
.service

the runner-name segment between those two anchors is non-empty
```

The validator must use anchored structural validation. It must not use ordinary substring matching, unanchored `grep`, repo-name-only matching, or a truncated repository prefix as proof of identity.

The `RUNNER_NAME` segment is intentionally opaque. A runner created with:

```text
--runner-name CUSTOM_NAME
```

is valid recovery residue if the directory identity and the complete normalized repository-scope component match. Recovery must not require the runner-name suffix to equal the default `local-ci-<LOCAL_ID>`.

Examples:

```text
requested:
ernestyu/github-runner-tools

normalized repository scope:
ernestyu-github-runner-tools

valid:
actions.runner.ernestyu-github-runner-tools.local-ci-ernestyu--github-runner-tools.service

valid custom runner name:
actions.runner.ernestyu-github-runner-tools.my-custom-runner.service

invalid repository scope:
actions.runner.someoneelse-github-runner-tools.my-custom-runner.service

invalid unanchored prefix:
foo.actions.runner.ernestyu-github-runner-tools.my-custom-runner.service

invalid empty runner-name segment:
actions.runner.ernestyu-github-runner-tools.service
```

### 5.2 Official service-name truncation boundary

The official Actions Runner may truncate a long service name. In that state, the complete normalized repository-scope component may no longer be present in `.service`.

This amendment does not attempt to reproduce or reverse the official truncation algorithm, including any generated/random suffix behavior.

If the complete normalized repository scope cannot be established exactly from the anchored service name:

```text
identity unknown
→ FAIL CLOSED
→ no service stop
→ no service uninstall
→ no directory deletion
```

An official-style truncated service name is therefore an unsupported/manual administrative recovery case for this amendment.

A truncated prefix must never be treated as sufficient repository identity evidence.

Supporting automatic recovery for truncated service names would require a separate amendment with an additional reliable identity source.

For recovery, the identity proof is therefore the conjunction:

```text
exact canonical new-style runner directory
+
.runner absent
+
complete normalized repository scope proven by .service
+
non-empty runner-name segment
```

If any part cannot be established, identity is unknown and recovery must fail closed before any service mutation or directory deletion.

Legacy repo-only directories are explicitly excluded from this predicate and remain governed by §10.

## 6. `.runner` absence guard

For this amendment, `--recover-local` is eligible only when `.runner` is absent.

The implementation must not interpret or classify any existing `.runner` content.

Required behavior:

```text
.runner absent
→ may continue to directory/service identity validation

.runner exists in any form
→ fail closed
→ no stop
→ no uninstall
→ no directory deletion
```

"Exists in any form" includes at least:

```text
valid configured file
empty file
malformed file
partial file
unreadable file
symlink, including a broken symlink
other unexpected filesystem object at .runner
```

The implementation must not parse such a file and decide that it is "unconfigured residue".

This amendment intentionally supports only the real observed state:

```text
GitHub-side deletion
→ Runner.Listener removes .runner/.credentials
→ .runner is absent
→ local .service + systemd unit + runner directory remain
```

Any future recovery path for an existing but abnormal `.runner` requires a separate SPEC amendment.

This guard prevents `--recover-local` from becoming a token-bypass path for a still-configured or ambiguously configured GitHub runner.

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

### F. Any existing `.runner` fails closed

Deterministic cases must cover all of:

```text
.runner valid/configured
→ FAIL

.runner malformed
→ FAIL

.runner unreadable
→ FAIL

.runner empty
→ FAIL

.runner symlink or broken symlink
→ FAIL

.runner absent
→ only then continue recovery validation
```

For every existing-`.runner` case:

```text
→ no service stop
→ no service uninstall
→ no directory deletion
```

### G. Identity mismatch and custom runner-name handling

```text
requested OWNER/REPO
.service has a different anchored repository scope
→ FAIL
→ no service mutation
→ no deletion
```

Also cover:

```text
.service matches requested repository scope
runner-name suffix is custom/non-default but non-empty
→ identity validation may PASS
→ continue normal recovery checks
```

And:

```text
.service only contains repo name as an unanchored substring
→ FAIL
```

Also cover the official truncation boundary:

```text
official-style truncated .service name
complete normalized repository scope cannot be proven
→ FAIL
→ no service stop
→ no service uninstall
→ no directory deletion
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
5. Recovery is eligible only when `.runner` is completely absent.
6. Any existing `.runner` object, including valid, malformed, empty, unreadable, partial, symlinked, or otherwise abnormal state, fails closed before service mutation.
7. Missing `.runner` alone is not treated as proof of identity.
8. The new owner+repo runner path can be recovered only when the canonical directory matches the requested `LOCAL_ID` and `.service` proves the complete normalized repository scope under §5.1.
9. Repository-scope normalization follows the official allowed-character rule: characters outside `[0-9a-zA-Z._-]` are replaced with `-`.
10. Service-name validation is anchored and case-normalized; ordinary substring matching, repo-name-only matching, and truncated-prefix matching are forbidden.
11. A custom `--runner-name` suffix is allowed and must not be required to equal the default runner name.
12. If official service-name truncation prevents exact repository-scope proof, recovery fails closed and the case remains manual/out of scope.
13. Ambiguous legacy directories are never automatically deleted.
14. Service state must be known before directory deletion.
15. Service uninstall failure blocks deletion unless the service is subsequently confirmed absent.
16. An already-absent service is a valid recovery state.
17. Directory deletion remains constrained to the canonical runner base.
18. Local artifact archives are never deleted by recovery mode.
19. Recovery requires explicit destructive confirmation.
20. Deterministic tests cover cases A–I, including all existing-`.runner` states, custom runner-name validation, normalized repository scope, and truncated-service fail-closed behavior.
21. `bash tests/run-all.sh` passes on a suitable test host.
22. README, Chinese README, and CHANGELOG describe the new recovery path.

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
