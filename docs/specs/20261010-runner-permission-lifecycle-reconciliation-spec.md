# Runner Permission Contract & Lifecycle Reconciliation — SPEC

Status: **DRAFT — independent SPEC audit required**  
Repository: `ernestyu/github-runner-tools`  
Baseline: `fc8f6e2f16c366cba313819248579ff58df2b04b` (audited Service Uninstall Reconciliation implementation)  
Incident environment: Debian `actions` runner owner; HyperGrid and AgentCI observations supplied by operator.  
Scope: Create, normal Web Remove, verified historical runner permissions, and terminal local-cleanup semantics. Production changes, real Runner removal and unreviewed cleanup are **not authorized**.

## 1. Observed failures and non-assumptions

HyperGrid `ernestyu/HyperGrid`, `/home/actions/actions-runner-ernestyu--hypergrid`, was shown Active but Web Remove stopped at `preflight_failed` before intended service mutation. Its `.runner` and `.credentials` are `actions:actions 0664`; `.service` is `actions:actions 0644`. The current `scripts/web-service-record.py` rejects group/world-writable configured metadata via `mode & 0o022`. Consequently this permission mismatch explains the observed `identity` exit 1; this is **not evidence** of incorrect repository metadata or removal token. No live systemd Unit metadata for HyperGrid was yet provided.

A previous **manually completed** AgentCI unregister left `/home/actions/actions-runner-ernestyu--agentci` (1.1 GB), with `.runner`, `.credentials` and `.service` absent and `status-runners.sh --json` reporting `repository:null, management_state:incomplete`. This proves a local residue exists, **not** that the latest Web Remove success path wrongly reported success. There is no valid identity record for current Recover Local, so do not automatically delete this directory on a mere basename match.

The baseline `register-runner.sh` invokes upstream `config.sh` to register and, in Web mode, privileged Dispatcher `service_install`; it does not normalize `.runner`/`.credentials` permissions after registration. Normal CLI mode uses upstream `sudo ./svc.sh install`, whose v2.328.0 template creates mode-0664 Unit; Dispatcher strict verification rejects group-writable Units. The actual creating process and umask of historical files is **unproven**; tests must establish it rather than assert it.

## 2. Invariants, authority and forbidden shortcuts

1. User-facing success means **all three independently proved**: (a) official GitHub unregister completed positively, (b) expected systemd Unit absent, (c) expected Runner directory absent. An Offline GitHub status alone is not successful unregister; directory absence alone proves neither.
2. Never do a blind `chmod -R`, global group permission change, global migration of all runners, direct unvalidated `rm -rf`, generic privileged file repair, or generic `systemctl` request. No arbitrary path, UID or unit may be supplied to new privileged operations.
3. No automatic removal of `.runner`, `.credentials` or directory on unconfirmed/unknown GitHub unregister; no retry if `.service` absent or `.grt-service-reconcile-*` residue is present; retain the frozen UNKNOWN re-entry gate.
4. No changing ownership of unexpected files; no following symlinks; no hardlink manipulation; no group/world-writable Runner directory accepted. Must not touch another Runner's files, service or token.
5. Maintain existing root Dispatcher versus runner-owner Worker separation, authenticated IPC, mutation lock, deadlines, token FD/PTY handling, diagnostics allowlist and server-side confirmation.
6. Only **fixed, verifiably safe and narrowly identified historical permission states** are eligible for automatic reconciliation. Unrecognized state fails closed with an actionable fixed diagnostic; no SSH/manual chmod for eligible normal historical state.
7. Existing frozen Service Uninstall Reconciliation SPEC and audited implementation remain authority for service record quarantine and remote-result semantics. This new SPEC adds permission handling and successful local cleanup verification; it must not redefine prior security gates.

## 3. Fixed permission contract

