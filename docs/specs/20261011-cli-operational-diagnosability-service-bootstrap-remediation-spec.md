# CLI Operational Diagnosability + Fresh Runner Service Bootstrap Remediation SPEC

Status: **DRAFT — TARGETED RE-AUDIT REQUIRED BEFORE IMPLEMENTATION**

Repository: `ernestyu/github-runner-tools`

Parent frozen logging SPEC: `e73f8fc8f782a67bacaf3660ee0a8768aa46ab95`

Supersedes remediation draft: `718d8b221965c0e1683c21a20cf80166ce2ed838`

This SPEC is remediation-only. It does not amend the parent frozen Logging SPEC retroactively. It defines a separately audited extension required by live Debian acceptance.

## 1. Live evidence and problem statement

Live Debian acceptance established all of the following:

- CLI logger upgrade completed successfully.
- CLI Create for `ernestyu/github-runner-tools` registered `grt-accept-cli-20261011` remotely.
- GitHub Runner ID is `22`.
- The instance-bound registration attestation exists and records Runner ID `22`.
- Create state is `REGISTERED_UNIT_INCOMPLETE`.
- No canonical systemd Unit exists.
- No local `.service` record exists.
- Root-level `runsvc.sh` does not exist.
- `bin/runsvc.sh` is part of the official Runner layout source and the official Linux service installer is responsible for copying it to the runner root.
- Operational JSONL identified `service_install` at `REGISTERED_UNIT_INCOMPLETE`, but only recorded coarse `service_install_failed`, requiring manual source and filesystem reconstruction.

The current failure must remain untouched until a separately audited implementation provides a bounded reconciliation path. No manual deletion of GitHub Runner ID 22, the runner directory, active instance, Create ledger, attestation, or any other lifecycle evidence is authorized.

## 2. Regression origin and updater boundary

The previously working CLI path at `4189331db8dd9ec210dae5e077cb1e278bd323fd` used:

```text
config.sh registration
→ sudo ./svc.sh install <runner-user>
→ authority verification
→ svc.sh start
```

Commit `d99ca3a61fdfe6d5a7c5cfa32f27d1a10c20a43f` changed interactive CLI Create to the shared canonical 0644 Unit installer. The later logging updater deployed the whole newer `register-runner.sh`, not only logger instrumentation. Therefore an observability-focused update also activated a lifecycle behavior change that had not been proven by a fresh Runner E2E on Debian.

Frozen remediation rule:

> A logging-only or observability-focused updater must not silently activate unrelated lifecycle semantic changes.

If an updater replaces lifecycle CLIs, it must prove either:

1. lifecycle behavior is byte/semantic-equivalent except for logging hooks; or
2. every lifecycle behavior change is explicitly listed in updater scope and has executable fresh-environment coverage.

Prepared fixtures are insufficient when production behavior depends on artifacts created only during actual fresh Runner bootstrap.

## 3. B-01 — bounded incomplete-Create reconciliation

### 3.1 Why ordinary Remove rejects the live state

Current `runner_lifecycle_authority.remove_stage(..., "BEGIN")` admits ordinary Remove only when the active Create record is:

- `CREATE_COMPLETE`; or
- `LEGACY_VERIFIED`.

Current code otherwise raises `AuthorityError("creation not complete")`.

Therefore a correctly preserved `REGISTERED_UNIT_INCOMPLETE` instance cannot enter ordinary Remove. This is intentional fail-closed behavior and must not be weakened globally.

### 3.2 Dedicated reconciliation admission

Implementation must add a separate, explicitly named incomplete-Create reconciliation authority path. It must not make ordinary `remove_stage(BEGIN)` accept all incomplete Create stages.

Admission is allowed only when **all** conditions hold atomically under the existing mutation lock:

1. repository, runner name and canonical runner directory are exact validated values;
2. active instance exists and its instance ID matches the Create record;
3. Create stage is exactly `REGISTERED_UNIT_INCOMPLETE`;
4. instance-bound registration attestation exists;
5. attestation repository, runner, runner directory, instance, directory device/inode and GitHub Runner ID all match the active Create cycle;
6. current `.runner` is a regular non-symlink file with the expected owner/mode and matches the attested digest and identity;
7. `.runner.agentId` equals the attested GitHub Runner ID;
8. current `.credentials` is a regular non-symlink file with the expected owner/mode and matches its attested digest;
9. runner directory device/inode still equals the Create/attestation device/inode;
10. canonical systemd Unit is absent, including symlink/non-regular aliases;
11. local `.service` is absent, including symlink/non-regular aliases;
12. no prior Remove ledger exists for the same instance;
13. no unit attestation exists for the same instance;
14. the operation uses a fresh explicit GitHub removal token supplied through the existing secret input contract.

