# Web Remove Service Uninstall State Reconciliation — SPEC

Status: **DRAFT — independent design audit required; implementation NOT authorized**  
Repository: `ernestyu/github-runner-tools`  
Baseline: `17e49752a7a97f239c5a6f6121c4a064e53d9765`  
Scope: Web **normal Remove only**, except explicitly identified non-regression checks.  
Authority: this SPEC is additive to the frozen 2026-10-09 Web Remove Official CLI SPEC; it does not edit or silently supersede that document.

## 1. Incident and contractual failure

Debian real-host Remove reached `config.sh remove --token` after successful Dispatcher systemd Unit removal, but GitHub Runner failed with `System.Exception: Uninstall service first` and exit code 1. Local `.service` still existed. GitHub runner was Offline; Unit was absent; `.runner` and `.credentials` remained. Moving `.service` outside the runner directory permitted manual completion. This is evidence for the missing local-service-record transition; it is **not** evidence that a Token error caused the failure.

The required ordering is: validate identity → confirm service stop/uninstall → confirm Unit absent → reconcile local `.service` → confirm record absent → invoke official `config.sh remove --token` **once** → delete directory only on positively confirmed successful registration removal.

## 2. Non-negotiable boundaries

1. No production deployment or actual Runner Remove within this work. No changes to frozen SPEC, Create, existing PTY adapter, Web auth/session, Dispatcher peer authentication, token transport, mutation lock, or timeout policy.
2. Root Dispatcher retains **only** the current narrowly authenticated systemd operations. No generic unlink command, arbitrary file path, open-ended sudo or execution of `svc.sh` as root.
3. Normal Remove remains a runner-owner Worker process. The worker may reconcile a local record only within its *independently validated* canonical Runner directory; it may not mutate Unit files.
4. Never unlink `.runner`, `.credentials`, registration data, or the entire runner directory as a response to a failed/unknown `config.sh remove`.
5. No background retries or automatic replays of remote removal on uncertain results. Preserve existing bounded diagnostic Pipe/token FD isolation, redaction, and single terminal marker.
6. Reconciliation means **local service-state cleanup only**; it does not assert or imply GitHub deregistration.
7. All validations and transitions are under the existing shared mutation lock, but this lock alone does not prevent an untrusted same-UID process from racing the filesystem. Use descriptor-relative filesystem checks rather than relying on the lock for local path safety.

## 3. Authoritative identity and scope (S-02)

Normal Remove requires a configured Runner: canonical expected owner+repo directory (or existing strictly verified legacy-directory option), a non-symlink regular `.runner` containing a *validated* exact repository URL and nonempty agent name, and the existing executable management scripts. The repository URL and agent name are cross-checked with the request's validated repository and the canonical `canonical_service_name(repository, agentName)` algorithm used by Dispatcher; no trust is placed in a caller-supplied service name alone.

The expected service identity is derived from independently verified `.runner` metadata **plus** validated repository and canonical directory scope. When `.service` exists it must contain *exactly* that expected full service name (single bounded line with optional trailing LF; reject embedded newline, CR, NUL, trailing junk, empty/oversized values). When it is absent, expected identity may be derived from the independently verified configured `.runner` and canonical algorithm. Do **not** invent a service name merely from the repository, a basename, or the client input. Verify that the actual Runner version produces the canonical naming convention before implementation; any incompatible runner identity must fail closed.

The current `validate_recovery_identity` for **Recover Local** deliberately requires `.runner` *absent* and trustworthy `.service` present. This SPEC **does not** change that entry, delete its identity anchor, or make missing `.service` sufficient for recovery. If both `.runner` and `.service` are absent, Recover Local must still refuse automatic cleanup: escalate to a separate, independently approved recovery design.

## 4. Two-layer state machine (S-01)

`U`: systemd state = VERIFIED_PRESENT / VERIFIED_ABSENT / UNKNOWN_OR_PARTIAL. Verified absent requires a fresh authenticated Dispatcher query, `LoadState=not-found` and no unit path, under existing provenance checks. `R`: local `.service` = MATCHING_REGULAR / ABSENT / INVALID_OR_UNSAFE. `G`: GitHub registration = KNOWN_REGISTERED / CONFIRMED_REMOVED / UNKNOWN, tracked independently; do not infer G from U or R.