| Artifact | New Create target | Legacy auto-eligible | Identity owner and condition |
|---|---|---|---|
| Runner directory | owner `actions`, 0755 or stricter, **no group/other write** | existing secure owner and mode only | anchored canonical scoped directory; no symlink components |
| `.runner` | owner-only 0600 | 0644/0664, **only if all eligibility constraints of §4 hold** | regular file, expected runner UID/GID, nlink=1, exact repo URL and runner agent name |
| `.credentials` | owner-only 0600 | 0644/0664, **only if all eligibility constraints of §4 hold** | regular file, expected runner UID/GID, nlink=1; **never print or parse credentials** |
| `.service` | 0600 or 0644 according to existing service implementation; no group/world write | preserve already-secure file; no blanket downgrade | exact canonical service name, owner, type and link checks per frozen contract |
| systemd Unit | root:root 0644 | root:root 0664 repair **only by separately audited restricted Dispatcher operation** | canonical Unit fragment, no drop-ins, exact User/Workdir/ExecStart/service identity and no extra Exec hooks |

`0600` is chosen for the two registered metadata files because the Runner process is owned by the same runner UID; implementation must test official Runner startup, reconnect and removal compatibility. On Create, normalize **after official `config.sh` returns confirmed success and before starting the service**. After `svc.sh install` (CLI path), verify `.service` and Unit state, normalize only with the appropriate existing permission authority; do not silently repair unrelated systemd Units. Unit `0644` is already the Web Dispatcher install target.

If upstream version or permissions make one of these invariant targets incompatible, return a partial-creation failure preserving registration assets, not an apparent completed Create. Changes to `svc.sh` or official Runner bundle are prohibited.

## 4. Historical Runner permission reconciliation (normal Web Remove) — B-01/B-02 frozen

### 4.1 Historical integrity authority (B-01)

**Frozen decision: no historical auto-heal without an existing independent attestation.** The project presently has **no verified, pre-existing trusted baselines** for HyperGrid or other historical `0664` registrations. Current hashes, matching repository metadata, file ownership, trusted-looking contents, new root snapshots, or an operator clicking Remove do **not** establish that a previously group-writable file was never altered. HyperGrid is **not auto-eligible** based on currently known evidence.

For **future** managed creations only, create a root-owned audit record at the moment registration first succeeds and **after** the content and target UID/GID are verified and normalized. The root Dispatcher is the authority: it accepts only an authenticated, narrow `registration_attestation_create` operation for a validated in-progress Create, with fixed repo/runner/canonical-directory fields, strict no-symlink access, and checksums derived from its own FD reads; the Worker cannot supply digests. The authority store is `/var/lib/github-runner-tools/runner-attestations/` with root:root 0700 directory and root:root 0600 files; no runner/group/worker writes. File name uses a validated deterministic opaque identifier from the repo+runner+canonical directory identity (not a path selected by caller). Record schema version 1: repository, runner name, directory canonical path and device/inode, runner UID/GID, `.runner` and `.credentials` device/inode, SHA-256 of each file, mode and capture timestamp, approved installed Runner version, attestation creation stage and issuer. Never store plaintext credentials or raw registration metadata. File publication uses O_EXCL temporary file, fsync, atomic rename and parent fsync under existing mutation lock; never overwrite an existing mismatching record. Root must validate owner and contents before reading existing attestations.

**Important chronology:** attestations captured *after* files were previously group-writable cannot retroactively establish historical integrity, even if stored root-only. A record is usable for automatic historical reconciliation only if it was issued by this trusted creation path before any period of untrusted group write, or if a separately approved trusted provisioning system already recorded an equivalent immutable prior baseline. The latter integration is **not authorized** here; absent this evidence, fail closed. Creation must prevent any writable-by-untrusted-group interval before provenance becomes authoritative: strictly control the runner's Create execution context and umask (077), immediately verify/normalize via descriptor-bound operations before starting a service, and record attestation once normalized. Attestation is a future integrity guard, not a shortcut to authorize rewriting old 0664 files.

On normal Remove: establish identity using current configured metadata without making any permission changes. For 0600 files matching expected owner/type/identity, proceed with existing strict checks. For legacy 0644 or 0664 files, accept silent repair **only if** a pre-existing provenance record meets the above chronology, matches exact paths, identity, dev/inode, file hashes and metadata, and current trust conditions; missing/stale/unmatched evidence means fixed `permission_reconcile_failed`, no chmod and no systemd mutation. Explicit host-administrator review outside this Web flow is required; this SPEC does not authorize a checkbox-based bypass. For 0664 specifically, confirm no untrusted group writers and no active same-inode write handles via available host evidence; absence of a proof is a refusal, not a guess.

