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
4. Read and validate the token **before the first service-state mutation**, after runner identity and privilege-context preflight. This explicitly supersedes the earlier late-read preference: token-input failure must leave the service untouched; avoid placing it in exported environment variables, log messages, persistent files or response bodies. Ensure existing inherited FD and token-bearing shell variables are not forwarded to unrelated subprocesses beyond what is necessary for this invocation; clear the shell token after the invocation on success and failure paths. For clarity: cleaning shell variables does not erase observed argv history.
5. Do not pass or log raw `config.sh` output: it is not safe to assume stdout/stderr is credential-free. Capture/suppress it using bounded or otherwise non-persistent handling. No `set -x`, argv-bearing debug traces, `ps` diagnostics, shell history, structured logging of command arrays, or `subprocess` exception repr containing token arguments.
6. The decision to expose this short-lived removal token to local process observers is accepted **only** for Web Remove on this controlled host. Preserve existing stricter Create token handling.

### Security acceptance test

Prove the called executable receives `["./config.sh", "remove", "--token", "<synthetic token>"]` with one token argument, while Web logs, Dispatcher/Worker diagnostics, CLI stderr/stdout surfaced upstream, failure JSON and durable files contain no token. The test must use a disposable stub `config.sh`, never actual GitHub credentials.

## 4. Frozen failure-marker transport and validation (B-01)

### 4.1 A dedicated private FD — not stdout or stderr

The only trusted script → Worker diagnostic channel for Web Remove is a **new anonymous, one-way pipe**, created by the non-root `lifecycle_worker.py` immediately before invoking `remove-runner.sh --web-worker`. Worker retains the pipe's **read end** and passes only the **write end** to this script via `subprocess.run(pass_fds=...)` and a remove-only `--result-fd <decimal-fd>` option. This is an additive Worker→Remove-script FD, **not** a Dispatcher protocol change. Maintain the existing token FD, privileged FD and Worker identity. Do not pass the result FD to Create or Recover Local.

The Remove script exclusively owns writing the marker to the result FD. Its external `config.sh` child, `svc.sh`, privileged helper invocations, and other subprocesses must **not inherit** the marker FD: explicitly close it in every child execution context while keeping it open in the parent script until the terminal outcome. Child stdout/stderr must be captured/discarded in their existing bounded and non-persistent manner, with no forwarding into the marker channel. Shell progress text may still appear on regular stdout/stderr; Worker must treat them as **untrusted and never parse them for stage information**. All failure markers are generated from the Remove script's own checks and captured command return statuses, not from child output.

The private pipe has a maximum one-record payload of **128 ASCII bytes** including its terminating LF. No unbounded reads: on subprocess completion, Worker closes its copy of the write end, reads at most 129 bytes from its read end, rejects overflow, then closes the read end. The script closes its write end on completion. The transport must be nonblocking for the finite record; a short single write fits below PIPE_BUF. The Worker must not wait indefinitely on an FD accidentally inherited by grandchildren; FD-closing discipline and a bounded read are mandatory. If a timeout/kill path prevents guaranteed marker collection, use `unknown_failed` without blocking. Do not weaken existing request timeout/locking policy to achieve diagnostic delivery.

### 4.2 Exact marker grammar and ownership

For every terminal Web Remove **failure** recognized by the script, write exactly one line to its private result FD:

```text
GRT_REMOVE_RESULT_V1 stage=<stage> exit=<exit>
```

with precisely one ASCII LF and no other bytes. `<stage>` is exactly one of the fixed labels below; `<exit>` is `unknown` or a canonical decimal integer in `0..255` (no signs, spaces, zero-padding except `0`). A recognized failed subprocess with numeric exit code `0` is internally inconsistent: downgrade to `unknown_failed`. Successful Remove writes **no failure marker** and reports success only via its real zero process exit and existing state checks.

A single script-owned result-emission routine records the *first terminal failure* and its associated exit code and writes once (guarded against duplicated trap execution). No freeform strings or command output enter the record. Failure to write this record never changes the underlying script exit outcome or retries any mutation.

The Worker is the **sole anchored parser and allowlist validator**. Accept only one exact, ASCII, fully anchored grammar match against the entire bounded pipe payload. Unknown labels, multiple records, missing LF, extra whitespace, oversize, truncated, malformed, conflicting data, or absent marker on nonzero script exit **all downgrade to `unknown_failed` with `exit=unknown`**. A valid marker is accepted only when the script actually exited nonzero through normal completion; markers on zero exit, crashes or signalled termination must not override the actual outcome. Never extract a substring, scan captured stdout/stderr, or trust any output generated by the Runner executable.

