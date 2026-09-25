# Local GitHub Actions Artifact Archive Specification

**Date:** 2026-09-25 20:39 (+02:00)  
**Repository:** `ernestyu/github-runner-tools`  
**Status:** Revised draft for audit  
**Implementation state:** SPEC ONLY — no implementation is authorized by this document yet

## 1. Purpose

The current host runs multiple repository-level GitHub self-hosted runners. The existing `github-runner-tools` project already handles runner download, registration, systemd service installation, startup, status, and removal.

Large CI and research jobs currently use `actions/upload-artifact@v4` to upload complete result sets back to GitHub. This is wasteful for this deployment model because the data is produced on the user's own Debian self-hosted runner and can remain on that machine. Uploading multi-hundred-megabyte or multi-gigabyte experiment results back to GitHub consumes GitHub Artifact Storage quota without adding useful durability for the primary local workflow.

The project will therefore be extended into the common management point for local self-hosted CI artifacts.

The target lifecycle is:

```text
ChatGPT / Codex modifies code
        ↓
push GitHub
        ↓
GitHub Actions schedules the job
        ↓
Debian self-hosted runner
        ↓
build / test / experiment
        ↓
job steps finish
        ↓
runner-level completed hook
        ↓
complete workspace archived locally on Debian
        ↓
completed hook attempts final GitHub Summary
        ↓
GitHub keeps run / logs / status

Fallback only if live runner validation proves completed-hook Summary is unreliable:
downstream reusable summary action
```

The user should not need to design an archive path per repository, manually move artifacts, edit each runner's `.env` by hand, or manually correlate GitHub run IDs with local directories.

The first design goal is **artifact completeness**, not minimum disk usage.

## 2. Scope

This specification defines the first local-artifact release for `github-runner-tools`.

It includes:

- one platform-level archive configuration;
- a shared runner job-completed hook;
- automatic hook configuration for newly registered runners;
- one-time migration for existing runners;
- a deterministic local archive directory contract;
- safe workspace copying;
- per-job manifests;
- explicit archive failure semantics;
- completed-hook GitHub Summary as the preferred zero-repository-configuration path, subject to live runner validation;
- a reusable GitHub Action and downstream summary job template as the fallback path if completed-hook Summary is not reliable on the validated runner version;
- migration of existing GitHub artifacts to local storage;
- retention cleanup;
- permanent keep markers;
- disk guard;
- basic archive health information in runner status;
- tests and implementation acceptance criteria.

It does not include a web service or remote artifact server.

## 3. Existing deployment assumptions

The primary tested environment is the same Debian/systemd host used by current repository-level runners.

The current host already has multiple independent runner application directories under one normal Linux service account. Examples may include legacy repo-only names and newer owner+repo names.

The archive design must not hard-code any existing repository name.

The v1 archive model assumes that runners participating in this feature execute under the same normal Linux account on one Debian host and can write to the same local archive filesystem.

Support for multiple runner service accounts sharing the same archive root is not required in v1.

## 4. GitHub runner hook facts that constrain the design

The implementation will use the official self-hosted runner job hook:

```text
ACTIONS_RUNNER_HOOK_JOB_COMPLETED
```

The completed hook runs synchronously after workflow steps have run and before the GitHub job fully completes. A non-zero hook exit is treated as a job failure by the runner design, and hook output appears in the `Complete runner` section of the GitHub Actions log.

The hook itself must live outside the GitHub runner application directory.

The hook has no GitHub-provided timeout. The implementation must therefore provide its own bounded execution time so a broken local copy operation cannot leave a GitHub job indefinitely stuck in `Complete runner`.

The implementation must not assume that workflow-step modifications to `GITHUB_ENV` are available to the completed hook. The hook must derive identity from runner-provided/default GitHub variables and its root-owned platform configuration.

## 5. Platform-level archive configuration

The archive system has one host-level configuration.

The default archive root is:

```text
/srv/github-actions-archive
```

A custom archive root may be supplied during platform setup through:

```text
RUNNER_ARCHIVE_ROOT
```

However, this variable is an **administrator/setup input only**. The job-completed hook must not trust a repository workflow's runtime value of `RUNNER_ARCHIVE_ROOT`.

The resolved configuration must be written to a host-controlled configuration file outside all repository workspaces and runner application directories.

Recommended path:

```text
/etc/github-runner-tools/archive.conf
```

Recommended v1 contents:

```text
ARCHIVE_ROOT=/srv/github-actions-archive
RETENTION_DAYS=90
MIN_FREE_PERCENT=15
COPY_TIMEOUT_SECONDS=3600
```

The configuration file must be owned by root and must not be writable by the runner service account or repository workflows.

The hook may read this file, but repository code must not be able to alter archive destination, retention, disk threshold, or hook path.

## 6. Platform setup contract

A one-time platform setup entry point should be added during implementation, for example:

```text
scripts/setup-local-archive.sh
```

### 6.1 Invocation and privilege model

The setup command must be invoked by the same **normal Linux user that owns and runs the self-hosted runners**.

Example:

```text
invoking user       = actions
runner service user = actions
privileged setup    = explicit sudo operations inside the script
```

The documented/default invocation must **not** be:

```text
sudo scripts/setup-local-archive.sh
```

The script must reject direct root invocation for the normal v1 setup path so it cannot accidentally infer:

```text
runner service user = root
```

Host-level writes that require privilege must be performed through explicit, narrow `sudo` commands from inside the script.

Examples include:

```text
sudo mkdir/install/chown/chmod for /srv/github-actions-archive
sudo install for /usr/local/lib/github-runner-tools/...
sudo install for /etc/github-runner-tools/archive.conf
```

The setup script must preserve the identity of the invoking normal user and use that identity as the runner/archive service account.

Cross-user setup such as:

```text
ernest invokes setup
→ configure archive for actions
```

is out of scope for v1 unless a future specification defines the complete ownership model.

### 6.2 Responsibilities

Its responsibilities are limited to host-level setup:

