# CLI Native Operational Logging — Implementation Closeout and Debian Acceptance Preparation

Status: **PREPARATION ONLY — DEPLOYMENT NOT AUTHORIZED**  
Repository: `ernestyu/github-runner-tools`  
Frozen SPEC commit: `e73f8fc8f782a67bacaf3660ee0a8768aa46ab95`  
Independent implementation audit: **PASS**, B-01/B-02/B-03 **CLOSED**, remaining blockers **0**  
Audited implementation HEAD: `43d10afc93524dbe964ad337b3783ed4d6cffdbe`  
Implementation PR: [#6](https://github.com/ernestyu/github-runner-tools/pull/6) — **Draft; merge not authorized**.

This document prepares a controlled acceptance, and grants **no permission** to run the updater, register/remove a real Runner, change a production Unit, merge PR #6, or alter root-owned lifecycle state. The deployment operator and independent auditor must approve a **specific immutable checkout SHA** after this document is committed. Never assume the audited implementation SHA is the final approved deployment SHA.

## 1. Closeout evidence and permitted scope

- Frozen SPEC stays unchanged. CLI-first architecture, fixed lifecycle authority, UNKNOWN preservation, and the exact `/var/log/github-runner-tools` Executor `ReadWritePaths` exception remain fixed.
- The independently audited implementation at `43d10afc93524dbe964ad337b3783ed4d6cffdbe` passed Development and Integration checks. Full Integration run: [38162408340](https://github.com/ernestyu/github-runner-tools/actions/runs/38162408340), at `7af6143bf46e2345e929a4a58711fc924f2117cf`. [Compare](https://github.com/ernestyu/github-runner-tools/compare/7af6143bf46e2345e929a4a58711fc924f2117cf...43d10afc93524dbe964ad337b3783ed4d6cffdbe): only the temporary Integration workflow branch trigger was reverted.
- Development checks at final audited implementation HEAD: [38162508651](https://github.com/ernestyu/github-runner-tools/actions/runs/38162508651), [38162512935](https://github.com/ernestyu/github-runner-tools/actions/runs/38162512935), both SUCCESS.
- Actual Debian acceptance is **PENDING**. CI does not establish real-host success.
- Authoritative closeout must record current PR HEAD, approved deployment SHA, operator, auditor, date, production baseline evidence identifiers, backup location, and each independent gate outcome. Approval must be explicit, not inferred from CI.

## 2. Hard acceptance boundaries

The host has **six production Runners**. Never stop, restart, register, remove, rename, reconfigure, or repoint these Runners. Never modify their systemd units, registration identities, services, archives, or root-owned lifecycle records. An Executor-only restart is permitted **only in a separately approved updater operation**, as required by the SPEC. No V1→V2 setup rerun.

The disposable test Runner(s) must be new identities for `ernestyu/github-runner-tools`, not names of existing or historical Runners. Reserve unique names such as `grt-accept-cli-<date>-<nonce>` and `grt-accept-web-<date>-<nonce>`; reconcile any leftover identity before reuse. Token acquisition is via GitHub's normal short-lived registration/removal flow; never put tokens into shell history, command arguments, environment variables, test reports, logs, or transcripts.

Any UNKNOWN lifecycle result, unexpected installed Unit, nonempty mutation lock, mismatched production inventory, failed rollback verification, or ambiguous remote registration triggers **STOP**, evidence preservation, and manual reconciliation; do not retry or auto-clean. A partial failure is not permission to force Remove. No destructive fault injection on the real host.

## 3. Gate A — read-only predeployment host baseline (no apply)

Capture with timestamps and secure evidence storage:

1. Confirm Debian 13, operator `actions` (not root), current Git SHA/clean checkout, expected root-installed CLI provenance, installed Web V2 Executor Unit exact content/permissions/mount allowlist, active Web and Executor, stopped old dispatcher if applicable, and presence/mode of shared mutation lock.
2. Read-only snapshot **all six** existing production Runner Unit names and unit-file digests, `is-enabled`/`is-active` states, installation directories, current local identities, and GitHub repository registration identities/IDs/status. Record both raw read-only inventories in protected evidence, compare counts and mappings; a unit-name-only hash is insufficient.
3. Read-only inspect authoritative lifecycle state for UNKNOWN or unfinished Create/Remove. Consult hashed state and prior records; do not infer completion from filenames. If anything is unresolved, halt.
4. Check free space and inode capacity, root-only backup location, log directory pre-existence and ownership/modes, sudo scope, socket existence/permissions, `ProtectSystem=strict`, `ReadWritePaths` exact known baseline, and safe availability of new disposable Runner names.
5. Capture baseline checksums and redact credentials and private paths as appropriate. Do not run `git pull` into a dirty or uncertain production checkout. Confirm the **approved full SHA**, not merely branch name.

**Gate A PASS:** operator and independent reviewer sign off on exact host and runner inventory, clean version match, reconciled state, backup capacity, and explicit change window/rollback authority. Otherwise STOP; no updater.

## 4. Gate B — authorized incremental already-V2 logger update

This section is **a runbook for later authorization**, not an instruction to execute now.

1. Confirm separate deployment approval identifying host, immutable SHA, exact `scripts/update-cli-operational-logging-v2.sh`, maintenance window, and who can call STOP/rollback.
2. From the clean pinned checkout, run `bash scripts/update-cli-operational-logging-v2.sh --dry-run`. Note: dry-run is intentionally non-mutating and **does not prove readiness**; independently complete Gate A.
3. After approval, from the `actions` account only, run `bash scripts/update-cli-operational-logging-v2.sh --apply --expected-sha <APPROVED_40_CHARACTER_SHA>`. No outer `sudo bash`. This updater is **only** for already-installed V2 and must not invoke `setup-web-management-v2.sh --apply`.
4. Record exit code, root-only backup path (without disclosing protected content), installed CLI checksums, exact Executor Unit delta (one added log `ReadWritePaths` directory), service status and effective systemd sandbox values. Confirm exactly **Executor** was restarted and Web plus six production Runner services were not restarted.
5. Verify socket/Status end-to-end via the real Executor-launched CLI and operational JSONL event. Direct `actions` log write alone does not prove the Executor's restricted mount namespace can write.
6. Reconcile production six-runner local and GitHub identities/states against Gate A. On error, do not blindly rerun: inspect `ROLLBACK_VERIFIED` versus `ROLLBACK_INCOMPLETE` and independently verify restored file contents, unit SHA, active socket/Web and six-runner identities. Preserve backups, logs and UNKNOWN states. STOP on any uncertainty.

**Gate B PASS:** update has fully verified real-host observability and unchanged production inventory. Only then proceed.

## 5. Gate C — CLI-first disposable Runner lifecycle

The CLI-first stage is a **supplementary isolation gate** requested by the operator, before the SPEC-mandated Web lifecycle stage.

1. Acquire an isolated disposable registration identity and the minimum necessary temporary token, from the test repository only. Verify unique name and absence of any conflicting local/GitHub entry.
2. Invoke the **installed** CLI registration path as `actions` using its documented interactive token handling, not an old checkout script. Record exit status, newly created identity, unit name, and authorized local state. Never infer success solely from a zero exit code.
3. Confirm GitHub registration identity and local Unit/service both refer to the **test** Runner. Use installed CLI Status (including `--json`) and inspect the JSONL sequence for Create/Status stages, result and safe failure reasons.
4. Run one harmless workload only on an **exactly targeted, uniquely labeled** test Runner, after checking that no existing production Runner matches the labels/routing. A job is not allowed to run on one of the original six.
5. Remove only the test Runner using the installed CLI's normal interactive removal-token flow. Confirm GitHub removal, Unit and local directory cleanup, authoritative state outcome, and Remove/Status log sequence.
6. Recompare all six production Runners to Gate A. Preserve evidence (redacted logs, checksums, stage chronology, return codes, GitHub and Unit proofs).
7. If the CLI operation fails or becomes UNKNOWN, **STOP** before Web acceptance. Classify the boundary from safe logs and authoritative state; no automatic retry.

**Gate C PASS:** newly created disposable CLI Runner fully registered, ran an isolated harmless job, was normally removed, and produced useful safe JSONL evidence without affecting the original six.

## 6. Gate D — SPEC-mandated Web / Executor disposable lifecycle

1. Create a **second** unique disposable Runner for the same test repository using Web V2's authorized Create flow. Validate user-visible error handling and Executor/CLI event correlation, without changing Web implementation.
2. Confirm GitHub identity, local installed files, test Runner Unit/service, root lifecycle state, and real Executor-launched CLI logging (under `ProtectSystem=strict` and the exact mount exception). Record only safe operation metadata, stage, reason and timestamp—not tokens or raw child output.
3. Dispatch one harmless workflow job to this **exclusive test identity** with a checked label/routing constraint. Check that the job ran only on the intended disposable Runner.
4. Remove only this disposable Runner through Web's normal Remove path, using a fresh removal token. Confirm actual GitHub absence, local Unit and directory cleanup, final authoritative state and CLI operational events.
5. Check that Web/Executor did not reinterpret lifecycle outcome or silently turn UNKNOWN into a success/failure. Test failure diagnosis from logs if there is a naturally occurring failure; do **not** force destructive errors on the live host.
6. Recompare all six original Runner identities, unit digests, registration, and active/enabled states. Failure or UNKNOWN means STOP and preserve evidence.

**Gate D PASS:** Web Create → Status → harmless job → Web Remove end to end, with Executor-mediated logging and invariant production inventory.

## 7. Log sufficiency evaluation

For **each** CLI and Web transaction, extract a sanitized operation chronology:

| Dimension | Required judgment |
| --- | --- |
| Correlation | Safe operation type, target test identity and distinct event sequence can be matched without leaking credentials |
| Stage | Entry, key lifecycle checkpoints and operation exit are intelligible; errors locate a meaningful stage |
| Reason | Stable, safe failure class when one exists; no fabricated diagnosis from discarded child output |
| Outcome | Distinguish success, failure before side effect, and ambiguous/UNKNOWN correctly |
| Evidence | Correlate JSONL with CLI exit code, installed Unit, GitHub status and authoritative state |
| Security | No token, Authorization header, raw untrusted subprocess output or secret-bearing argv |
| Storage | Expected owner/mode, no-follow integrity, 10 MiB single file, 100 MiB total, 14-day on-demand cleanup; rely on bounded test evidence, not destructive production fill-up |

Successful transactions demonstrate expected stage chronology; they **do not** alone prove failure logging is sufficient. For failure classes without safe live reproduction, cite the automated fault-injection evidence and record that no live failure was induced. The frozen SPEC, not this checklist, determines the logger's permitted fields.

## 8. Gate E — acceptance closeout and independent review

Acceptance record must include:

- Host identifier (restricted distribution), operator, approval reference, frozen SPEC, approved implementation SHA and comparison to PR #6, time window and CLI/software provenance.
- Before/after table for all six production Runner IDs/names/Unit digests, enabled/active states and GitHub registration; explicit unchanged conclusion supported by evidence.
- Gate A–D commands executed, start/finish, exit codes, expected versus actual filesystem and service effects, relevant redacted JSONL events and source evidence locations.
- Separate CLI and Web disposable Runner identities, GitHub registration/removal verification and exclusive harmless-job run IDs.
- Update backup location, rollback verification (if used), any residual state and operational log retention status.
- Outstanding unknowns and stop conditions. No approval inferred from CI or from Web returning HTTP success.
- Independent reviewer verdict: `DEBIAN_ACCEPTANCE_PASS` or `BLOCKED / REMEDIATION_REQUIRED`.

Only after explicit independent acceptance may anyone propose PR closeout/merge through a separate authorization. Nothing here grants that authorization.

## 9. Current decision and outstanding authorizations

**Implementation audit PASS / B-01 CLOSED.**  
**Implementation closeout evidence documented; deployment review not yet approved.**  
**Debian Gate A (read-only inventory): NOT EXECUTED.**  
**Debian updater / real Runner lifecycle: NOT EXECUTED.**  
**PR #6 merge: NOT AUTHORIZED.**

Next external action is independent review of this acceptance plan and an explicit decision whether to permit **read-only Gate A** on the specified Debian host. Approval for Gate A alone must not be interpreted as approval for Gate B (install) or C/D (Runner mutations).