### 4.3 Worker → Dispatcher → Web

For Web Remove script failure with valid marker, Worker emits a single JSON response on its **existing** stdout protocol:

```json
{"ok":false,"error":"lifecycle_failed","stage":"service_stop_failed","exit_code":1}
```

`error` stays `lifecycle_failed` for backwards compatibility. `stage` is the validated label; `exit_code` is either JSON integer 0–255 (subject to the failure consistency check) or JSON `null` for unknown. Unknown or absent markers produce `{"ok":false,"error":"lifecycle_failed","stage":"unknown_failed","exit_code":null}`. A Worker-local failure **before** successful script execution is `worker_error` or the existing fixed Worker error (without invented stage); if a Worker-local exception/timeout occurs after execution may have begun, the externally displayed state must be **unknown**. If the Worker is signalled, crashes or times out, the Dispatcher may return its existing `lifecycle_failed`, `operation_timed_out`, `internal_error` or other stable error; Web maps those to unknown outcome with manual state verification. No new Dispatcher code, JSON framing or privilege operations are needed: it already parses and forwards Worker JSON objects. `web/app.py` must independently allowlist `stage` and `exit_code` before any user-facing rendering; unexpected/malformed fields display the existing generic `Runner operation failed`. All Remove lifecycle failures remain HTTP **500**; preserve existing **303** redirect on success and pre-existing **403/400/409** session/confirmation/input/lock behavior.

User-facing text for a validated stage is a static sentence drawn from the mapping table, optionally followed by the bounded numeric exit code. No raw repository, token, command line, child stdout/stderr, arbitrary exception repr or uncontrolled JSON strings in HTML or logs. The Web must not treat stage labels as an authorization or mutation decision. Diagnostic logging itself is best-effort, strictly fixed-field, flushed and noninterfering; exceptions while logging do not change the script exit, Worker JSON, HTTP status, or mutation sequence.

## 5. Complete stage-to-operation and partial-state mapping (B-02)

**Read and validate the Removal Token once, after identity/context preflight but strictly before the first service stop/uninstall.** This removes the previous ambiguous possibility of a token-FD validation failure after service uninstall. It does **not** reorder any actual lifecycle mutation: state lookup → stop → uninstall → `config.sh remove` → verified directory deletion. No Token FD read occurs after local service changes. Token input failures use `preflight_failed` while the service remains unmodified. A token may nevertheless become invalid by the later GitHub call; that failure is `config_remove_failed`, not `preflight_failed`.

Allowed stages: `preflight_failed`, `service_state_failed`, `service_stop_failed`, `service_uninstall_failed`, `config_remove_failed`, `local_cleanup_failed`, `unknown_failed`. **No success stage**, no new recovery action. The table below freezes classification, prior possible mutations, allowed deletion, user text and remote verification; in each case, "NO" means absolutely no directory deletion on that failure path.