1. resolve and validate the invoking normal user as the runner service account;
2. create or validate the archive root using explicit privileged operations where required;
3. install the shared completed-hook implementation outside runner application directories;
4. create the root-owned archive configuration;
5. validate required tools;
6. verify that the runner user can create files below the archive root;
7. verify that the runner user can read/execute the shared hook but cannot modify it;
8. verify that the runner user can read the archive configuration but cannot modify it;
9. report resolved paths, identities, permissions, and configuration.

Recommended shared hook installation path:

```text
/usr/local/lib/github-runner-tools/hooks/archive-job-completed.sh
```

Supporting host-side helper code, if needed, should live below:

```text
/usr/local/lib/github-runner-tools/
```

The shared hook must not be installed into an individual `actions-runner-*` directory.

### 6.3 Minimum permission contract

Exact numeric modes may be chosen during implementation, but the following ownership behavior is normative.

`archive.conf`:

```text
owner: root
runner user: readable
runner user: not writable
not world-writable
```

Shared hook and host-side helper code:

```text
owner: root
runner user: readable/executable
runner user: not writable
not world-writable
```

`ARCHIVE_ROOT`:

```text
runner service user: traversable/readable/writable
not world-writable
```

The setup command must prove these effective permissions before reporting success.

The setup command must be idempotent.

It must not silently replace a different administrator-defined completed hook.

## 7. Local archive directory contract

The canonical run archive tree is:

```text
ARCHIVE_ROOT/
└── OWNER/
    └── REPO/
        └── RUN_ID/
            └── attempt_N/
                └── JOB_KEY/
                    ├── manifest.json
                    ├── manifest.sha256
                    └── workspace/
```

Example:

```text
/srv/github-actions-archive/
└── ernestyu/
    └── clawpolymarket/
        └── 36169269077/
            └── attempt_1/
                └── acquire/
                    ├── manifest.json
                    ├── manifest.sha256
                    └── workspace/
```

The directory tree is derived only from trusted GitHub/runner identity variables after strict validation and sanitization.

Repository workflows must never supply an arbitrary archive destination.

### 7.1 Repository identity

The hook must obtain:

```text
GITHUB_REPOSITORY
```

and require it to represent exactly one `OWNER/REPO` identity.

OWNER and REPO must be normalized to deterministic safe path components.

The manifest preserves the original GitHub repository string.

The on-disk path may use lowercase normalized owner/repository components because GitHub repository identity is case-insensitive for this use.

### 7.2 Run identity

`GITHUB_RUN_ID` must be present and numeric.

`GITHUB_RUN_ATTEMPT` must be present and numeric.

The attempt directory is:

```text
attempt_<GITHUB_RUN_ATTEMPT>
```

A different run ID or attempt must never share a final job archive directory.

### 7.3 Job identity and matrix collision handling

`GITHUB_JOB` is a logical job identifier. It is not a stable per-execution identifier and is not sufficient to distinguish matrix legs or repeated executions.

For example, two legitimate executions can have all of the following equal:

```text
repository
run ID
run attempt
GITHUB_JOB
GITHUB_SHA
RUNNER_NAME
```

while still representing two different matrix executions.

Therefore v1 must never infer "same exact execution" from those fields alone.

The final component is named `JOB_KEY`, not simply `JOB_NAME`.

For the first occurrence where no target collision exists:

```text
JOB_KEY = sanitized GITHUB_JOB
```

If the run/attempt already contains an archive for that logical `GITHUB_JOB`, and the runner environment does not provide a separately validated stable per-job-execution identifier, the hook must assume the new invocation may be a different legitimate execution.

It must allocate a new collision-safe `JOB_KEY` and must never overwrite the existing PASS archive.

Collision-safe form:

```text
<safe-job>__<safe-runner-name>__<execution-suffix>
```

The execution suffix must be generated locally and collision-resistant within the run attempt. A UTC timestamp plus random suffix, or an equivalent local nonce, is acceptable.

The manifest must always record:

```text
original GITHUB_JOB
final JOB_KEY
any validated stable job-execution identifier, if GitHub/runner later provides one
```

If a future runner version exposes a stable per-job-execution identifier, implementation may use it only after the identifier has been documented and validated on the actual runner version.

The v1 safety ordering is:

```text
no overwrite
>
perfect deduplication
```

Rare duplicate archives caused by a hook retry are acceptable when the implementation cannot prove that the retry is the same exact execution.

Two legitimate executions with the same run, attempt, `GITHUB_JOB`, runner, and SHA must still produce independent archives.

## 8. Required hook environment

The completed hook must obtain and validate at least:

```text
GITHUB_REPOSITORY
GITHUB_RUN_ID
GITHUB_RUN_ATTEMPT
GITHUB_JOB
GITHUB_SHA
GITHUB_REF
GITHUB_WORKFLOW
GITHUB_WORKSPACE
RUNNER_NAME
```

The hook may also record other GitHub default variables when useful, but the fields above define the minimum identity contract.

If any variable required to construct repository/run/job identity is missing or invalid, the archive operation fails.

No repository workflow variable may override the archive root or final archive path.

## 9. Workspace source contract

The default archive payload is the job's final:

```text
GITHUB_WORKSPACE
```

The hook must validate that:

- `GITHUB_WORKSPACE` is an absolute path;
- it exists;
- it is a directory;
- it is readable by the runner user.

An existing but empty workspace is valid and produces a zero-file archive.

A missing or unreadable workspace is an archive failure.

The hook must never accept a repository-provided alternate source path in v1.

## 10. Default archive inclusion and exclusion policy

The first goal is artifact completeness.

The complete workspace is copied by default except for directories that are normally reproducible dependencies, source-control metadata, or caches.

Default exclusions:

```text
.git/
node_modules/
.venv/
venv/
__pycache__/
.pytest_cache/
```

The exclusion rule applies to matching directory names below the workspace.

The default policy must **not** exclude:

```text
data/
results/
reports/
artifacts/
output/
checkpoints/
JSON / JSONL results
model outputs
experiment evidence
generated reports
```

The default exclusions must be implemented centrally in `github-runner-tools`, not separately in each repository.

