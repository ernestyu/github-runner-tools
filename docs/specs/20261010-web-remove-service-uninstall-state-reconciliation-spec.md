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

Runner identity naming mismatch for any version is a fixed `preflight_failed` identity error; no heuristic fallback, guessed suffix, or accepted unknown-version naming convention.\n\nThe current `validate_recovery_identity` for **Recover Local** deliberately requires `.runner` *absent* and trustworthy `.service` present. This SPEC **does not** change that entry, delete its identity anchor, or make missing `.service` sufficient for recovery. If both `.runner` and `.service` are absent, Recover Local must still refuse automatic cleanup: escalate to a separate, independently approved recovery design.

## 4. Two-layer state machine (S-01)

`U`: systemd state = VERIFIED_PRESENT / VERIFIED_ABSENT / UNKNOWN_OR_PARTIAL. Verified absent requires a fresh authenticated Dispatcher query, `LoadState=not-found` and no unit path, under existing provenance checks. `R`: local `.service` = MATCHING_REGULAR / ABSENT / INVALID_OR_UNSAFE. `G`: GitHub registration = KNOWN_REGISTERED / CONFIRMED_REMOVED / UNKNOWN, tracked independently; do not infer G from U or R.

| U | R | Normal Web Remove policy |
|---|---|---|
| VERIFIED_PRESENT, expected identity | MATCHING_REGULAR | stop, uninstall, verify U absent; then synchronize R |
| VERIFIED_ABSENT | MATCHING_REGULAR | revalidate `.runner` and record; safe idempotent record reconciliation |
| VERIFIED_PRESENT | ABSENT | Identity may be validated from configured `.runner`, but **no normal Web Remove may proceed to `config.sh`**: missing record implies unverifiable prior history; manual-only disposition (S-05) |
| VERIFIED_ABSENT | ABSENT | configured `.runner` validates target identity only; **deny normal Web Remove** and require manual recovery review, never infer G or execute `config.sh` |
| Any | INVALID_OR_UNSAFE (symlink, mismatch, wrong type/owner/mode, unreadable, malformed) | fail closed without file mutation or official config remove |
| UNKNOWN_OR_PARTIAL | any | fail closed: do not unlink service record; no official config remove |
| VERIFIED_PRESENT but stop/uninstall fails | MATCHING_REGULAR or ABSENT | preserve local state, report service failure; do not invoke config remove |

A Unit's absence does not mean GitHub deregistration. An idempotent return for U=absent/R=absent means **only** the local service-uninstall prerequisite is met. A timeout or ambiguous remote removal remains `G=UNKNOWN`, requiring manual GitHub/systemd/local review before *any* subsequent remote request.

## 5. Uninstall mechanism and authority (S-03)

**Selected architecture:** keep Dispatcher-driven `systemctl` stop/disable/Unit unlink/reload/absence verification, then perform narrowly scoped `.service` record reconciliation as runner-owner Worker, not root. This avoids the privilege expansion and unit-identity bypass risk of calling `sudo ./svc.sh uninstall` from Worker. `svc.sh uninstall` is the normal CLI pathway, but its implementation and privilege requirements vary by Runner version; current Web design must not grant it open-ended root execution. The SPEC does **not** claim official `svc.sh` and Dispatcher code are internally identical. Instead it mandates verification of their externally necessary postcondition (Unit absent **and** `.service` absent) with version-specific fixture tests.

The Dispatcher must not report `service_uninstall` success until its current verified Unit-absent requirement holds. A Worker must not reconcile R on a previous cached service status or merely a successful IPC reply: re-query the authoritative Dispatcher immediately before record mutation, and again before the first `config.sh remove`. If unit identity or state becomes unknown at any check, fail closed.

## 6. Safe record reconciliation contract (S-04) — FROZEN atomic quarantine strategy

**Chosen Linux strategy:** Worker uses descriptor-relative **atomic rename into a uniquely named quarantine entry**, then checks the *moved inode* before deleting anything. In particular, **do not directly `unlinkat(dirfd, ".service")`** after a separate identity check. This is an inode-confirmed post-rename deletion design, not a claim that advisory locks exclude same-UID processes.

