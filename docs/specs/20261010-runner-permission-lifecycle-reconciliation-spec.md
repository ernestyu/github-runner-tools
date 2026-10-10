# Runner Permission Contract & Lifecycle Reconciliation — SPEC

Status: **DRAFT — independent SPEC audit required**  
Repository: `ernestyu/github-runner-tools`  
Baseline: `fc8f6e2f16c366cba313819248579ff58df2b04b` (audited Service Uninstall Reconciliation implementation)  
Incident environment: Debian `actions` runner owner; HyperGrid and AgentCI observations supplied by operator.  
Scope: Create, normal Web Remove, verified historical runner permissions, and terminal local-cleanup semantics. Production changes, real Runner removal and unreviewed cleanup are **not authorized**.

## 1. Observed failures and non-assumptions

HyperGrid `ernestyu/HyperGrid`, `/home/actions/actions-runner-ernestyu--hypergrid`, was shown Active but Web Remove stopped at `preflight_failed` before intended service mutation. Its `.runner` and `.credentials` are `actions:actions 0664`; `.service` is `actions:actions 0644`. The current `scripts/web-service-record.py` rejects group/world-writable configured metadata via `mode & 0o022`. Consequently this permission mismatch explains the observed `identity` exit 1; this is **not evidence** of incorrect repository metadata or removal token. No live systemd Unit metadata for HyperGrid was yet provided.

A previous **manually completed** AgentCI unregister left `/home/actions/actions-runner-ernestyu--agentci` (1.1 GB), with `.runner`, `.credentials` and `.service` absent and `status-runners.sh --json` reporting `repository:null, management_state:incomplete`. This proves a local residue exists, **not** that the latest Web Remove success path wrongly reported success. There is no valid identity record for current Recover Local, so do not automatically delete this directory on a mere basename match.

The baseline `register-runner.sh` invokes upstream `config.sh` to register and, in Web mode, privileged Dispatcher `service_install`; it does not normalize `.runner`/`.credentials` permissions after registration. Normal CLI mode uses upstream `sudo ./svc.sh install`, whose v2.328.0 template creates mode-0664 Unit; Dispatcher strict verification rejects group-writable Units. The actual creating process and umask of historical files is **unproven**; tests must establish it rather than assert it.

## 2. Invariants, authority and forbidden shortcuts

1. User-facing success means **all three independently proved**: (a) official GitHub unregister completed positively, (b) expected systemd Unit absent, (c) expected Runner directory absent. An Offline GitHub status alone is not successful unregister; directory absence alone proves neither.
2. Never do a blind `chmod -R`, global group permission change, global migration of all runners, direct unvalidated `rm -rf`, generic privileged file repair, or generic `systemctl` request. No arbitrary path, UID or unit may be supplied to new privileged operations.
3. No automatic removal of `.runner`, `.credentials` or directory on unconfirmed/unknown GitHub unregister; no retry if `.service` absent or `.grt-service-reconcile-*` residue is present; retain the frozen UNKNOWN re-entry gate.
4. No changing ownership of unexpected files; no following symlinks; no hardlink manipulation; no group/world-writable Runner directory accepted. Must not touch another Runner's files, service or token.
5. Maintain existing root Dispatcher versus runner-owner Worker separation, authenticated IPC, mutation lock, deadlines, token FD/PTY handling, diagnostics allowlist and server-side confirmation.
6. Only **fixed, verifiably safe and narrowly identified historical permission states** are eligible for automatic reconciliation. Unrecognized state fails closed with an actionable fixed diagnostic; no SSH/manual chmod for eligible normal historical state.
7. Existing frozen Service Uninstall Reconciliation SPEC and audited implementation remain authority for service record quarantine and remote-result semantics. This new SPEC adds permission handling and successful local cleanup verification; it must not redefine prior security gates.

## 3. Fixed permission contract

