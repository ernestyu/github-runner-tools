# Web Management V2 — CLI-First Simplification SPEC

Status: **B-04 NARROW REVISION / REVIEW REQUIRED / NOT FROZEN**
Date: 2026-10-10
Repository: `ernestyu/github-runner-tools`
Scope: Web management only. No modification to working CLI behavior or existing deployed runners.

## 0. Objective and governing constraint

Build a small, reliable **single-host, trusted-administrator, Tailscale-only** Web interface around the repository's **existing, proven CLI**. Its three operations are Create, Status, and Remove. The Web layer MUST invoke the same scripts and lifecycle behavior as the CLI, not independently implement registration, state transitions, systemd orchestration, or cleanup. **Working Create and Remove are release gates; security scaffolding is not a substitute.**

This is an intentionally narrower threat model than a public/multi-tenant service. It supersedes the Web V1 architecture **only if and when this SPEC is separately approved and frozen**. Until then the V1 frozen SPEC and current deployed version remain unchanged.

## 1. Known facts and boundaries

- CLI entrypoints: `scripts/register-runner.sh OWNER/REPO`, `scripts/remove-runner.sh OWNER/REPO` (with separately gated `--recover-local`), `scripts/status-runners.sh --json`.
- The current CLI uses interactive temporary GitHub registration/removal tokens and privileged systemd/dependency steps; V1 Web uses Dispatcher + UID-dropped Worker + private privileged channel + Web-only lifecycle checkpoints.
- Debian Web V1 deployment at `4189331db8dd9ec210dae5e077cb1e278bd323fd` succeeded, but a real Web Create failed at `create_preflight exit=1`. Six existing production runner services remained active. This is **not** evidence that the CLI registration itself is broken.
- The current PR #3 and PR #4 remain separate, unmerged records. This SPEC does not authorize their merge or deployment.
- A GitHub temporary runner token is a limited-scope, expiring credential; it does **not** eliminate the impact of local systemd, file deletion, or arbitrary-code execution by runner jobs. Avoid treating temporary tokens as equivalent to full account credentials, but do not assume private Tailscale access makes arbitrary shell execution safe.

## 2. Minimal user workflow

1. Administrator visits the existing authenticated Web endpoint reachable **only through Tailscale Serve**, not Funnel or any public bind.
2. Create: enter exactly `OWNER/REPO` and GitHub temporary **registration** token; click once. Display result and runner identity/status.
3. Status: show runner inventory using **only** `status-runners.sh --json`; clearly show unavailable/error states.
4. Remove: choose an explicitly listed runner repository; show exact identity/path and require a separate, deliberate confirmation; enter temporary **removal** token; click once. Show the actual CLI outcome. No default recover/force/remove-all.
5. Failed requests show a concise, **redacted** error and a correlation identifier; operator should not need to execute repeated diagnostics just to learn which CLI step failed.

No custom user/role system, team administration, remote Docker orchestration, multi-host routing, public API, or generic command execution.

## 3. One source of lifecycle authority

- All real Create/Remove/Status behavior MUST remain in the named CLI scripts and their existing CLI-owned helpers. Web MUST NOT duplicate the CLI's registration decisions, service naming, runner directory scheme, lifecycle persistence rules, cleanup algorithm, or recovery authority.
- If a CLI script presently requires a TTY or sudo interactively, implement **one narrowly scoped, documented noninteractive CLI invocation mechanism**, reusable and testable without the browser; do **not** create another registration/removal implementation. Token delivery through a one-shot private FD or equivalent protected input is allowed.
- No `bash -c`, `sh -c`, `eval`, concatenated command line from browser input, arbitrary executable/path selection, or browser-controlled CLI flags. Use fixed executable path and positional argv with strictly validated `OWNER/REPO`.
- Existing CLI user-facing interactive operation MUST remain usable. A Web failure MUST NOT mutate or weaken CLI behavior.
- V2 SHOULD remove the V1 Web-specific lifecycle state machine, duplicate attestation/transition plumbing and privileged request protocol **where proven redundant to CLI authority**. Do not delete CLI-owned integrity safeguards or data needed for safe Remove/Recover. Migration must explicitly inventory which V1 state records can be retained/read/migrated, and must not silently delete them.
- Status MUST NOT modify runner state.

## 4. Privilege boundary — simple, explicit, and bounded