**Threat model and trust boundary:** the Runner directory is writable by the runner-owner. Concurrent Runner scripts and unrelated same-UID processes may write to it, including in the interval between validation and the rename. The existing mutation lock coordinates only this application; it cannot exclude these processes. The design tolerates a `.service` replacement race by **never permanently deleting a moved object whose metadata/content/inode does not match the object verified before the atomic rename**. It cannot promise that a hostile same-UID process cannot temporarily disrupt the runner directory or move its own records; such interference triggers a fail-closed result. The security guarantee is bounded to operations inside the verified directory, no symlink traversal or cross-runner deletion, and no irreversible deletion of a substituted inode. A transient rename of a raced-in directory entry may occur; the specified rollback is mandatory and residual interference is an explicit failure, not success.

Required procedure, without discretionary alternatives:

1. Under the existing mutation lock, verify configured `.runner` identity independently, validate expected repository/runner Unit name, and verify systemd Unit absent through Dispatcher. Open the canonical Runner directory **by walking anchored directory FDs with `O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC`**; check owner/mode, allowed path, and `(st_dev, st_ino)` against the expected currently resolved canonical Runner path. The caller may not supply an arbitrary basename or quarantine directory. Reject directory symlink or unexpected ancestor. Re-check the path-to-open-FD inode immediately before and after the operation; any inconsistency is failure.
2. Using `open(".service", O_RDONLY|O_NOFOLLOW|O_CLOEXEC, dir_fd=runner_fd)`, `fstat`, `stat(..., follow_symlinks=False, dir_fd=runner_fd)` and a bounded exact-byte read, establish the approved record `(dev, ino, type, uid, gid, mode, nlink=1, size, content)`. It must be a regular file owned by runner UID, no group/other write, containing only the exact expected Unit name (single bounded line; optionally final LF). Reject symlink, hardlink, foreign owner, malformed content, or inode disagreement.
3. Revalidate authoritative Unit absence immediately before the rename. Generate a collision-resistant name using a fixed prefix `.grt-service-reconcile-` and an unpredictable suffix **within the same verified Runner directory**. The name is a temporary quarantine entry, never a credential snapshot. Invoke Linux `renameat2(runner_fd, ".service", runner_fd, quarantine_name, RENAME_NOREPLACE)`. `EXDEV`, collision, missing source, unsupported syscall, or any other failure ⇒ abort safely; no fallback to plain `unlink` or shell `mv`. Avoid selecting any existing entry or following symlinks.
4. After rename, `open` quarantine with `O_NOFOLLOW` and re-`fstat`, re-read and compare **exactly** against the pre-rename approved inode/device/type/UID/mode/nlink/content, and confirm the original `.service` pathname is absent. Also recheck canonical Runner directory identity. If all match and Unit remains verified absent, descriptor-relative `unlinkat` of the **quarantine name** is permitted *only after a fresh `lstat` comparison*. If another process substitutes the quarantine entry between check and unlink, this remains a race: therefore the quarantine directory entry MUST be protected from same-UID replacement during final deletion. Since the Worker cannot obtain such exclusivity in the writable Runner directory, **do not unlink the quarantine entry inside the writable Runner directory**.
5. **Selected final-deletion mechanism:** transfer only the checked quarantined record to a dedicated *root-owned* private staging directory with no write access for the runner UID, under a **fixed narrowly authorized Dispatcher operation**. Dispatcher must validate the source is exactly the authorized quarantine entry under the independently verified canonical Runner directory, use descriptor-relative `renameat2(..., RENAME_NOREPLACE)`, verify the moved inode/content against the approved record, and then unlink within its root-owned staging directory; any unverified inode is never unlinked. This is a narrowly scoped exception to §2's no-root-local-file-mutation preference and must not grant a generic delete/move API. **If the existing Dispatcher privilege boundary cannot be extended this narrowly and proven safe, implementation MUST NOT proceed: return SPEC for redesign.**
6. If the post-rename identity is wrong, use `renameat2(..., RENAME_NOREPLACE)` to restore the quarantined entry to `.service` **only if `.service` remains absent**. Never overwrite a competitor's new entry. If restore is impossible, retain the quarantined object, report `service_record_reconcile_failed`, and request manual inspection; never proceed to config remove. For interrupted execution, any `.grt-service-reconcile-*` residue is an **ambiguous state**; fail closed with manual inspection, do not silently remove or auto-restore it.
7. After success, independently verify `.service` absent, quarantine absent, directory identity unchanged, Unit absent, and `.runner`/`.credentials` still present and unchanged. Recheck no other Runner's directory/unit/record was accessed. Do not create credential copies or durable record backups. All open descriptors must be closed across success/failure.

