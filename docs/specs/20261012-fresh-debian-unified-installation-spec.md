# Fresh Debian Unified Installation + Legacy Web Removal SPEC

Status: **DRAFT — INDEPENDENT SPEC AUDIT REQUIRED BEFORE IMPLEMENTATION**

Repository: `ernestyu/github-runner-tools`

Implementation baseline: `4510cc3c0982627124a72fd3139b9ea39a69d3da`

This SPEC changes packaging, first-install behavior, Web transport exposure, and removal of the historical Web installation/migration surface. It does **not** redesign the already-audited CLI-first lifecycle authority, Create/Remove state machine, operational logging contract, canonical Runner Unit installation, or UNKNOWN semantics.

## 1. Motivation

The current tree has reached a contradictory product state:

1. the audited current runtime is the CLI-first Web/Executor architecture;
2. fresh Web installation still begins with `scripts/setup-web-management.sh`, which installs the historical Web implementation, requires active Tailscale, binds to `127.0.0.1`, and writes `managed-by=github-runner-tools-web-v1`;
3. `scripts/setup-web-management-v2.sh` is a migration/cutover script that requires an existing V1 installation and a Git checkout;
4. current `web/app.py` still contains a runtime V1/V2 routing switch and a V1 dispatcher fallback;
5. documentation still describes historical V1/Tailscale-only setup as the active installation model;
6. therefore a clean Debian host cannot install the current product directly with one `curl | bash` command.

The target product has only one current architecture:

```text
CLI ───────────────┐
                   ├── shared lifecycle authority / operational logging
Web → Executor ────┘
```

A user must not install an obsolete Web generation and then migrate it.

## 2. Product requirements

A clean supported Debian host must be installable with one bootstrap command:

```bash
curl -fsSL https://github.com/ernestyu/github-runner-tools/releases/latest/download/install.sh | bash
```

After that command completes successfully:

- CLI runner lifecycle management is installed and usable;
- Web management is installed, enabled, and usable;
- Web and CLI use the same audited lifecycle semantics;
- no Git clone or persistent source checkout is required;
- Tailscale is not required;
- default Web access is directly over the trusted local LAN using HTTP;
- Web administrator authentication remains required;
- temporary GitHub registration/removal tokens remain request-scoped and must not be persisted or logged;
- the current Executor is the only privileged Web mutation path.

The installer may prompt through `/dev/tty` for the Web administrator password and, when network-interface selection is ambiguous, for the LAN bind address. Interactive password entry does not violate the one-command installation requirement.

## 3. Supported fresh-host baseline

Initial acceptance target:

- Debian 13;
- systemd as PID 1;
- x86_64 host acceptance is mandatory;
- installer is executed by the intended non-root Runner owner;
- that user has working `sudo` access;
- `curl` and CA certificates must exist sufficiently to fetch the bootstrap; the installer may install remaining Debian package dependencies through `sudo apt-get`.

The installer must derive the Runner owner, primary group, and home directory from the invoking user. It must not hard-code `actions` or `/home/actions`.

ARM64 support already present in Runner lifecycle code is not removed, but fresh-installer release acceptance on ARM64 is not a release gate until separately validated.

## 4. No V1 installation or migration in the current product tree

The current branch must no longer contain an active historical Web generation.

Implementation must delete or replace all current-tree artifacts whose only purpose is historical V1 installation/migration, including at minimum:

- `scripts/setup-web-management.sh`;
- `scripts/setup-web-management-v2.sh`;
- `scripts/web-v2-cutover-rollback.sh`;
- `scripts/rollback-web-management-v2.sh`;
- V1/V2 cutover-only tests;
- V1 managed markers;
- V1 dispatcher-service installation paths;
- active documentation that instructs V1 installation or V1→V2 migration.

The following strings/concepts are forbidden in active installation/runtime behavior:

- `managed-by=github-runner-tools-web-v1`;
- `MARKER_V1`;
- “install V1 first”;
- “V1 -> V2” / “V1→V2” cutover;
- V1 rollback as a supported installation path;
- Tailscale as a Web prerequisite.

Historical Git commits/tags are not rewritten.

Release version names such as `v1.0.0` in historical release history are software version identifiers and are not themselves a Web-V1 architecture marker. However, current README/setup instructions and the Unreleased product description must not present Web Management V1 as a current feature.

Historical architecture/spec files that exist only to describe the removed Web generation must be removed from the active tree or moved out of active documentation. Git history remains the archive.

## 5. One current Web runtime path

The installed Web frontend must always use the audited Executor path.

Current runtime branches such as:

```text
if V2_EXECUTOR_MODE:
    use Executor
else:
    use historical dispatcher socket
```