The mutation primitive for verified eligibility is `openat(O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC)`, bounded reads, `fstat`, root-attestation comparison, `fchmod(fd,0600)` on the **same opened inode**, then `fstat`, `lstat(dir_fd)`, identity and hash revalidation. Because another writer with already-open FD can remain able to change contents, fchmod does not remove such write access: a credible writer-exclusion condition is mandatory or fail closed. No independent historical attestation means no automatic `fchmod`, regardless of whether a chmod would be easy. Newly created compliant files do not need this historical pathway.

### 4.2 Root-owned systemd Unit permission (B-02)

**Frozen decision:** a root-owned Unit with mode 0664 may not be automatically changed to 0644 on the strength of schema inspection alone. An immutable trusted baseline acquired when the Unit was created, outside runner-group control, must predate unsafe group-writable exposure; otherwise return fixed `permission_reconcile_failed` before service stop. This means old official `svc.sh install` Units with no trustworthy prior baseline require admin review, and this change does **not** promise unattended healing of every old Unit.

No generic root chmod endpoint is permitted. A future strictly scoped `service_unit_permission_reconcile` Dispatcher operation must accept exactly the validated repository, runner name, canonical runner directory and canonical Unit identity; no arbitrary path/mode/UID fields. It must: (1) independently recompute identity and Unit path; (2) open fixed `/etc/systemd/system/<canonical service>` using no-follow FD traversal; (3) validate root ownership, regular type, link count=1, exact **0664** candidate mode, content against the fully deterministic Unit schema, no drop-ins or extra Exec hooks; (4) obtain and validate pre-existing root-protected pre-exposure attestation bound to exact Unit path/device/inode/content hash, repo, runner and directory; (5) ensure no active writer remains capable of mutating the inode, else refuse; (6) `fchmod(fd,0644)` on the validated FD only; (7) recheck FD and path identity, schema, hash and metadata; (8) run the **unchanged** strict `validate_unit` and require success; (9) report a fixed result without exposing raw Unit data. Every failure is fail-closed **before** service stop, with no generic relaxed `validate_unit`. If inability to establish write exclusion makes this mechanism unprovable for 0664, it must remain disabled rather than silently broaden permissions.

Web Create already uses Dispatcher-generated root-owned Unit 0644. CLI Create must explicitly produce or normalize a correct Unit 0644 in a trusted creation transaction; the new Unit attestation must be generated by root immediately upon secure creation, not retroactively after unverified exposure. If a newly created official Unit is already 0664 without a pre-exposure trusted snapshot, the CLI Create path must report partial success and require administrator remediation; do not call legacy repair implicitly.

### 4.3 Permission outcome and interaction

`permission_reconcile_failed` is emitted for ineligible or failed owner/record/Unit permission coordination. Check this condition before service mutation and before `config.sh remove`. No bulk migration, no automatic group membership changes and no permission-bypass checkbox. The existing UNKNOWN remote-remove re-entry gate remains higher priority when a missing `.service` or quarantine indicates prior attempted removal.

## 5. Create contract and partial-state handling — M-01 frozen

All Create modes use a single durable, access-controlled transaction state located outside the runner-owned directory, created before beginning registration. State is root-owned, 0600, atomic/fsynced under `/var/lib/github-runner-tools/create-state/`; writes are performed only through a narrow authenticated Dispatcher operation using validated fixed repo/runner/directory identity. Never put tokens or credentials in this state. The state must record request identity, attempt ID, known official registration result, mode/attestation checkpoint, unit installation and start checkpoint, last confirmed stage, and whether the remote result is uncertain. No automatic re-registration is permitted while a previous registration may exist.

