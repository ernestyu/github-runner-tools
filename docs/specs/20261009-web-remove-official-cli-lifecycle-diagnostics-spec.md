# Web Remove — Official CLI and Lifecycle Diagnostics SPEC

Status: PROPOSED — independent SPEC audit required; implementation NOT AUTHORIZED.
Date: 2026-10-09
Repository: `ernestyu/github-runner-tools`
Frozen baseline candidate: `e64bf38a959ae19b4e6d932d00b2c420f1bc22ac`
Scope: Web UI **Remove** only, for already-configured GitHub Actions runners.

## 1. Problem and decision

The Web UI Remove flow currently runs `remove-runner.sh --web-worker`, which stops/uninstalls the runner service and then invokes `pty_token_adapter.py --mode remove -- ./config.sh remove`. This relies on detecting a token prompt in PTY output. The normal CLI path instead uses the GitHub Runner-supported `./config.sh remove --token "$TOKEN"`. On the Debian production host, an authenticated Web Remove reached `dispatch_invoked` and returned HTTP 500 / `Runner operation failed`; the local AgentCI runner remained configured with its systemd service active. The observed failure stage is **unknown**; PTY incompatibility is a hypothesis, not an established root cause.

**Product decision:** In Web Remove, invoke the installed Runner's official command `./config.sh remove --token "$TOKEN"`. Do not simulate a prompt or require PTY for removal. Explicitly accept temporary removal-token exposure in the local `config.sh` process argv as an operator-approved tradeoff for this private Debian deployment. Do not state that the token is intrinsically harmless; before successful removal, a local observer might consume it, and process-commandline collectors may retain it. The temporary GitHub removal token is distinct from the Web's five-minute confirmation nonce.

## 2. Scope and frozen exclusions

Allowed implementation files, subject to SPEC audit: `scripts/remove-runner.sh` (Web remove command and minimal fixed-stage reporting), `web/lifecycle_worker.py` (allowlisted stage/exit-code propagation), `web/app.py` (safe response mapping only if needed), and targeted Web/CLI tests plus `tests/run-all.sh` only if registration is necessary. `web/dispatcher.py` may be touched **only** if the audit proves a narrow, essential forwarding change; that change requires a separate scoped approval. Do not modify `web/pty_token_adapter.py` or the Create code path; the adapter remains available for Create. No changes to GitHub runner identity rules, registration, Recover Local semantics, Tailscale, units, privileged protocol, privilege dropping, locking, timeout policy, state schema, confirmation nonce/CSRF/session semantics, or archive policy. Do not modify the existing Gate A diagnostic implementation. No production deployment or real runner removal is authorized by approval of this SPEC.

The only intended lifecycle semantic change is replacing the Web Remove PTY-based invocation with the standard GitHub CLI invocation. Keep the existing preflight → service stop → service uninstall → GitHub unregistration → verified local directory deletion order. Do not introduce retries, force removal, silent fallback, a new GitHub API path, or re-registration.

## 3. Official command execution and secret boundary

Within the existing authenticated Web → Dispatcher → identity-dropped Worker → `remove-runner.sh --web-worker` chain:

1. Preserve existing repository validation, strict runner/service identity checks, mutation lock, dispatcher-owned privileged systemd operations, and Web token FD transport up to the script.
2. For Web Remove only, read **one** temporary token from the inherited `--token-fd` in the non-root `remove-runner.sh` execution context, using a bounded single-line read and existing maximum length/format validation without echo. Reject missing/empty/oversized/malformed input before invocation. The token must not be read twice or reused from a previously consumed FD. The implementer must document exact validation equivalence to the current Web entry and avoid inventing narrower token syntax.
3. Execute the runner-installed binary from the already-validated runner directory as `./config.sh remove --token "$TOKEN"`, passing the token as a **single argv value** (no `eval`, `sh -c`, shell interpolation in a command string, or logging of the expanded command). Invoke as the runner owner, never as root. Retain the existing Worker/privileged dispatcher boundaries.
4. Read the token at the latest practical point (after existing preflight and service operations); avoid placing it in exported environment variables, log messages, persistent files or response bodies. Ensure existing inherited FD and token-bearing shell variables are not forwarded to unrelated subprocesses beyond what is necessary for this invocation; clear the shell token after the invocation on success and failure paths. For clarity: cleaning shell variables does not erase observed argv history.
5. Do not pass or log raw `config.sh` output: it is not safe to assume stdout/stderr is credential-free. Capture/suppress it using bounded or otherwise non-persistent handling. No `set -x`, argv-bearing debug traces, `ps` diagnostics, shell history, structured logging of command arrays, or `subprocess` exception repr containing token arguments.
6. The decision to expose this short-lived removal token to local process observers is accepted **only** for Web Remove on this controlled host. Preserve existing stricter Create token handling.