must be removed.

Fresh configuration must not need `V2_EXECUTOR_MODE`.

The Web frontend must call the fixed Executor client directly for supported operations. There must be no runtime fallback to the historical `web-dispatch.sock` protocol.

The systemd service `github-runner-tools-dispatch.service` must not be installed or required.

Internal Python module names containing `v2_` do not need to be renamed in this implementation if renaming provides no runtime/security value. User-visible product naming, configuration, installation, systemd descriptions, and documentation must describe one current architecture rather than a V1/V2 product split.

## 6. Fresh-install release packaging

Installation must not depend on Git.

Each public release intended for fresh installation must publish at least:

```text
install.sh
github-runner-tools-<version>.tar.gz
```

The release bootstrap `install.sh` must be generated or finalized with an immutable target version and expected SHA-256 for the matching release archive.

Bootstrap sequence:

```text
install.sh
→ identify embedded release/version
→ download matching release archive over HTTPS
→ verify exact SHA-256
→ extract with normal system tar into a private temporary directory
→ execute the archive's fixed fresh-install entrypoint
→ remove temporary extraction
```

The payload archive must contain only the source/runtime material required to install the current product. The production host must not retain:

- `.git`;
- CI workflow files;
- development-only test suites;
- historical specs;
- source checkout state.

A checksum mismatch is fatal before any persistent installation mutation.

No bootstrap may execute code from an unverified downloaded payload.

## 7. Fresh installer responsibilities

The fresh installer must directly provision the current architecture, not call a legacy setup or migration script.

It must install and verify at least:

### 7.1 Runtime code

Root-owned, non-group/world-writable fixed installation paths under:

```text
/usr/local/lib/github-runner-tools/cli
/usr/local/lib/github-runner-tools/web
```

including the currently audited:

- CLI Create/Status/Remove/logging/cleanup components;
- lifecycle authority;
- canonical Unit installer;
- Executor runtime/client/protocol components;
- Web application;
- required archive/hook runtime helpers.

Installed runtime code must match the extracted verified release payload byte-for-byte.

### 7.2 Persistent authority/log state

Provision with the audited ownership/mode semantics:

```text
/var/lib/github-runner-tools
/var/log/github-runner-tools
/run/lock/github-runner-tools
```

The lifecycle mutation lock and operational log files must be initialized safely without weakening existing authority rules.

### 7.3 Local artifact archive platform

The fresh installer must perform the required local archive-platform initialization automatically. A user must not have to clone the repository and separately run `setup-local-archive.sh` before the first Runner Create.

The resulting archive configuration/hook semantics remain those already implemented and tested; this SPEC changes installation orchestration, not archive retention/publication semantics.

### 7.4 Web identity/configuration

Provision the dedicated non-login `grt-web` service account if absent.

Prompt twice on `/dev/tty` for the administrator password on a fresh install and store only the existing password hash format in a root-owned configuration file readable only by the Web service group as required.

Do not generate or store a default plaintext password.

### 7.5 systemd services

Fresh install must create only the current required management services:

- `github-runner-tools-executor.service`;
- `github-runner-tools-web.service`.

The Web service must require the Executor service.

The old dispatcher service must not be installed.

Executor sandboxing, fixed socket ownership/mode, root authority boundaries, `NoNewPrivileges`, and narrow `ReadWritePaths` must remain at least as restrictive as the audited current implementation.

### 7.6 First-install smoke checks

Before reporting success, installer must verify:

- installed runtime files are root-owned with expected modes;
- Executor is active;
- Web is active;
- Executor socket exists with expected type/owner/group/mode;
- CLI Status succeeds;
- Web Status/list through Executor succeeds without a lifecycle mutation;
- operational JSONL receives a valid read-only Status event;
- no existing Runner Unit was created, removed, restarted, or altered by installation.

A smoke-check failure must return nonzero and identify the failed installation stage. Fresh-install diagnostics must not be a bare rollback-only message.

## 8. Default LAN Web access

Tailscale is not a dependency and must not be checked by the installer or systemd units.

Default Web transport is trusted-LAN HTTP:

```text
http://<selected-private-LAN-IPv4>:8765/
```

The installer must not default to `0.0.0.0`.

### 8.1 LAN address selection

Default automatic selection:

1. enumerate non-loopback IPv4 addresses;
2. accept only RFC1918 private addresses:
   - `10.0.0.0/8`;
   - `172.16.0.0/12`;
   - `192.168.0.0/16`;
3. prefer an eligible address on the default-route interface;
4. if exactly one unambiguous candidate exists, use it;
5. if multiple plausible candidates remain, prompt on `/dev/tty` and require explicit selection;
6. if no private candidate exists, fail with a clear instruction to supply an explicit supported bind address.

