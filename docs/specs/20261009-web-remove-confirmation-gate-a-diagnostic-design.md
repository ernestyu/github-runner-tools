# Gate A Diagnostic Evidence Design — Web Remove Confirmation

**Repository:** `ernestyu/github-runner-tools`  
**Frozen Bugfix Proposal:** `011a136b94f36a7e9cae691aa432391a3bbbd619`  
**Runtime baseline:** `fd9a85bcaede4a88c9da94d8bc6b08b14abbab3b`  
**Gate A investigation HEAD before this document:** `e81c587886f072c2c1e6f34ef321259b3f60d085`  
**Gate A independent audit:** INCONCLUSIVE; CI `37969658469` PASS  
**Document status:** DESIGN ONLY — INDEPENDENT APPROVAL REQUIRED  
**Production instrumentation:** NOT AUTHORIZED  
**Gate B runtime fix:** NOT AUTHORIZED  
**Production Runner removal:** NOT AUTHORIZED

## 1. Why this design is needed

On 2026-10-09, the operator reported one apparently prompt Web Remove confirmation submission for `ernestyu/AgentCI` returning `Confirmation expired or invalid.` The production HTTP request sequence was not preserved in usable existing logs.

Gate A tests with actual HTTP login/Set-Cookie/prepare/confirm reproduced premature nonce consumption following an earlier malformed token or wrong-operation request. They **did not** establish that either preceding request occurred during the production incident. Independent Gate A audit therefore ruled incident causality UNCONFIRMED and blocked Gate B.

Subsequent **read-only Debian host inspection** showed:
- `github-runner-tools-web.service` active, PID `3133385`, current process started `2026-10-09 12:45:28 CEST`.
- systemd `StandardOutput=journal`, `StandardError=inherit`, `Type=simple`.
- `ExecStart` uses `/usr/bin/python3 .../web/app.py` without `-u`.
- no `PYTHONUNBUFFERED` or `PYTHONIOENCODING` override in the process environment.
- journald contained service lifecycle messages but no observed HTTP request metadata, even with a full-day filter.
- source `Handler.log_message()` uses ordinary `print()`, without explicit `flush=True`.

**Inference, not definitive proof:** buffered Python stdout is a plausible explanation for missing timely journald HTTP records. This explains an observability gap, **not** nonce rejection. No direct observation of the Python buffer contents exists. Historical 12:27 service restart-loop/start-limit-hit is documented but no demonstrated incident association.

## 2. Scope and two separately approvable options

**Option D1 — allowlisted log content AND timely delivery (B-D1-01):** Replace the existing unconstrained access log rendering with strict fixed-route labels plus explicit flushing of that sanitized line. The baseline logger currently prints client IP, HTTP method, raw path up to `?`, and a positional log argument; this is **not** an allowlist. Flushing it without sanitization is prohibited. D1 requires both sanitization and flush in the same implementation change; the reviewer must not authorize flush-only behavior. Do not change the server invocation, add `python3 -u`, set `PYTHONUNBUFFERED`, or modify the systemd unit. D1 alone cannot distinguish all nonce failure categories.

**Option D2 — bounded confirmation failure classification:** Instrument fixed, non-sensitive rejection categories and explicit request boundary markers while preserving existing validation/consumption outcomes. D2 offers the causal evidence needed if D1 metadata does not suffice.

### 2.1 D1 exact output contract (B-D1-01)

Only the following fixed, source-code-defined route labels are permitted: `login` (`/login`), `index` (`/`), `remove_prepare` (`/remove/prepare`), `remove_confirm` (`/remove/confirm`), `recover_prepare` (`/recover/prepare`), `recover_confirm` (`/recover/confirm`), `create` (`/create`), `logout` (`/logout`). All other paths, even paths with percent-encoding, control characters, unexpected trailing segments or query strings, become `other`. Matching uses the **literal request path before `?`** only against those exact route strings. Never log the original path or query. Do not interpolate client-controlled text.