| Confirmed checkpoint | Required durable stage | Operator-facing action |
|---|---|---|
| No registration attempted | `PRE_REGISTRATION` | Standard safe retry only when no uncertain prior request |
| Official config success, metadata permission/attestation failure | `REGISTERED_PERMISSION_INCOMPLETE` | Preserve registration assets; host-admin review before repair or official removal |
| Permission complete, Unit installation failure | `REGISTERED_UNIT_INCOMPLETE` | Preserve registration; no second registration; separately authorized service recovery |
| Unit installed, service start failure | `REGISTERED_START_INCOMPLETE` | Preserve registration and Unit; separately authorized restart or cleanup |
| Service started, final health probe failed | `REGISTERED_HEALTH_UNKNOWN` | Preserve all state; investigate; no automatic duplicate Create |
| Official config failed/timed out with uncertain side effect | `REGISTRATION_OUTCOME_UNKNOWN` | Do not retry without manual GitHub reconciliation |
| Registration, permissions, Unit and health positively verified | `CREATE_COMPLETE` | UI may report completed Create |

A Create continuation is **not** a new Create: future recovery endpoint requires separate SPEC/authorization; until then these stages have fixed read-only diagnostic instructions and cannot be bypassed via ordinary Create. On confirmed full success, keep a minimal durable, root-protected identity/attestation history required for later Remove; do not store plaintext secrets. Reconcile crash-after-step-before-state-write conservatively to UNKNOWN. Any backend unable to persist the required checkpoints cannot claim complete support for this Create contract.

The setup must use umask 077, trusted non-symlink directory and FD-scoped file verification, handle upstream registration artifacts atomically where feasible, and ensure 0600 before official service launch. Verify compatibility of the actual Runner version without patching its bundled scripts. Never delete credentials or local directories merely because Create ended in a partial state.

## 6. Remove success, local cleanup and old residue — B-03/M-02 frozen

### 6.1 Full Remove success and final cleanup (M-02)

Success requires the official GitHub unregister to return an unambiguously successful result, authenticated Dispatcher verification that Unit is absent, and verified absence of the exact Runner directory after cleanup. Keep the frozen service record quarantine and UNKNOWN semantics: no local directory removal when remote outcome is failed or uncertain.

**Chosen safe deletion mechanism:** Linux descriptor-relative, no-follow, single-filesystem traversal implemented as a constrained non-root Worker routine, **not** a plain `rm -rf` after a pathname test. Hold the existing mutation lock. Pin allowed parent hierarchy by `O_DIRECTORY|O_NOFOLLOW`, verify canonical basename and target directory `(st_dev,st_ino)`, owner and allowed mode. Refuse if there is any mountpoint (including nested bind mounts); mount graph must be checked before and during traversal, with ambiguity or changes failing closed. Recurse through directory FDs using `openat(...O_DIRECTORY|O_NOFOLLOW)` and `unlinkat` relative to pinned descriptors; never follow symlinks, never cross `st_dev`, never descend into mounted subtrees and never use caller-selected relative traversal paths. Symlinks **inside** the validated runner directory may be unlinked as entries but their targets must never be opened; unexpected hardlinks to external regular files must be rejected/handled by a separately defined policy before deletion (for this SPEC: reject `st_nlink>1` for ordinary files). Refuse exotic file types or unsafe ownership if provenance cannot be established. A concurrent directory-entry replacement may cause failure or partial cleanup; never redirect traversal outside pinned directories. Confirm the pinned top-level inode still matches the expected entry before removing the final directory entry and again verify pathname absence, no unexpected symlink and Unit absence. No authority to remove files outside the Runner directory or archives stored elsewhere.

**Crash/partial cleanup:** persist a root-protected terminal lifecycle record `REGISTERED_REMOVED_LOCAL_CLEANUP_PENDING` after confirmed remote success but before recursive removal, carrying exact runner identity and pinned directory metadata (no credentials). If interrupted, the normal Web Remove path must not invoke `config.sh remove` again; operator-visible result is `local_cleanup_failed`. Re-entry into cleanup requires a separate explicitly authorized continuation using the persisted, independently verified terminal record, strict current directory and Unit revalidation and new explicit confirmation. If record or identity is missing, refuse and enter manual forensic review. Under no condition may a partial deletion be reported as complete. Mark `REMOVE_COMPLETE` only after all postconditions, including directory absent, are verified and durably recorded.