Loopback, link-local, multicast, wildcard, and public IPv4 addresses must not be selected automatically.

The selected address is persisted as `WEB_BIND_ADDRESS`.

The Web application must permit that validated configured private address rather than enforcing `127.0.0.1`.

### 8.2 HTTP security model

Default LAN HTTP is explicitly a **trusted-network mode**, not encrypted transport.

The installer and README must state that administrator password and temporary GitHub tokens are transported without TLS encryption and therefore this mode is for trusted home/lab/management LANs only.

It must not be described as suitable for:

- public Wi-Fi;
- untrusted shared LANs;
- direct Internet exposure.

Authentication is still mandatory. HTTP does not remove or weaken application authorization.

### 8.3 Session cookies

Current unconditional `Secure` cookies are incompatible with default HTTP.

Cookie behavior must be transport-aware:

For default HTTP:

```text
HttpOnly; SameSite=Strict; Path=/
```

For an explicitly configured HTTPS mode in a future/optional path:

```text
Secure; HttpOnly; SameSite=Strict; Path=/
```

The installer must never silently mark HTTP as HTTPS merely to retain the Secure attribute.

Existing session expiry, password verification, login rate limiting, CSRF tokens, confirmation nonces, request-size bounds, CSP, frame denial, no-store responses, and token redaction remain required.

## 9. Optional Tailscale / TLS

Tailscale, reverse proxies, and TLS are optional deployment choices external to the default fresh-install requirement.

The core install must:

- succeed when the `tailscale` command does not exist;
- succeed when `tailscaled.service` does not exist;
- not add `tailscaled.service` to systemd `After=`, `Wants=`, or `Requires=`;
- not configure Tailscale Serve/Funnel.

Documentation may describe optional Tailscale/reverse-proxy use after installation, but it cannot be a prerequisite or the canonical path.

This SPEC does not require an automated local PKI or self-signed certificate workflow.

## 10. CLI and Web lifecycle capability

Fresh installation must leave both interfaces usable immediately.

### CLI

Installed fixed CLI must support the already-audited lifecycle behavior:

- Create;
- Status/inventory;
- normal Remove;
- bounded local recovery/reconciliation operations already authorized by current implementation.

### Web

Web must support the normal lifecycle without a CLI prerequisite:

- authenticated inventory/Status;
- Create with request-scoped registration token;
- normal Remove with confirmation and request-scoped removal token.

Existing safe recovery UI behavior must not regress.

Emergency/admin-only reconciliation that is intentionally CLI-only under an existing frozen authority rule does not need to be moved into Web by this packaging SPEC. This SPEC must not weaken that authority boundary merely for UI parity.

CLI and Web must continue to serialize mutations through the same mutation lock and use the same lifecycle authority semantics.

## 11. Existing audited lifecycle behavior is frozen

Implementation must not redesign:

- Create stage ordering;
- registration attestation;
- official Runner wrapper provenance;
- canonical root-owned Unit installation;
- Remove state machine;
- incomplete-Create UNKNOWN rules;
- `CREATE_ABORTED_RECONCILED`;
- operational JSONL schema/reason semantics;
- six existing production Runner Unit/registration semantics.

Any code edits needed to remove the V1 fallback or enable LAN HTTP must be narrowly limited to routing/transport/configuration and must not introduce an alternative lifecycle implementation.

## 12. Fresh-install failure and existing-host behavior

This installer is first-install authority, not a migration engine.

By default it must fail closed if it detects an existing github-runner-tools management installation whose ownership/configuration cannot be proven as an exact supported reinstall target.

It must not contain V1→current migration code.

Replacement/upgrade of an already installed current version may be added only as a separately specified mode or release task. It is not required to satisfy fresh Debian installation.

Fresh-install failure after persistent mutation must report the exact failed stage and leave enough deterministic state for safe administrator cleanup/retry. It must never mutate registered Runner services as part of installer rollback.

## 13. Required automated tests

### 13.1 Legacy removal

Tests must prove:

- historical setup/migration entrypoints are absent;
- no active Web configuration/unit contains the V1 managed marker;
- Web runtime has no historical dispatcher fallback;
- no Tailscale prerequisite remains;
- current docs do not instruct V1 installation/migration.

### 13.2 Bootstrap/package integrity

Tests must build a release payload, verify expected SHA-256 handling, normal extraction, tampered archive rejection, missing payload rejection, and no-Git installation.

Installer tests must execute from a directory that is not a Git checkout.

### 13.3 Fresh Debian filesystem installation

