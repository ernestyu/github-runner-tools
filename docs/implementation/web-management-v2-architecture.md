# Web Management V2 — implementation architecture and closeout evidence

Frozen authority: `docs/specs/20261010-web-management-v2-cli-first-simplification-spec.md` at `d5b935ec99400e8cb3e0496af4172ec4840eb97c`.

This document describes **implementation source and expected trust boundaries**, not Debian acceptance. V2 is not authorized for merge or deployment by this document.

## Process call graph

### V1 (retained for reviewed rollback)

```text
Authenticated Web process (grt-web)
  -> /run/github-runner-tools/web-dispatch.sock
  -> root Dispatcher (Web-specific schema and lifecycle orchestration)
     -> UID-dropping lifecycle_worker.py
        -> --web-worker register-runner.sh / remove-runner.sh
           -> private privileged RPC -> root Dispatcher
           -> V1 Web-specific lifecycle stage and removal diagnostics
```

V1 includes a separate Worker, Dispatcher-managed token pipes, privileged RPC loop, and Web lifecycle orchestration. These files remain **installed only for rollback compatibility** until V2 acceptance and migration closeout; their presence on the draft branch is not evidence that V2 calls the V1 worker.

### V2 target (Draft implementation)

```text
Browser -> authenticated + CSRF-protected Web (grt-web)
  -> /run/github-runner-tools/executor.sock (root:grt-web 0660)
  -> root Executor [SO_PEERCRED, bounded JSON/secret transport]
     |-- status -> pinned status-runners.sh --json as actions
     |-- create/remove -> existing shared mutation flock
         -> root preflight + attested Remove identity check
         -> posix_spawn fixed v2_cli_launcher.py
              -> clears UID/GID/groups/capability sets
              -> fixed register-runner.sh / remove-runner.sh --executor-mode
                   |-- token from private one-shot FD 3 -> PTY official config.sh
                   |-- privileged operations over private socket FD 4
                       -> transaction/stage-bound fixed operation allowlist
                       -> existing dispatcher.Runtime.privileged authority
                       -> existing runner_lifecycle_authority durable records
                       -> canonical root 0644 systemd Unit
         -> existing durable CLI stage closeout proof
         -> bounded redacted response (no raw subprocess transcript)
```

The V2 internal privileged socket is scoped to **one transaction** and serviced by a **single event loop**, not by a separately running privileged Worker or a second root service. The Web public socket cannot submit raw privileged operation requests.

## Remaining root-owned executables and persistence

| Component | Role | Persistent authority? |
|---|---|---|
| `v2_executor.py` | Root Unix Socket listener and peer admission | No |
| `v2_mutation.py`, `v2_binding.py`, `v2_event_loop.py` | Shared lock, transaction-bound privilege pump | No |
| `v2_cli_launcher.py`, `v2_cli_spawn.py` | Fixed binary/argv FD handoff; permanent UID drop before CLI | No |
| `v2_privileged.py` | Exact operation/field allowlist delegated to existing authority | No |
| `dispatcher.Runtime.privileged` | Existing root-side Unit/state operations reused in V2 | **Existing** root authority, not an active V1 Web dispatcher |
| `runner_lifecycle_authority.py` | Root-owned Create/Remove/attestation/active-instance journals | **Sole durable authority** |
| `v2_unit_installer.py` | Shared canonical Unit creation, root-owned 0644 | No independent journal |
| `cli_create_authority.py` | Existing interactive Create lifecycle privilege facade | Existing authority facade |
| `/var/lib/github-runner-tools` | Durable V1/V2-compatible records, **never reset on switch** | **Yes** |
| `/run/lock/github-runner-tools/mutation.lock` | Existing process-wide mutation lock | **Yes, single lock** |

The Executor does not create a Web-specific Create or Remove journal, nonce store, replay database, or separate runner inventory. `recover_local` remains CLI-only. An uncertain GitHub call requires review, **not** automatic retry.

## Safety and acceptance gates

1. Ordinary CLI invocation retains TTY confirmation and sudo authorization; internal executor mode requires the fixed FD3/FD4 channels, root peer identity and trusted launch context.
2. The Web authenticates users, verifies CSRF and confirms the exact Remove target in the server-side session before forwarding the fixed Remove tuple. Executor independently checks current root state and GitHub agent identity before mutation.
3. A zero exit code does **not** imply success: Create requires an existing `CREATE_COMPLETE` journal entry and directory; Remove requires `REMOVE_COMPLETE` for the same instance and absence of the directory.
4. Temporary credentials are delivered separately from JSON, never on Executor-mode official CLI argv; their inherited token descriptor is closed after PTY consumption. Do not persist raw CLI logs/response bodies.
5. A failed remote call leaves UNKNOWN/cleanup-pending journal evidence; duplicate Remove of that instance is rejected and no synthetic completion is issued.
6. The V2 service switch is explicit, exact-SHA-gated and dry-run by default. It snapshots V1 configuration, code, CLI and root state without converting or rewriting records; reviewed rollback must retain the current lifecycle store. **No Debian command was executed as part of branch implementation.**
7. Focused Development Check includes full `register-runner.sh` / `remove-runner.sh` execution against **isolated fake GitHub/systemd operations** plus socket/FD/PTY, identity and UNKNOWN retry regressions. These are simulations, **not live GitHub acceptance**.
8. Full Integration Check is `workflow_dispatch`/release-tag-only and must be separately evidenced at the final fixed HEAD. Do not label Development Check success as Integration Check success.
9. Debian acceptance is **a distinct later approval**: one disposable Runner Create -> Status -> Job -> Remove, with all six existing Runner identities, service Units and state records unchanged. No deployment is authorized by an Implementation Audit alone.

## Implementation closeout status

The currently submitted V2 process path and mock tests are available in Draft PR #6. Full test evidence, fixed HEAD, and separate independent Implementation Audit must be recorded before readiness is asserted. V1 Dispatcher/Worker source remains in the repo during the rollback window and must not be removed while rollback compatibility is required.