| Artifact | New Create target | Legacy auto-eligible | Identity owner and condition |
|---|---|---|---|
| Runner directory | owner `actions`, 0755 or stricter, **no group/other write** | existing secure owner and mode only | anchored canonical scoped directory; no symlink components |
| `.runner` | owner-only 0600 | 0644/0664, **only if all eligibility constraints of §4 hold** | regular file, expected runner UID/GID, nlink=1, exact repo URL and runner agent name |
| `.credentials` | owner-only 0600 | 0644/0664, **only if all eligibility constraints of §4 hold** | regular file, expected runner UID/GID, nlink=1; **never print or parse credentials** |
| `.service` | 0600 or 0644 according to existing service implementation; no group/world write | preserve already-secure file; no blanket downgrade | exact canonical service name, owner, type and link checks per frozen contract |
| systemd Unit | root:root 0644 | root:root 0664 repair **only by separately audited restricted Dispatcher operation** | canonical Unit fragment, no drop-ins, exact User/Workdir/ExecStart/service identity and no extra Exec hooks |

`0600` is chosen for the two registered metadata files because the Runner process is owned by the same runner UID; implementation must test official Runner startup, reconnect and removal compatibility. On Create, normalize **after official `config.sh` returns confirmed success and before starting the service**. After `svc.sh install` (CLI path), verify `.service` and Unit state, normalize only with the appropriate existing permission authority; do not silently repair unrelated systemd Units. Unit `0644` is already the Web Dispatcher install target.

If upstream version or permissions make one of these invariant targets incompatible, return a partial-creation failure preserving registration assets, not an apparent completed Create. Changes to `svc.sh` or official Runner bundle are prohibited.

## 4. Historical Runner permission reconciliation (normal Web Remove)

Web Remove must first establish configured Runner identity from independently verified metadata without requiring already-safe group mode: exact repository path and expected owner+repo directory (or independently verified existing legacy naming), `.runner` readable as a bounded non-symlink regular file, validated `gitHubUrl` and `agentName`, correct UID/GID, nlink=1, expected canonical Unit name, and secure parent-directory traversal. The `.credentials` content is never read, exported or logged.

**Eligibility:** an observed ordinary regular file with mode 0644 or 0664 and expected runner UID/GID, nlink=1, in an approved non-group-writable directory and scoped path, with no foreign ownership, directory replacement, quarantine ambiguity or unexpected `.service` state. The designated runner group and its membership/trust assumptions must be documented and checked: if other untrusted principals can modify the group-writable files or the file's authenticity cannot be established from trusted immutable metadata, **fail closed** rather than turn a potentially altered file into trusted state by `chmod`. A mere `664` bit and plausible text are not proof that the file has never been modified. The SPEC implementation must define a deterministic host-local trust attestation (e.g. a previously captured trusted content hash or equivalent authenticated provenance); **without such evidence, do not automatically reconcile a legacy 0664 file**. Group membership alone cannot prove historical integrity. The operator should not have to SSH/chmod for ordinary safe cases, but truly unverifiable legacy state requires manual adjudication.

**Chosen mutation primitive:** FD-relative `openat(O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC)`, `fstat`, exact inode/device/UID/GID/nlink/mode/content/parent identity verification, `fchmod(fd,0600)` only on the *same open inode*, then `fstat` and `lstat` re-verification. This closes the file-path swap window for the chmod target, though a same-UID adversary may still modify a writable directory or contents; do not claim `fchmod` alone establishes authenticity. Verify identity a second time with the original strict removal helper before any systemd mutation. Failure preserves registration and stops before `config.sh remove`.

A historical root-owned Unit mode 0664 is a **separate** trust boundary. The existing Dispatcher currently rejects it before identity parsing. A new extremely narrow `service_unit_permission_reconcile` operation may be designed only with exact existing repository/runner/dir/unit fields and exact root-owned canonical Unit path; pinned no-symlink FD, parsing of full expected unit structure and any drop-ins/Exec hooks, and `fchmod(fd,0644)` plus revalidation. Because a group-writable root Unit could have been tampered with, pre-existing trustworthy unit content/provenance is mandatory; otherwise fail closed. Neither Worker nor Web receives a root chmod API. Do not weaken `validate_unit` for routine service operations.

