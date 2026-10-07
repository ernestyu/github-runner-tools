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

V1 uses three OS identities and a fixed local execution path.

```text
Browser / phone
    ↓ HTTPS
Tailscale Serve
    ↓ loopback HTTP
github-runner-tools-web.service
    UID: grt-web
    ↓ typed Unix socket
github-runner-tools-dispatch.service
    UID: root
    ↓ fixed worker launch with UID/GID drop
github-runner-tools lifecycle worker
    UID/GID: actions
    ↓
    ordinary runner lifecycle
    +
    request-scoped private control channel for narrow host service operations
```

The roles are frozen as follows.

### 5.1 Web frontend

The Web process runs as `grt-web`.

It owns only HTTP, session, CSRF, confirmation, and rendering behavior.

It must not switch UID, manipulate runner directories directly, execute runner configuration scripts, call system service commands, or receive sudo rights.

### 5.2 Root dispatcher

The dispatcher runs as root but is not the lifecycle implementation.

For an accepted request it may only:

1. authenticate the Web peer;
2. validate the fixed request schema;
3. acquire the global mutation lock for mutating requests;
4. create request-scoped pipes/control channels;
5. launch one fixed root-owned lifecycle worker;
6. drop the child process to the configured runner-owner UID/GID before lifecycle logic starts;
7. handle only the narrow privileged host-service operations defined later in this SPEC;
8. enforce timeout and collect a sanitized result.

It must not execute repository checkout code or runner-owned lifecycle code as root.

### 5.3 Runner-owner lifecycle worker

The lifecycle worker is installed as root-owned, non-runner-writable code but executes as the configured runner owner, normally `actions`.

It performs the shared lifecycle semantics for List, Create, Normal Remove, and Recover Local.

When a host-level privileged operation is required, the worker uses only the private request-scoped control channel inherited from the dispatcher.

An unrelated process running as `actions` does not have that request-scoped channel.

### 5.4 Privilege boundary

Root privilege is limited to host-level dependency and service-management operations.

Runner registration, runner-directory creation, runner credentials/configuration, archive-hook configuration, GitHub unregister, and verified runner-directory cleanup execute as `actions`, not root.

The whole runner lifecycle must never execute as root.

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

Authentication uses an opaque server-side session.

Required model:

```text
browser cookie
→ random opaque session identifier with at least 256 bits of entropy

server memory
→ session identifier
→ authenticated state
→ login time
→ last activity
→ CSRF state
→ pending confirmation nonces
```

No authentication or authorization state is encoded into a client-visible signed payload.

A Web service restart invalidates all sessions. Persistent login is out of scope for V1.

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

Login attempts must be rate-limited. A bounded in-memory limiter is sufficient; no database is required.

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

Production Web mode must disable framework debug mode and any request-body/form-value logging.

Exception handling must not dump submitted form values or full request bodies to logs or HTML error pages.

Request bodies and token fields must have explicit bounded maximum sizes. Oversized requests must be rejected before lifecycle execution.

The implementation must not retain a full request body after the request has been parsed and the required fields copied into bounded in-memory variables.

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

The Web frontend and runner execution identity must remain separate:

```text
grt-web != actions
```

The runner owner must not be able to read Web authentication material.

The runner owner must not receive access to the privileged-helper socket or another privileged IPC endpoint.

Any controller path that performs runner-owner lifecycle work must explicitly execute with the configured runner-owner UID/GID and must not inherit root identity.

## 10. Privileged dispatcher/helper contract

### 10.1 Public Unix socket

The persistent privileged IPC endpoint is:

```text
/run/github-runner-tools/web-dispatch.sock
```

Required ownership:

```text
root:grt-web
mode 0660
```

The runner owner `actions` must not belong to the `grt-web` group and must not be able to connect to this socket.

The dispatcher must verify Unix peer credentials and accept requests only from the configured `grt-web` UID.

### 10.2 Request schema

The public socket accepts only typed operations:

```text
list
create
remove
recover_local
```

Request data may contain only fields defined for the selected operation.

No request may supply an executable path, shell command, arbitrary path, UID/GID, service name, or systemd unit name.

Unknown operations and unknown fields are rejected before worker launch.

### 10.3 Worker launch

The dispatcher launches exactly one fixed, root-owned lifecycle worker path.

Before lifecycle logic starts, the child must drop supplementary groups and switch to the configured runner-owner UID/GID.

Failure to complete and verify the identity drop is fatal.

The dispatcher must not execute lifecycle logic while retaining root identity.

### 10.4 Private privileged control channel

The lifecycle worker may request privileged host operations only through a private, request-scoped socketpair/file descriptor inherited from its dispatcher parent.

This channel is never exposed through the filesystem and is unavailable to unrelated `actions` processes.

Allowed privileged operation classes are limited to:

```text
ensure_runner_dependencies
service_install
service_start
service_stop
service_restart
service_state
service_uninstall
```

The exact internal names may differ, but the semantic scope may not expand beyond dependency and system-service management.

### 10.5 Root-owned code boundary

Root execution is limited to root-owned, non-runner-writable installed code.

Root must never execute:

- repository checkout code under the runner owner's home;
- runner-owned `svc.sh`;
- runner-owned `config.sh`;
- runner-owned `runsvc.sh`;
- another executable writable by `actions` or `grt-web`;
- a browser-supplied executable/path.

No new passwordless sudo permission may be granted to `actions` or `grt-web`.

### 10.6 Dependency installation

The existing CLI may use the official runner dependency installer interactively with sudo.

Web V1 must not turn the extracted runner-owned dependency script into a permanent privileged Web path.

For Web Create, required host dependencies must be installed during root-controlled platform setup or through a fixed root-owned dependency helper. If dependencies are missing, Web Create fails with a stable error rather than falling back to root execution of runner-owned code.

### 10.7 Full systemd authority validation

Runner-owned `.service` metadata is not sufficient authority for a privileged service mutation.

For every existing service mutation, root-controlled systemd state must establish the complete managed-unit execution surface.

At minimum validate:

```text
FragmentPath
unit file owner and write permissions
DropInPaths
User
WorkingDirectory
ExecStart
ExecStartPre
ExecStartPost
ExecStop
ExecStopPost
ExecReload
```

Required rules:

1. FragmentPath must be the expected root-controlled managed unit location.
2. The unit file must be root-owned and not writable by `actions` or `grt-web`.
3. V1 managed runner units must have no unverified drop-ins.
4. User must equal the configured runner owner.
5. WorkingDirectory must equal the exact canonical runner directory.
6. ExecStart must match the canonical managed runner-service schema for that directory.
7. Unexpected ExecStartPre, ExecStartPost, ExecStop, ExecStopPost, or ExecReload commands are forbidden.
8. Every command systemd could trigger through the requested mutation must match the managed allowlist/schema.

Any mismatch or inability to prove these properties is:

```text
identity/provenance unknown
→ FAIL CLOSED
→ no privileged service mutation
```

### 10.8 Service installation

Web-created services must be installed through a root-owned canonical unit mechanism.

The helper must not run runner-owned `svc.sh install` as root.

The installed unit must pass §10.7 validation before first start.

## 11. Lifecycle authority and mutation revalidation

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

### 11.1 Frontend eligibility is advisory only

Machine-readable fields such as:

```text
can_remove
can_recover_local
```

control what the UI displays.

They are not authorization for a mutation.

Immediately before every create/remove/recover operation, the server-side lifecycle controller must:

```text
acquire global mutation lock
→ re-read current filesystem/systemd state
→ re-resolve repository/runner identity
→ re-run current eligibility validation
→ only then begin mutation
```

The controller/helper must not trust:

- stale list-page data;
- browser hidden fields;
- a previously generated `can_remove` value;
- a previously generated `can_recover_local` value.

This revalidation must occur while the mutation lock is held.

### 11.2 Privileged systemd revalidation

For any service mutation, the root-controlled systemd cross-check in §10.7 must occur again immediately before that privileged operation.

