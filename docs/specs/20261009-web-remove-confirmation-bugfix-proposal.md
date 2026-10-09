# Web Remove Confirmation Failure — Revised Bugfix Proposal (Audit Round 2)

**Repository:** `ernestyu/github-runner-tools`  
**Baseline:** `fd9a85bcaede4a88c9da94d8bc6b08b14abbab3b`  
**Scope:** Web Management V1, Remove confirmation; shared confirmation helper only where necessary  
**Status:** PROPOSED / INDEPENDENT RE-AUDIT REQUIRED  
**Implementation authorization:** NOT GRANTED  
**Root-cause reproduction authorization:** NOT GRANTED until this proposal passes audit

## 1. Observed incident and evidence classification

On 2026-10-09 the operator attempted to remove the discontinued `ernestyu/AgentCI` runner. The browser displayed `Confirmation expired or invalid.` after the operator copied the temporary removal token from GitHub's normal removal instructions and immediately submitted the Web confirmation form. The operator reports that the interval was short. A subsequent CLI status inspection still reported `configured=true`, `service_state=active`, `management_state=configured`; no matching Web service restart appeared in the checked 60-minute journal window.

**Production observation, not a proven cause:** a confirmation request was rejected; the runner remained configured. Neither the browser's submitted form fields nor the exact nonce/session history were captured. A restart outside the checked window, a stale/duplicated form, session mismatch, or clock/expiry condition has not been ruled out.

**Code fact:** `web/app.py` stores confirmations in a process-local `SESSIONS[sid]["confirm"]` dictionary; `CONFIRM_TTL=300`. `consume_confirmation()` performs `pop(nonce)` **before** checking operation and expiration. The Remove handler calls it **before** validating the temporary token format. Thus an operation mismatch, expired confirmation, or bad token can consume a nonce before any Dispatcher call. This is independently demonstrable code behavior, **not evidence that it caused the reported production failure**.

**Established boundary:** the exact `Confirmation expired or invalid.` branch returns 403 before constructing/sending the Remove request to the Dispatcher. This establishes only that **that rejected HTTP request** did not start removal; it does not establish the outcome of an earlier submission of the same nonce.

## 2. Two gates; no implementation shortcut (B-01)

### Gate A — Root-cause Evidence Gate (investigation only)

Gate A work is authorized **only after independent approval of this revised proposal**. Its initial change set may add non-production HTTP reproduction tests, controlled test fixtures, and a reviewed plan for bounded secret-free diagnostic evidence. It must **not** alter runtime confirmation consumption, session lifecycle, cookie attributes, CSRF, TTL, or production behavior. Adding production instrumentation, even if secret-free, requires a separate explicit audit authorization; its design alone is allowed in Gate A.

Investigation must record: exact request sequence and elapsed time; response codes and redirect locations; whether the same session cookie and CSRF values are used (compare in-memory inside tests, never print secrets); nonce creation/lookup/consumption behavior; whether the controlled Dispatcher is invoked; and browser-versus-test-client cookie handling. Distinguish (a) real incident observation, (b) source-proven flaw, (c) reproduced behavior, and (d) hypothesis.

**Gate A PASS** requires a deterministic reproduction of the reported symptom with evidence for the responsible state transition, **or** direct bounded production diagnostic evidence corroborating a specific failure path approved for collection. It must also demonstrate a corrected test expectation that fails against baseline and corresponds to that evidenced cause. The existence of premature `pop` alone does not satisfy this causal gate.

**Gate A BLOCKED/INCONCLUSIVE:** if the tests only establish an unrelated code flaw, or cannot reproduce/explain the production failure, stop. Deliver a diagnostic report, remaining hypotheses and a new audit request. Do not implement speculative fixes, extend TTL, modify cookies, or claim the incident root cause is known. A confirmed separate flaw may be proposed as a separate scoped change only after explicit approval.

### Gate B — Runtime Fix Authorization

Only after Gate A passes and independent reviewers approve its evidence and minimal-fix scope may runtime code and related tests be changed. CI PASS is necessary but insufficient; independent code review precedes live Debian Remove acceptance. Production mutation must not be used for root-cause discovery.

## 3. Confirmation state-transition contract (B-02)

The following is the **proposed target contract**, not a description of current behavior. A confirmation is `PENDING(op, repository, expires, session)`, `CLAIMED` (irreversibly used), `EXPIRED`, or absent. `CLAIMED` and `EXPIRED` cannot become `PENDING` again. New prepare generates a new unpredictable nonce.