Run permission repair on the normal Web Remove path before service stop/uninstall, and record explicitly whether each artifact was unchanged, safely tightened, or refused. No bulk migration of active legacy Runner directories.

## 5. Create contract and partial-state handling

All Create modes (Web and CLI) must have explicit post-registration permission checkpoints and deterministic error handling:
1. On successful official `config.sh`, verify strict configured identity and normalize `.runner`/`.credentials` as approved.
2. Only then configure the service. For Web Create use existing Dispatcher canonical Unit creation; for CLI Create use official `svc.sh install` and a narrowly specified root-owned Unit permission normalization if needed.
3. Confirm effective mode/owner and, after start, verify Runner service active; only then show full Create success.
4. If registration succeeds but later permission/service steps fail, preserve runner directory, credentials and accurate partial-state record; do not automatically rerun official registration, silently remove a remote runner, or claim success.
5. For legacy Runner without trustworthy attestation, provide an **operator-facing admin-only recovery procedure** (can be a separately authorized, explicitly inspected maintenance workflow), not an unbounded Web Remove bypass. This SPEC does not authorize generic privilege elevation.

## 6. Remove success, local cleanup and old residue

After positive `config.sh remove` return and verified systemd Unit absence, normal Web Remove must safely clean up the exact scoped Runner directory. Before delete, assert canonical path and inode still refer to the validated target, no symlinked directory path components, no unexpected mount points under target, and no links out via unsafe cleanup mechanism. An existing shared mutation lock does not exclude same-UID filesystem adversaries; deletion requires fail-closed path stability checks and no crossing mounted file systems. Remove only the intended Runner directory, **not local artifact archives that are outside this Runner directory**, and report errors accurately.

**After** directory removal, explicitly check its absence (including symlink/path reappearance), then check Unit absence via trusted Dispatcher; do not emit full success if either is incomplete. Distinguish `local_cleanup_failed` (GitHub unregister positively completed but directory cleanup incomplete) from `unknown_failed` (remote result uncertain) and `service_*_failed` (Unit incomplete). Do not pretend the system can query GitHub registration state locally when the official command returns ambiguous results.

**AgentCI historical residue:** `actions-runner-ernestyu--agentci` with `.runner`/`.credentials`/`.service` absent is unidentifiable under existing Recover Local identity contract. Status listing must not hide it or label it safely recoverable. Define a separate, explicitly approved **admin residual cleanup** with independently verified GitHub runner registration absence, exact historical unit and absent state, verified directory identity/owner, no live processes or mountpoints, and explicit confirmation before whole-directory deletion. Never infer repository from a bare incomplete basename and never silently clean historical residues during unrelated Remove. The 1.1 GB case is a real acceptance fixture, not authority to delete production AgentCI automatically.

## 7. Diagnostics and UI

Keep `GRT_REMOVE_RESULT_V1` compatibility and Worker strict allowlist. Define deterministic fixed, non-sensitive outcomes:
- invalid repository or `.runner` identity/provenance ⇒ `preflight_failed`;
- eligible permission reconciliation failure, invalid `.credentials` file security or unsafe `.service` ⇒ `service_record_reconcile_failed` **only if no remote removal attempted**; if an additional stage is needed, freeze it and update script/Worker/UI atomically;
- unknown/missing prior service record or quarantine ⇒ existing `unknown_failed` and prohibit retry;
- incomplete Unit permission correction ⇒ `service_state_failed` or `service_uninstall_failed` according to exact pre/post phase; never mislabel as successful uninstall;
- registration success followed by local directory removal failure ⇒ `local_cleanup_failed`.

Create's partial-state diagnostics must distinguish official registration already completed from pre-registration failure. Do not expose metadata, credentials, tokens, raw exception text, symlink destinations or arbitrary paths to Web users. Web should state when permission self-healing completed, and when administrative review is mandatory.

