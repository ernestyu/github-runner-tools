# Web Management V1 — SPEC

Date: 2026-10-07  
Repository: `ernestyu/github-runner-tools`  
Baseline: `2d8255504aea30b73ef9f74cf471b7222a0486a5`

Status: SPEC ONLY / NO IMPLEMENTATION

## 1. Background

`github-runner-tools` currently manages repository-level GitHub Actions self-hosted runners on a Debian/systemd host through command-line scripts.

The existing lifecycle is intentionally conservative:

```text
register-runner.sh
→ register one repository-level runner
→ configure local completed-job archive
→ install/start systemd service

status-runners.sh
→ inspect local runner state

remove-runner.sh OWNER/REPO
→ normal GitHub unregister/removal

remove-runner.sh --recover-local OWNER/REPO
→ local-only cleanup after the GitHub-side runner was already removed
```

The project also maintains local CI artifacts under:

```text
/srv/github-actions-archive
```

The current command-line workflow is practical when an operator has a terminal, but inconvenient from a phone. The new requirement is a thin, mobile-friendly Web management layer for the two common lifecycle operations:

```text
list current runners
create a runner
remove a runner
```

The Web layer is not a replacement implementation of runner lifecycle logic. Existing lifecycle safety semantics remain authoritative.

## 2. Goal

Add a small resident Web management service that runs on the Debian runner host and provides:

1. a mobile-friendly list of current local runners;
2. runner creation from:
   ```text
   OWNER/REPO
   temporary GitHub registration token
   ```
3. runner removal from the list;
4. local recovery cleanup for a runner residue when the GitHub-side runner was already removed.

The Web UI must remain a thin control surface over the existing runner lifecycle.

## 3. Scope

V1 contains only:

```text
List
Create
Normal Remove
Recover Local Residue
```

No other operational surface is included.

## 4. Non-goals

V1 must not add:

- browser shell or terminal;
- arbitrary command execution;
- file browser/editor;
- Git repository management;
- GitHub workflow dispatch;
- workflow log browsing;
- artifact browsing/download UI;
- artifact deletion UI;
- runner update/upgrade UI;
- bulk create/delete;
- organization-level runners;
- GitHub PAT storage;
- automatic generation of registration/removal tokens;
- GitHub API discovery for runner lifecycle;
- OAuth;
- Tailscale identity integration;
- public Internet exposure;
- Cloudflare Tunnel or equivalent public tunnel;
- direct production deployment functions;
- database;
- task scheduler;
- background polling of GitHub.

## 5. Architecture

V1 uses two local processes with a strict privilege boundary.

```text
Browser / phone
    ↓ HTTPS
Tailscale Serve
    ↓ loopback HTTP
github-runner-tools-web.service
    unprivileged dedicated account: grt-web
    ↓ local Unix socket only
github-runner-tools-broker.socket
github-runner-tools-broker.service
    root-owned privileged broker
    ↓
existing/shared runner lifecycle implementation
    ↓
GitHub Actions Runner + systemd
```

The Web frontend must never run as root.

The privileged broker must never expose a TCP listener.

The privileged broker must only be reachable through a root-controlled Unix-domain socket.

## 6. Network exposure

### 6.1 Default V1 access path

The Web application must bind only to:

```text
127.0.0.1:<configured-port>
```

The default port is:

```text
8765
```

Remote access is provided through Tailscale Serve over HTTPS.

The intended access path is:

```text
phone/browser
→ authenticated Tailnet
→ Tailscale HTTPS
→ 127.0.0.1:8765
```

The Web application must not bind to:

```text
0.0.0.0
::
public/WAN addresses
```

### 6.2 Direct LAN access

Direct LAN binding is intentionally not included in V1.

Reason:

```text
plain LAN HTTP would expose the Web login password and temporary GitHub tokens in transit
```

A later amendment may add direct LAN access only with an explicit TLS transport contract.

A machine on the same LAN may still access the service through Tailscale.

## 7. Authentication

Tailscale network membership is not sufficient by itself.

The Web UI must also require one local administrator password.

The password:

- is configured during Web setup;
- is never stored in plaintext;
- is stored only as a salted password hash;
- must use a standard password-hardening primitive available in the chosen runtime, such as scrypt;
- is never written to logs;
- is never passed in argv or environment variables.

Authentication state uses a server-side signed session.

Required cookie properties when accessed through Tailscale HTTPS:

```text
Secure
HttpOnly
SameSite=Strict
```

