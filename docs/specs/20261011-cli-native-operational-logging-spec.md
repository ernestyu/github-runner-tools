# CLI Native Operational Logging — Incremental SPEC

Status: PROPOSED / AWAITING INDEPENDENT SPEC AUDIT  
Date: 2026-10-11  
Repository: `ernestyu/github-runner-tools`  
Implementation baseline: `70f815e0f41490f0f648beb788a846c8d302d7ec`  
Post-revert content-equivalent HEAD: `42625f3ac80ec9f6b8a362c22a5a0fad9f0c5b78`  
Frozen Web V2 authority: `d5b935ec99400e8cb3e0496af4172ec4840eb97c`  
Upstream decision: `PASS — APPROVE INCREMENTAL SPEC`

## 1. Purpose and boundaries

This is an independent, additive SPEC for CLI-generated, durable operational diagnostics. It does not modify or reinterpret Web Management V2, the privileged Executor, the current Create/Remove lifecycle state authority, registration/unit attestations, or UNKNOWN/reconciliation semantics.

Both invocation paths call the **same installed CLI** and therefore must generate the same kind of CLI-originated events:

- interactive shell → fixed CLI → CLI native operational log;
- authenticated Web → existing root Executor → fixed CLI → CLI native operational log.

CLI logging is not a Web logging protocol, a second lifecycle state machine, or a substitute for the authoritative files in `/var/lib/github-runner-tools`. Web continues to display existing operation outcomes; it need not read or display the log. The Executor's own admission, process launch, timeout, and IPC errors remain infrastructure diagnostics and are not reclassified in the CLI logger.

**No changes are authorized by this document yet.** This SPEC must be audited and frozen separately before implementation.

### 1.1 Explicit exclusions

No implementation changes in `web/v2_mutation.py`, `web/v2_event_loop.py`, or `web/v2_executor.py` for CLI business logging. No Web-specific operational ledger, additional privileged endpoint, credential logger, remote log sink, daemon, timer, cron job, generic shell trace, or migration of existing authoritative lifecycle records. No modification of the six existing production runner registrations or units.

## 2. Source-grounded current state

The existing `scripts/register-runner.sh` and `scripts/remove-runner.sh` print progress and failures to stdout/stderr and invoke their existing lifecycle authority helpers to persist root-protected stages. `scripts/status-runners.sh` prints status and has a JSON mode. These CLI scripts do not currently have a single native file logger with rotation and retention. In V2 the Executor captures CLI child output, but captured text is not itself a durable CLI operations file. Lifecycle checkpoints in `/var/lib/github-runner-tools/{active-instances,create-state,remove-state,runner-attestations,unit-attestations}` are **not** logs and must remain unchanged.

Real Debian 13 V2 acceptance observed a first Web Create of `ernestyu/github-runner-tools` returning `FAILED_PRE_EXTERNAL`, CLI exit 1, request ID `6e5d84658cdb`. Web and Executor were active, but journal did not expose the specific CLI error. The target runner directory and unit were absent on initial examination; absence of hashed state was not fully proved. Do not infer the precise failure reason or retry safety from those observations.

## 3. Minimal implementation scope

Expected implementation changes, **only after SPEC approval**:

- `scripts/register-runner.sh`: shared logger startup, explicit safe stage/decision/failure events and one final event on exit.
- `scripts/remove-runner.sh`: equivalent coverage, including remote uncertainty and local cleanup.
- `scripts/status-runners.sh`: safe operation start/end/error events, including JSON mode without contaminating stdout.
- **One common logger component**, proposed `scripts/cli-operational-logger.py`, implemented with the Debian-shipped Python 3 standard library. CLI scripts invoke this component using fixed subcommands and validated fields; no separate listener or daemon.
- Focused deterministic unit/integration tests in `tests/`.
- Only the minimal Debian first-install, upgrade and packaging/install configuration necessary to provision the logger and safe directory.
- A narrowly scoped, separately reviewed **already-V2** deployment/update path may be required to install the new CLI component. The existing V1→V2 cutover script is **not** an in-place V2 updater and must never be blindly rerun on an existing V2 installation.