In an isolated Debian/systemd-capable test environment or faithful root-isolated host fixture:

- begin with no `/usr/local/lib/github-runner-tools`, no management config, no management units, and no lifecycle state;
- execute the production fresh installer;
- verify CLI + Web + Executor + archive platform installation;
- verify root ownership/modes and dependency closure;
- verify no old dispatcher unit;
- verify no Tailscale dependency;
- verify smoke checks.

### 13.4 LAN binding

Cover:

- one private IPv4 candidate → automatic bind;
- multiple candidates → explicit selection required;
- no private IPv4 → fail;
- public-only address → not automatically selected;
- loopback-only → not accepted as default LAN install;
- wildcard `0.0.0.0` → not selected automatically.

### 13.5 HTTP auth/session

Real HTTP tests must prove:

- login works over configured LAN HTTP;
- session cookie omits `Secure` in HTTP mode;
- cookie retains `HttpOnly` and `SameSite=Strict`;
- unauthenticated lifecycle actions are rejected;
- CSRF protection remains effective;
- confirmation nonce remains one-time/bounded;
- password/token values do not appear in application, systemd, or operational logs.

### 13.6 CLI/Web fresh-host lifecycle acceptance

After fresh install, use disposable Runner identities to prove:

```text
CLI Create → online → Status → Remove
Web Create → online → Status → Remove
```

Both flows must use the installed production files, not checkout-relative paths.

The acceptance must assert that pre-existing production Runner inventory is unchanged when such inventory is present in the host fixture.

## 14. Debian live acceptance

Implementation audit PASS is required before live-host acceptance.

Final live acceptance should be performed on a clean/disposable Debian 13 host or VM, not by simulating “fresh install” over the current production-like host.

Required live sequence:

1. confirm no github-runner-tools installation;
2. execute the single public `curl | bash` command;
3. confirm installer-selected LAN URL is reachable from a second LAN machine;
4. log in with administrator password over HTTP;
5. CLI Create disposable Runner;
6. verify online and run a harmless targeted job;
7. CLI Remove;
8. Web Create a second disposable Runner;
9. verify online and run a harmless targeted job;
10. Web Remove;
11. inspect operational logs;
12. reboot Debian;
13. confirm Web/Executor return automatically and inventory remains correct.

No Tailscale installation is permitted merely to make this acceptance pass.

## 15. Documentation requirements

README and README.zh-CN must start from the fresh-install experience.

Primary installation path must be the single release bootstrap command.

Documentation must explain:

- supported Debian baseline;
- installer must run as intended non-root Runner owner with sudo;
- Web is installed by default;
- selected LAN URL;
- trusted-LAN HTTP security limitation;
- administrator password;
- CLI lifecycle commands;
- Web lifecycle operations;
- optional Tailscale/reverse proxy/TLS as optional add-ons only.

Documentation must not instruct users to clone the repository for normal installation.

Git clone remains a developer/contributor workflow.

## 16. Out of scope

This implementation does not add:

- public Internet Web exposure;
- automatic firewall port forwarding;
- Tailscale installation/configuration;
- automatic TLS PKI;
- organization-level Runner management;
- Kubernetes/containers as production deployment;
- automatic GitHub token generation;
- changes to GitHub workflow repositories.

## 17. Implementation boundaries

Implementation may modify packaging/install scripts, Web bind/session transport configuration, systemd templates, release workflow, documentation, and tests required by this SPEC.

Lifecycle runtime changes are permitted only where strictly necessary to remove the historical Web fallback or connect fresh-installed Web to the existing Executor.

No implementation work begins until independent audit freezes this SPEC.

## 18. Audit questions

Independent auditor should answer:

1. Does fresh installation truly bypass every V1/migration dependency?
2. Is one-command installation possible without Git checkout?
3. Is the release payload integrity chain deterministic and fail-closed?
4. Does fresh install provision current CLI + Web + Executor + archive platform directly?
5. Is Tailscale fully optional?
6. Is LAN HTTP behavior explicit and technically compatible with session cookies?
7. Is automatic binding constrained to trusted private IPv4 rather than wildcard/public interfaces?
8. Are password, CSRF, nonce, and token-redaction controls preserved?
9. Is the historical dispatcher runtime fallback removed?
10. Are existing lifecycle/UNKNOWN semantics unchanged?
11. Are fresh CLI and Web lifecycle acceptance requirements sufficient?
12. Is a clean Debian live acceptance required before release?

Required verdict:

`PASS — FREEZE FRESH DEBIAN UNIFIED INSTALLATION SPEC`

or

`BLOCKED — SPEC REMEDIATION REQUIRED`.