Any mismatch is terminal for this reconciliation attempt: preserve all state and require administrator review.

### 3.3 Durable Remove intent before remote mutation

Before invoking official `config.sh remove`, the reconciliation authority must create the **same-instance** Remove ledger with:

- schema compatible with existing Remove records;
- repository, runner, runner_dir, directory device/inode and `github_runner_id` copied only from verified authority/attestation state;
- stage `REMOTE_REMOVE_OUTCOME_UNKNOWN`;
- fixed marker `origin: "incomplete-create-reconciliation"`;
- fixed `create_stage_at_begin: "REGISTERED_UNIT_INCOMPLETE"`.

This is the durable Remove intent. It must be persisted before the external unregister call.

No field may be derived from arbitrary caller text once authority identity has been established.

### 3.4 Remote unregister and UNKNOWN handling

Use the official Runner `config.sh remove` flow with a fresh one-shot removal token.

Outcomes:

- **definitive success (exit 0):** transition the same Remove ledger using existing `CONFIRM_REMOTE_REMOVED` semantics to `REGISTERED_REMOVED_LOCAL_CLEANUP_PENDING`;
- **nonzero, timeout, signal, transport ambiguity, or killed process:** leave Remove ledger at `REMOTE_REMOVE_OUTCOME_UNKNOWN`, preserve runner directory, credentials, Create state and attestation, emit `remote_result_uncertain`, and stop;
- no retry is automatically authorized from UNKNOWN;
- GitHub API absence alone must not silently convert UNKNOWN to confirmed removal.

### 3.5 Local cleanup after confirmed remote removal

Only after durable `REGISTERED_REMOVED_LOCAL_CLEANUP_PENDING`:

1. revalidate the pinned directory device/inode;
2. revalidate no Unit and no `.service` appeared;
3. remove only the exact attested runner directory through the bounded cleanup path;
4. verify the exact path is absent;
5. transition Remove ledger to `REMOVE_COMPLETE`.

No service stop/uninstall is performed for this reconciliation class because admission requires Unit and `.service` absence. Appearance of either object at any later checkpoint aborts cleanup.

### 3.6 Terminal Create reconciliation state

A failed Create must **never** be rewritten as `CREATE_COMPLETE`.

After and only after `REMOVE_COMPLETE` is durably confirmed for the same instance, authority must transition the Create record to a distinct terminal state:

`CREATE_ABORTED_RECONCILED`

and preserve:

- `reconciled_from_stage: "REGISTERED_UNIT_INCOMPLETE"`;
- same instance ID;
- completion timestamp.

Future `PRE_REGISTRATION` admission may treat the prior cycle as closed only when:

- prior Create stage is `CREATE_ABORTED_RECONCILED`; and
- same-instance Remove stage is `REMOVE_COMPLETE`.

This creates a new instance for a later fresh Create. It does not reuse or erase the failed instance.

Historical attestation, Create and Remove ledgers remain root-owned evidence and are not deleted.

## 4. B-02 — Official Runner wrapper bootstrap

### 4.1 Authoritative upstream source evidence

The relevant official GitHub Actions Runner source was checked at both the previous pinned family and the live acceptance version.

#### actions/runner v2.328.0

- `src/Misc/layoutbin/systemd.svc.sh.template`
  - Git blob: `8429854293708c0657822e5625cb7f2b61903871`
  - official `install()` copies `./bin/runsvc.sh` to `./runsvc.sh`;
  - then `chown <run_as_uid>:<run_as_gid> ./runsvc.sh`;
  - then `chmod 755 ./runsvc.sh`;
  - then enables the service;
  - then writes `.service`.
- `src/Misc/layoutbin/runsvc.sh`
  - Git blob: `63d1b62e1a6012792a01085f9ca42cd60747aed9`
  - service wrapper uses `node20`.
- `src/Misc/layoutbin/actions.runner.service.template`
  - Git blob: `4dcec7e368c17b9db6a3ce69790b8b2f09b9f846`
  - `ExecStart={{RunnerRoot}}/runsvc.sh`.

#### actions/runner v2.338.0

- `src/Misc/layoutbin/systemd.svc.sh.template`
  - Git blob: `8429854293708c0657822e5625cb7f2b61903871`
  - same service-install wrapper copy/chown/chmod sequence.
- `src/Misc/layoutbin/runsvc.sh`
  - Git blob: `35e44227cb85a2877f27ab81fc5c81d15a4e4c78`
  - service wrapper uses `node24`.