No need to alter existing Executor child-output handling, request schema, or Web HTTP/UI behavior.

## 4. Log contract: fixed event vocabulary

The shared logger is the sole formatter/persistence facility. CLI code supplies only explicitly approved event keys. The logger **must not** accept arbitrary strings for message, exception, command, path, URL, stderr, stdout, environment, request payload, or external tool response.

A persisted record is one UTF-8 JSON object followed by LF (JSONL), deterministic schema version `1`, with the following keys:

| Key | Type and constraints | Source |
|---|---|---|
| `v` | literal integer `1` | logger |
| `ts` | RFC 3339 with UTC offset, milliseconds | logger clock |
| `run_id` | locally generated random 128-bit lowercase hex, per CLI invocation; **observational only** | logger |
| `seq` | positive monotonically increasing integer within invocation | CLI/logger |
| `op` | `create`, `remove`, `status` | fixed CLI selector |
| `event` | approved enum, see below | CLI |
| `stage` | approved enum or `none` | CLI |
| `result` | `begin`, `ok`, `failed`, `unknown`, `skipped` | CLI |
| `reason` | approved fixed enum or `none` | CLI |
| `exit_code` | integer 0–255 or `null` | CLI |
| `repository` | canonical validated `owner/repo`, or `null` | CLI validation |
| `runner` | validated non-secret runner name, or `null` | CLI validation |

Maximum serialized record **4 KiB inclusive of final LF**. No newlines inside field values. Invalid values result in safe fixed-constant logger rejection and never raw-value fallback. The logger computes `ts`, validates every field, and does not log caller-supplied arbitrary text. `run_id` is local to the logger/CLI invocation; it is **not** a lifecycle instance ID, transaction ID, Web Request ID, security token, or remotely authoritative identifier. It can appear on CLI console as an optional safe reference but no Web changes are required.

Approved `event` values: `operation`, `preflight`, `archive_check`, `release_lookup`, `runner_download`, `digest_check`, `runner_extract`, `dependency_check`, `lifecycle_checkpoint`, `remote_registration`, `remote_removal`, `registration_permissions`, `registration_attestation`, `service_install`, `service_start`, `service_health`, `service_stop`, `service_uninstall`, `local_cleanup`, `inventory`, `operation_exit`.

Approved `stage` values: `none`, `preflight`, `PRE_REGISTRATION`, `REGISTRATION_OUTCOME_UNKNOWN`, `REGISTERED_PERMISSION_INCOMPLETE`, `REGISTERED_UNIT_INCOMPLETE`, `REGISTERED_START_INCOMPLETE`, `REGISTERED_HEALTH_UNKNOWN`, `CREATE_COMPLETE`, `BEGIN`, `CONFIRM_REMOTE_REMOVED`, `COMPLETE`, `REMOTE_REMOVE_OUTCOME_UNKNOWN`, `REGISTERED_REMOVED_LOCAL_CLEANUP_PENDING`. The latter values are observation labels only and must be reconciled with actual authority transitions; logger must never initiate a stage transition. Do not emit a future/checkpoint-success stage before the corresponding authority operation returns success.

Approved `reason` values: `none`, `invalid_input`, `environment_unavailable`, `archive_unavailable`, `release_metadata_failed`, `download_failed`, `integrity_failed`, `dependency_failed`, `checkpoint_failed`, `permissions_failed`, `attestation_failed`, `service_install_failed`, `service_start_failed`, `service_health_failed`, `service_stop_failed`, `service_uninstall_failed`, `remote_result_uncertain`, `local_cleanup_failed`, `inventory_failed`, `unexpected_cli_failure`, `logging_unavailable`. These are stable coarse-grained causes; diagnosis precision comes from stage + event + reason, **not** untrusted detailed strings.

### 4.1 Emission rules