Session policy:

```text
idle timeout: 30 minutes
absolute lifetime: 8 hours
```

Login attempts must be rate-limited per client/session source. A simple bounded in-memory limiter is sufficient; no database is required.

## 8. CSRF and browser safety

Every state-changing request must use:

```text
POST
+
CSRF token
```

GET requests must never:

- create a runner;
- remove a runner;
- trigger recovery;
- mutate host state.

Pages containing tokens, operation forms, or operation results must send:

```text
Cache-Control: no-store
```

Temporary GitHub tokens must never appear in:

- URL;
- query string;
- browser redirect URL;
- HTML response;
- application logs;
- systemd journal;
- process argv;
- environment variables.

## 9. Dedicated Web account

The Web frontend must run as a dedicated locked service account:

```text
grt-web
```

The account must:

- have no interactive shell;
- have no sudo rights;
- not own runner directories;
- not be a member of the runner owner group solely for convenience;
- not be able to read runner credentials such as `.credentials`;
- not be able to read unrelated user home content.

Runner jobs continue to run as the normal runner owner, for example:

```text
actions
```

The Web frontend and runner execution identity must therefore remain separate:

```text
grt-web != actions
```

This separation is required so workflow code executing as `actions` cannot directly read Web authentication material or invoke the privileged broker.

## 10. Privileged broker

### 10.1 Purpose

Operations that require root/systemd authority must be performed through a dedicated broker.

The broker is installed as root-owned code and is activated through a Unix-domain socket.

Suggested paths:

```text
/usr/local/lib/github-runner-tools/web/
/run/github-runner-tools/web-broker.sock
```

### 10.2 Socket contract

The socket must be:

```text
owner: root
group: grt-web
mode: 0660
```

The broker must verify the peer credentials of the connecting process and accept requests only from the configured Web service UID.

Filesystem permissions alone are not the sole identity check.

### 10.3 Allowed operations

The broker accepts only these typed operations:

```text
list
create
remove
recover_local
```

There is no:

```text
exec
shell
command
path
script
argv
```

operation.

The request schema must reject unknown fields.

### 10.4 No arbitrary shell execution

The broker must not build shell command strings from Web input.

The implementation must not use:

```text
shell=True
os.system
eval
bash -c <user-controlled-string>
```

for Web-supplied values.

### 10.5 Root execution boundary

The broker must not execute a file that is writable by the runner owner as root.

In particular, a permanent Web privilege path must not reduce to:

```text
NOPASSWD sudo ./svc.sh
```

from a runner-owned directory.

If implementation refactoring is required, privileged service-management logic must be located in root-owned, non-user-writable installed code.

The Web feature must not grant the runner owner `actions` new passwordless root execution capability.

## 11. Lifecycle authority

Existing lifecycle semantics remain authoritative.

The Web implementation must not create an independent second definition of:

- local runner identity;
- runner directory naming;
- legacy runner handling;
- registration collision behavior;
- service cleanup;
- recovery eligibility;
- archive configuration;
- destructive path safety.

Shared lifecycle logic may be extracted into reusable code if needed, but:

```text
CLI path
and
Web path
```

must use the same semantic authority and must be covered by parity tests.

The existing CLI commands must continue to work after this change.

## 12. Non-interactive secret transport

The current CLI reads temporary GitHub tokens from `/dev/tty`.

The Web path requires a non-interactive adapter.

Temporary GitHub registration/removal tokens must be delivered to lifecycle code through an anonymous pipe or inherited file descriptor.

They must not be delivered through:

```text
argv
environment variable
temporary file
persistent file
database
URL
```

Required semantic interface:

```text
token input = file descriptor / anonymous pipe
```

The exact internal flag name may be chosen during implementation, but the transport contract is frozen.

Token buffers/variables must be cleared or released immediately after the lifecycle call completes.

## 13. Status JSON contract

The Web UI must not scrape human-formatted terminal output.

`status-runners.sh` must gain a machine-readable mode:

```bash
bash scripts/status-runners.sh --json
```

V1 JSON output is an array of objects.

Each item must contain:

```json
{
  "repository": "OWNER/REPO or null",
  "runner_name": "name or null",
  "runner_dir": "/absolute/path",
  "configured": true,
  "service_state": "active|inactive|absent|unknown",
  "management_state": "configured|recoverable_residue|incomplete|ambiguous",
  "can_remove": true,
  "can_recover_local": false
}
```

Rules:

- `repository` may be `null` when identity cannot be proven.
- `can_remove=true` only when normal removal eligibility is established.
- `can_recover_local=true` only when the frozen `--recover-local` eligibility rules are established.
- an ambiguous runner must never become removable merely because a directory name resembles a repository.
- JSON must not contain tokens, credentials, registration secrets, or arbitrary contents of runner metadata files.

Human-readable status output must remain supported and unchanged unless a separate presentation-only improvement is needed.

## 14. Runner list UI

The main page shows one compact row/card per discovered local runner.

Minimum visible fields:

```text
repository
runner name
service state
management state
```

Actions are rendered from the machine contract:

```text
can_remove=true
→ show Remove

can_recover_local=true
→ show Recover local residue

otherwise
→ no destructive action
```

The browser must never infer eligibility itself.

## 15. Create runner flow

The create form contains only:

```text
Repository: OWNER/REPO
Registration token: secret field
```

V1 does not expose custom:

- runner name;
- labels;
- base directory;
- runner version.

Those remain CLI-only settings in V1.

Repository input must pass the same `OWNER/REPO` validation used by existing CLI lifecycle code.

Create sequence:

```text
authenticated POST
→ CSRF validation
→ repository validation
→ acquire global mutation lock
→ pass registration token by anonymous pipe/FD
→ invoke shared registration lifecycle
→ wait for bounded completion
→ return sanitized result
→ release lock
→ refresh list
```

On success, the resulting runner must have the same defaults and archive hook configuration as a runner created through the normal CLI.

No `--replace` behavior may be introduced.

## 16. Remove runner flow

### 16.1 Normal removal

For an item with:

```text
can_remove=true
```

the UI displays:

```text
Repository
Removal token
Remove
```

Removal token is required.

The operation must preserve all current normal-removal semantics, including:

- identity verification;
- systemd cleanup ordering;
- GitHub unregister;
- local directory safety boundary.

The Web layer must not call GitHub APIs directly.

### 16.2 Local recovery

For an item with:

```text
can_recover_local=true
```

the UI displays:

```text
Recover local residue
```

No GitHub removal token is requested.

The exact frozen `--recover-local` rules remain authoritative:

- `.runner` must be completely absent;
- exact new-style owner+repository identity;
- valid non-truncated repository-scoped service identity;
- legacy ambiguous residue rejected;
- unknown systemd state rejected;
- local artifact archive preserved.

## 17. Destructive confirmation

A destructive action requires a second server-generated confirmation step.

Required flow:

```text
user selects Remove / Recover
→ server renders confirmation page with exact repository
→ server generates one-time confirmation nonce
→ user confirms
→ POST nonce + CSRF
→ server consumes nonce once
→ mutation begins
```

A confirmation nonce:

- expires after 5 minutes;
- is single-use;
- is bound to the authenticated session;
- is bound to the exact operation and repository.

The browser must not be able to submit a different repository by editing a hidden field after confirmation.

## 18. Mutation serialization

Only one mutating lifecycle operation may execute at a time.

Required operations covered by the lock:

```text
create
remove
recover_local
```

Use one host-level lock.

If another mutation is in progress, V1 returns:

```text
409 Conflict
operation already in progress
```

Do not queue multiple destructive operations in V1.

List/status requests remain available while no state-reading conflict exists.

## 19. Timeouts

Every lifecycle mutation must have a bounded execution timeout.

Default V1 mutation timeout:

```text
15 minutes
```

Timeout must:

- terminate the Web request;
- attempt safe child-process cleanup;
- return an explicit failure state;
- not convert an uncertain partial lifecycle into success.

Existing lifecycle recovery semantics remain responsible for subsequent cleanup.

## 20. Logging

The Web service and broker may log:

- timestamp;
- authenticated session/user identifier;
- operation type;
- repository;
- final success/failure;
- stable error class.

They must not log:

- registration token;
- removal token;
- password;
- session cookie;
- CSRF token;
- confirmation nonce;
- runner credentials;
- request body containing secrets.

HTTP access logging must be configured so request bodies are never logged.

## 21. Error contract

The UI must distinguish at least:

```text
authentication failed
CSRF failed
invalid repository
operation already in progress
registration failed
normal removal failed
recovery not eligible
service state unknown
identity ambiguous
operation timed out
broker unavailable
internal error
```

The UI must show a safe message.

Detailed diagnostics may be written to the local journal only if they contain no secrets.

## 22. Web setup

Add a setup tool, for example:

```bash
bash scripts/setup-web-management.sh --dry-run
bash scripts/setup-web-management.sh --apply
```

Default mode is dry-run.

Setup is run by the normal runner owner/operator and may use sudo internally for host-level installation.

It must not require running the entire setup script as root.

Setup responsibilities:

- verify Tailscale CLI/service availability;
- verify the expected runner owner;
- create the locked `grt-web` account;
- install root-owned Web and broker code;
- install root-owned configuration;
- install authentication hash/session secret;
- install systemd Web service;
- install broker socket/service;
- set Unix socket ownership/permissions;
- configure or print the exact Tailscale Serve command;
- validate that the Web backend binds only to loopback;
- start/enable the Web service only after validation succeeds.

Setup must stop rather than replace an unmanaged conflicting service/configuration.

## 23. Configuration

Suggested root-owned configuration:

```text
/etc/github-runner-tools/web.conf
```

It must contain only non-secret operational settings such as:

```text
WEB_BIND_ADDRESS=127.0.0.1
WEB_PORT=8765
RUNNER_USER=actions
MUTATION_TIMEOUT_SECONDS=900
```

Authentication secrets must be stored separately, for example:

```text
/etc/github-runner-tools/web-auth.conf
```

Required ownership:

```text
root:grt-web
```

Required permissions must make the file:

- readable by Web service;
- not writable by Web service;
- unreadable by runner owner `actions`;
- not world-readable.

Exact numeric modes may be selected during implementation if these properties are satisfied.

## 24. Tailscale Serve contract

V1 remote access depends on Tailscale Serve.

Setup must either:

1. configure Tailscale Serve automatically after explicit operator confirmation; or
2. print the exact command and verify it during acceptance.

The resulting public surface must be Tailnet-only HTTPS.

The implementation must not enable a Funnel/public Internet endpoint.

Any use of:

```text
tailscale funnel
```

is forbidden in V1.

## 25. Frontend technology

V1 should remain small.

Allowed implementation shape:

```text
Python backend
server-rendered HTML
small CSS
minimal JavaScript only where necessary
```

V1 must not require:

- React;
- Vue;
- Node build chain;
- npm;
- frontend bundler;
- database.

A small Python dependency set is acceptable if pinned and documented.

## 26. Systemd hardening

The Web frontend service must use systemd hardening appropriate for an unprivileged network service.

At minimum evaluate and enable where compatible:

```text
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
MemoryDenyWriteExecute=true
```

The frontend must not require write access to runner home directories.

The broker is privileged and will require a narrower but different hardening profile. Its writable paths and capabilities must be limited to the lifecycle operations actually required.

## 27. Security invariants

The implementation must preserve all of these:

```text
Web frontend is never root.

Runner workflow user cannot read Web auth secrets.

Runner workflow user cannot directly access broker socket.

No NOPASSWD path to runner-user-writable svc.sh.

No arbitrary shell input.

No token in argv/env/file/log/URL.

No public Internet listener.

No direct LAN plaintext Web management in V1.

No GitHub PAT storage.

No automatic GitHub API lifecycle discovery.

No bulk destructive operation.

No Web deletion of local artifact archive.

Ambiguous identity always fails closed.

Existing CLI safety semantics remain unchanged.
```

## 28. Tests

Implementation must add deterministic automated coverage for at least:

### 28.1 Authentication/session

```text
unauthenticated GET management page
→ login required

wrong password
→ rejected

valid login
→ session established

expired session
→ rejected

session cookie flags correct
```

### 28.2 CSRF

```text
state-changing POST without CSRF
→ rejected

wrong CSRF
→ rejected

valid CSRF
→ allowed to continue
```

### 28.3 Create input

```text
invalid repository
→ rejected before broker call

valid OWNER/REPO + token
→ broker receives typed create request

token absent
→ rejected

token never appears in captured argv/env/log
```

### 28.4 Status JSON

```text
configured runner
recoverable residue
incomplete runner
ambiguous legacy runner
service active/inactive/absent/unknown
```

Verify action flags:

```text
can_remove
can_recover_local
```

### 28.5 Remove

```text
normal removal requires removal token
recovery never asks for removal token
ambiguous item exposes no destructive action
```

### 28.6 Confirmation

```text
missing confirmation nonce
expired nonce
reused nonce
nonce for another repository
nonce for another operation
→ all rejected
```

### 28.7 Broker input safety

Reject:

- unknown operation;
- unknown JSON field;
- malformed repository;
- arbitrary path;
- arbitrary command;
- shell metacharacter injection attempts.

### 28.8 Privilege boundary

Tests must prove:

```text
frontend service user != runner owner
runner owner cannot read Web auth config
runner owner cannot connect to broker socket
frontend cannot perform privileged action except through broker
broker does not execute runner-user-writable files as root
```

### 28.9 Mutation lock

```text
first mutation active
second mutation request
→ 409
→ second mutation not started
```

### 28.10 Secret redaction

Captured:

```text
application log
broker log
argv
environment
HTTP response
```

must not contain registration/removal token.

## 29. Live Debian acceptance

After static/code audit passes, validate on the real Debian host in this order:

```text
1. bash tests/run-all.sh
2. setup-web-management.sh --dry-run
3. inspect generated user/config/service/socket plan
4. setup-web-management.sh --apply
5. verify frontend only listens on 127.0.0.1
6. verify broker has no TCP listener
7. verify Tailscale Serve HTTPS endpoint
8. login from phone through Tailnet
9. list current runners
10. create a disposable/test repository runner using a temporary registration token
11. verify runner appears and service is active
12. normal-remove that disposable runner using a temporary removal token
13. create a second disposable runner
14. delete it on GitHub first
15. wait for local .runner/.credentials cleanup
16. use Web Recover local residue
17. verify systemd unit absent
18. verify runner directory removed
19. verify /srv/github-actions-archive is untouched
20. verify no secrets appeared in journal
```

Do not use a production/private research runner as the first Web mutation test.

## 30. Documentation

After implementation, update:

```text
README.md
README.zh-CN.md
CHANGELOG.md
```

Documentation must explain:

- what the Web UI does;
- Tailscale-only V1 network boundary;
- setup commands;
- login;
- create flow;
- normal remove flow;
- local recovery flow;
- why direct LAN HTTP/public Internet exposure is not supported;
- security boundary between `grt-web`, broker, and runner owner.

## 31. Acceptance criteria

Web Management V1 is complete only when all of the following are true:

1. Web frontend runs as dedicated non-root `grt-web`.
2. Runner owner and Web frontend identities are separate.
3. Frontend binds only to loopback.
4. Remote access is Tailnet-only HTTPS through Tailscale Serve.
5. No Tailscale Funnel/public exposure is enabled.
6. Local administrator password is stored only as a hardened hash.
7. Session cookie uses Secure, HttpOnly, SameSite=Strict.
8. CSRF protection covers every mutation.
9. No temporary GitHub token is sent through argv, environment, file, URL, or logs.
10. Broker is reachable only through a protected Unix socket.
11. Broker validates peer credentials.
12. Broker exposes only typed list/create/remove/recover_local operations.
13. Unknown request fields/operations are rejected.
14. Broker never executes runner-user-writable code as root.
15. Runner owner receives no new passwordless root execution path.
16. Existing CLI lifecycle remains functional.
17. CLI and Web lifecycle share the same semantic authority.
18. `status-runners.sh --json` implements the frozen machine contract.
19. UI action availability comes only from machine-readable eligibility flags.
20. Create accepts only OWNER/REPO + temporary registration token in V1.
21. Normal remove requires temporary removal token.
22. Local recovery requires no GitHub token and preserves existing frozen recovery semantics.
23. Destructive operation uses session-bound, operation-bound, repository-bound single-use confirmation.
24. Only one mutation runs at a time.
25. Mutation timeout is bounded.
26. Local artifact archive is never deleted by Web runner removal/recovery.
27. Direct LAN plaintext management is not available in V1.
28. Automated tests cover §28.
29. Full repository test suite passes.
30. Live Debian acceptance sequence in §29 passes before release.
31. README, Chinese README, and CHANGELOG are updated only after implementation passes audit.

## 32. Implementation boundary

This feature is intentionally a thin management UI over an existing runner lifecycle.

Do not expand implementation into a general CI dashboard.

The expected implementation surface is limited to:

```text
Web frontend
+
authentication/session/CSRF
+
status JSON adapter
+
non-interactive token FD adapter
+
privilege-separated broker
+
systemd/Tailscale setup
+
List/Create/Remove/Recover UI
+
tests
+
documentation
```

Any request for:

```text
terminal
logs
artifact browser
workflow control
GitHub PAT automation
direct LAN HTTP
public Internet access
```

requires a separate SPEC amendment.

---

End of SPEC.