Repository-specific custom exclude rules are out of scope for v1 unless introduced later through an audited administrator-side configuration.

## 11. Symlink policy

The archive process must not dereference workspace symlinks.

A symlink inside the workspace may be copied as a symlink object, but the archive process must never follow that link and copy data from outside the workspace.

In rsync terms, the implementation must preserve symlinks as symlinks and must not use link-following behavior such as `-L` / `--copy-links`.

The hook must not use any option that follows directory symlinks outside the workspace.

The manifest should record the number of symlinks when this can be obtained without a second expensive full-tree scan.

A symlink that points to `/etc`, `/home/actions`, another repository, or another host path must never cause that target's contents to enter the archive.

## 12. Copy mechanism and atomicity

The preferred v1 copy mechanism is local `rsync`.

The copy must occur into a staging path under the exact final run/attempt/job parent.

Example:

```text
JOB_KEY/
├── .workspace.tmp.<nonce>/
├── .manifest.tmp.<nonce>
└── ...
```

A PASS `manifest.json` must not exist until the workspace copy has completed successfully.

On successful copy:

1. the staged workspace is moved into its final `workspace/` name;
2. file count and total bytes are measured from the archived copy;
3. the final manifest is written atomically;
4. `manifest.sha256` is written;
5. temporary files are removed.

A completed PASS archive is immutable by default.

The hook must never overwrite a PASS archive that belongs to another execution.

## 13. Retry and idempotency rules

The completed hook may be re-entered after a runner/service retry or administrator recovery, but v1 must not deduplicate executions using weak identity assumptions.

In particular, the following fields are **not sufficient** to prove that two hook invocations are the same exact execution:

```text
repository
run ID
run attempt
GITHUB_JOB
GITHUB_SHA
RUNNER_NAME
```

Those fields can be identical for two legitimate matrix or repeated executions.

Therefore:

- an existing PASS archive is never overwritten merely because those fields match;
- if no separately validated stable per-job-execution identifier is available, a colliding invocation receives a new collision-safe `JOB_KEY`;
- the system may retain a small number of duplicate archives rather than risk merging or overwriting two legitimate executions;
- stale staging data may only be removed when the implementation can prove it belongs to the exact staging namespace created by the same invocation or an explicitly recoverable local transaction;
- ambiguous existing data is preserved and causes allocation of a new safe target or a fail-safe stop, never destructive reuse.

Idempotent success against an existing PASS archive is allowed only when the implementation has a validated stable execution identifier that proves the invocation is the same exact GitHub job execution.

If no such identifier exists in the validated runner version, v1 does not claim exact hook-level deduplication.

Different repository, run ID, attempt, or job execution data must never be overwritten.

## 14. Manifest contract

Every successful archived job must contain:

```text
manifest.json
```

The schema identifier for v1 is:

```text
github-runner-tools/local-artifact-manifest/v1
```

Minimum fields:

```json
{
  "schema": "github-runner-tools/local-artifact-manifest/v1",
  "archive_status": "PASS",
  "repository": "ernestyu/clawpolymarket",
  "owner_path": "ernestyu",
  "repo_path": "clawpolymarket",
  "run_id": "36169269077",
  "run_attempt": "1",
  "job": "acquire",
  "job_key": "acquire",
  "workflow": "Historical acquisition",
  "git_sha": "4808a7a5...",
  "git_ref": "refs/heads/main",
  "runner_name": "local-ci-clawpolymarket",
  "workspace_source": "/home/actions/.../_work/...",
  "completed_at_utc": "2026-09-25T18:00:00Z",
  "archive_path": "/srv/github-actions-archive/ernestyu/clawpolymarket/36169269077/attempt_1/acquire",
  "archive_uri": "archive://ernestyu/clawpolymarket/36169269077/attempt_1/acquire",
  "file_count": 540,
  "total_bytes": 123456789
}
```

The manifest must not contain secrets.

The manifest may additionally record:

```text
hook_version
runner_arch
symlink_count
exclude_policy_version
copy_duration_seconds
disk_free_percent_before
disk_free_bytes_before
error/warning counters
```

### 14.1 Manifest hash

The manifest cannot meaningfully contain its own SHA-256.

Therefore v1 writes:

```text
manifest.sha256
```

as a sidecar containing the SHA-256 of the finalized `manifest.json`.

Full SHA-256 hashing of every multi-gigabyte archived file is explicitly out of scope for v1.

## 15. Failure manifest and partial-state contract

A PASS manifest is authoritative and may only be written after archive completion.

On failure, the hook should write a failure record when the archive filesystem is still writable.

Recommended filename:

```text
manifest.failed.json
```

It should contain the same execution identity plus:

```text
archive_status = FAILED
failure_code
failure_message
failed_at_utc
```

Examples of stable failure codes:

```text
LOCAL_ARTIFACT_ARCHIVE_FAILED
LOCAL_ARTIFACT_DISK_GUARD_FAILED
LOCAL_ARTIFACT_ARCHIVE_TIMEOUT
LOCAL_ARTIFACT_IDENTITY_INVALID
LOCAL_ARTIFACT_WORKSPACE_INVALID
LOCAL_ARTIFACT_PATH_CONFLICT
```

A failure record must never be confused with a successful archive.

If even the failure record cannot be written, the hook must still emit the stable failure marker to stderr/GitHub logs and return non-zero.

## 16. Archive failure semantics

Archive failure must not be silent.

If the business/test steps succeed but local archival fails, the completed hook must:

1. emit a GitHub error annotation where supported;
2. print a line containing the stable marker:
   `LOCAL_ARTIFACT_ARCHIVE_FAILED` or a more specific stable code;
3. return non-zero.

The expected externally visible result is that the GitHub job fails in the `Complete runner` stage.

This behavior must be verified against the actual runner version during Debian integration testing before implementation is declared complete.

The project must not claim that a run is safely archived when only the business job succeeded.

## 17. Hook execution timeout

GitHub does not provide a timeout for runner pre/post-job hooks.

Therefore v1 must wrap the copy/finalization operation in a finite administrator-controlled timeout.

