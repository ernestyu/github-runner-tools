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

**Option D1 — log-delivery visibility only:** Ensure existing request metadata reaches journald promptly. Preferred candidate is explicit flushing of the existing allowlisted HTTP access log line. Alternative `python3 -u` / `PYTHONUNBUFFERED=1` changes the service invocation and requires service restart; the reviewer must independently evaluate and authorize it. D1 alone cannot distinguish all nonce failure categories.

**Option D2 — bounded confirmation failure classification:** Instrument fixed, non-sensitive rejection categories and explicit request boundary markers while preserving existing validation/consumption outcomes. D2 offers the causal evidence needed if D1 metadata does not suffice.

These are **proposed designs only**. Independent audit must authorize D1 and D2 separately (or request revision). Neither option is approved by the existing Gate A test-only permission. No code, unit, installed service, or running server is changed by this document.

## 3. Minimum sufficient diagnostic record (D2 proposal)

Capture only static event names and bounded categorical values at branch boundaries; include a non-secret per-request diagnostic counter **only if the audit approves its necessity**. Do not emit stable cross-request identifiers or values derived from credentials.

Allowed suggested event classes:
- `web_request`: allowlisted route label among `remove_prepare`, `remove_confirm`, `recover_confirm`, `login`; HTTP status code; no path query.
- `session_rejection`: `missing_cookie`, `unknown_session`, `idle_expired`, `absolute_expired` (categories only; no session ID).
- `csrf_rejection`: fixed `invalid_csrf` marker.
- `confirmation_rejection`: `nonce_missing_or_used`, `nonce_expired`, `operation_mismatch`; avoid distinguishing valid vs invalid secret through browser response.
- `token_format_rejection`: `invalid_token_format`, never token content or derived fingerprint.
- `remove_dispatch_boundary`: `pre_dispatch_rejected` vs `dispatch_invoked`, but no repository name or outcome claimed solely from client replay.

Diagnostic events must be emitted at precisely mapped checkpoints: session validation; CSRF guard; confirmation lookup/claim currently implemented by `consume_confirmation()`; token format guard; immediately prior to the existing `dispatch()` call; and after result handling where safely knowable. Diagnostics **must not move** checks, calls, or mutation boundaries.

**Noninterference invariant:** logging must not change confirmation consumption timing, session lifetime, token validation, the HTTP status, dispatcher invocation count, or any outcome. Production diagnostics must not consume or retry nonce. A hook that raises must not cause a mutation to repeat or change authentication decisions. All diagnostic output must be deterministic/redacted, written only to systemd journal through an approved facility with a bounded rate. No additional network endpoint or log file.

Absolutely forbidden log fields: temporary GitHub registration/removal tokens; POST data; cookies; session IDs; CSRF values; nonce values or hashes; authorization headers; user password; runner directory; arbitrary `repr()` of requests/exceptions. Neither GitHub token nor nonce is to be sent back to the user for debugging.

**Subtlety:** `nonce_missing_or_used` does not prove a replay: missing entry can also reflect state loss, stale form, pruning or earlier consumption. Logs must preserve this distinction as uncertainty, not make a causal claim from the category name. `session_rejection` needs internal checks only after approval; current baseline does not expose the full category safely.

## 4. Security/privacy and concurrency review constraints

Diagnostics are passive. Do not add a lock, adjust `pop()`, refactor session pruning, modify TTL, change `Set-Cookie` flags, alter CSRF comparison, or introduce any storage of secrets. D2 instrumentation should be reviewed for races in categorization under `ThreadingHTTPServer`: a later observation of dictionary state is not conclusive proof of the state at the authorization transition. Instrument at the existing decision point or classify as unknown; do not introduce check-then-act behavior.

Use minimal counters and static labels. Avoid logging user IP or repository because the existing HTTP access logger has route and status sufficient for preliminary chronology. Define journal retention expectations using existing host policy, not a new persistent storage mechanism.

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

1. Does the evidence justify investigating stdout buffering as a visibility issue without calling it the Remove root cause?
2. Should D1 flush existing safe log lines, D2 bounded categorization, both, or neither be authorized?
3. Are fixed categories sufficient and free from credential disclosure, including under errors/concurrent HTTP handlers?
4. Can D2 preserve code execution and nonce consumption ordering exactly?
5. Are retention, rate limits, restart consequences, rollback and operational consent adequately specified?
6. Does the design correctly preserve an INCONCLUSIVE outcome when incident correlation is missing?
7. What precise further implementation/deployment authorization, if any, is being granted?

**Requested review decision:** APPROVE / REVISE / BLOCKED, with a separately identified scope for **design approval**, **diagnostic implementation**, **production deployment**, and **Gate B runtime fix**. None should be inferred from another.