| Event / guard | Transition | Dispatcher invoked? | User-visible behavior |
| --- | --- | --- | --- |
| Successful prepare with eligible repository | absent → PENDING, bound to current session/op/repository | No | Render fresh confirmation |
| Missing nonce, wrong session, or already claimed nonce | unchanged/absent → reject | No | Confirmation invalid; refresh status |
| Correct session, wrong operation endpoint | PENDING remains PENDING for its original operation; reject | No | Invalid confirmation |
| Expired nonce at lookup | PENDING → EXPIRED/removed | No | Expired confirmation |
| Explicit expiry pruning | expired PENDING → removed | No | No side effect |
| Invalid/expired login session | reject; session cannot authorize future operation | No | Reauthenticate, refresh status |
| Invalid CSRF | reject; no nonce claim | No | CSRF rejected |
| Missing/malformed temporary Removal Token | PENDING remains PENDING *if still valid*; reject | No | Token format invalid; stay on form, no token echo |
| All guards pass (session, CSRF, nonce, op, expiry, eligible form data/token format) | PENDING → CLAIMED **atomically before** the mutating Dispatcher request | At most one | Submit once |
| Dispatcher returns failure or cannot be reached after claim | CLAIMED remains consumed; no silent retry | Attempted once (outcome may be unknown) | Refresh inventory / verify actual state |
| Dispatcher returns success | CLAIMED remains consumed | Once | Refresh inventory |
| Sequential replay / concurrent duplicate confirm | CLAIMED remains consumed; reject replay | No additional invocation | Prior outcome cannot be inferred |
| New explicit prepare after checking current inventory | new nonce, independent of old nonce | No | Fresh confirmation |

Token syntax checking is **not** proof of GitHub token validity. GitHub token rejection after Dispatcher submission remains a claimed/consumed confirmation. The same rule applies to lifecycle failures, network ambiguity, exceptions, and client disconnects after claim. No automatic mutation retry. The target contract intentionally lets a correctly bound nonce survive **pre-claim** token-format and CSRF failures but never a request that crosses the mutation authorization boundary.

A wrong-operation submission must not consume an otherwise-valid confirmation for a different operation. A nonce tied to a different session must never be visible or consumed in the present session. A forged nonce and an expired nonce cannot authorize any operation. An expired entry may be removed opportunistically or by bounded cleanup, but expiration denial cannot depend on cleanup having run.

The repository binding is server-side: the confirm handler obtains the repository exclusively from the pending confirmation, never from a client-provided repository field.

## 4. Concurrency and replay contract (B-03)

The Web server uses `ThreadingHTTPServer` and shared process-local session state. Gate B implementation must provide a demonstrable linearization point for validation-plus-claim of a nonce, including expiry, expected operation, owning session, and claim status. For **one valid nonce, at most one HTTP request** can cross into the mutating Dispatcher path.

A bare check-then-`pop` split is forbidden. The implementation may choose a narrowly scoped lock or another justified atomic mechanism, but must prove that Session pruning/invalidation and nonce claim cannot race to authorize an invalid session. `SESSIONS` lookup, `sess["confirm"]` access, expiration checks, and relevant cleanup must have consistent synchronization where authorization depends on them. Do not assume the Python GIL or individual dictionary operation atomicity establishes the full invariant.

Independent nonces must retain their own state: a claim, wrong-op request, expiry cleanup, or failure for nonce A cannot erase nonce B. Separate mutations may still be serialized by the existing Dispatcher/CLI mutation lock; do not weaken it or conflate it with nonce atomicity.

A replaying request cannot infer whether the first attempt succeeded. After a claim and before any retry attempt, the operator must refresh actual local runner inventory and, when needed, verify GitHub registration independently.

## 5. HTTP / Cookie / proxy test contract (B-04)

Tests must exercise one actual HTTP server instance with real HTTP exchanges and a controlled, non-mutating Dispatcher double:

```text
POST /login (credentials)
  → 303 + Set-Cookie
GET / with returned Cookie
  → 200 + CSRF parsed from HTML
POST /remove/prepare (Cookie + CSRF + repository)
  → confirmation HTML with nonce
POST /remove/confirm (same Cookie + CSRF + nonce + synthetic token)
  → Dispatcher spy records ≤1 remove call
```

The fixture must use an actually eligible synthetic runner in list results and must never contact GitHub or mutate systemd/runner directories.

Minimum matrix:

| Test | Required assertion |
| --- | --- |
| Login success, 303 Location, cookie round-trip, GET /, prepare, confirm | One controlled Remove dispatch, correct bound repository |
| Cookie policy | `Path=/`, `Secure`, `HttpOnly`, `SameSite=Strict`; login sets correct cookie; logout clears it |
| Cookie/session failures | Missing, modified, expired and stale cookie safely rejected |
| CSRF | Correct accepted; absent/incorrect rejected before claim |
| Wrong, absent, expired nonce; wrong endpoint/op | Rejected; wrong op does not consume valid original-op nonce |
| Cross-session nonce | Rejected; original session confirmation unaffected |
| Sequential replay | Exactly one dispatch |
| Same-server concurrent replay (barrier, not just sequential mock) | Exactly one dispatch under race |
| Two independent nonces and concurrent confirms | No accidental cross-deletion; mutation lock outcome preserved |
| Session prune racing with confirmation claim | Either valid single claim or pre-dispatch denial; never invalid-session authorization |
| Invalid token format pre-claim | No dispatch and nonce handling matches transition table |
| Dispatcher failure/timeout or unknown outcome after claim | No automatic replay; nonce consumed |
| Browser tab switching / multiple forms in same session | Same Cookie and distinct nonces remain correctly scoped |
| Stale form following Web restart (new process/session store) | Safe denial; new login/prepare required |
| Redaction | No token, cookie, CSRF, nonce or request body in logs/diagnostics |
| Tailscale Serve reverse proxy headers | Headers neither grant auth nor change authority; trusted HTTPS external origin remains required |