The Web process must not be root and must not gain general-purpose sudo access. Use **one root-owned, fixed-operation local executor** (or a demonstrably smaller equivalent) with exactly `create`, `remove`, `status` on a validated repository; execution of arbitrary commands, file paths, runner names, shell fragments, or supplementary arguments is forbidden.

The executor must call the proven CLI as the configured runner owner (`actions` on Debian), with only the **specific** elevated operations required by that CLI. If the present CLI's sudo/TTY contract cannot be reused without a broad NOPASSWD grant, the design must adjust **the CLI's own narrow privileged interface** first and prove interactive CLI compatibility. Do **not** grant the Web service root, membership in `sudo`/`docker`, or blanket `NOPASSWD: ALL`. No privilege escalation based solely on browser-supplied input.

The implementation SPEC/PR must explicitly document the executor's exact Unix peer identity, filesystem mode, allowed operations, argument validation, token FD semantics, privileged commands, and shutdown/timeout behavior. No multi-stage privilege broker or duplicate Web lifecycle protocol absent a concrete need demonstrated by tests.

**Design stop condition:** If a working, bounded executor cannot be specified without substantial CLI changes, return the smallest feasible CLI-interface amendment for review before proceeding. Do not improvise privileged shortcuts.

## 5. Security controls — each has a concrete purpose

| Retained control | Concrete risk addressed | Cost / limit |
|---|---|---|
| Tailscale-only entry, loopback Web bind, no Funnel | Excludes direct public/LAN exposure | Requires Tailnet membership/device trust; not a substitute for local privilege restrictions |
| Existing simple Web password/session + CSRF protection | Prevents inadvertent/unauthenticated browser operation and cross-site action | No new RBAC, identity provider, or multi-user framework |
| Validate `OWNER/REPO` and fixed operation allowlist | Prevents injection, path targeting, arbitrary CLI execution | Exactly three operations; no user-selected shell/flags |
| Fixed, root-owned executor and runner-user separation | Prevents Web compromise becoming unrestricted root/sudo | Least-privilege helper only; no general command runner |
| Temporary Token through private FD / no disk or logs | Avoids exposure in argv, environment, process lists, persistent files and diagnostics | Keep it transient; do not build token vault |
| Single-operation serialization | Prevents concurrent Create/Remove on same host | Reuse existing CLI lifecycle lock where possible |
| Exact-target Remove confirmation and fail-closed cleanup | Prevents accidental damage to existing runners and ambiguous removal | No automatic `rm -rf` or unverified recover |
| Redacted, bounded error reporting | Allows practical debugging without credential leakage | Not a separate distributed diagnostics platform |

These are the **only default controls**. Each additional mechanism must cite a specific threat in this operating model, demonstrate why the listed measures are insufficient, and quantify its implementation/test/operational cost; otherwise exclude it.

## 6. Error reporting and operator usability

- Return CLI success/failure faithfully; never turn nonzero exit into a generic unqualified success.
- Capture the minimal CLI phase + exit code and **safe, explicit redacted diagnostics**. Filter all potentially sensitive output, including echoed tokens, shell traces, response bodies and any command line containing credentials.
- Include a request/correlation ID in UI and bounded server logs; do not persist token, entire subprocess stdout/stderr, runner registration files or OAuth headers.
- If a CLI action has uncertain external effects, report **UNKNOWN / REVIEW REQUIRED**, not `failed; retry`. Never automatically retry registration/removal.
- On failure, display the next safe action and prevent repeated Create/Remove submissions until the actual local state is reconciled.
- One aggregated read-only support command may summarize version, service state, sanitized operation phase, expected runner identity, and existing service inventory. Do not require multiple rounds of ad hoc shell commands.

## 7. Preserve currently working installations

- The six production runners on `debian-ci` are protected fixtures, **not** test targets. No stopping, renaming, deleting, modifying, registering-over, or implicit migrating them.
- Create/Remove E2E uses one **disposable** runner only for `ernestyu/github-runner-tools`; verify repo and path do not already exist before provisioning. Git branches are not runner isolation.
- Do not modify arbitrary existing CLI runners or data under `/home/actions`; do not recursively chown or clean broad parent directories.
- Keep backup and rollback procedures for existing Web config, service units and any V1 lifecycle state. Deployment must be all-or-stop, explicit SHA-verified, and preserve Web auth.
- V2 roll-forward must refuse any ambiguous V1 state or partially registered runner and require review. No silent state purge.
- Preserve current CLI registration/removal/archiving contracts; do not loosen provenance safeguards just to make Web pass.