- Start one `operation/begin` event after logger initialization and input normalization; if validation fails first, emit fixed `operation/failed/invalid_input` without reflecting the invalid input.
- Before each critical step, emit `event/begin`; on confirmed success emit `event/ok`; on failure emit `event/failed` with fixed reason, except ambiguous external effects emit `event/unknown`.
- A failed checkpoint is reported as `checkpoint_failed`; never log `ok` before durable authority success.
- For Create, at minimum instrument input/preflight/archive, release lookup/download/digest/extraction/dependencies, PRE_REGISTRATION checkpoint, remote registration uncertainty, permissions/attestations, service installation/start/health, CREATE_COMPLETE, and final exit.
- For Remove, instrument identity/preflight, BEGIN checkpoint, service stop/uninstall and record reconciliation, remote unregister, CONFIRM_REMOTE_REMOVED, local cleanup and COMPLETE; events must track **actual current ordering**, not assume remote removal occurs before local service operations.
- For Status, instrument `inventory` and final exit without modifying the exact existing `--json` stdout contract.
- The one final `operation_exit` event must record shell exit status and last *observed* safe reason, best effort once; the log cannot claim successful remote outcome solely from process exit. Uncaught exit traps must not overwrite prior exit code or authoritative records.
- No raw environment, args, URLs, API results, subprocess output, exceptions, backtraces, command expansions, `set -x`, or `config.sh` transcripts are persisted. The existing console behavior is separate and remains subject to its current safety constraints.
- CLI exit behavior, interactive token prompts, stdin/stdout, existing Remove result FD handling, and Executor FD3/FD4 contract remain unchanged; logger never reads, duplicates or inherits Token FD or privileged control FD.

### 4.2 Failure before CLI entry

CLI-native logging **cannot** report an error before the launcher successfully execs CLI, e.g. Executor admission failure, launcher privilege drop, spawn failure or killed process before logger init. Those remain bounded infrastructure errors in existing system journal. This SPEC does **not** falsely promise CLI logs for events that did not reach CLI.

## 5. Filesystem and threat model

Default directory `/var/log/github-runner-tools`, root-owned `root:actions`, mode **2770** (setgid; writable only by the existing trusted `actions` runner owner and root); create only during explicit root-authorized Debian installation. Setgid alone is insufficient to guarantee file ownership/mode: logger sets umask 0077 and creates files **0600**, owned by `actions` when called after the existing launcher drops to actions. Root is not used as an implicit logger privilege escalation. The logging directory is treated as a **trusted administrative directory writable by actions**; as a consequence, the `actions` principal may alter/delete logs. These logs are operational evidence, **not tamper-evident audit records**. Other local principals including `grt-web` cannot write or read them. If root-only integrity is needed, it would require a separate architecture and is out of scope.

Important: a directory whose group equals `actions` may be writable by the `actions` user only if it is a group member; the target is already `actions`. The installer must use root:actions 2770 with explicit invariant checks, and must not change the identities/capabilities of existing services. If an already-present directory has different ownership, extra ACLs, symlink components, or unsafe modes, initialization **fails** instead of forcibly repairing it.

The logger operates only below that fixed directory. It verifies every ancestor and the directory as real directories (no symlinks), owner/group/mode expectations, and no unsafe ACLs where observable; opens managed active/archived files using directory-relative operations, `O_NOFOLLOW|O_CLOEXEC`, `O_CREAT|O_EXCL` for new files, `fstat` to verify regular-file type/owner/mode/link count=1, and refuses symlink/hardlink/path substitution. Prevent arbitrary file deletion by treating only strict logger-generated basename patterns as managed files and validating opened inodes before unlink/rename. Do not traverse into subdirectories or follow reparse points.

The logger's lock file is fixed `operational.lock`, mode 0600, owned by `actions`, `O_NOFOLLOW`, link count 1. JSONL active file `operational.jsonl`; rotated files `operational.YYYYMMDDTHHMMSSffffffZ.<random128>.jsonl` (UTC timestamp plus uniqueness). All managed files have mode 0600 and are owned by `actions`. Use secure dirfd-relative open and atomic rename. No compression or external `logrotate` in v1.

**Trust limitation:** `actions` already owns and executes the CLI, and is a powerful principal on the machine. Log file modes prevent other unprivileged users from reading secrets accidentally, but cannot prevent deliberate tampering by `actions`. Do not advertise this as a forensic audit trail.