Access record format is exactly `web_access method=<METHOD_LABEL> route=<ROUTE_LABEL> status=<STATUS_CODE>`, with exactly one ASCII newline and immediate `flush=True`. `METHOD_LABEL` is `GET` or `POST` for exact recognized methods, otherwise `OTHER`; no raw method string. `STATUS_CODE` is the decimal integer of the actual response status, constrained to 100–599; if the logging callback cannot verify a concrete numeric status then use the fixed `000` sentinel, never the raw `args` string. The log formatter must not use original `fmt` or other positional arguments, except a status value after strict integer parsing. No IP address, session/user/runner/repository identifier, original path, referrer, user-agent, headers, request bodies, or exception object.

The D1 logger must remain **observational**: no effect on routing, request parsing, redirects, authorization, dispatcher calls, response code or bytes, nonce creation/consumption, or session lifetime. If output fails, diagnostics fail silent without recursively logging or changing HTTP response; do not suppress preexisting request exceptions or retry business operations. No extra network endpoint or file. Existing journald retention applies.

Required D1 tests: recognized literal routes render exact labels; unknown/malicious URL paths, query strings, newline/ANSI/control characters and forged status strings cannot be emitted; method fallback `OTHER`; status fallback `000`; no IP/raw path/body/secret leakage; immediate record visibility/flush via a mocked writable stream; identical HTTP response and Dispatcher spy results with logging success versus simulated logging exception. The logger receives baseline callback arguments but cannot print them directly.

These are **proposed designs only**. Independent audit must authorize D1 and D2 separately (or request revision). Neither option is approved by the existing Gate A test-only permission. No code, unit, installed service, or running server is changed by this document.

## 3. Minimum sufficient diagnostic record (D2 proposal)

Capture only static event names and bounded categorical values at branch boundaries; include a non-secret per-request diagnostic counter **only if the audit approves its necessity**. Do not emit stable cross-request identifiers or values derived from credentials.

Allowed suggested event classes (only static labels, never values from a user-controlled input):
- `web_request`: allowlisted route label among `remove_prepare`, `remove_confirm`, `recover_confirm`, `login`; HTTP status code; no path query.
- `session_rejection`: `missing_cookie`, `unknown_session`, `idle_expired`, `absolute_expired` (categories only; no session ID).
- `csrf_rejection`: fixed `invalid_csrf` marker.
- `confirmation_rejection`: `nonce_missing_or_used`, `nonce_expired`, `operation_mismatch`; avoid distinguishing valid vs invalid secret through browser response.
- `token_format_rejection`: `invalid_token_format`, never token content or derived fingerprint.
- `remove_dispatch_boundary`: `pre_dispatch_rejected` vs `dispatch_invoked`, but no repository name or outcome claimed solely from client replay.

### 3.1 D2 checkpoint/source mapping (B-D2-01)

The following describes the **existing** control flow and where a later approved passive diagnostic hook may observe its decision. It does not authorize changing that flow.

| Event | Single decision checkpoint and authoritative data | Race / safety rule |
| --- | --- | --- |
| `session_rejection` | Branch that currently denies `_session()` after cookie extraction, session lookup and existing `session_is_valid()` decision. | Record only which **existing** guard denied; no additional lookup/recheck or change to `SESSIONS` / `last`. If another handler changes state before classification, report static `session_unknown`. |
| `csrf_rejection` | Existing `if not self._csrf_ok(form, sess)` branch. | Fixed category, no CSRF value/comparison operand. |
| `nonce_missing_or_used` | `pending` local returned from the **one existing** `sess["confirm"].pop(nonce, None)` inside `consume_confirmation()`; missing/non-dict pending. | Never inspect the dictionary a second time; missing does not imply replay. |
| `nonce_expired` | The same popped `pending` object and the existing comparison `pending.get("expires", 0) < t`, where `t` is the **same captured** `now()` (or supplied `at`) already used for the authorization decision. | No second clock read; no new expiration check. |
| `operation_mismatch` | The same popped `pending` object and the existing comparison `pending.get("op") != expected_op`. | Do not re-read stored nonce or infer from endpoint alone. |
| `confirmation_unknown` | Only when the specific decisive condition cannot reliably be observed from that single invocation or concurrent interference makes provenance unclear. | Never infer a historical nonce state, previous request, or replay from a later snapshot. |
| `token_format_rejection` | Existing `validate_temporary_token(token)` exception branch **after** baseline `consume_confirmation()`. | No token bytes, length, fingerprint, or echoes; preserves current early-consumption defect for evidence. |
| `dispatch_invoked` | Immediately at the **existing** `dispatch(self.app.config, request)` invocation in `/remove/confirm`, with no second call. | Marker means entered the call, not that GitHub removal succeeded. |
| `pre_dispatch_rejected` | One of the actual existing return branches before the Remove dispatch invocation. | No claim about earlier requests. |