### 6.2 Admin Residual Cleanup — separate authorization (B-03)

**Frozen scope decision: no general-purpose Admin Residual Cleanup endpoint is implemented or authorized under this SPEC.** Incomplete directories with all `.runner`, `.credentials` and `.service` absent (e.g. AgentCI 1.1 GB) have no automatically trustworthy repository/runner association. The basename, missing GitHub runner name in a listing, root/runner ownership alone, or a newly captured hash is not historical identity proof. The status list must continue to expose `incomplete` and refuse normal Web Remove and Recover Local.

A distinct future maintenance operation may be designed **only** with pre-existing independent identity evidence (e.g. an authenticated root-owned creation/removal ledger created *before* identifiers were deleted, binding exact GitHub runner ID, repository, runner name, and canonical directory dev/inode). A ledger created now from a guessed directory name does not qualify. Remote check must positively establish absence of the **exact runner ID**, not merely the same display name: use a separately authorized read-only GitHub API integration or documented administrator-evidence procedure; if no stable historical runner ID exists, fail closed. Independently verify absence of any systemd Unit referencing the inode/path (not just the guessed service name), active Runner process or file descriptors referencing the directory, mountpoints, symlink path components and cross-directory hardlinks. Missing evidence or any unknown state ⇒ no automated deletion.

**Human authorization gate for any future maintenance interface:** a host administrator with privileged access must review the authoritative historical evidence and a dry-run inventory, explicitly confirm the exact runner ID/repository/path/device/inode and intended deletion; the action is separate from ordinary Remove/Recover Local, uses a short-lived one-time approval bound to immutable identity and new fresh-state checks, and keeps an append-only root-controlled audit trail. The prospective executor must use §6.1's FD-relative cleanup; interrupted execution remains pending with re-verification and requires another explicit approval. **This paragraph defines eligibility and required future safeguards, not a currently available executable cleanup path.** AgentCI stays untouched without a separately authorized forensic cleanup task.

No ordinary Web Remove may silently clean an unrelated incomplete directory. Under this SPEC, future creates with trusted root-controlled ledgers will have evidence for safe post-success cleanup; orphaned historical dirs without it stay manual-forensic-only.

## 7. Diagnostics and UI — m-01 frozen

Add dedicated fixed `permission_reconcile_failed` to Remove terminal `GRT_REMOVE_RESULT_V1` stage allowlist, Worker strict parser and Web fixed, non-sensitive message. Preserve marker ASCII size cap, FD isolation, exactly-one terminal marker, unknown-marker fallback and existing HTTP behavior. It reports validation/repair failure **before GitHub registration removal was attempted**; do not reuse `service_record_reconcile_failed` for `.credentials` or Unit permission defects. Invalid or untrusted repository/`.runner` identity is `preflight_failed`; missing previous `.service`/quarantine is `unknown_failed`; true `.service` record reconciliation remains `service_record_reconcile_failed`; post-remote cleanup problem is `local_cleanup_failed`. Keep distinct service state/uninstall failure phases. No raw owner, path, hash, token, credentials, traceback or shell stderr in Web output.

Create returns fixed state categories specified in §5, with separate operator-facing wording for confirmed registration versus unknown registration; not a generic retry prompt. Worker and Web maps must be updated atomically with tests. Terminal ledger state must never be inferred merely from the last displayed UI message.

## 8. Tests and acceptance matrix — revised, executable