Default:

```text
COPY_TIMEOUT_SECONDS=3600
```

If the timeout is exceeded:

- the hook fails;
- it emits `LOCAL_ARTIFACT_ARCHIVE_TIMEOUT`;
- it leaves no PASS manifest;
- it preserves enough failure/staging evidence for diagnosis when safe;
- it does not automatically delete an existing successful archive.

The timeout is host configuration, not repository-controlled workflow input.

## 18. Disk guard

Before staging the workspace copy, the hook must inspect the filesystem containing the resolved archive root.

Default minimum free percentage:

```text
15
```

Administrator configuration:

```text
MIN_FREE_PERCENT=15
```

The implementation must calculate free percentage from the archive filesystem, not from the runner workspace filesystem.

Behavior:

```text
free percentage >= threshold
→ archive may proceed

free percentage < threshold
→ do not begin the copy
→ emit LOCAL_ARTIFACT_DISK_GUARD_FAILED
→ fail the completed hook
```

Disk guard v1 only performs:

```text
detect
refuse
report
```

It must never automatically delete older archives to make room.

If the filesystem becomes full during a copy despite passing the preflight guard, the copy is an ordinary archive failure and no PASS manifest is written.

## 19. New-runner integration

`scripts/register-runner.sh` must eventually integrate this feature after the specification is frozen.

The desired registration lifecycle is:

```text
download runner
→ configure repository runner
→ validate local archive platform
→ configure ACTIONS_RUNNER_HOOK_JOB_COMPLETED
→ install systemd service
→ start runner
→ verify runner and archive hook configuration
```

Archive support is enabled by default for runners registered by the updated tool.

Before final runner startup, registration must verify:

- the platform archive configuration exists;
- the shared hook exists and is executable;
- the archive root exists;
- the archive root is writable by the runner user;
- configured disk threshold is valid;
- the runner `.env` does not contain a conflicting completed-hook path.

If archive support is enabled and any of these checks fail, registration must fail before presenting the runner as ready.

### 19.1 Runner `.env` editing

The registration tool must edit the runner `.env` file idempotently.

It must preserve unrelated existing lines.

Required value:

```text
ACTIONS_RUNNER_HOOK_JOB_COMPLETED=/usr/local/lib/github-runner-tools/hooks/archive-job-completed.sh
```

If the key already exists with the exact expected value, that is success.

If the key exists with a different value, registration must stop and report a hook conflict.

V1 must not silently replace or chain an unknown administrator hook.

## 20. Existing-runner migration

The implementation must provide a one-time migration command, for example:

```text
scripts/enable-local-archive.sh
```

Its responsibility is to enable the same shared archive hook for existing runner instances.

It must:

1. discover candidate `actions-runner-*` directories under the current managed runner base;
2. require valid runner metadata such as `.runner`;
3. establish repository identity from metadata, not directory name;
4. verify the runner is owned/managed by the current service account;
5. verify the shared platform hook and archive root;
6. update `.env` idempotently;
7. restart the exact runner systemd service;
8. confirm the service is active;
9. report success/failure per runner.

It must not modify a directory only because its name begins with `actions-runner-`.

It must not silently overwrite a conflicting completed-hook setting.

It must support:

```text
--dry-run
--apply
```

Default behavior should be `--dry-run` unless the user explicitly requests `--apply`.

## 21. GitHub Summary capability and timing contract

The completed hook runs after all normal workflow steps and before the GitHub job fully completes.

A normal workflow step inside the artifact-producing job cannot know final archive status before the completed hook has finished. Therefore a same-job **workflow step** must not pre-declare:

```text
Archive status: PASS
final file count
final archive size
```

However, GitHub runner hook documentation indicates that hooks can use workflow commands and environment files. Therefore this specification must not assume in advance that the completed hook is unable to write the final GitHub Job Summary.

The V1 summary path is selected only after a live capability test on the actual pinned/current runner version used by the Debian host.

### 21.1 Primary path: completed-hook Summary

The primary zero-repository-configuration design is:

```text
business workflow steps
→ ACTIONS_RUNNER_HOOK_JOB_COMPLETED
→ archive workspace
→ finalize PASS manifest
→ completed hook writes final Summary through $GITHUB_STEP_SUMMARY
→ job completes
```

The hook may write a PASS summary only after the local archive and PASS manifest are finalized.

If archival fails:

```text
no PASS Summary
→ hook may write Archive status: FAILED when summary output remains available
→ emit stable LOCAL_ARTIFACT_* failure marker
→ return non-zero
```

This primary path is preferred because it gives new repositories local archive + summary automatically after runner registration, without repository workflow changes.

### 21.2 Required live capability test

Before choosing the V1 summary implementation, Debian integration validation must explicitly test whether the actual runner version allows `ACTIONS_RUNNER_HOOK_JOB_COMPLETED` to append reliably to:

```text
$GITHUB_STEP_SUMMARY
```

and whether the resulting Summary appears in the GitHub UI after job completion.

The test must cover both:

```text
archive PASS
archive FAILED
```

The project must not claim zero-configuration Summary until this capability has passed live validation.

### 21.3 Fallback path

Only if completed-hook Summary cannot be made reliable on the validated runner version does V1 use:

```text
reusable local-artifact-summary action
+
explicit downstream summary job
```

This fallback requires repository workflow configuration and is therefore secondary.

The fallback must not be selected merely because it is easier to implement.

## 22. Reusable local artifact summary action

The project should still provide a reusable GitHub Action such as:

```text
.github/actions/local-artifact-summary/action.yml
```

Its role depends on the live capability decision:

```text
completed-hook Summary validated
→ reusable action remains an optional/reusable capability

completed-hook Summary not reliable
→ reusable action becomes the V1 fallback summary mechanism
```

Fallback workflow shape:

```yaml
artifact-summary:
  if: always()
  needs: [test, experiment]
  runs-on: [self-hosted, Linux, X64, <repo-label>]
  steps:
    - name: Publish local artifact summary
      uses: ernestyu/github-runner-tools/.github/actions/local-artifact-summary@VERSION
```

