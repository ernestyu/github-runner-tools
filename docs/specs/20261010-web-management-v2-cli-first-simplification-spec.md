# Web Management V2 — CLI-First Simplification SPEC

Status: **PROPOSED / REVIEW REQUIRED / NOT FROZEN**
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