## 6. Capacity, rotation and retention

Default exact binary units:

- `MAX_FILE_BYTES = 10 * 1024 * 1024`;
- `MAX_DIRECTORY_BYTES = 100 * 1024 * 1024`;
- `RETENTION_SECONDS = 14 * 24 * 3600`;
- `MAX_RECORD_BYTES = 4096`.

No user-writable configuration overrides these security/retention limits in the first release. No daemon, cron or timer. Every CLI command that initializes the logger performs maintenance, even when there is no event beyond start/end; each event append also checks capacity.

All managed content, including active JSONL, rotated JSONL **and lock file**, counts toward the total directory cap. Other non-managed entries in this directory are an error, not deletion candidates. On startup: acquire logging lock, validate directory/file set, delete expired **closed** rotated logs (time based on embedded validated rotation timestamp; detect impossible future timestamps and fail), rotate active if its age/capacity requires, and evict oldest closed logs until within cap. During append: serialize bounded full record first, lock, validate active inode/size, rotate **before** write if `active_size + record_size > MAX_FILE_BYTES`, evict oldest closed logs to ensure `total_size + record_size <= MAX_DIRECTORY_BYTES`, then append complete record with bounded write, flush and release lock. A partial/short write is failure; do not claim successful event delivery.

At directory cap, prefer expired files, then oldest closed files. Never delete active or lock. If no eligible closed files remain, refuse further appends with `logging_unavailable`; **do not** discard a log silently, violate cap or delete non-log data. Logger must not grow beyond file/dir bounds through a single event. The initialization/rotation code must handle the timestamp of the active log using safe metadata; expired active logs are rotated before cleanup, but no guarantee of real-time 14-day erasure while CLI is idle. Closed files with timestamps beyond current clock plus a limited (documented) skew fail closed rather than being silently sorted. Capacity counts bytes on disk for these regular files, not filesystem block allocation.

If a write is interrupted or crashes mid-record, the next invocation validates tail JSONL integrity under lock; truncated final record is quarantined or repaired using a **specified conservative policy**: fail logging initialization and require explicit admin inspection (no silent truncation, no rewriting). The `actions` owner can administratively repair with prior backup under a separately documented recovery runbook, never automatically rewrite prior evidence.

Rotation and pruning acquire **one logger-specific advisory flock**, distinct from lifecycle mutation lock. Fixed lock ordering: lifecycle code may hold mutation lock before acquiring short-lived log lock; logger **must never** acquire mutation lock or execute lifecycle functions under log lock. Lock held only during bounded in-process file operations, never around downloads, shell commands, network, or systemd changes. Lock acquisition timeout **2 seconds**, using monotonic clock; on timeout log operation is unavailable and follows §7 policy. On process crash, kernel releases flock.

## 7. Logging failure strategy and lifecycle authority

The logging facility is diagnostic, not authoritative. Its safety policy must not create new ambiguous lifecycle results.

1. **Before external or systemd side effects**: logger initialization and first operation event are **mandatory** for Create/Remove. If unsafe directory, lock timeout, rotation/prune failure, capacity exhaustion, disk full, or invalid logger binary prevents a durable first event, abort Create/Remove before mutating runner files, GitHub or systemd, with a fixed non-secret stderr message and nonzero exit. Status may continue as read-only with a fixed stderr warning and must preserve original status/JSON stdout.
2. **Once potentially irreversible external or systemd effects may have started**: logging failures must **not** abort, retry or unwind the lifecycle operation; emit a fixed stderr warning at most once, continue existing state authority and return the original CLI/lifecycle outcome. Critical checkpoints continue to use existing authority. If the CLI subsequently reports UNKNOWN, it remains UNKNOWN regardless of logging health.
3. **Before first side effect, but after preflight files or temporary runner directory exist**: the SPEC's implementation must identify the safe boundary explicitly; cleanup stays governed by existing CLI cleanup rules. It must never introduce new automatic deletion of a partially registered directory.
4. Logger errors must not hide the original CLI exit status, introduce `set -x`, leak original exception text or cause a second operation. Unexpected logger failure before side effects maps to safe fixed failure; after side effects, fail-open for observability only, never for lifecycle authority.
5. When logging fails after side effects, absence of a record is not evidence of success or safe retry. Operators must use authoritative state and GitHub reconciliation, not log files, for decisions.