## 8. Development and integration gates

Apply `ernestyu/agent-playbooks/07-development/project-bootstrap/ci-tier-contract.md`:
- **Development Check** on normal development pushes and PRs: fast shell/Python syntax, focused CLI/Web contract, token-handling, allowlist, and simulated Create/Remove/Status regressions; GitHub `ubuntu-latest` is sufficient. Do not automatically run full tests or reinstall large dependencies every push.
- **Integration Check** only on `workflow_dispatch` or release tag: full suite, dependency/environment reconstruction and any required smoke. Real Debian Web lifecycle is a **separate, authorized host acceptance**, not assumed from hosted CI.
- Add one high-signal test that drives the V2 entry point into the **same CLI** operation path, exercising error response/redaction, with no live GitHub token.

## 9. Explicit stage-gated delivery

**Phase A — SPEC only (this submission):** freeze scope, simple architecture, retained security rationale, risk boundaries and acceptance evidence. No runtime implementation or Debian operation.

**Phase B — Implementation:** first prove a noninteractive CLI adapter using a local, simulated, no-secret end-to-end fixture; then connect the UI. Do not add new lifecycle state machine. Run Development Check; independent narrow implementation audit.

**Phase C — Authorized Debian acceptance:**
1. Verify exact SHA and backup state; record six existing running services.
2. Deploy via one repeatable script; verify Web auth, loopback/Tailscale boundary and privilege policy.
3. **Create** one disposable repository-scoped runner with one fresh temporary token; verify online registration and systemd service.
4. **Status** reflects that runner accurately.
5. Run a workflow restricted to this disposable runner only; do not schedule on the six existing runners.
6. **Remove** the disposable runner with one fresh temporary removal token; confirm remote unregister + local directory/unit/state reconciliation.
7. Recheck all six untouched original services and identities. Collect one complete redacted report.
Stop on any unsafe or ambiguous state; never automatically attempt destructive cleanup.

## 10. Acceptance criteria / hard prohibitions

**MUST PASS**
- UI Create/Status/Remove invoke the existing CLI paths and produce the same lifecycle outcome.
- No second Web-owned registration/removal implementation or state machine.
- Security controls are limited to those in §5 or individually justified in review.
- Neither token appears in argv/env/logs/state; no generic root shell or broad sudo.
- Safe error information is visible without repeated Token retries.
- CLI interactive behavior and six existing runners remain unchanged.
- Development Check PASS; independent implementation audit PASS.
- Real Debian end-to-end Create → Status → dedicated Job → Remove PASS; no credit for mock-only success.

**MUST NOT**
- Merge PR #3/#4, alter existing frozen SPEC, or deploy from this document alone.
- Reuse a production runner as the test subject or turn public access on.
- Auto-run Integration Check on ordinary push/PR.
- Treat Tailscale/private tokens as justification for general-purpose privileged command execution.

## 11. Architecture review questions (explicit decisions)

Reviewers must answer:
1. Is the three-command Web wrapper genuinely CLI-first, or does the proposed bounded executor recreate V1 complexity?
2. Can the current scripts' interactive sudo + TTY behavior be reused with a *minimal* CLI-owned noninteractive entrypoint? If not, identify the exact smallest required CLI API change.
3. Are all retained controls traceable to the table in §5, and is any important control missing?
4. Are V1 lifecycle records and the six existing runners protected during migration and rollback?
5. Do the user-visible diagnostics reliably distinguish preflight, partial registration and uncertain state without exposing credentials?
6. Are CI tiers and Debian E2E acceptance feasible and unambiguous?

**Approval condition:** The reviewer must mark PASS/BLOCKED for architecture feasibility, reuse of CLI authority, least privilege, minimality, migration safety, testability, and acceptance. Unresolved privileged-call design counts as BLOCKED; do not grant implementation permission merely for a plausible-looking diagram.


## 12. Binding amendment — concrete V2 execution contract (B-01 and B-02)

This section resolves architecture options in §§3–4; where earlier prose was permissive, **this section is binding**. The selected design is **one resident root-owned bounded Executor service with a Unix-domain socket**, invoking the **same CLI scripts** as the interactive terminal. There is **no second lifecycle Worker Python process, separate privileged-loop thread, independent Web registration state machine, or two-hop Web-specific privileged lifecycle authority**. Do not implement an alternative sudoers-only architecture.