Because all `needs` jobs have completed before this downstream job starts, their completed hooks have already finalized their manifests.

The fallback action reads manifests for:

```text
GITHUB_REPOSITORY
GITHUB_RUN_ID
GITHUB_RUN_ATTEMPT
```

from the local archive root and writes to:

```text
$GITHUB_STEP_SUMMARY
```

The fallback summary job must run on a self-hosted runner with access to the same local archive filesystem. Running it on `ubuntu-latest` cannot provide local archive status and must fail clearly.

The reusable action never uploads the local payload.

## 23. Summary output contract

The summary content contract is the same whether written directly by the completed hook or by the fallback reusable action.

A summary should be concise.

Example:

```text
## Local CI Artifact

Repository: ernestyu/clawpolymarket
Run ID: 36169269077
Attempt: 1
Commit: 4808a7a5...
Archive status: PASS

Jobs archived: 2
Files: 540
Size: 389.9 MB

Local archive:
archive://ernestyu/clawpolymarket/36169269077/
```

For a completed-hook job-level summary, the content may naturally describe that job's archive rather than aggregate all jobs in the run.

For the downstream fallback, the action may aggregate finalized manifests for the run/attempt.

Neither path may report PASS before the relevant PASS manifest or manifests exist.

If archival fails, the Summary must not report PASS.

`archive://...` is a stable logical identifier only. V1 does not need to make it clickable or remotely accessible.

The Summary must not display:

```text
registration tokens
PATs
GitHub tokens
API keys
passwords
secret values
private webhook URLs
```

## 24. Recommended workflow template

The recommended template must follow the summary path selected by live runner validation.

If completed-hook Summary is validated, the normal V1 template needs no summary step or summary job:

```text
repository workflow
→ normal self-hosted build/test/experiment jobs
→ runner completed hook automatically archives and writes Summary
```

That is the preferred zero-repository-configuration behavior.

If completed-hook Summary is not reliable, the template may include the downstream reusable summary job described in Section 22.

In either case, the template should demonstrate:

- repository-specific self-hosted runner labels;
- normal build/test/experiment jobs;
- no full-workspace `actions/upload-artifact` by default;
- optional compact GitHub audit artifact where needed.

This template is guidance, not an automatic repository mutation.

`register-runner.sh` must not silently rewrite an existing repository workflow.

## 25. GitHub Artifact strategy

This project does not reimplement the GitHub Artifact API.

The intended split is:

```text
complete result set
→ Debian local archive

GitHub artifact
→ optional small audit/evidence package
```

Existing repository workflows containing:

```yaml
actions/upload-artifact
```

must not be automatically deleted or rewritten by runner registration.

Existing repositories are migrated deliberately.

New recommended templates should not upload the entire workspace to GitHub.

The project may later include a compact audit-package helper, but that helper must remain independent from the local workspace archive.

## 26. Existing GitHub artifact migration

The implementation should provide:

```text
scripts/migrate-github-artifacts.sh
```

Purpose:

```text
existing GitHub Artifact
→ download to Debian
→ verify local copy
→ record migration metadata
→ optionally delete remote artifact only when explicitly requested
```

Required repository argument:

```text
OWNER/REPO
```

Required modes:

```text
--download-only
--verify
--delete-after-verified
```

Safe default when no delete flag is present:

```text
download + verify
do not delete GitHub artifact
```

`--delete-after-verified` must be explicit.

It must be impossible for a failed or incomplete verification to proceed to remote deletion.

## 27. GitHub migration authentication

The migration tool runs outside the repository workflow and therefore requires explicit GitHub authentication.

V1 should use an already authenticated GitHub CLI session or an explicitly supplied GitHub token through the standard GitHub CLI environment.

The tool must not store the token in the archive manifest, repository, or shell-generated config.

Read/download mode requires permission to read Actions artifacts.

Remote deletion requires permission to delete Actions artifacts.

If the credentials do not have deletion permission, `--delete-after-verified` must fail without deleting local data.

## 28. Migrated artifact directory contract

Existing GitHub artifacts must still be grouped below the same repository/run root.

Recommended structure:

```text
ARCHIVE_ROOT/
└── OWNER/
    └── REPO/
        └── RUN_ID/
            └── github_artifacts/
                └── artifact_<ARTIFACT_ID>--<SAFE_NAME>/
                    ├── artifact-manifest.json
                    └── payload/
```

The migration tool must preserve at least:

```text
artifact_id
artifact_name
repository
run_id
original_size
created_at
downloaded_at_utc
verification_status
local_payload_path
remote_deleted
```

If GitHub exposes a trustworthy artifact digest, it should also be preserved and verified.

If no digest is available, v1 verification must at least confirm successful complete download/extraction and compare available metadata such as expected size where the API representation permits reliable comparison.

The tool must never invent a digest.

## 29. Remote deletion ordering for migrated artifacts

The order for `--delete-after-verified` is:

```text
download
→ local verification PASS
→ finalized artifact-manifest.json
→ remote delete request
→ verify remote artifact deletion response
→ update local manifest remote_deleted=true
```

If remote deletion fails, the local copy remains.

The migration tool must never delete the local copy because remote deletion failed.

## 30. Manual deletion model

One goal of the local layout is simple operator recovery.

A complete GitHub run is grouped below:

```text
ARCHIVE_ROOT/OWNER/REPO/RUN_ID/
```

Therefore manual deletion of a known run can be performed by the administrator with a normal filesystem deletion.

Deleting an entire repository history similarly means deleting:

```text
ARCHIVE_ROOT/OWNER/REPO/
```

The project does not need a web UI to perform these operations.

The documentation must still warn users to verify the path before manual `rm -rf`.

## 31. Retention contract

Default retention:

```text
90 days
```

Configuration:

```text
RETENTION_DAYS=90
```

Retention cleanup must be implemented outside the completed hook.

Recommended maintenance command:

```text
scripts/cleanup-local-artifacts.sh
```

Supported modes:

```text
--dry-run
--apply
```