The existing `consume_confirmation()` calls `pop` before validating operation and expiry. If D2 is implemented, it may emit a fixed decision category using the very `pending` object consumed by that invocation (or a fixed code passed back from a private, observational classification path). It **must** preserve the original one-time `pop`, same branch precedence, same comparison operands/time, return value and exception propagation, and the exact placement of token-format validation. Do not introduce a pre-read, second `pop`, helper that tests nonce again, or a delayed post-consumption dictionary lookup.

If multiple denial guards are true, log **only the category of the first existing decisive guard** in evaluation order (currently: non-dict/missing pending, operation mismatch, expiration); do not reorder these expressions. A passive category captured at the same decision checkpoint may be locally returned to the logging site only if doing so demonstrably preserves the public helper's existing behavior and all handler outcomes; otherwise log immediately at that checkpoint. Failure to classify reliably must yield `confirmation_unknown` rather than add a check.

A logger write error must be caught and discarded **without altering** authorization decisions, HTTP response/status, nonce state or the number/ordering of Dispatcher calls. Do not blanket-catch application failures: the catch boundary must cover **only** diagnostic I/O. Verify this using injected logger exceptions and HTTP/Dispatcher assertions.

Diagnostic events must be emitted at these mapped checkpoints only. Diagnostics **must not move** checks, calls, or mutation boundaries.

**Noninterference invariant:** logging must not change confirmation consumption timing, session lifetime, token validation, the HTTP status, dispatcher invocation count, or any outcome. Production diagnostics must not consume or retry nonce. A hook that raises must not cause a mutation to repeat or change authentication decisions. All diagnostic output must be deterministic/redacted, written only to systemd journal through an approved facility with a bounded rate. No additional network endpoint or log file.

Absolutely forbidden log fields: temporary GitHub registration/removal tokens; POST data; cookies; session IDs; CSRF values; nonce values or hashes; authorization headers; user password; runner directory; arbitrary `repr()` of requests/exceptions. Neither GitHub token nor nonce is to be sent back to the user for debugging.

**Subtlety:** `nonce_missing_or_used` does not prove a replay: missing entry can also reflect state loss, stale form, pruning or earlier consumption. Logs must preserve this distinction as uncertainty, not make a causal claim from the category name. `session_rejection` needs internal checks only after approval; current baseline does not expose the full category safely.

## 4. Security/privacy and concurrency review constraints

Diagnostics are passive. Do not add a lock, adjust `pop()`, refactor session pruning, modify TTL, change `Set-Cookie` flags, alter CSRF comparison, or introduce any storage of secrets. D2 instrumentation should be reviewed for races in categorization under `ThreadingHTTPServer`: a later observation of dictionary state is not conclusive proof of the state at the authorization transition. Instrument at the existing decision point or classify as unknown; do not introduce check-then-act behavior.

Use only static route/status/category labels; do not log user IP or repository. D1 **replaces** the existing unconstrained access log content with its exact whitelist; it does not flush the old format. Define journal retention expectations using existing host policy, not a new persistent storage mechanism.

Any diagnostic evidence copied to an audit report must be stripped to timestamps, static categories and status codes, explicitly reviewed for secret absence.

## 5. Authorization, deployment and rollback gate

**Prior to any production deployment:** obtain an explicit independent audit decision on each option, including exact changed paths, allowable event set, implementation method, unit restart necessity, operational window, and rollback. A proposal approval by itself is not deployment approval; test-only investigation authorization remains distinct from permission to change production logging.