This prevents runner-owned metadata or service state from changing between list rendering and destructive execution.

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
→ re-check target identity/collision state
→ perform runner-owner lifecycle as actions
→ pass registration token by anonymous pipe/FD
→ use privileged helper only for allowed host dependency/service operations
→ wait for bounded completion
→ return sanitized result
→ release lock
→ refresh list
```

The Web/frontend process itself must not create runner files as `grt-web`.

The lifecycle controller must not create runner files as root.

On success, the resulting runner must have the same owner, defaults, labels, archive hook configuration, and collision semantics as a runner created through the normal CLI.

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

After confirmation and while holding the mutation lock, the server must independently revalidate current removal eligibility.

The operation must preserve all current normal-removal semantics, including:

- identity verification;
- systemd cleanup ordering;
- GitHub unregister;
- local directory safety boundary.

Required privilege split:

```text
identity/filesystem validation        runner-owner/shared lifecycle
systemd stop/uninstall                privileged helper
GitHub config.sh remove               actions
verified local directory deletion     actions
```

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

After confirmation and while holding the mutation lock, the server must independently revalidate current recovery eligibility.

The exact frozen `--recover-local` rules remain authoritative:

- `.runner` must be completely absent;
- exact new-style owner+repository identity;
- valid non-truncated repository-scoped service identity;
- legacy ambiguous residue rejected;
- unknown systemd state rejected;
- local artifact archive preserved.

Before privileged service cleanup, the systemd unit must additionally pass the root-controlled cross-check in §10.7.

Required privilege split:

```text
identity/filesystem validation        runner-owner/shared lifecycle
systemd stop/uninstall                privileged helper
verified local directory deletion     actions
```

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

Use one host-level lock stored in a root-/service-controlled location that is not writable by runner workflow code.

The lock must be acquired before the authoritative mutation revalidation in §11.1 and held until the mutation reaches a terminal result.

If another mutation is in progress, V1 returns:

```text
409 Conflict
operation already in progress
```

Do not queue multiple destructive operations in V1.

List/status requests may continue, but their eligibility data remains advisory and must be revalidated before any later mutation.

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
- install root-owned Web/controller/helper code;
- install root-owned configuration;
- install authentication hash/session secret;
- install systemd Web service;
- install only the narrow privileged-helper IPC/service required by §10;
- configure IPC ownership/permissions so `actions` cannot invoke privileged operations;
- install/verify any root-controlled dependency/service-management mechanism;
- configure or print the exact Tailscale Serve command;
- validate that the Web backend binds only to loopback;
- start/enable the Web service only after validation succeeds.

Setup must stop rather than replace an unmanaged conflicting service/configuration.

Setup must verify that no installed privileged executable/script is writable by either:

```text
grt-web
actions
```

unless that file is deliberately non-executable data and its mutability is part of the frozen contract.

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

V1 remote access depends exclusively on Tailscale Serve.

Setup must either:

1. configure Tailscale Serve automatically after explicit operator confirmation; or
2. print the exact command and verify it during acceptance.

The resulting management surface must be Tailnet-only HTTPS.

The implementation must not enable a Funnel/public Internet endpoint.

Any use of:

```text
tailscale funnel
```

is forbidden in V1.

Direct access to the backend through a LAN address is forbidden because the backend listens only on loopback.

Tailnet policy may further restrict which Tailnet identities/devices can reach this service. Such policy restriction is recommended but is not a substitute for the Web administrator password.

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

The runner-owner controller/lifecycle process must receive only the filesystem access required by the existing runner lifecycle.

The privileged helper must have a separate, narrower hardening profile. Its accepted operations, writable paths, executable paths, and systemd authority must be limited to §10.

The privileged helper must not have a general-purpose shell/command interface.

## 27. Security invariants

The implementation must preserve all of these:

```text
Web frontend is never root.

Whole runner lifecycle is never executed as root.

Runner registration/configuration/unregister/directory operations execute as actions.

Root privilege is limited to host dependency/service management.

Runner workflow user cannot read Web auth secrets.

Runner workflow user cannot invoke privileged helper IPC.

No NOPASSWD path to runner-user-writable svc.sh.

Privileged helper never executes runner-user-writable code as root.

Privileged service mutation cross-checks root-controlled systemd properties.

Frontend eligibility flags are advisory only.

Every mutation revalidates current identity/state under the mutation lock.

No arbitrary shell input.

No token in argv/env/file/log/URL.

No request-body or form-value secret logging.

No public Internet listener.

No direct LAN Web management in V1.

Tailscale Serve is the only remote access path.

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
→ opaque server-side session established

expired session
→ rejected

service restart / missing server-side session
→ old cookie rejected

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
→ rejected before lifecycle call

valid OWNER/REPO + token
→ runner-owner lifecycle receives typed create request

token absent
→ rejected

token never appears in captured argv/env/log/file/response
```

### 28.4 Status JSON

Cover:

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

### 28.7 Mutation-time revalidation / TOCTOU

Cover at least:

```text
list page says can_remove=true
→ state changes before POST
→ mutation revalidation rejects operation

list page says can_recover_local=true
→ .runner appears before POST
→ mutation revalidation rejects operation

service identity changes before privileged operation
→ systemd cross-check rejects operation
```

### 28.8 Privileged-helper input safety

Reject:

- unknown operation;
- unknown field;
- malformed repository;
- arbitrary path;
- arbitrary command;
- shell metacharacter injection attempts.

### 28.9 Privilege boundary

Tests must prove:

```text
frontend service user != runner owner

runner owner cannot read Web auth config

runner owner cannot invoke privileged helper

frontend does not own/write runner directories

runner lifecycle files are created as actions, not root/grt-web

privileged helper does not execute runner-user-writable svc.sh/config.sh/runsvc.sh as root

privileged helper rejects a systemd unit whose User != actions

privileged helper rejects a systemd unit whose WorkingDirectory != expected canonical runner directory

privileged helper rejects an unexpected ExecStart