- `src/Misc/layoutbin/actions.runner.service.template`
  - Git blob: `5509aa4a7b77680525147e672ea5ace89f6bf857`
  - `ExecStart={{RunnerRoot}}/runsvc.sh`.

Source URLs:

- https://github.com/actions/runner/blob/v2.338.0/src/Misc/layoutbin/systemd.svc.sh.template
- https://github.com/actions/runner/blob/v2.338.0/src/Misc/layoutbin/runsvc.sh
- https://github.com/actions/runner/blob/v2.338.0/src/Misc/layoutbin/actions.runner.service.template
- https://github.com/actions/runner/blob/v2.328.0/src/Misc/layoutbin/systemd.svc.sh.template
- https://github.com/actions/runner/blob/v2.328.0/src/Misc/layoutbin/runsvc.sh
- https://github.com/actions/runner/blob/v2.328.0/src/Misc/layoutbin/actions.runner.service.template

Conclusion frozen by this SPEC:

> Root-level `runsvc.sh` is not a safe precondition to assume after `config.sh`. The official Linux service install path explicitly prepares it by copying the verified archive's `bin/runsvc.sh` into the runner root.

### 4.2 Trusted wrapper source

The only permitted source for bootstrap is:

`<runner_dir>/bin/runsvc.sh`

from the same official Runner archive already downloaded and integrity-checked by Create.

The implementation must not:

- synthesize a new wrapper from a hard-coded local template;
- download another wrapper separately;
- copy from another installed Runner;
- use the checkout repository as wrapper source;
- trust a caller-supplied path.

### 4.3 Source validation before copy

Before preparing root-level `runsvc.sh`, trusted code must establish:

1. active Create instance and current stage are exactly the expected same cycle;
2. Runner version equals the version stored in the instance-bound registration attestation;
3. `<runner_dir>/bin/runsvc.sh` exists;
4. every path component is opened without following symlinks;
5. source is a regular file, link count 1, owned by the validated Runner uid/gid;
6. source is not group/world writable;
7. source size is nonzero and bounded;
8. source has executable owner bit;
9. source content is read from the already verified archive extraction, not external output.

Because the release archive itself is already verified by the Create download/digest gate when GitHub release metadata supplies a SHA-256 digest, no separate network trust channel is introduced. The wrapper copy must additionally compute a local SHA-256 over the validated source and require the destination bytes to match exactly.

If release metadata did not provide a digest and the existing Create path therefore records the archive digest check as skipped, wrapper bootstrap must fail closed for the canonical custom Unit path unless implementation can prove equivalent trusted provenance under an independently audited rule. This remediation does not authorize silently promoting an unverified wrapper to root service execution.

### 4.4 Destination preparation

For a fresh cycle the destination is fixed:

`<runner_dir>/runsvc.sh`

Rules:

1. destination must initially be absent; symlink or any existing object is a reconciliation error, not overwritten;
2. create a private temporary regular file inside the already verified runner directory using no-follow/exclusive semantics;
3. copy bytes from the validated source descriptor;
4. `fsync` file;
5. set owner to validated Runner uid/gid;
6. set mode exactly `0755`;
7. compare destination SHA-256 with the validated source SHA-256;
8. revalidate source and destination inode/path identity against substitution;
9. publish to `runsvc.sh` atomically without overwriting any object that appeared concurrently;
10. `fsync` the directory.

Only after successful wrapper publication may canonical Unit installation begin.

Failure before wrapper publication must not create a Unit or `.service`.

### 4.5 Canonical Unit ordering

Fresh Create ordering is frozen as:

```text
verified official archive
→ config.sh registration
→ registration permission normalization
→ instance-bound registration attestation
→ REGISTERED_UNIT_INCOMPLETE
→ trusted bin/runsvc.sh validation
→ trusted root-level runsvc.sh bootstrap
→ canonical 0644 Unit publication
→ daemon-reload
→ enable
→ .service record
→ unit attestation
→ REGISTERED_START_INCOMPLETE
→ service start
→ health verification
→ CREATE_COMPLETE
```

CLI and Web/Executor modes must share the same trusted wrapper-bootstrap implementation and the same canonical Unit semantics.

Do not execute upstream `svc.sh install` as an unrestricted root workaround.

### 4.6 Full fresh-runner E2E

Required E2E begins with:

1. clean official Runner archive for the tested version;
2. archive extracted normally;
3. no root-level `runsvc.sh`;
4. no `.service`;
5. no systemd Unit;
6. successful `config.sh` registration or a faithful isolated official-registration fixture that produces the exact post-config file layout;
7. test must assert root-level `runsvc.sh` is absent immediately before trusted bootstrap.