### 12.1 Fixed request interface and admission

- Web service: existing unprivileged \`grt-web\`, bound to \`127.0.0.1\`; Tailnet HTTPS via Tailscale Serve. Executor: systemd \`github-runner-tools-executor.service\`, \`User=root\`, \`NoNewPrivileges=true\`, \`ProtectSystem=strict\`, with only the exact write paths required for CLI lifecycle state, \`/home/actions\`, managed systemd unit installation and runtime socket/lock directories. Socket \`/run/github-runner-tools/executor.sock\`, owned \`root:grt-web\`, mode \`0660\`.
- On every connection Executor checks kernel \`SO_PEERCRED\` for the configured \`grt-web\` UID (not JSON-supplied identity); rejects all other callers before parsing credentials. The socket pathname, server code and service files must be root-owned and not writable by \`grt-web\` or \`actions\`. Limit request size; serialize mutation requests by the **existing** shared lifecycle lock; do not introduce a second lock authority.
- JSON metadata is exactly \`{"op":"create","repository":"OWNER/REPO"}\`, \`{"op":"remove","repository":"OWNER/REPO","runner_name":"<verified>","runner_id":<verified integer>,"instance":"<verified instance ID>"}\`, or \`{"op":"status"}\`. Reject additional/missing keys. Repository is validated by the existing shared CLI identity function. The Remove tuple is a *selection claim* only: Executor independently verifies the expected directory, registered \`.runner\` identity, root-owned attestation, GitHub runner ID where available, expected service and active state immediately before any mutation. A changed or ambiguous target yields \`TARGET_STALE\` and performs no removal. Never trust a browser-provided path, service name or CLI flag.
- \`status\` spawns \`status-runners.sh --json\` as \`actions\`, without a token or mutation lock; filters fields returned to Web and does not change state. Create/Remove spawn fixed installed \`register-runner.sh\`/\`remove-runner.sh\` entrypoints with **no arbitrary user-supplied options**. All CLI paths must be pinned, root-owned, read-only to \`grt-web\` and to ordinary runner jobs; do not execute a writable Git checkout as authority.
- Executor child begins as root only to set supplementary groups to empty and permanently \`setresgid(actions)\`/\`setresuid(actions)\` before \`execve\` of the CLI. Verify post-drop real/effective/saved IDs and no effective/permitted/inheritable/ambient capabilities; do not rely on an \`execve\` setuid binary. The Executor parent retains the narrowly bounded root actions described in §12.3. No API to execute scripts supplied by clients.
- Parent → CLI private Unix \`socketpair\` for **privileged operations only**, and separate one-shot pipe for token; both passed as fixed FD numbers (e.g. \`3\` token, \`4\` privilege channel). These are **process-internal transports**, not independent privileged Web worker services. Executor checks that each internal privileged request belongs to its current authenticated operation, repository, instance and runner directory; reject any operation outside the current transaction stage.

### 12.2 Frozen noninteractive CLI contract

The existing interactive CLI and its implementation remain the source of lifecycle behavior. Add only an **internal executor invocation mode** to the three scripts; no public \`--token\` argument and no alternate \`web-worker\` lifecycle logic.

| Operation | Fixed CLI argv after privilege drop | Secret / confirmation |
|---|---|---|
| Create | \`/usr/local/lib/github-runner-tools/cli/register-runner.sh --executor-mode --token-fd 3 --privileged-fd 4 --lock-already-held OWNER/REPO\` | Private token FD 3, one registration token, no TTY |
| Remove | \`/usr/local/lib/github-runner-tools/cli/remove-runner.sh --executor-mode --token-fd 3 --privileged-fd 4 --lock-already-held OWNER/REPO\` | Private token FD 3, one removal token, no TTY |
| Status | \`/usr/local/lib/github-runner-tools/cli/status-runners.sh --json\` | No token, no confirmation, always unprivileged |

- These names and paths are **target V2 interfaces to implement**, not claims that current scripts already accept them. \`--executor-mode\` is internal: reject it absent a verified private channel whose \`SO_PEERCRED\` peer is the Executor root process and a matching request-scoped response. Reject invocation from ordinary \`actions\` shell with forged environment flags or FDs. Terminal CLI defaults to original TTY token prompt, Remove \`REMOVE\` confirmation and interactive sudo authorization.
- Web authenticates the administrator and obtains a deliberate Remove confirmation in its session; Web verifies authentication, CSRF and an explicit target-specific Remove confirmation **before** forwarding a Remove request. Executor does **not** mint, accept, validate or persist a confirmation nonce: §12.1's fixed Remove JSON is the complete request metadata schema. Executor authorizes the request solely through kernel-verified Web peer UID, strict target tuple and current lifecycle attestation checks, and the existing shared mutation lock. CLI executor-mode treats the Executor-admitted Remove request as equivalent to its own interactive \`REMOVE\` prompt; it does not ask TTY again. No separate confirmation service, token, nonce field or replay ledger is introduced. A repeated identical request after completed Remove must be refused by missing/changed attestation, target state or instance; concurrent repeats are serialized and refused as BUSY. After an UNKNOWN outcome, retries remain prohibited pending independent reconciliation. This is state-based duplicate prevention, **not cryptographic replay protection** and no at-most-once guarantee is claimed across crashes. \`recover_local\` is deliberately **not** a V2 Web operation; leave it CLI-only under separate manual administrator control. \`recover_local\` is deliberately **not** a V2 Web operation; leave it CLI-only under separate manual administrator control.
- Web sends the short-lived token once over the authenticated Unix socket with a bounded binary length field separate from JSON metadata. Executor validates bytes and never serializes them into JSON, argv, env, journal or files; creates a CLOEXEC pipe for transmission, clearing CLOEXEC **only** on the numeric read descriptor intentionally passed into the dropped child. Parent closes the token read descriptor after spawning and its write descriptor after sending once; child closes its token FD immediately after handing it to the official GitHub configuration adapter. No retries or token caching; wipe intermediate application buffers as best effort, with no claim of guaranteed memory erasure.
- CLI stages, lock, registration, service handling, remote outcome recording and cleanup are **shared functions/branches** for interactive and executor modes; only credential/confirmation acquisition and privileged-call transport differ. Existing V1 \`--web-worker\` functions must not become an alternative lifecycle implementation. Explicitly prove shared behavior through tests calling both entry modes against the same simulated backends.

### 12.3 Enumerated root operations; no general sudo

Executor exposes **no direct root API to the browser**. Internal CLI ↔ Executor privileged requests have a fixed enum and typed fields bound to the active transaction:

- \`create_stage\`, \`registration_attestation_create\`, \`registration_permission_normalize\`, \`unit_validate_normalize\`, \`unit_attestation_create\`, \`remove_stage\` — operate only in \`/var/lib/github-runner-tools\` with validated CLI-owned state schema and verified instance.
- \`service_install\`, \`service_start\`, \`service_state\`, \`service_stop\`, \`service_uninstall\` — only for the canonical, validated \`actions.runner.*\` unit belonging to the exact selected runner; never arbitrary systemd unit names or shell commands.
- \`runner_dependency_install\` — only if required for the pinned official runner distribution, using a reviewed **root-owned dependency installation entrypoint with fixed validated inputs**; no execution of downloaded \`bin/installdependencies.sh\` with unrestricted root privileges. If the required dependencies are already present, this operation is a no-op; unexpected dependency requirements fail closed for administrator-controlled host setup.
- Existing lock initialization if needed — root-owned \`/run/lock/github-runner-tools\` with configured runner-group rights, never browser-selected locations.

The Executor implements these using direct filesystem APIs or fixed \`systemctl\` argv, with strict service/unit validation and no generic \`sudo\` request. \`sudo ./svc.sh ...\` within executor-mode CLI **must be routed to the corresponding fixed privileged enum**; interactive CLI remains unchanged. Root operations require the current transaction’s immutable repository/instance/directory binding. Web cannot select privileged enum operations independently.

There is one Executor privileged-operation table, one token delivery mechanism and one CLI lifecycle flow, rather than V1's separate Web worker, per-operation adapters and Web-controlled stages. For reviewer verification, implementation must include a call graph comparing V1 and V2 processes and enumerate all remaining root-owned components; merely renaming Dispatcher is not a valid simplification.

### 12.4 Timeout/crash/unavailable GitHub semantics

- Executor acquires the shared mutation lock **before** CLI invocation and releases it only after the CLI process group and in-flight privileged requests have terminated; requests arriving while busy receive \`BUSY\`.
- A timeout sends TERM to the dedicated process group, then a bounded KILL after grace period, closes FDs and records \`OUTCOME_UNKNOWN\` if official GitHub Create/Remove could have started. It must **not** delete directories or re-run \`config.sh\` solely because of timeout.
- If Executor itself crashes, systemd restarts it but **never automatically resumes** incomplete Create/Remove. Existing durable CLI state is checked on every admission; missing/ambiguous evidence fails closed. A persistent per-instance operation stage must be written **before** each external registration/removal boundary so an abrupt crash is distinguishable from confirmed completion.
- UI displays \`COMPLETE\`, \`FAILED_PRE_EXTERNAL\`, or \`UNKNOWN_RECONCILIATION_REQUIRED\` plus a safe correlation ID. No raw token, CLI stdout/stderr or traceback reaches the UI or journal. GitHub registration/removal is not auto-retried. The administrator must use existing reviewed CLI reconciliation/recovery procedures for ambiguous cases.

## 13. Binding amendment — V1 safety records and migration (B-03)

**Choice: retain the existing root-owned lifecycle authority schema in V2. No migration or deletion in first V2 release.** CLI becomes the *sole calling authority* and the fixed Executor is only the root write/verify adapter. The schema's safety facts are shared by CLI and Web; the Web server never writes them. Existing state records remain at \`/var/lib/github-runner-tools\` in original layout, with ownership, permissions and historical identity preserved. Removal of V1 *application orchestration* does **not** imply removal of lifecycle evidence.

| V1 data/capability | V2 binding decision | Writer / verifier |
|---|---|---|
| Browser sessions, confirmation forms | Reuse minimal Web auth/CSRF and explicit target-bound UI confirmation; do not introduce Executor nonce issuance or replay storage | Unprivileged Web performs user confirmation; Executor independently verifies peer UID, exact Remove tuple and lifecycle state |
| Create transaction / instance ID and active-record pointer | **KEEP unchanged**, one new instance per attempt, no overwriting old cycles | CLI shared lifecycle code via Executor |
| Root-owned registration attestation and GitHub \`agentId\` binding | **KEEP unchanged**; missing or mismatch blocks remove, recovery or replacement | Existing CLI authority helpers via Executor |
| Unit attestation and installed-unit provenance | **KEEP unchanged**; exact directory and service/unit identity required | Existing CLI authority helpers via Executor |
| \`REGISTRATION_OUTCOME_UNKNOWN\` and \`REMOTE_REMOVE_OUTCOME_UNKNOWN\` | **KEEP** sticky until independently reconciled; block new Create/Remove on that identity | CLI state guard; Executor enforces admission |
| \`REGISTERED_REMOVED_LOCAL_CLEANUP_PENDING\` | **KEEP**; only verified recovery may complete local cleanup, never repeat remote removal | Existing CLI recovery/authority; Web cannot force |
| Completed Create/Remove cycles and history | **KEEP** immutable per-instance/cycle records | CLI authority; no silent purge |
| Six existing Debian Runner services and their directories | **UNCHANGED**, read-only inventory only; no automatic adoption, recertification or migration | Protected external installation fixtures |
| Web-only V1 privileged RPC framing and worker orchestration | **REMOVE AFTER V2 ACCEPTANCE**; not historical lifecycle data | No writer; do not remove until rollback window ends |

Migration process is **no data conversion**: (1) snapshot existing root-owned state and SHA/ownership/mode of any records; (2) verify schema readable using the existing authority library without modification; (3) refuse deploy if an incomplete or unknown active record exists for the disposable test repo; (4) install V2 side-by-side or with a reversible service switch, leaving the V1 code/config and records archived root-only; (5) use existing records unchanged; (6) rollback reactivates V1 code/config and the **same state directory without replay, downgrade or rewriting data**. If V2 has written new records and older V1 cannot interpret them, rollback **must refuse state mutation** until a reviewed recovery plan is approved. No automatic state truncation, flattening, synthetic \`REMOVE_COMPLETE\` or migrated replacement ID.

State transition invariants must be tested for active-pointer reuse, instance binding, remote unknown, cleanup pending and historical cycles in both invocation modes. No V2 path may bypass these checks because the token is temporary or Tailnet access is private.

## 14. Binding amendment — Create Unit compatibility (M-01)

The CLI must handle the official GitHub runner \`svc.sh install\` path without weakening provenance checks. V2 executor-mode shall **not invoke the official \`svc.sh install\` as root and then silently fix an untrusted Unit**. Instead:

1. Generate/install the known canonical Unit through the Executor's fixed \`service_install\` operation using a root-owned **pinned approved template** and validated repository/runner identity, with owner \`root:root\`, mode \`0644\` from creation; the runner directory and \`runsvc.sh\` must already be validated and attested.
2. Validate no preexisting unit or symlink; generate into same filesystem temporary file with \`O_EXCL\`, write/fsync, chmod/chown and atomically rename; run \`systemctl daemon-reload\` and \`enable\` using fixed argv.
3. Use the existing service/Unit attestation and systemd verification logic. Any fragment/execution/drop-in mismatch fails closed and preserves state; no blanket \`chmod\` on unknown Units.
4. Interactive CLI may retain \`svc.sh install\` only when existing official Unit provenance/permission guards accept it. V2 implementation must provide a shared validated installation function, and **CLI interactive Create must also use the known canonical template by default** so both modes reach the same known-good Unit path; changing interactive CLI behavior must be regression tested and explicitly noted as a necessary compatibility change, not concealed as a Web workaround.
5. This affects new Runner Units only; no service-unit rewriting of six existing Runners.

## 15. Binding amendment — exact Remove identity (m-01)

V2 Web supports one runner per canonical repository-derived directory in the initial release. The Status row is derived from verified \`.runner\` metadata and canonical local service information (not untrusted labels alone). The UI displays \`OWNER/REPO\`, directory identity, runner name, and GitHub runner ID; before presenting Remove it resolves the root-owned attestation and active instance, otherwise disables removal. Browser presents the confirmed tuple through the authenticated CSRF-protected Web form and Web sends **only** the exact §12.1 Remove tuple; Executor re-reads and independently compares all identity fields. No confirmation nonce or extra JSON field crosses the Web-to-Executor boundary. Multiple runners per repository, legacy alias directories, missing ID/attestation, service mismatch or ambiguous historical active pointer are **not supported for Web Remove** and require manual CLI administrator review. Do not infer runner identity from repository alone.

## 16. Revised architecture acceptance checklist

- One bounded Executor daemon with authenticated Unix socket, no separate Worker daemon, no arbitrary sudo interface.
- CLI invocation mode and FD protocol exactly as §12; interactive TTY behavior preserved, single shared lifecycle logic.
- Enumerated privileged operations only; execute reviewed root-owned binaries/templates, not downloaded root scripts.
- No destructive V1 record migration; retain existing root-owned safety facts and exact history.
- New Unit installs use fixed trustworthy root-created \`0644\` template and preserve Unit validation.
- Remove bound to repository + runner name + verified GitHub ID + active instance, not repo string alone.
- Prove **reduced** processes, control messages and independent lifecycle branches with a before/after inventory in implementation audit; otherwise architecture is BLOCKED.
- Run focused Development Check for the SPEC changed HEAD; do **not** run Integration Check or deploy before separate authorization.


## 17. Binding B-04 clarification — Remove confirmation and duplicate-request tests

Selected scheme: **Web-side confirmation, Executor-side authorization and state validation; no Executor nonce.** The strict §12.1 Remove JSON remains unchanged with exactly four target attributes plus `op`. The Executor receives no CSRF value, nonce, signed blob or confirmation proof. Browser POST must pass authenticated session + CSRF and an explicit confirmation screen displaying repository, runner name and GitHub runner ID. Web must reject a changed selection between display and submit. It then submits the fixed tuple exactly once per accepted HTTP request. Disabling the submit control and rejecting duplicate Web form submissions is a usability measure, **not** the security authorization boundary; malicious/replayed requests from an authenticated Web process are limited by Executor target/instance guards and the existing mutation lock. Terminal CLI retains its independent TTY `REMOVE` prompt.

Required focused regression tests: (1) Web refuses Remove without login, valid CSRF, deliberate confirmation or matching displayed target; (2) Executor rejects extra JSON keys, wrong peer UID, incorrect repository/name/runner ID/instance, changed active state or missing attestation; (3) a concurrent duplicate receives BUSY; (4) repeat after successful removal is denied without a new remote request; (5) repeat after timeout/remote UNKNOWN is denied until reviewed reconciliation; (6) restart Executor and verify completed/UNKNOWN durable state still blocks duplicate Remove. No nonce replay database is necessary. These tests verify state-based duplicate rejection, not universal network at-most-once delivery.