To implement this without adding a new lifecycle state machine, each CLI script keeps a **process-local one-way boolean** `SIDE_EFFECT_BOUNDARY_CROSSED` set conservatively **before** its first possibly externally visible or persistent side effect. It is not persisted, consulted for business-state decisions, or used to authorize retries. The SPEC requires audits of current Create/Remove ordering to freeze exact placements before implementation: existing mutation-lock setup, temp directory creation, root authority checkpoint, download and systemd actions must not be accidentally performed prior to a mandatory log gate. If a subprocess exits early between gates, safe final event is best effort only. For Remove, stopping a service is already a side effect even before unregister; boundary must be set before stop. This local flag is only a logger failure-policy guard.

## 8. Debian initialization, packaging and controlled upgrade

- Approved root installer creates `/var/log/github-runner-tools` (root:actions, 2770) and fixed `operational.lock` (actions:actions, 0600) **with secure no-follow validation and without replacing pre-existing objects**. Confirm `actions` group/user, Debian 13/Python runtime compatibility, and no unintended ACLs. Logger Python code is installed root:root, regular file 0755, under trusted `/usr/local/lib/github-runner-tools/cli/` with integrity/provenance and import path requirements consistent with V2 pinned code. Never make installed code runner-writable.
- Installer must be idempotent on a correctly initialized environment; it must reject unexpected ownership, object types and modes rather than silently chown or erase old logs.
- Add explicit preflight for free space, ability for `actions` to write after drop, logger lock, and same-service execution context (existing Executor sandbox). Do not change Executor sandbox/capabilities simply to satisfy logging; if sandbox denies writes to `/var/log/github-runner-tools`, stop and request independent audit of a minimal service policy change, not a covert workaround.
- The existing Web V2 `setup-web-management-v2.sh --apply` is only for V1→V2; the logging SPEC requires a **separate controlled update path** for already-installed V2. Snapshot changed installed CLI/logger/config files, stage code and validate hashes, install atomically, support rollback of **code/config only** if preflight fails, do not rollback lifecycle authority or log content, and do not stop/restart or reinstall any of the six runner units. Ensure active CLI operations have exited; coordinate the mutation lock without re-entrant deadlock. Any restart of the Executor/Web must be separately justified: default no restart because code is spawned from installed CLI per operation.
- Initial bootstrap for fresh installs and already-V2 updates must converge to the same validated logger installation and directory configuration. Do not require rerunning V1→V2 cutover or registering existing runners.
- V2's pinned CLI code provenance (including imported component) must be updated as necessary **only within approved install/provenance configuration**. No silent relaxation of provenance checks and no changes to `web/v2_mutation.py`, `web/v2_event_loop.py`, `web/v2_executor.py`.

## 9. Executable verification matrix (required implementation tests)

Automated tests must be runnable without real GitHub credentials or destructive systemd access, with isolated temp filesystem/identity adapter where needed. A test-only directory override is accepted **only** under a deliberately compiled/test-only invocation guard; production Web and CLI must reject any environment override that redirects logger to attacker-chosen paths.