### Security acceptance test

Prove the called executable receives `["./config.sh", "remove", "--token", "<synthetic token>"]` with one token argument, while Web logs, Dispatcher/Worker diagnostics, CLI stderr/stdout surfaced upstream, failure JSON and durable files contain no token. The test must use a disposable stub `config.sh`, never actual GitHub credentials.

## 4. Stage taxonomy and error contracts

A stage is the **first terminal failure boundary** in the existing ordered workflow, not a speculative cause. Use a single allowlisted diagnostic structure, for example `remove_stage=<FIXED_LABEL> exit_code=<SAFE_INT_OR_UNKNOWN>`; no repository identity, runner name/path, token, command, stdout/stderr, arbitrary exception repr, Cookie, CSRF or nonce in diagnostic event fields.

Freeze the allowed terminal labels:

- `preflight_failed`: identity/path/metadata/token FD validation before any local service mutation; token validation that occurs after service uninstall must report its actual stage and preserve the already-modified service state, not pretend no mutation occurred.
- `service_state_failed`: privileged service state lookup/context failure.
- `service_stop_failed`: stop operation returned failure.
- `service_uninstall_failed`: uninstall returned failure.
- `config_remove_failed`: official `config.sh remove --token` returned nonzero or could not be started.
- `local_cleanup_failed`: post-unregistration path verification or directory deletion failed.
- `unknown_failed`: an unexpected failure not safely attributable to one of the above stages.

For every label, map the actual existing failing operation; do not infer a label solely from a generic Worker return code. The final exit code, where available, must be a validated bounded numeric process exit status (0–255); otherwise use a fixed `unknown` marker. Never report a false GitHub rejection based only on a nonzero config exit: display “Runner registration removal failed” instead of attributing cause without evidence.

Transport the fixed category and safe exit code across existing script stdout/stderr capture and Worker response without forwarding raw output. Define a deterministic, versioned and anchored parse of a single final failure marker (e.g. `GRT_REMOVE_RESULT_V1 stage=<label> exit=<integer|unknown>`); reject duplicate, malformed, conflicting, partial or unexpected values to `unknown_failed`. Ensure the marker cannot be forged by `config.sh` stdout/stderr: script must keep external child output separate from the dedicated result channel. Do not rely on ad hoc grep of captured child output. Worker must return a stable allowlisted code to Web; Web must render an escaped, user-comprehensible error without secret data. Preserve existing HTTP success/redirect semantics. Explicitly document and test the HTTP status selected for each failure class (normally existing 500, without changing authentication/confirmation failures). For legacy or unavailable stage markers, fall back to the existing generic failure response.

Diagnostics must be written immediately with bounded, fixed fields; failures in diagnostic I/O must not alter the lifecycle outcome. Do not expose token in access logs or systemd journal. Avoid using raw shell `$?` after additional commands; capture the actual failing command's exit status once.

## 5. Failure and partial-state guarantees

- Failure before the service lifecycle operation: no stop/uninstall, config remove, or local deletion.
- Failure at service stop/uninstall: never invoke `config.sh remove` or delete the runner directory. Do not hide partial systemd effects.
- Failure during `config.sh remove`: **never** delete the local runner directory; service may already be uninstalled. Display that partial-state possibility and require operator inspection rather than blindly retrying or offering automatic Recover.
- If `config.sh remove` succeeds but local cleanup fails: report `local_cleanup_failed`; do not misreport that GitHub unregistration failed or attempt a second unregister.
- Only delete the already-validated runner directory after successful unregistration and existing safe-path checks. Never remove unrelated runners, backups or archived artifacts.
- Successful operation must preserve established status and redirect behavior, locking and audit semantics.
- Never claim remote GitHub registration is unchanged based only on local `.runner` and `.credentials`; no automatic second mutation while state is unknown.