Tests are isolated unless explicitly marked Debian acceptance:
1. Under umask 002 and 077, future Create normalizes `.runner` and `.credentials` to 0600 **before service start**, records trustworthy root-controlled attestation and durable partial checkpoints; tested Web and CLI paths with pinned Runner v2.328.0 fixture.
2. Existing 0600 configured metadata follows strict unchanged path. An old 0664/0644 file with **no pre-existing trusted attestation** (HyperGrid analogue) must return `permission_reconcile_failed` with no chmod, service stop or official config remove; newly computed hash cannot satisfy provenance.
3. For a specially crafted valid **pre-exposure** protected attestation fixture, exact repo/runner/dev/inode/hash/mode is required; FD-targeted fchmod and post-verification pass only when reliable writer exclusion is established. Stale/forged/root-store-writable-by-runner attestations, group-trust failures and open writable inode interference must fail closed.
4. Unit 0664 with no trustworthy historical baseline refuses root repair **before stop**; only authenticated narrow operation with valid pre-exposure attestation, full expected schema, no drop-in, no symlink, nlink 1, no concurrent writable handle permits fchmod to 0644 then existing strict validate_unit; no generic chmod API or blanket 0664 acceptance.
5. Create partial stages `PRE_REGISTRATION`, `REGISTERED_PERMISSION_INCOMPLETE`, `REGISTERED_UNIT_INCOMPLETE`, `REGISTERED_START_INCOMPLETE`, `REGISTERED_HEALTH_UNKNOWN`, `REGISTRATION_OUTCOME_UNKNOWN` and `CREATE_COMPLETE` survive simulated crash/restart and block ambiguous ordinary retry.
6. Config remove exit nonzero, timeout, lost marker or crash leaves directory and credentials; UNKNOWN state prohibits next Web Remote Remove.
7. Successful Remove proves external unregister response, Unit absent and exact directory absent. FD-relative recursive deletion never follows symlink or mountpoint, never touches sibling/archives; reject hardlinks, hostile type, path replacement and mount appearing during walk. Partial cleanup yields `local_cleanup_failed` plus durable terminal ledger; no second GitHub remove.
8. Incomplete AgentCI-style directory with no independently pre-existing identity ledger must stay visible, remain undeletable by normal flows and fail any candidate admin cleanup admission. A future authorized cleanup interface (not implemented here) requires exact ID, independent remote evidence, privileged approval and new verification.
9. Correct fixed Marker/Worker/Web messages for `permission_reconcile_failed` and all existing stages, exact FD/token isolation, no leaked credential/hash or unknown fallback bypass.
10. Full existing Create/Remove/Recover Local regressions, official v2.328.0 service template checks, no unsolicited change to other active Runners.
11. CI PASS, independent Implementation PASS and separate, expressly authorized Debian disposable-Runner acceptance of full Create → service → unregister → Unit/directory absence; no production runner mutation in automated unit tests.

No test may claim that a post-hoc content hash proves an old insecure file was never modified.

## 9. Bounded implementation scope and gates

Expected edits after SPEC PASS and separate implementation authorization: `scripts/register-runner.sh`, `scripts/remove-runner.sh`, `scripts/web-service-record.py`, narrowly `web/dispatcher.py` for exact Unit repair if audited, `web/lifecycle_worker.py`/`web/app.py` only for necessary fixed outcomes, `scripts/status-runners.sh` only if status accuracy changes, plus testing/fixtures and setup integration. No unrelated Runner tooling, no frozen SPEC rewrite, no production config/password change.

Gate A: the policy is already frozen: no silent historical 0664 repair without trustworthy pre-exposure independent evidence; no post-hoc attestation or ordinary UI override. SPEC audit must verify that this conservative rule and new trustworthy-creation ledger are complete and internally consistent.

Gate B: Implementation audit confirms exact FD operations, race behavior, writer-exclusion safety, ledger durability, mount-safe cleanup, partial-state semantics, full CI and Runner compatibility. Any unprovable historical auto-repair subcase must be disabled, not weakened.

Gate C: separate Debian deployment authorization and targeted test Runner owner confirmation, without modifying other active Runners.

```yaml
spec_status: DRAFT
baseline: fc8f6e2f16c366cba313819248579ff58df2b04b
trigger: HyperGrid-legacy-0664-and-AgentCI-1.1GB-residue
implementation: NOT_AUTHORIZED
debian_deployment: NOT_AUTHORIZED
real_runner_remove: NOT_AUTHORIZED
next_gate: INDEPENDENT_SPEC_DESIGN_AUDIT
```