**Important architecture gate:** §5's default non-root Worker owns *validation/coordination* only; the final deletion privilege exception described in step 5 is **not automatically authorized** by this SPEC draft. It deliberately exposes a conflict with the previously PASSed S-03 architecture. Independent design audit must decide whether to authorize the exact scoped staging action or require a different solution. There is no implicit permission for implementation to choose another strategy.

## 7. Re-entry and partial failures (S-05) — FROZEN no-history fail-closed policy

**Selected policy: no persistent remote-outcome history, hence no automated re-entry when service record is absent.** The system cannot distinguish a first-time missing `.service`, a previous confirmed failure, and a previous UNKNOWN result from `.runner` or a new Token. Therefore the normal Web Remove path **must refuse to invoke `config.sh remove` whenever `.service` is absent on entry**, regardless of U, G, a new token, or routine Prepare → Confirm. It may inspect states but must not stop/uninstall or mutate anything further in this branch. It presents a fixed `unknown_failed`/manual-review message; no UI checkbox, confirmation, retry token, or claim of previous failure overrides this gate. A subsequent request cannot regain eligibility simply because time has passed or the user logs in again.

Only when `.service` is present, safe and identity-matching and independently validated Unit state is known can normal Web Remove start a fresh operation. It must complete the U/R reconciliation and one official config removal. If config removal then fails or is uncertain, the state is intentionally no longer ordinary Web-removable: no automatic retry, preserved `.runner`/`.credentials`/directory, and manual disposition is required.

**Who can clear the gate:** not the Web operator through ordinary Web UI. A system administrator with host access must independently inspect GitHub Actions registration for the *exact* repository and runner ID/name; verify systemd Unit and unit file, current `.service` presence/absence, and `.runner`/`.credentials` presence without exposing secrets; decide whether GitHub is registered, already deregistered, or unknown. If GitHub still lists the Runner, administrator may initiate an **out-of-band** manually supervised official `config.sh remove` with a fresh token, after explicitly acknowledging remote state and documenting host-local result. If GitHub no longer lists it, administrator follows a separately authorized manual local cleanup procedure. If remote state remains unknown, no removal retry. **There is no Web bypass or in-memory override in this change.**

The external manual action may update/delete the local `.runner`/record as GitHub official CLI requires; this SPEC never gives Web automatic authority to rewrite missing `.service` or to treat an administrator's verbal assurance as persisted evidence. Recover Local retains its pre-existing `.runner`-absent/`.service`-present contract. Cases with both missing stay manual-only.

When `config.sh remove` succeeds conclusively, existing local cleanup may proceed. On every failed/unknown result, never delete the directory or credentials or call Recover Local automatically. High signal-like exits, timeouts, worker crashes, missing/legacy markers and any potentially partial remote side effect are treated as uncertain for next-entry purposes.

Partial failure stages remain:
- service stop failed → `service_stop_failed`, no R change.
- systemd uninstall incomplete / Unit unknown → `service_uninstall_failed`, no R change.
- unsafe record/rename/quarantine/final deletion error → `service_record_reconcile_failed`, no remote config call.
- config removal failed → existing `config_remove_failed` or `unknown_failed`, no automatic retry (now gated by R absent).
- local cleanup failure after confirmed registration removal → `local_cleanup_failed`; do not claim registration still active.