## 6. Verification plan and acceptance matrix

Tests must run entirely on isolated fixtures with fake runner directories, fake `config.sh` and `svc.sh`, controlled privileged Dispatcher doubles, and no network or real GitHub tokens. Extend existing tests rather than creating an alternate production path. Mandatory cases:

1. Web login → cookie → CSRF → prepare → confirmation → Worker → mocked command: official remove CLI argv with synthetic token; successful redirect and exactly one unregister; no PTY adapter invocation.
2. Wrong operation, missing/expired/used confirmation, invalid CSRF, invalid token: existing fail-closed HTTP results and nonce consumption unchanged; zero unregister.
3. Preflight, service-state, stop, uninstall, config-remove nonzero, config executable missing, cleanup failure, Worker error, missing/invalid stage marker: assert safe label, actual exit code if available, HTTP status/body, non-leaking logging, order and count of mutations, preserved local artifacts.
4. Failure during config removal after successful stop/uninstall: runner directory remains; no automatic recovery or retry; response explicitly allows partial systemd state.
5. Config succeeds followed by cleanup failure: stage accurately identifies local cleanup rather than GitHub registration.
6. Synthetic token containing metacharacters is passed as a single literal argv value (subject to existing validator limits); never interpreted by a shell. Token never appears in stdout/stderr, logs, JSON, or persistent files.
7. Concurrent remove attempts and held mutation lock: unchanged single-operation gating, no duplicate command execution. Test timeout outcomes and bounded exit-code handling.
8. Create and Recover Local regression tests: no changed argv/FD/PTY behavior. Check Web D1/D2 existing diagnostics still pass.
9. Error diagnostic emission raises `OSError` or fails flush: same exit path, HTTP result, privileged calls, nonce/lock and filesystem effects as without diagnostic failure.
10. Existing baseline `config.sh remove` CLI mode and its user interaction remain unchanged.

Execute `bash tests/run-all.sh` on the CI runner and record the successful GitHub Actions run against the exact implementation HEAD. Include a source review of all code paths that can print, trace, serialize or persist argv or child output. A passing CI alone does not prove the safety property.

## 7. Rollout and approval gates

1. Independent SPEC audit: PASS with no blockers; freeze SPEC commit and scope.
2. Separate explicit implementation authorization; commit only scoped code and tests.
3. CI success and independent implementation audit; test and audit observed failure classification, argv propagation, token leakage and partial state.
4. **Separate Debian deployment authorization** documenting exact commit, changed installed files, backups/rollback, restart scope and bounded window. A deployment must not automatically restart Runner services; any required Web/Dispatcher restart needs explicit operator approval. The `setup-web-management.sh --apply` installer restarts Dispatcher and Web and is not the default for a narrow rollout.
5. Read-only production checks first. Only after additional, explicit operator authorization may a real AgentCI runner be removed, using a newly obtained temporary GitHub token. Do not reuse any token pasted into chat or logs.
6. Confirm both GitHub remote removal and local service/configuration cleanup; archive status stays unchanged. Stop on ambiguous partial state; restore only code/config safely, not the runner's registration by inference.

This SPEC does not claim the October 9 production HTTP 500 was caused by PTY. It defines a simpler, user-selected official invocation and testable diagnostics so subsequent failures are attributable to a bounded stage.

## 8. Independent audit questions

- Is argv disclosure acceptance explicit and appropriately limited to temporary **removal** tokens?
- Does the proposed FD → shell → official argv transfer preserve privilege boundaries and prohibit token leakage to logs/diagnostics?
- Are stage markers unforgeable by child output and transportable using the existing Worker response path without Dispatcher changes?
- Do error classification and failure handling preserve current service/uninstall ordering and partial-state safety?
- Can the scope be implemented without changing Create, Recover Local, GitHub/API clients, unit files, Gate A confirmation semantics, or changing access controls?
- Is every mandatory failure branch covered by fixture-based tests? Are deploy and actual remove separately gated?

Requested decision: `PASS / REVISE / BLOCKED`. Until PASS and implementation authorization, **SPEC ONLY**.