| ID | Scenario | Expected assertion |
|---|---|---|
| LOG-01 | Create/Remove/Status CLI direct success | fixed JSONL schema; start/end and stage ordering |
| LOG-02 | same CLI via simulated Executor FD context | same file logging semantics; no Web-owned log or Executor business classifier |
| LOG-03 | Create preflight error before side effects | nonzero; concrete event/stage/reason; no remote or unit mutation |
| LOG-04 | Create download/digest/dependency failures | correct safe reasons; bounded output |
| LOG-05 | Create remote registration ambiguous | `unknown` event; no false `failed_pre_external` from logs |
| LOG-06 | Create permissions/attestations/unit/start/health errors | authoritative state untouched by logger; correct stage labels |
| LOG-07 | Remove identity rejection/BEGIN checkpoint failure | no unapproved cleanup; safe failure event |
| LOG-08 | Remove service stop/uninstall/remote unregister/cleanup failures | original ordering and outcome; uncertainty preserved |
| LOG-09 | Status `--json` | byte-for-byte compatible stdout schema; no logging text in stdout |
| LOG-10 | appends near 10 MiB and record max 4096 bytes | rotate before crossing; no oversized file |
| LOG-11 | repeated rotations and 100 MiB directory cap | bounded aggregate size; oldest closed eviction |
| LOG-12 | 14-day expiration/clock skew/idle interval | on-demand purge; no background promise; future timestamp handling |
| LOG-13 | concurrent append/rotate/prune and lock timeout | complete line atomicity; bounded wait; no lifecycle lock inversion |
| LOG-14 | SIGKILL/crash during append or rotation | next startup detects corruption; no silent truncate |
| LOG-15 | permissions/ACL/symlink/hardlink/path substitution | refuse unsafe objects; no outside-file writes/deletes |
| LOG-16 | token and arbitrary external output injection | no Token, Authorization header, argv, raw stderr/traceback anywhere in persisted events |
| LOG-17 | logger failure before side effect | Create/Remove abort without external mutation |
| LOG-18 | logger failure after side effect | CLI continues existing lifecycle/UNKNOWN handling; original rc preserved |
| LOG-19 | fresh Debian install, repeated init, already-V2 upgrade, rollback | safe ownership/provenance, no V1 cutover rerun |
| LOG-20 | regression for existing V2 permission/FD/timeout/lifecycle rules | no changes to Executor business logic or lifecycle state schema |
| LOG-21 | production six-runner baseline comparison | IDs/names/units remain unchanged; no test mutates them |

Tests must include deterministic/fault-injected timing, ENOSPC, EACCES, lock contention, partial writes, future timestamps and ambiguous external outcomes. Unit tests must establish that `--json` output remains machine-consumable. Verify both Create and Remove existing stage checkpoint logic with mocks rather than GitHub tokens.

## 10. Debian real-host acceptance and operator workflow

Only after independent SPEC audit, frozen approval, implementation review and CI PASS:

1. Snapshot six existing runner identities/unit states and root lifecycle authority metadata without exposing credentials.
2. Verify current V2 provenance and zero active Create/Remove operations; ensure previous failed Create is reconciled from **authoritative hashed state**, not guessed from file names.
3. Deploy scoped CLI logger + initialization through the controlled V2 updater, without V1→V2 cutover and without touching production runner units.
4. Confirm logger permissions, bounded maintenance and that `actions` can write through the **actual Executor-launched CLI context**.
5. Create a **new isolated temporary** runner for `ernestyu/github-runner-tools` via Web and independently verify GitHub, local files, Unit and CLI events.
6. Submit one harmless job to temporary runner (unique labels verified) and then remove only that runner through Web.
7. Inspect operational log stage sequence/reasons, retention and capacity tests, GitHub absence and local cleanup; compare all six original runners with baseline.
8. On UNKNOWN, no retries, forced cleanups or synthetic completions; retain evidence and reconcile manually.

Audit and tests cannot retroactively reconstruct child output discarded by the original `6e5d84658cdb` attempt.

## 11. Review gates and acceptance

Independent SPEC audit must evaluate: (a) event/schema completeness and no arbitrary-value channels; (b) path, ownership, inode/link and rotation security; (c) actual 10 MiB/100 MiB constraints, 14-day on-demand policy; (d) concurrency and lock ordering; (e) logging failure gate before vs after side effects; (f) Debian install and pinned provenance; (g) test matrix and six-runner protection.

Auditor returns `PASS — FREEZE INCREMENTAL SPEC` or `BLOCKED — REVISE SPEC`, with enumerated blockers and precise amendments.

**Non-authorizations:** This SPEC commit does not authorize implementation, testing against production Runner mutations, modifications of existing Web V2 core Python files, PR merge, or Debian deploy.