Default mode is `--dry-run`.

V1 does not automatically install or enable a periodic timer.

A future systemd timer may be added only after cleanup behavior has been independently validated.

## 32. Run-level keep contract

A run is permanently protected from automatic retention cleanup when this file exists:

```text
ARCHIVE_ROOT/OWNER/REPO/RUN_ID/.keep
```

The existence of `.keep` is sufficient.

Its file contents are optional and may contain a human note.

Cleanup must not delete or rewrite a kept run.

The archive hook must not remove a `.keep` file.

Migrating additional data into an already-kept run must preserve the marker.

## 33. Cleanup eligibility

Cleanup may only remove a whole run directory.

It must not delete individual job workspaces inside a run as its normal retention action.

A run becomes eligible only when:

- no `.keep` exists;
- its newest successful archive/migration completion timestamp is older than the retention cutoff;
- no active staging/lock marker exists;
- there is at least one finalized local manifest;
- no manifest/staging state marks the run as currently incomplete or in-progress.

If completeness is ambiguous, cleanup skips the run.

This intentionally favors leaving stale data behind over deleting evidence that might still be needed.

The cleanup tool must print the reason a run is skipped in verbose/dry-run output.

## 34. Retention age source

For completed-hook archives, age is based on:

```text
manifest.completed_at_utc
```

For migrated GitHub artifacts, age may use the finalized migration completion timestamp for local-retention purposes, while still preserving the original GitHub artifact creation time in metadata.

When a run contains multiple manifests, cleanup uses the newest relevant local completion timestamp.

Filesystem mtime must not be the primary retention authority when valid manifests are available.

## 35. Archive health in status-runners

`status-runners.sh` should receive a minimal archive-health extension.

It should display:

```text
archive root
archive configuration path
archive root exists
archive root writable by current runner user
filesystem total bytes
filesystem used bytes
filesystem free bytes
free percentage
configured retention days
configured disk guard threshold
configured hook path
```

It does not need:

```text
web monitoring
Prometheus
database
alerting daemon
historical graphs
```

This is a local health/status view only.

## 36. Path sanitization and traversal defense

The archive hook treats GitHub job metadata as untrusted path material even though the variables originate from the runner.

Only validated/sanitized components may be joined beneath `ARCHIVE_ROOT`.

The hook must reject:

- empty owner/repo/job components;
- `.` or `..`;
- path separators inside a component;
- control characters;
- NUL-equivalent/invalid shell data;
- any normalized destination escaping `ARCHIVE_ROOT`.

After constructing a candidate archive target, the implementation must verify that its canonical parent remains beneath the canonical archive root.

The workflow must not be able to set a destination such as:

```text
../../..
/etc
/home/actions
another repository's archive
```

## 37. Archive root trust boundary

Repository workflows are untrusted relative to host administration.

The following are host-admin configuration and must never be taken from job-level workflow `env`:

```text
archive root
retention days
disk threshold
hook executable path
cleanup apply mode
GitHub artifact migration credentials
```

The hook may trust only the root-owned archive config for those values.

A workflow setting a variable named `RUNNER_ARCHIVE_ROOT` must have no effect on the hook destination.

## 38. Locking and concurrent jobs

Multiple self-hosted runners on the same Debian host may complete jobs at the same time.

Archive writes must therefore be concurrency-safe.

V1 must use a lock scoped narrowly enough that unrelated repositories/jobs can archive in parallel, while the same exact final job target cannot be finalized concurrently.

Acceptable lock granularity:

```text
repository/run/attempt/job-key
```

A global archive-root lock is discouraged because it would serialize unrelated large copies.

Lock files must be outside the finalized workspace payload and must not be mistaken for archive evidence.

Stale-lock recovery must be conservative.

## 39. Permissions

The archive root must not be world-writable.

The runner service account must be able to:

- create repository/run/job directories;
- create staging files;
- finalize manifests;
- read existing manifests for idempotency.

Repository workflow code runs as the same runner service account, so filesystem permissions alone cannot prevent malicious workflow code from altering local archives.

V1 therefore treats the archive store as protection against quota loss and accidental workflow behavior, **not** as a cryptographically tamper-proof evidence store against a malicious repository workflow with arbitrary shell access on the runner.

This limitation must be documented.

A stronger immutable/tamper-resistant evidence store is out of scope.

## 40. Secrets and logging

The hook and maintenance tools must not print or archive known secret-bearing runner control files merely for diagnostics.

GitHub/runner logs must not print:

```text
tokens
PAT values
GitHub credentials
secret environment values
webhook URLs
private keys
```

The workspace itself may contain files created by repository code. V1 archives the workspace for completeness and does not attempt general secret scanning.

Users must therefore continue to avoid writing production secrets into CI workspaces.

## 41. No automatic GitHub workflow mutation

Runner registration, platform setup, migration, cleanup, and status tools must not silently edit repository workflow YAML.

The completed-hook Summary requires no repository workflow mutation when the primary path is validated. The reusable summary action and recommended template are offered as fallback/reusable capabilities.

Existing repositories migrate their `actions/upload-artifact` usage deliberately.

This rule prevents a runner-management operation from unexpectedly changing repository semantics.

## 42. V1 non-goals

The first release explicitly excludes:

```text
Web UI
MinIO
S3 compatibility
Artifact REST API
user accounts
database
full-text search
browser file manager
remote artifact download server
automatic cloud backup
Docker/Kubernetes artifact service
organization-level runner fleet
complex automatic disk reclamation
automatic repository workflow rewriting
tamper-proof/WORM evidence storage
cross-host distributed archive index
```

The archive store is a local filesystem feature.

## 43. Expected user experience

After one host-level setup, a future repository such as:

```text
ernestyu/gorter
```

should require only the normal `github-runner-tools` runner registration flow.

The new runner automatically receives the shared completed hook.

After that:

```text
code change
→ push
→ GitHub Actions
→ self-hosted runner
→ build/test/experiment
→ completed hook
→ complete local archive
→ completed hook writes GitHub Summary when live validation supports it
→ GitHub logs/status remain

Fallback only if completed-hook Summary is unreliable:
→ downstream reusable action writes summary
```