## 8. Diagnostics and protocol (S-06)

Introduce exactly one fixed diagnostic stage **`service_record_reconcile_failed`** to the remove terminal marker allowlist, Worker parser and Web fixed text mapping. The message must tell the operator that local service record synchronization failed and that GitHub removal has **not** been attempted in that invocation. Keep the existing marker format `GRT_REMOVE_RESULT_V1 stage=<stage> exit=<exit>`, maximum 128 ASCII bytes, private one-way pipe, exactly one terminal failure marker and strict parsing. Record sync errors have `exit=unknown` except where an actual bounded directly attributable exit code exists; never invent an exit status. No filenames, tokens, exception strings, or raw subprocess output in Web responses/logs.

Unknown/unparseable/legacy marker ⇒ existing fixed unknown fallback. Dispatcher JSON envelope and status codes remain unchanged, including HTTP 500 for failed Remove. No changes to Create or Recover Local error surfaces.

## 9. Implementation file scope, tests, and negative cases

Expected allowed implementation changes after separate authorization: `scripts/remove-runner.sh` (normal Web branch), `web/dispatcher.py` only for a separately reviewed strictly scoped staging transfer API plus verified-state response; no general-purpose path mutation, `web/lifecycle_worker.py` (stage allowlist), `web/app.py` (fixed message), and focused `tests/test-web-official-remove.py` / `tests/test-web-management.py` / tests for real filesystem races. **Do not** expand dispatcher privilege schema or alter Recover Local as a side effect.

Tests must use disposable runners and fake bounded systemd interface, with no actual GitHub action, production services, real token, or real runner deletion. Required cases:
1. U present + R matching → stop, uninstall, verify absent, record reconcile, one config remove, and success cleanup.
2. U absent + R matching → validated idempotent convergence; no redundant Unit mutation.
3. U present + R absent → refuse automated Remove (no stop/uninstall/config) despite metadata-derived identity.
4. U absent + R absent with `.runner` intact → deny Web re-entry; manual host-admin-only remote/local adjudication.
5. U absent + R absent with `.runner` absent → refuse normal Remove and Recover Local where identity cannot be established.
6. R symlink, hardlink, foreign UID, malformed/multiline bytes, wrong content, wrong owner/mode, directory replacement → fail closed; race an incorrect inode into the pre-rename source and post-rename quarantine: never unlink incorrect inode; restore without overwrite or retain residue for manual resolution.
7. Unit identity mismatch, wrong fragment/owner/User/WorkingDirectory/ExecStart, drop-in, unknown state and partial uninstall → no record unlink, no config remove.
8. Stop, disable, daemon-reload, final-state-query failure cases and duplicate Remove → correct stage and state preservation.
9. Atomic `RENAME_NOREPLACE` collision/unsupported syscall, staging transfer rejected, racing disappearance, substitution in quarantine, cleanup failure, un-restorable residue ⇒ fixed reconciliation error; registration assets untouched; verify private staging protection from same-UID processes.
10. Config remove ordinary exit 1/high exit/signal/hang/crash/ambiguous result ⇒ preserve directory and credentials, exactly one invocation; *next* Web Prepare → Confirm with new Token must still refuse because R is absent; verify no bypass after restart; separate verified-success path.
11. Verify sole permitted marker text, strict Worker allowlist, fixed Web text and HTTP status, no FD or token leak; existing B-01/B-02/B-03 regression.
12. Create and Recover Local behavior unchanged, including recovery dependency on `.service` and absence of `.runner`.
13. Fixture comparison of official Runner `svc.sh uninstall` side effects vs Dispatcher+reconciliation, with documented Runner version coverage; no assumptions about unexamined versions.
14. Test permission boundary for fixed root staging operation, rejection of all caller-chosen paths, staging ownership and same-UID adversarial replacement attempts; CI, rootless integration, then **separately authorized** isolated Debian host acceptance: fresh disposable runner, no production AgentCI or other current runner; test safe-stop, failure injection, real systemd state, local record postcondition, and log sanitization. Explicit rollback/stop points.

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