## 8. Tests and acceptance matrix

Mandatory isolated, repeatable tests without production GitHub API or Runner deletion:

1. Web Create and CLI Create with official-version v2.328.0 fixtures, simulated `umask 002`, producing initially 0664 metadata → final 0600 before service start; self-hosted Runner still starts and performs config remove.
2. Fresh Web Unit install 0644, official CLI `svc.sh install` 0664 then verified root-controlled normalization where authorized; existing Dispatcher identity validations remain strict.
3. Valid historical 0644/0664 files, attestation present and valid → descriptor-relative safe `fchmod` to 0600; unchanged 0600 accepted; repeated calls idempotent.
4. Historical 0664 files without trusted provenance or with untrusted group membership → fail closed, no permission mutation. Wrong UID/GID, inode changes, hardlink, symlink, FIFO, malicious content, wrong repository/name, directory race, TOCTOU / fd reuse → no alteration of foreign object; no `config.sh remove`.
5. Unit 0664 cases: expected root-owned correct Unit plus trusted integrity attestation allows narrow correction; symlink/drop-in/wrong ExecStart/unknown unit content/unsupported version must refuse.
6. UNKNOWN prior removal (`.service` missing or quarantine present) never bypassed by permission repair or fresh token.
7. Normal Remove success: actual local directory disappears, Unit absent, no retained `.grt-service-reconcile-*` inside vanished directory; foreign Runner and external archive remain intact.
8. Official unregister nonzero/timeout/crash → no local directory deletion; fixed marker. Local cleanup `rm` fail, mount or path-replacement race → `local_cleanup_failed` and preserved recoverable state; no full Success.
9. Existing old AgentCI-style `repository:null/incomplete` directory cannot be silently recovered or deleted; explicit admin residue cleanup is tested separately with remote/Unit evidence and positive confirmation.
10. Exact Create/Web Remove/Recover Local regressions, FD/token redaction, fixed markers, root boundary and no privilege schema widening except an independently approved narrow Unit mode repair.
11. Runner version compatibility: current pinned v2.328.0 test fixture, version name derivation, official `svc.sh uninstall`/Unit mode behavior; unsupported patterns fail closed.
12. CI full suite + isolated Debian acceptance with a **disposable project Runner**, validating Create → Web Remove → GitHub registration absent + Unit absent + directory absent. Deployment and any actual Remove require distinct explicit authorizations.

CI PASS alone never substitutes for independent Implementation audit or Debian evidence.

## 9. Bounded implementation scope and gates

Expected edits after SPEC PASS and separate implementation authorization: `scripts/register-runner.sh`, `scripts/remove-runner.sh`, `scripts/web-service-record.py`, narrowly `web/dispatcher.py` for exact Unit repair if audited, `web/lifecycle_worker.py`/`web/app.py` only for necessary fixed outcomes, `scripts/status-runners.sh` only if status accuracy changes, plus testing/fixtures and setup integration. No unrelated Runner tooling, no frozen SPEC rewrite, no production config/password change.

Gate A: independent SPEC audit decides whether historical 0664 files have sufficient provenance to qualify for automated repair and whether a root Unit 0664 can ever be safely repaired without prior evidence. If not, the safe remedy must be a restricted user-confirmed administrative attestation workflow or pre-provisioning correction — **do not claim universal silent auto-heal**.

Gate B: Implementation audit confirms exact FD operations, race behavior, trust assumptions, partial-state semantics, full CI and Runner compatibility.

Gate C: separate Debian deployment authorization and targeted test Runner owner confirmation, without modifying other active Runners.

```yaml
spec_status: DRAFT
baseline: fc8f6e2f16c366cba313819248579ff58df2b04b
trigger: HyperGrid-legacy-0664-and-AgentCI-1.1GB-residue
implementation: NOT_AUTHORIZED
debian_deployment: NOT_AUTHORIZED
real_runner_remove: NOT_AUTHORIZED
next_gate: INDEPENDENT_SPEC_DESIGN_AUDIT
```
