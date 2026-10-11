# CLI Operational Diagnosability + Fresh Runner Service Bootstrap Remediation SPEC

Status: **DRAFT — AUDIT REQUIRED BEFORE IMPLEMENTATION**

Repository: `ernestyu/github-runner-tools`

Parent frozen logging SPEC: `e73f8fc8f782a67bacaf3660ee0a8768aa46ab95`

Live Debian evidence:
- CLI logger upgrade succeeded.
- CLI Create for `ernestyu/github-runner-tools` registered GitHub Runner `grt-accept-cli-20261011` successfully.
- Lifecycle reached `REGISTERED_UNIT_INCOMPLETE`.
- `service_install` failed; no Unit or `.service` was created.
- GitHub Runner remained registered but offline.
- `runsvc.sh` was absent in the freshly configured runner directory.
- Existing operational log recorded only coarse `service_install_failed`, requiring manual source/filesystem investigation.

## 1. Problem statement

The current frozen logging contract intentionally uses coarse fixed reason enums. Live acceptance demonstrates that this is insufficient for the operational goal of diagnosing a failed lifecycle transaction from the structured log without manual source-level reconstruction.

At the same time, live acceptance exposed a separate Create defect: the canonical custom Unit installer requires `<runner_dir>/runsvc.sh` to pre-exist, but the fresh registration path does not guarantee that file exists before the custom Unit install step. The official service-management flow creates/prepares the service wrapper during service installation. A custom Unit path must explicitly reproduce the required trusted runner service entrypoint preparation rather than assuming it already exists.

These are separate defects and both must be fixed before Debian acceptance restarts.

## 2. Regression origin and change-management constraint

Live comparison established that the previously working CLI path at `4189331db8dd9ec210dae5e077cb1e278bd323fd` still used:

```text
config.sh registration
→ sudo ./svc.sh install <runner-user>
→ authority verification
→ svc.sh start
```

Commit `d99ca3a61fdfe6d5a7c5cfa32f27d1a10c20a43f` changed interactive CLI Create to the shared canonical 0644 Unit installer. The later logging updater deployed the whole newer `register-runner.sh`, not only logging instrumentation. Thus a logging-focused incremental upgrade also activated a lifecycle behavior change that had not been proven by a real fresh-runner Create on Debian.

This remediation freezes the following rule:

> A logging-only or observability-focused incremental updater must not silently activate unrelated lifecycle behavior changes.

If an updater replaces lifecycle CLIs, it must prove either:

1. the replaced lifecycle behavior is byte/semantic-equivalent except for logging hooks; or
2. each lifecycle behavior change is explicitly listed in the updater scope and has its own fresh-environment executable acceptance coverage.

A successful unit/integration test against prepared fixtures is insufficient when the production path depends on artifacts created only during a real fresh registration/service bootstrap.

The remediation must therefore add a deployment-closure test that compares the installed CLI/service-bootstrap dependency set against the audited source set and exercises the real fresh-runner service bootstrap contract from post-extract/post-config state.

## 3. Goals


1. A trusted internal lifecycle failure must emit a safe fixed diagnostic code detailed enough to identify the failed internal invariant or operation without recording raw exception text, stdout/stderr, tokens, argv, environment, URLs, or subprocess output.
2. The final `operation_exit` record must preserve the last observed safe failure reason instead of replacing it with generic `unexpected_cli_failure`.
3. Fresh CLI/Web Create must prepare and validate the canonical `runsvc.sh` service entrypoint before installing a custom systemd Unit.
4. The currently failed disposable acceptance Runner must be recoverable through an explicit, bounded reconciliation path after the new implementation is deployed; no blind retry and no manual lifecycle-record deletion.
5. Six production Runners remain untouched.

## 4. Diagnostic contract extension

Keep JSON schema version 1 and existing keys. Extend the fixed `reason` vocabulary with trusted internal codes only:

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

No arbitrary strings are allowed. The logger must continue to reject any reason outside the frozen allowlist plus these additions.

Trusted authority helpers may communicate only a fixed reason code to the parent CLI. They must not pass exception messages or child output into persisted logs. The mechanism may use a documented bounded exit-code mapping or fixed machine-readable token on a dedicated trusted channel; it must not reuse untrusted subprocess stderr.