Then exercise the production bootstrap implementation through Unit install/start/health. A fixture that pre-creates root-level `runsvc.sh` is invalid.

At minimum test both `v2.328.0` semantics and current acceptance `v2.338.0` semantics, including the different official wrapper content/node version.

## 5. B-03 — Safe Trusted Authority → CLI reason transport

### 5.1 Chosen transport: reserved exit-code mapping

The transport is frozen as **reserved process exit codes from trusted root authority to its direct parent CLI**.

No diagnostic reason is parsed from stderr/stdout.

Authority may still print a generic human-safe console sentence, but CLI logging must derive the structured reason only from the reserved exit code.

This channel is valid only for fixed installed trusted authority executables invoked directly by the CLI. It must never be applied to GitHub `config.sh`, `svc.sh`, `systemctl`, curl, tar, or any other external/untrusted child.

### 5.2 Fixed mapping

Reserve:

| Authority exit | Structured reason |
| --- | --- |
| 64 | `runner_service_entrypoint_missing` |
| 65 | `runner_service_entrypoint_invalid` |
| 66 | `unit_target_exists` |
| 67 | `unit_stage_create_failed` |
| 68 | `unit_publish_failed` |
| 69 | `unit_daemon_reload_failed` |
| 70 | `unit_enable_failed` |
| 71 | `authority_state_rejected` |
| 72 | `authority_identity_rejected` |
| 73 | `authority_install_failed` |

Exit `0` means success.

Any other nonzero trusted-authority exit, malformed invocation, signal status, timeout, or unavailable mapping is logged with fixed fallback:

`authority_install_failed`

for the `service_install` event, or the existing coarse fixed event-specific reason for other authority events.

Do not persist the numeric child exit as arbitrary diagnosis beyond the existing bounded CLI `exit_code` field semantics.

### 5.3 Authority classification boundary

Trusted authority implementation must classify only errors it creates or validates itself.

Examples:

- missing validated `bin/runsvc.sh` → 64;
- unsafe/symlink/non-regular wrapper → 65;
- canonical Unit target already exists → 66;
- trusted staging file create/write/fsync failure → 67;
- no-overwrite publish failure → 68;
- bounded direct `systemctl daemon-reload` failure after Unit publication → 69;
- bounded direct `systemctl enable` failure → 70;
- lifecycle stage/ledger invariant rejection → 71;
- repository/runner/path/attestation/inode identity rejection → 72;
- otherwise classified internal install failure → 73.

Raw Python exception text, traceback, errno string, command line, subprocess stdout/stderr, environment, token, HTTP/API response and arbitrary path content must not cross into the logger.

### 5.4 CLI logging semantics

CLI maintains a per-invocation fixed variable representing the last observed safe reason.

Rules:

1. `event/begin` does not clear the last confirmed failure reason;
2. a failed trusted operation sets the mapped safe reason before invoking common failure handling;
3. the failed event logs that exact reason;
4. final `operation_exit` uses the same last safe reason;
5. `operation_exit` must not replace it with `unexpected_cli_failure`;
6. when no specific safe reason was established, use the existing fixed fallback appropriate to the current event;
7. UNKNOWN remote outcomes remain `remote_result_uncertain` and are never narrowed using this trusted-authority channel.

Example required result for the live defect class:

```json
{"op":"create","event":"service_install","stage":"REGISTERED_UNIT_INCOMPLETE","result":"failed","reason":"runner_service_entrypoint_missing","exit_code":1}
{"op":"create","event":"operation_exit","stage":"REGISTERED_UNIT_INCOMPLETE","result":"failed","reason":"runner_service_entrypoint_missing","exit_code":1}
```

The CLI's own final exit remains its existing lifecycle-facing value; authority's reserved child exit is an internal classification channel and must not change public CLI exit semantics.

## 6. Logger allowlist extension

Keep JSON schema version `1` and existing keys.

Extend fixed `reason` vocabulary only with:

- `runner_service_entrypoint_missing`
- `runner_service_entrypoint_invalid`
- `unit_target_exists`
- `unit_stage_create_failed`
- `unit_publish_failed`
- `unit_daemon_reload_failed`
- `unit_enable_failed`
- `authority_state_rejected`
- `authority_identity_rejected`
- `authority_install_failed`

No arbitrary reason/message field is introduced.

## 7. Required tests

### B-01 reconciliation