| Actual failing boundary | Prior mutation potentially completed | Stage | Exit code | Delete directory? | Fixed UI message | Verify GitHub remote? |
|---|---|---|---|---|---|---|
| Repository/runner identity, path, metadata, dispatcher context or Token FD read/validation **before stop** | None | `preflight_failed` | `unknown` for internal check, or actual safe failure code | NO | "Runner removal preflight failed; service was not intentionally changed." | Only if prior independent attempts make state uncertain |
| Privileged service-state lookup/context check | None from this attempt | `service_state_failed` | Actual safe code or `unknown` | NO | "Cannot verify runner service state; no removal started." | If earlier state uncertain |
| Privileged service stop reports failure | Stop may have partially occurred | `service_stop_failed` | Actual safe code or `unknown` | NO | "Runner service stop failed; check systemd state before retrying." | If earlier state uncertain |
| Privileged service uninstall reports failure | Stop succeeded; uninstall may be partial | `service_uninstall_failed` | Actual safe code or `unknown` | NO | "Runner service uninstall failed; check systemd state before retrying." | If earlier state uncertain |
| `config.sh` executable missing, inaccessible or cannot start | Stop and uninstall may have completed; unregister not started | `config_remove_failed` | `unknown` for launch failure | NO | "Runner registration command could not start; local service may be uninstalled." | YES, given possible earlier attempts |
| `config.sh remove --token` returns nonzero | Stop and uninstall completed; GitHub effect may be partial or unknown | `config_remove_failed` | Exact child 1–255 where available | NO | "Runner registration removal failed or is uncertain; check GitHub and systemd before retrying." | **YES** |
| `config.sh remove` exited 0, then safe-path verification fails | GitHub unregister reported success; no local deletion | `local_cleanup_failed` | `unknown` for check failure | NO | "GitHub unregister completed; local cleanup was blocked by a safety check." | YES for closeout |
| `config.sh remove` exited 0, verified directory deletion fails | GitHub unregister reported success; local deletion may be partial | `local_cleanup_failed` | Actual safe command code or `unknown` | NO further deletion or retry | "GitHub unregister completed; local directory cleanup is incomplete." | YES for closeout |
| Worker-local exception, signal, Dispatcher timeout, missing/malformed marker, marker transport failure, outcome indeterminate | Any step, including remote mutation, may have started | `unknown_failed` as Web-facing fallback; retain existing lower-layer error as applicable | `unknown` | NO automatic deletion or retry | "Runner removal outcome is uncertain; inspect GitHub, systemd and local files before another operation." | **YES** |
| All steps succeed | Stop, uninstall, unregister, verified deletion completed | No failure stage; existing success/303 | Not applicable | Only the existing successful validated deletion | Existing success redirect | Verify remote/local state at acceptance closeout |

For error-stage transport only, the `exit` field is **the exact safely captured exit code of the named failing child process** when available, not the generic script exit code or a guessed errno. For privileged operations with no child process exit exposed, use `unknown` and do not change the privileged Dispatcher protocol to manufacture one. A signal termination, timeout, invalid marker or internal exception is not safely attributable to a stage: return `unknown_failed` to the user and do not assert that GitHub unregister did not occur. If a command succeeds but a subsequent stage fails, its successful effect is retained in the table; do not relabel cleanup failure as registration failure.

**Partial-state rule:** Failure never triggers automatic retry, Recover Local, force removal, cleanup continuation or service restart. After `config_remove_failed` or `unknown_failed`, the user must inspect GitHub remote registration, systemd and runner-directory state prior to any new mutation. An exit-0 `config.sh` only establishes reported command success, not independent remote verification. Existing archive and other runners are never modified by diagnostic reporting.

## 6. Verification plan and acceptance matrix

Tests must run entirely on isolated fixtures with fake runner directories, fake `config.sh` and `svc.sh`, controlled privileged Dispatcher doubles, and no network or real GitHub tokens. Extend existing tests rather than creating an alternate production path. Mandatory cases:

1. Web login → cookie → CSRF → prepare → confirmation → Worker → mocked command: official remove CLI argv with synthetic token; successful redirect and exactly one unregister; no PTY adapter invocation.
2. Wrong operation, missing/expired/used confirmation, invalid CSRF, invalid token: existing fail-closed HTTP results and nonce consumption unchanged; zero unregister.
3. Preflight (including pre-mutation Token FD failure), service-state, stop, uninstall, config-remove nonzero, config executable missing, cleanup failure, Worker error, missing/invalid stage marker: assert safe label, actual exit code if available, HTTP status/body, non-leaking logging, order and count of mutations, preserved local artifacts.
4. Failure during config removal after successful stop/uninstall: runner directory remains; no automatic recovery or retry; response explicitly allows partial systemd state.
5. Config succeeds followed by cleanup failure: stage accurately identifies local cleanup rather than GitHub registration.
6. Synthetic token containing metacharacters is passed as a single literal argv value (subject to existing validator limits); never interpreted by a shell. Token never appears in stdout/stderr, logs, JSON, or persistent files.
7. Dedicated result FD: verify write-end ownership, no inheritance by `config.sh` or helpers, exactly one marker, anchored parser, duplicate/malformed/truncated/oversize/unknown-marker fallback, normal zero-exit vs marker conflicts, Worker signal/crash/timeout behavior and Dispatcher pass-through; use untrusted stdout/stderr containing forged markers to prove they are ignored. Concurrent remove attempts and held mutation lock: unchanged single-operation gating, no duplicate command execution. Test timeout outcomes and bounded exit-code handling.
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