| U | R | Normal Web Remove policy |
|---|---|---|
| VERIFIED_PRESENT, expected identity | MATCHING_REGULAR | stop, uninstall, verify U absent; then synchronize R |
| VERIFIED_ABSENT | MATCHING_REGULAR | revalidate `.runner` and record; safe idempotent record reconciliation |
| VERIFIED_PRESENT | ABSENT | derive identity from verified configured `.runner`; Dispatcher validates exact Unit; safely stop/uninstall; R stays absent |
| VERIFIED_ABSENT | ABSENT | configured `.runner` is mandatory authority; continue only if revalidated; never infer G |
| Any | INVALID_OR_UNSAFE (symlink, mismatch, wrong type/owner/mode, unreadable, malformed) | fail closed without file mutation or official config remove |
| UNKNOWN_OR_PARTIAL | any | fail closed: do not unlink service record; no official config remove |
| VERIFIED_PRESENT but stop/uninstall fails | MATCHING_REGULAR or ABSENT | preserve local state, report service failure; do not invoke config remove |

A Unit's absence does not mean GitHub deregistration. An idempotent return for U=absent/R=absent means **only** the local service-uninstall prerequisite is met. A timeout or ambiguous remote removal remains `G=UNKNOWN`, requiring manual GitHub/systemd/local review before *any* subsequent remote request.

## 5. Uninstall mechanism and authority (S-03)

**Selected architecture:** keep Dispatcher-driven `systemctl` stop/disable/Unit unlink/reload/absence verification, then perform narrowly scoped `.service` record reconciliation as runner-owner Worker, not root. This avoids the privilege expansion and unit-identity bypass risk of calling `sudo ./svc.sh uninstall` from Worker. `svc.sh uninstall` is the normal CLI pathway, but its implementation and privilege requirements vary by Runner version; current Web design must not grant it open-ended root execution. The SPEC does **not** claim official `svc.sh` and Dispatcher code are internally identical. Instead it mandates verification of their externally necessary postcondition (Unit absent **and** `.service` absent) with version-specific fixture tests.

The Dispatcher must not report `service_uninstall` success until its current verified Unit-absent requirement holds. A Worker must not reconcile R on a previous cached service status or merely a successful IPC reply: re-query the authoritative Dispatcher immediately before record mutation, and again before the first `config.sh remove`. If unit identity or state becomes unknown at any check, fail closed.

## 6. Safe record reconciliation contract (S-04)

Worker opens the already verified Runner directory via directory FD; verify canonical path, permitted directory layout, owner identity, non-symlink path components and stable directory inode. The configured metadata and expected service identity must be validated independently of `.service`. Under an existing lock, inspect `.service` *without following symlinks*, using `openat`/equivalent `dir_fd` and `O_NOFOLLOW|O_CLOEXEC`, `fstat` and bounded reads. Accept only a regular file owned by expected runner UID, with a restrictive mode (no group/other write); reject symlink, hardlink count > 1, malformed bytes, foreign owner, unexpected directory or identity, or file changed during validation. Use exact expected service text; prohibit path-supplied filenames.

Deletion must be descriptor-relative, limited to the literal basename `.service`. Immediately before unlink, compare `lstat(dir_fd)` with the opened inode/device/type/size/mtime, and compare freshly queried Dispatcher Unit-absent status. If any check fails, stop without unlink. After unlink, verify the directory entry is absent; any failure is a distinct record-reconciliation failure. Do not create service-record backups or new credential copies. Existing `.runner` and `.credentials` remain untouched on all failures.

**TOCTOU bound:** an attacker with write control over the Runner directory can replace the directory entry between `lstat` and `unlinkat` even with `dir_fd`. Implementation MUST prove an adequate exclusion mechanism (e.g., directory access invariant plus no concurrently running same-UID manipulator), or choose a stronger race-safe design; otherwise must refuse mutation. A mere pre-unlink pathname check is not an acceptable security proof. Tests must try swaps and symlink races. The Worker must not delete an object outside the verified directory even during adversarial changes.

## 7. Re-entry and partial failures (S-05)

A normal Remove may re-enter with `.runner` intact and `.service` absent after a confirmed unsuccessful `config.sh remove`. It must use validated configured `.runner` and a newly verified Unit-absent state, not demand `.service` to exist. After U/R preconditions are verified, it may proceed only upon a fresh explicit authenticated user confirmation with a new GitHub removal token **and after reviewing previous attempt status**. If prior result was uncertain (worker crash, timeout, high signal-style exit, missing marker, ambiguous 1 after partial remote effects), no automatic retry: show fixed manual-check instruction for GitHub, systemd and local files and require manual adjudication of remote status. The implementation must not pretend it has persistent authoritative `G` state if it does not; explicit operator verification is required.

When config removal definitely succeeds, permit existing successful local cleanup. When config removal fails or is uncertain, preserve Runner directory and all credentials; no implicit Recover Local. If `.runner` has already been deleted but local residue exists, normal Remove fails closed; Recover Local remains governed by its *existing* identity contract and may require manual handling if `.service` also absent.