If later authorized:
1. Freeze the approved diagnostic implementation commit and confirm CI PASS with tests asserting no authorization/consumption behavior change.
2. Get a separate operator approval for a bounded Debian deployment window. Warn that Web restart invalidates process-memory login sessions/confirmations; existing Runner services must remain unaffected.
3. Record installed Web version and approved rollback commit/backup. Verify service and access only using non-mutating requests before reproducing anything.
4. Start bounded capture; use synthetic/controlled requests if sufficient. A real production Remove confirmation must **not** be sent unless separately approved after risk review; no use of the existing AgentCI Runner as a diagnostic mutation target.
5. Stop capture after one controlled reproduction or the approved deadline; return to original log verbosity/mode and restart only if separately authorized. Confirm all other runners and Web service remain healthy.
6. Never clear journald, terminate a process to force a flush, or restart Web only to retrieve speculative buffered historical lines without explicit permission.

No executable rollout command is included here because production instrumentation is not yet authorized.

## 6. Root-cause evidence and Gate A exit criteria

Collect: time-ordered request metadata and status code sequence from HTTP access log, plus only approved categories. Record clock/timezone and whether log delivery latency is known. Compare a **single** prompt valid confirm, double submission, bad-format-first, cross-tab, old-form and missing-session scenarios in an isolated test harness.

**Gate A PASS requires** a reliable reproducible source-to-state transition corresponding to the observed production symptom **plus** independent causal corroboration from the specific incident's request sequence or bounded approved production diagnostic observation. Show a test assertion that fails on the runtime baseline and exercises the evidenced cause. Exclude competing explanations with evidence, not speculation. A one-off category `nonce_missing_or_used` alone is insufficient to establish which earlier event consumed it.

If capture only proves that a request was rejected but cannot distinguish replay, stale form, session replacement, expiry, or missing state, mark **INCONCLUSIVE** and stop. If existing production records are irrecoverable and no consent exists to re-enact production behavior, state that historical causation cannot be determined; propose separately approved remediation of an independently confirmed code flaw rather than laundering it into the original incident's confirmed root cause.

Gate B remains blocked until formal Gate A evidence review PASS and explicit authorization.

## 7. Independent audit questions

1. Does the evidence justify investigating stdout buffering as a visibility issue without calling it the Remove root cause, and is the D1 strict whitelist plus flush an adequate remedy for safe delivery?
2. Should D1 flush existing safe log lines, D2 bounded categorization, both, or neither be authorized?
3. Are fixed categories sufficient and free from credential disclosure, including under errors/concurrent HTTP handlers?
4. Does the D2 checkpoint mapping use the actual popped object/decision operands without additional state reads, preserve code execution and nonce consumption ordering exactly, and fail to `confirmation_unknown` where needed?
5. Are retention, rate limits, restart consequences, rollback and operational consent adequately specified?
6. Does the design correctly preserve an INCONCLUSIVE outcome when incident correlation is missing?
7. What precise further implementation/deployment authorization, if any, is being granted?

**Requested review decision:** APPROVE / REVISE / BLOCKED, with a separately identified scope for **design approval**, **diagnostic implementation**, **production deployment**, and **Gate B runtime fix**. None should be inferred from another.

## 8. Round-1 design audit blocker disposition

- **B-D1-01 — submitted for closure:** D1 now requires one inseparable change that both whitelists the full access-log output (exact literal routes, fixed methods, numeric statuses only) and flushes the new sanitized line; never flush the existing IP/raw-path logger. Explicit tests prove noninterference and resistance to forged content.
- **B-D2-01 — submitted for closure:** D2 now maps every decision to the original control-flow checkpoint; confirmation classifications derive exclusively from the existing popped `pending` object and original comparison operands/time, never a second nonce lookup. First decisive guard precedence, `confirmation_unknown` and injected diagnostic write-failure tests are mandated.

**Authorization unchanged:** this is a documentation-only revision. Diagnostic implementation, Debian deployment, Gate B fix and production Remove remain NOT AUTHORIZED pending independent re-audit.