- ordinary `remove_stage(BEGIN)` still rejects `REGISTERED_UNIT_INCOMPLETE`;
- dedicated reconciliation admits only the exact state/identity/attestation/no-Unit/no-`.service` class;
- wrong stage, wrong instance, wrong ID, attestation digest mismatch, changed inode, missing credential, existing Unit, existing `.service`, prior Remove ledger or unit attestation all fail closed;
- durable `REMOTE_REMOVE_OUTCOME_UNKNOWN` exists before external unregister;
- unregister uncertainty preserves all local evidence and does not clean;
- definitive unregister success → confirmation → exact directory cleanup → `REMOVE_COMPLETE`;
- Create becomes `CREATE_ABORTED_RECONCILED`, never `CREATE_COMPLETE`;
- new cycle allowed only after matching `REMOVE_COMPLETE`;
- historical failed-cycle evidence remains.

### B-02 wrapper/bootstrap

- official v2.328.0 and v2.338.0 wrapper sources represented by pinned test fixtures derived from the cited upstream blobs;
- clean extracted/post-config state has no root-level `runsvc.sh`;
- trusted bootstrap copies only `bin/runsvc.sh`;
- destination byte digest equals source;
- exact uid/gid and 0755 mode;
- source/destination symlink, hardlink, ownership, writable mode, empty/oversize, substitution and race cases fail closed;
- archive provenance/digest-unavailable case follows the fail-closed rule;
- Unit cannot publish before wrapper verification;
- fresh CLI Create E2E and equivalent Web/Executor Create E2E reach active service without a prebuilt root wrapper;
- no production Runner fixture changes.

### B-03 diagnostics

- every reserved authority exit maps to exactly one allowed reason;
- arbitrary exit maps to fixed safe fallback;
- stderr/stdout injection cannot affect structured reason;
- raw exceptions, errno text, traceback, token, environment and subprocess response never appear in JSONL;
- event failure and `operation_exit` preserve the same specific reason;
- remote uncertainty remains coarse `remote_result_uncertain`;
- public CLI exit semantics remain unchanged.

### Updater regression boundary

- logging-focused updater cannot introduce unlisted lifecycle semantic changes;
- installed CLI + trusted authority + wrapper-bootstrap helper dependency closure is checked against audited source;
- updater rollback covers every newly installed trusted dependency;
- fresh-runner E2E is part of lifecycle behavior-change acceptance, not replaced by prepared fixture tests.

## 8. Debian acceptance restart after implementation audit PASS

Only after this remediation implementation is independently audited:

1. deploy corrected CLI/logger/authority/bootstrap components through the controlled updater;
2. verify installed dependency closure and rollback backup;
3. use dedicated incomplete-Create reconciliation on the preserved Runner ID 22;
4. confirm GitHub absence, exact local directory absence, `REMOVE_COMPLETE`, `CREATE_ABORTED_RECONCILED`, and unchanged six production Runners;
5. start a fresh lifecycle cycle with a new disposable Runner identity;
6. evaluate failure diagnosis first from structured operational JSONL;
7. if Create succeeds, run the uniquely targeted harmless job;
8. perform normal CLI Remove and verify logs;
9. only after CLI acceptance passes proceed to Web Create/Job/Remove.

Acceptance criterion for diagnosability:

> For any trusted internal failure represented by the fixed reason vocabulary, an operator must be able to identify the failed internal condition from structured operational logs without reading source code, raw child stderr, or reconstructing the filesystem manually.

## 9. Non-authorizations

This SPEC does not authorize:

- implementation before targeted independent SPEC PASS;
- modifications to parent frozen SPEC `e73f8fc...`;
- manual deletion or mutation of live Runner ID 22 or its lifecycle evidence;
- retry of the failed Create;
- operation on the six production Runners;
- PR #6 merge;
- Debian deployment;
- raw stderr/stdout/traceback logging;
- generic force-remove or force-complete behavior.

## 10. Targeted re-audit questions

Auditor should decide only:

- **B-01:** Is incomplete-Create reconciliation bounded, same-instance/remote-ID bound, durable-before-remote, UNKNOWN-preserving, and non-forging?
- **B-02:** Is official wrapper provenance and bootstrap behavior source-grounded and safely reproducible without unrestricted `svc.sh install`?
- **B-03:** Is reserved trusted-authority exit-code transport fixed, non-spoofable by untrusted children, safe-fallback bounded, and correctly preserved through `operation_exit`?

Required verdict:

`PASS — FREEZE REMEDIATION SPEC`

or

`BLOCKED — REVISE B-01/B-02/B-03`

No Implementation authorization exists before PASS.