Partial failure matrix:
- service stop failure ⇒ `service_stop_failed`, no R sync.
- service uninstall incomplete or state unknown ⇒ `service_uninstall_failed`, no R sync.
- verified Unit absent but unsafe/mismatched/local unlink failure ⇒ `service_record_reconcile_failed`, no config call.
- record verified absent but official config removal fails ⇒ `config_remove_failed` with trustworthy code or `unknown_failed` under existing uncertainty policy, registration assets retained.
- later local directory deletion failure ⇒ existing `local_cleanup_failed`, no false remote success claim.

## 8. Diagnostics and protocol (S-06)

Introduce exactly one fixed diagnostic stage **`service_record_reconcile_failed`** to the remove terminal marker allowlist, Worker parser and Web fixed text mapping. The message must tell the operator that local service record synchronization failed and that GitHub removal has **not** been attempted in that invocation. Keep the existing marker format `GRT_REMOVE_RESULT_V1 stage=<stage> exit=<exit>`, maximum 128 ASCII bytes, private one-way pipe, exactly one terminal failure marker and strict parsing. Record sync errors have `exit=unknown` except where an actual bounded directly attributable exit code exists; never invent an exit status. No filenames, tokens, exception strings, or raw subprocess output in Web responses/logs.

Unknown/unparseable/legacy marker ⇒ existing fixed unknown fallback. Dispatcher JSON envelope and status codes remain unchanged, including HTTP 500 for failed Remove. No changes to Create or Recover Local error surfaces.

## 9. Implementation file scope, tests, and negative cases

Expected allowed implementation changes after separate authorization: `scripts/remove-runner.sh` (normal Web branch), `web/dispatcher.py` only if a tightly specified additional verified-state response is essential, `web/lifecycle_worker.py` (stage allowlist), `web/app.py` (fixed message), and focused `tests/test-web-official-remove.py` / `tests/test-web-management.py` / tests for real filesystem races. **Do not** expand dispatcher privilege schema or alter Recover Local as a side effect.

Tests must use disposable runners and fake bounded systemd interface, with no actual GitHub action, production services, real token, or real runner deletion. Required cases:
1. U present + R matching → stop, uninstall, verify absent, record reconcile, one config remove, and success cleanup.
2. U absent + R matching → validated idempotent convergence; no redundant Unit mutation.
3. U present + R absent → metadata-derived strict Unit identity; stop/uninstall; one config remove.
4. U absent + R absent with `.runner` intact → controlled re-entry; no assumption remote unregistered.
5. U absent + R absent with `.runner` absent → refuse normal Remove and Recover Local where identity cannot be established.
6. R symlink, hardlink, foreign UID, malformed/multiline bytes, wrong content, wrong owner/mode, directory replacement, file-swapping race → fail closed; cannot mutate another Runner.
7. Unit identity mismatch, wrong fragment/owner/User/WorkingDirectory/ExecStart, drop-in, unknown state and partial uninstall → no record unlink, no config remove.
8. Stop, disable, daemon-reload, final-state-query failure cases and duplicate Remove → correct stage and state preservation.
9. Record unlink denial, racing disappearance, post-unlink verification failure → fixed reconciliation error; registration assets untouched.
10. Config remove ordinary exit 1/high exit/signal/hang/crash/ambiguous result ⇒ preserve directory and credentials, exactly one invocation, never implicit retry; separate verified-success path.
11. Verify sole permitted marker text, strict Worker allowlist, fixed Web text and HTTP status, no FD or token leak; existing B-01/B-02/B-03 regression.
12. Create and Recover Local behavior unchanged, including recovery dependency on `.service` and absence of `.runner`.
13. Fixture comparison of official Runner `svc.sh uninstall` side effects vs Dispatcher+reconciliation, with documented Runner version coverage; no assumptions about unexamined versions.
14. CI, rootless integration, then **separately authorized** isolated Debian host acceptance: fresh disposable runner, no production AgentCI or other current runner; test safe-stop, failure injection, real systemd state, local record postcondition, and log sanitization. Explicit rollback/stop points.

CI must be successful at the implementation HEAD, but CI success alone does not grant production authorization.

## 10. Acceptance criteria and release gates

SPEC audit must independently approve: authority for missing `.service`, reconciliation TOCTOU safety, treatment of partial remote outcomes, and strict diagnostic schema. Implementation may start only after SPEC PASS and explicit authorization. Implementation audit must verify code, actual subprocess/process-group behavior, file race tests, non-regressions and CI. An additional explicit deployment approval is needed before any Debian changes. A separate user confirmation is needed for any **real** Runner registration removal. Repeating production Remove is never a substitute for a failed test.

### Gate record

```yaml
spec_status: DRAFT
baseline: 17e49752a7a97f239c5a6f6121c4a064e53d9765
requirements: [S-01, S-02, S-03, S-04, S-05, S-06]
spec_audit: PENDING
implementation_authorization: NOT_GRANTED
production_deployment: NOT_AUTHORIZED
production_remove: NOT_AUTHORIZED
```