actions receives no new passwordless root command
```

### 28.10 Mutation lock

```text
first mutation active
second mutation request
→ 409
→ second mutation not started
```

Also verify authoritative identity revalidation happens after lock acquisition.

### 28.11 Secret redaction

Captured:

```text
application log
helper log
argv
environment
temporary files
HTTP response
error response
```

must not contain registration/removal token.

Framework debug/error handling must not echo submitted form values.

### 28.12 Request bounds

Cover:

```text
oversized request body
oversized repository field
oversized token field
→ rejected before lifecycle execution
```

### 28.13 CLI/Web parity

For the same fixture state, CLI/shared lifecycle and Web/controller must agree on at least:

```text
repository validation
local identity
configured state
normal-removal eligibility
recovery eligibility
ambiguous legacy state
service mismatch failure
```

## 29. Live Debian acceptance

After static/code audit passes, validate on the real Debian host in this order:

```text
1. bash tests/run-all.sh
2. setup-web-management.sh --dry-run
3. inspect generated users/config/services/helper plan
4. setup-web-management.sh --apply
5. verify frontend runs as grt-web
6. verify frontend only listens on 127.0.0.1
7. verify no direct LAN/WAN listener exists
8. verify privileged helper has no TCP listener
9. verify actions cannot read Web auth config
10. verify actions cannot invoke privileged helper
11. verify grt-web cannot write runner directories
12. verify privileged installed code is root-owned and not writable by actions/grt-web
13. verify Tailscale Serve HTTPS endpoint
14. verify no Tailscale Funnel/public endpoint exists
15. login from phone through Tailnet
16. list current runners
17. create a disposable/test repository runner using a temporary registration token
18. verify runner files are owned by actions
19. verify runner appears and service is active
20. verify systemd unit User/WorkingDirectory/ExecStart match expected runner identity
21. normal-remove that disposable runner using a temporary removal token
22. create a second disposable runner
23. delete it on GitHub first
24. wait for local .runner/.credentials cleanup
25. use Web Recover local residue
26. verify systemd unit absent
27. verify runner directory removed
28. verify /srv/github-actions-archive is untouched
29. verify registration/removal tokens do not appear in journal, argv, environment, temp files, or responses
30. verify local CLI create/status/remove behavior still works
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
- security boundary between `grt-web`, runner-owner lifecycle, privileged service helper, and the runner owner.

## 31. Acceptance criteria

Web Management V1 is complete only when all of the following are true:

1. Web frontend runs as dedicated non-root `grt-web`.
2. Runner owner and Web frontend identities are separate.
3. Frontend binds only to loopback.
4. Remote access is exclusively Tailnet HTTPS through Tailscale Serve.
5. Direct LAN/WAN access to the backend is impossible under the default configuration.
6. No Tailscale Funnel/public exposure is enabled.
7. Local administrator password is stored only as a hardened salted hash.
8. Authentication uses opaque server-side sessions; restart invalidates sessions.
9. Session cookie uses Secure, HttpOnly, SameSite=Strict.
10. CSRF protection covers every mutation.
11. Production mode disables request-body/form-value/debug secret logging.
12. Request/token sizes are bounded.
13. No temporary GitHub token is sent through argv, environment, file, URL, logs, or response.
14. Whole runner lifecycle is never run as root.
15. Registration/configuration/unregister/directory operations execute as the configured runner owner.
16. Root privilege is limited to host dependency/service-management operations.
17. Privileged helper executes only root-owned, non-runner-writable code as root.
18. Runner owner receives no new passwordless root execution path.
19. Runner owner cannot invoke privileged-helper IPC.
20. Privileged service mutation cross-checks actual systemd User, WorkingDirectory, and ExecStart against expected identity.
21. Runner-owned `.service` metadata alone is never sufficient privileged authority.
22. Existing CLI lifecycle remains functional.
23. CLI and Web lifecycle share the same semantic authority.
24. `status-runners.sh --json` implements the frozen machine contract.
25. UI action availability comes from machine-readable eligibility flags, but those flags are advisory only.
26. Every mutation acquires the global lock before authoritative identity/state revalidation.
27. Every privileged service mutation revalidates root-controlled systemd state immediately before execution.
28. Create accepts only OWNER/REPO + temporary registration token in V1.
29. Normal remove requires temporary removal token.
30. Local recovery requires no GitHub token and preserves existing frozen recovery semantics.
31. Destructive operation uses session-bound, operation-bound, repository-bound single-use confirmation.
32. Only one mutation runs at a time.
33. Mutation timeout is bounded.
34. Local artifact archive is never deleted by Web runner removal/recovery.
35. Direct LAN management is not available in V1.
36. Automated tests cover §28.
37. Full repository test suite passes.
38. Live Debian acceptance sequence in §29 passes before release.
39. README, Chinese README, and CHANGELOG are updated only after implementation passes audit.

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
runner-owner lifecycle controller
+
narrow root-owned dependency/systemd helper
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
direct LAN access
public Internet access
```

requires a separate SPEC amendment.

---

End of SPEC.