The user should not need to:

```text
create a per-project archive directory
edit runner .env manually
copy artifacts manually
invent per-project archive path rules
map a GitHub run to a local folder by hand
upload multi-GB complete workspaces to GitHub
manually inspect disk before every job
```

## 44. Test strategy

Implementation must include automated tests before Debian live migration.

Tests should use temporary directories and command mocks where possible.

### 44.1 Pure/unit tests

Cover:

- repository identity validation;
- safe component sanitization;
- path traversal rejection;
- run/attempt numeric validation;
- job-key normal path;
- job-key collision path;
- archive URI generation;
- manifest schema generation;
- retention date calculation;
- `.keep` detection;
- disk percentage calculation;
- config validation.

### 44.2 Archive hook integration tests

Cover:

- normal workspace copy;
- empty workspace;
- required default exclusions;
- result/data directories retained;
- symlink preserved without dereference;
- external symlink target contents not copied;
- missing workspace failure;
- read failure;
- low-disk guard failure;
- rsync/copy failure;
- timeout failure;
- manifest written only after successful copy;
- failed manifest behavior;
- idempotent retry;
- existing conflicting target;
- concurrent different jobs;
- same logical job collision;
- same run + attempt + GITHUB_JOB + runner + SHA across two legitimate executions produces two independent archives with no overwrite;
- retry/collision behavior never assumes those weak fields prove exact execution identity;
- archive root runtime workflow-env override ignored.

### 44.3 Runner integration tests

Cover:

- new runner gets expected completed hook;
- unrelated `.env` entries survive registration;
- exact existing hook value is idempotent;
- conflicting existing hook causes safe failure;
- runner service restart sees hook;
- completed hook appears in GitHub `Complete runner`;
- completed-hook access to `$GITHUB_STEP_SUMMARY` is tested on the actual runner version;
- completed-hook PASS summary is visible in GitHub only after archive finalization, if supported;
- completed-hook FAILED summary behavior is tested, if supported;
- archive failure causes GitHub job failure;
- business-step failure still runs archive hook and archives final workspace.

### 44.4 Migration tests

Cover:

- only valid runner directories modified;
- invalid `actions-runner-*` directory ignored;
- metadata identity required;
- dry-run makes no changes;
- apply updates `.env`;
- exact systemd service restarted;
- service failure reported per runner;
- conflicting hook not overwritten.

### 44.5 Cleanup tests

Cover:

- dry-run default;
- apply deletes only eligible whole run directories;
- `.keep` always wins;
- recent run retained;
- stale but incomplete/ambiguous run retained;
- active staging/lock retained;
- newest manifest timestamp controls run age;
- no path can escape archive root.

### 44.6 GitHub artifact migration tests

Cover:

- download-only;
- verify success;
- verify failure;
- delete-after-verified requires verification PASS;
- remote delete failure preserves local copy;
- metadata manifest records artifact ID/name/size/time/run;
- wrong repository identity rejected;
- credentials absent/insufficient produces no destructive action.

## 45. Debian acceptance sequence

After automated tests pass, the implementation should be validated on the actual Debian CI host in this order:

1. run platform setup as the normal runner owner and confirm privileged operations happen through internal `sudo`, not root invocation;
2. confirm the runner user can write the archive root but cannot modify the root-owned config or shared hook;
3. activate local archive platform configuration;
4. register a fresh test repository runner and confirm hook configuration;
5. run a small successful job and inspect local archive + manifest;
6. during that job, test completed-hook `$GITHUB_STEP_SUMMARY` output and determine whether it appears reliably in the GitHub UI;
7. run a failing business job and confirm workspace still archives;
8. intentionally trigger an archive failure, confirm GitHub fails in `Complete runner`, and test FAILED Summary behavior;
9. freeze the V1 Summary path decision:
   - completed-hook Summary if live PASS/FAILED tests are reliable;
   - downstream reusable action fallback only otherwise;
10. test two legitimate executions with the same run, attempt, `GITHUB_JOB`, runner, and SHA and confirm independent archives with no overwrite;
11. test a workspace containing an external symlink;
12. test a run that produces a large result file;
13. migrate one existing runner;
14. verify existing runner still accepts jobs;
15. verify `status-runners` archive-health output;
16. if fallback Summary is selected, run the downstream summary job and confirm it matches finalized manifests;
17. run cleanup dry-run;
18. create `.keep`, confirm cleanup skips it;
19. migrate one existing GitHub Artifact without deleting remote;
20. verify local migrated manifest;
21. test remote deletion only on a disposable verified artifact.

No bulk migration or bulk remote deletion should happen before this sequence is audited.

## 46. Acceptance criteria

The implementation is complete only when all applicable criteria below are satisfied.