The event remains `service_install`; stage remains the actual lifecycle stage. Example:

```json
{"op":"create","event":"service_install","stage":"REGISTERED_UNIT_INCOMPLETE","result":"failed","reason":"runner_service_entrypoint_missing","exit_code":1}
```

The final `operation_exit` for this invocation must carry `runner_service_entrypoint_missing`, not `unexpected_cli_failure`.

## 5. Fresh runner service bootstrap

The custom canonical Unit path must not assume `runsvc.sh` exists after `config.sh`.

Implementation must:

1. derive the service entrypoint only from trusted files shipped in the verified GitHub Actions Runner archive;
2. prepare `runsvc.sh` in the runner directory with owner `actions:actions` (or the actual validated runner owner), regular-file/no-link checks, and executable mode;
3. verify the prepared entrypoint is the expected official service wrapper and is not caller supplied or replaced;
4. only then invoke the root-owned canonical Unit installer;
5. preserve the existing canonical root-owned 0644 Unit and no-overwrite semantics;
6. not run the upstream `svc.sh install` as an unrestricted root script merely to create the wrapper;
7. keep CLI and Web Create behavior equivalent.

If the required official wrapper source is absent or unsafe, fail before Unit publication with a specific fixed reason.

## 6. Incomplete acceptance Runner reconciliation

The live failed Runner is a disposable acceptance object:
- repository: `ernestyu/github-runner-tools`
- runner: `grt-accept-cli-20261011`
- GitHub runner ID observed: `22`
- create stage: `REGISTERED_UNIT_INCOMPLETE`
- registration attestation exists
- Unit and `.service` do not exist

Add or reuse a bounded recovery path that can reconcile exactly this class:

1. verify active create instance and instance-bound registration attestation;
2. verify stage is `REGISTERED_UNIT_INCOMPLETE`;
3. verify no systemd Unit and no `.service` record exist;
4. verify GitHub removal is explicitly authorized with a fresh removal token;
5. unregister the same attested GitHub runner through the official removal flow;
6. remove only the attested disposable runner directory;
7. write normal remove lifecycle completion records so the prior Create cycle is closed;
8. preserve operational logs and historical lifecycle evidence.

Do not permit generic forced cleanup, wildcard identity, inferred GitHub absence, or direct deletion of root lifecycle state.

## 7. Required tests

Add deterministic tests for:

- missing `runsvc.sh` source/preparation -> exact safe reason;
- invalid/non-regular/symlink service wrapper -> exact safe reason;
- Unit target exists;
- staging create/write/publish failure;
- daemon-reload failure;
- enable failure;
- final `operation_exit` preserves last safe reason;
- no raw exception/subprocess output reaches JSONL;
- successful fresh CLI Create creates valid wrapper + canonical Unit + active service;
- equivalent Web/Executor Create;
- reconciliation of `REGISTERED_UNIT_INCOMPLETE` with remote registration present and no Unit;
- reconciliation refuses wrong stage, wrong instance, missing attestation, existing unexpected Unit, or identity mismatch;
- six production Runner fixture remains invariant.
- regression test proving a logging-focused updater cannot introduce unlisted lifecycle semantic changes.
- fresh-runner E2E beginning from a clean extracted official Runner plus completed `config.sh` registration state, with no pre-created `runsvc.sh` fixture.

## 8. Debian acceptance restart

After implementation audit PASS:

1. deploy the corrected CLI/logger/authority components through the controlled updater;
2. use the bounded reconciliation path to remove the failed disposable Runner completely;
3. verify GitHub absence, local directory absence, remove-cycle completion, and six-production-Runner invariants;
4. create a new disposable CLI Runner from a fresh lifecycle cycle;
5. confirm the log alone identifies any failure with a safe specific reason;
6. if CLI Create succeeds, run the isolated harmless job and normal CLI Remove;
7. only then proceed to Web Create/Job/Remove acceptance.

## 9. Non-authorizations

This SPEC does not authorize:
- implementation before independent SPEC audit;
- manual deletion of the current failed Runner directory or lifecycle records;
- changes to the six production Runner units/registrations;
- PR #6 merge;
- Web core authority redesign;
- raw stderr/traceback logging;
- Debian deployment of unreviewed code.