**Test capability boundary:** raw HTTP clients that inject a `Cookie` header prove server protocol and session handling, **not browser enforcement** of Secure or SameSite. Separately assert `Set-Cookie` attributes; perform one actual-browser-through-Tailscale-Serve acceptance of HTTPS, redirect, cookie persistence and cross-tab return after code audit. `Secure` cookies are not expected to be accepted by real browsers from arbitrary plain HTTP origins; local plain-HTTP tests must not claim to validate browser Secure policy. Any simulated forwarded headers are untrusted inputs, not a reason to weaken cookie settings.

## 6. Safe UX and recovery contract (B-05)

Rejected pre-dispatch request:

> This confirmation is no longer valid. **This request was rejected before a removal operation was started.** Refresh the runner list and verify its current state before starting a new confirmation.

This wording describes **only the current rejected request**. It must not say `No runner removal was performed` without qualification, because a previous request may have been claimed and executed.

After a claimed request gets a Dispatcher error or uncertain response:

> The outcome of the previous removal attempt may be unknown. Refresh the local runner list and verify GitHub runner registration before attempting any new removal.

After a confirmed success, redirect to refreshed inventory or display a clear result. A new confirmation must always be initiated by an explicit user action after checking current status. Never auto-resubmit a temporary removal token; never retain it in a URL, cookie, session, page echo, or log. Maintain distinct CSRF/confirmation/token-format/operation failure messages without exposing internal secrets or claiming business outcomes that cannot be verified.

On the confirmation page, explain that the GitHub removal token comes from the repository's official Runner removal instructions, advise obtaining it **before** opening a short-lived confirmation form, and distinguish normal remove from GitHub's force removal. Any link must be constructed from a validated repository identifier; no new PAT, GitHub App, or API credentials are introduced.

## 7. Scope, non-negotiable invariants, and evidence deliverables

**Gate A allowed changes:** only test-only reproduction harness/tests and diagnostic design documentation, not runtime semantics. **Gate B expected code scope:** `web/app.py`, `tests/test-web-management.py`, `tests/test-web-contracts.sh`, plus minimal Web markup in `app.py` if needed. No changes to `web/dispatcher.py`, `web/lifecycle_worker.py`, runner lifecycle scripts, installer, systemd sandbox or Tailscale, unless separately approved with fresh evidence.

Preserve login Session idle/absolute bounds, existing cookie policy, CSRF, unpredictability of nonce, repository/op/session binding, request-scoped token transport, least-privilege Dispatcher and Worker architecture, shared mutation lock, and fail-closed lifecycle semantics. No privileged or public network listener expansion.

Evidence required at Gate A: reproducible HTTP trace with redacted metadata; baseline failing test; mapping of state transition to incident; alternative hypotheses ruled in/out; evidence-gate decision and commit SHA. Evidence required at Gate B: implementation diff and justification, state-transition test results, same-instance concurrent replay evidence, complete HTTP cookie/proxy test matrix, full `bash tests/run-all.sh`, GitHub CI run ID/conclusion, independent code audit. Test runs cannot substitute for real-browser acceptance.

Final Debian production acceptance may use **only** the explicitly authorized `ernestyu/AgentCI` disposable/end-of-life runner, after Gate B independent audit; check GitHub registration, local runner directory and systemd service are removed, other runners untouched, and archives preserved. No live mutation before approval. If the expected state has already changed, stop rather than guessing or switching targets.

## 8. Blocker disposition for re-audit

| Blocker | Revised contractual closure | Status |
| --- | --- | --- |
| B-01 | Separate evidence-only Gate A from Gate B; inconclusive reproduction stops implementation | Submitted for audit |
| B-02 | Complete nonce state-transition table and pre-/post-claim failure semantics | Submitted for audit |
| B-03 | Explicit atomic authorization/claim, session pruning race, same-server concurrency evidence | Submitted for audit |
| B-04 | Full login Cookie HTTP round-trip, 303, browser policy distinction, Tailscale real-browser acceptance | Submitted for audit |
| B-05 | Request-scoped outcome wording, uncertain prior result, refresh-before-retry, no auto resubmit | Submitted for audit |

**Requested independent decision:** APPROVE / REVISE / BLOCKED. An approval here authorizes **Gate A investigation only**; runtime implementation still requires an explicit Gate B authorization after evidence review.