1. Default archive root is `/srv/github-actions-archive`.
2. Archive root configuration is host-controlled and cannot be overridden by repository workflow environment.
3. Shared hook is installed outside all runner application directories.
4. Shared hook path is absolute.
5. New runners receive `ACTIONS_RUNNER_HOOK_JOB_COMPLETED` automatically.
6. Registration preserves unrelated runner `.env` entries.
7. Registration refuses a conflicting existing completed hook.
8. Registration fails if archive is enabled but root/hook/config is unusable.
9. Platform setup is invoked by the normal runner owner, not by running the whole setup script as root.
10. Platform setup preserves the invoking normal user as runner service identity and uses explicit internal `sudo` only for privileged host writes.
11. The runner user can write the archive root after setup.
12. The runner user can read/execute but cannot modify the root-owned shared hook.
13. The runner user can read but cannot modify the root-owned archive config.
14. Existing runner migration requires valid runner metadata.
15. Existing runner migration supports dry-run and explicit apply.
16. Migration restarts and verifies the exact runner service.
17. Archive path contains owner, repository, run ID, attempt, and job key.
18. Repository/run/attempt/job path components are sanitized.
19. Path traversal outside archive root is impossible.
20. No repository name is hard-coded.
21. Empty workspace archives successfully.
22. Missing/unreadable workspace fails archival.
23. Default exclusions include `.git`, `node_modules`, virtualenvs, Python bytecode cache, and pytest cache.
24. Data/results/reports/artifacts/output/checkpoints are not excluded by default.
25. Symlinks are never dereferenced during workspace archival.
26. External symlink target contents never enter the archive.
27. PASS manifest is written only after copy completion.
28. PASS manifest uses the v1 schema identifier.
29. Manifest contains repository, run, attempt, job, workflow, SHA, ref, runner, timestamp, path, file count, and bytes.
30. Manifest contains a stable logical `archive://` URI.
31. `manifest.sha256` validates finalized `manifest.json`.
32. Full payload SHA-256 hashing is not required in v1.
33. Different repositories can never share a final archive directory.
34. Different run IDs can never share a final archive directory.
35. Different attempts can never share a final archive directory.
36. Matrix/job collisions never overwrite another completed archive.
37. V1 never treats repository + run + attempt + GITHUB_JOB + SHA + RUNNER_NAME alone as proof of the same exact execution.
38. Two legitimate executions with the same run, attempt, GITHUB_JOB, runner, and SHA produce independent archives with no overwrite.
39. In the absence of a validated stable per-execution identifier, collision safety takes priority over perfect retry deduplication.
40. Idempotent retry never overwrites unrelated data.
41. Archive failure emits a stable `LOCAL_ARTIFACT_*` marker.
42. Archive failure returns non-zero.
43. Archive failure is visible in GitHub `Complete runner`.
44. Archive failure causes the GitHub job to fail on the validated runner version.
45. Job business failure does not prevent the completed hook from attempting archive.
46. Disk guard defaults to 15% free.
47. Disk guard checks the archive filesystem.
48. Disk guard never automatically deletes old data.
49. Disk guard failure prevents a new copy from starting.
50. Hook has a finite configured timeout.
51. Timeout failure never writes a PASS manifest.
52. Concurrent unrelated job archives can proceed without one global copy lock.
53. Completed-hook Summary capability is tested on the actual pinned/current Debian runner version before the V1 Summary path is selected.
54. If completed-hook Summary is reliable, it is the default V1 path and requires no per-repository summary job.
55. Completed-hook PASS Summary is written only after the PASS manifest is finalized.
56. Archive failure never produces a PASS Summary; FAILED Summary behavior is validated where supported.
57. If completed-hook Summary is not reliable, the reusable action + downstream summary job is used as the documented fallback.
58. A reusable local-artifact-summary action exists as fallback/reusable capability.
59. The fallback Summary reads finalized local manifests rather than assuming success.
60. Summary output does not expose secrets.
61. `archive://` remains a logical identifier and need not be remotely accessible.
62. Existing workflows are never silently rewritten.
63. Full GitHub artifact upload is optional, not emulated by this project.
64. GitHub artifact migration defaults to download + verify, without deletion.
65. Remote artifact deletion requires explicit `--delete-after-verified`.
66. Failed verification can never delete the remote artifact.
67. Migrated artifact metadata preserves artifact ID, name, original size, created time, and run ID.
68. Remote-delete failure preserves the local migrated payload.
69. Retention defaults to 90 days.
70. Cleanup is separate from the completion hook.
71. Cleanup defaults to dry-run.
72. Cleanup deletes only whole eligible run directories.
73. `.keep` prevents automatic cleanup.
74. Ambiguous/incomplete runs are skipped rather than deleted.
75. No periodic cleanup timer is enabled by default.
76. Status output reports archive root and filesystem capacity/free percentage.
77. Status output reports retention and disk threshold.
78. Archive root is not world-writable.
79. Repository workflows cannot choose arbitrary archive destinations.
80. The implementation documents that repository code sharing the runner account can still modify local archive files; v1 is not a tamper-proof evidence store.
81. No web UI, object store, database, or artifact server is introduced.
82. Existing GitHub artifact bulk deletion is not performed during initial validation.

## 47. Implementation order after spec freeze

No implementation should begin until this specification has passed audit and is frozen.

Recommended implementation order after approval:

1. host archive config parser and path/sanitization helpers;
2. shared completed hook with local temporary archive tests;
3. manifest/failure semantics;
4. symlink behavior;
5. disk guard and timeout;
6. atomic/retry-safe finalization and locking;
7. platform setup command;
8. `register-runner.sh` integration;
9. existing-runner migration;
10. archive health extension in `status-runners.sh`;
11. live completed-hook Summary capability validation on the Debian runner;
12. completed-hook Summary implementation if validated, plus reusable summary action/downstream job as fallback capability;
13. cleanup tool + `.keep`;
14. GitHub artifact migration tool;
15. automated failure-path suite;
16. Debian single-runner validation;
17. one existing-runner migration;
18. audit against acceptance criteria;
19. only then broader migration of current runners/projects.

## 48. SPEC-only boundary for this change

This commit must contain only the specification.

It must not add or modify:

```text
archive hook implementation
register-runner behavior
runner .env files
migration scripts
summary action
workflow templates
cleanup scripts
disk guard implementation
status-runners behavior
GitHub artifact migration code
systemd services/timers
README behavior claims that imply the feature already exists
```

After audit approval, implementation starts in a separate development batch.

## 49. External implementation references

The implementation should follow the current GitHub self-hosted runner hook behavior documented by GitHub, including these facts:

- `ACTIONS_RUNNER_HOOK_JOB_COMPLETED` runs after workflow job steps and before the job fully completes;
- hooks execute synchronously in the runner service account context;
- hook output is shown in `Complete runner`;
- hook paths are absolute and should live outside the runner application directory;
- GitHub does not provide a hook timeout;
- non-zero runner hook exit is intended to fail the job;
- hooks may use workflow commands/environment files according to current runner-hook documentation, so completed-hook access to `$GITHUB_STEP_SUMMARY` must be tested rather than assumed either way.

These external facts, especially completed-hook Summary behavior, must be rechecked against the exact runner version used during implementation and Debian acceptance testing.
