# github-runner-tools

[中文说明](README.zh-CN.md)

`github-runner-tools` is a small set of scripts for registering and managing GitHub repository-level self-hosted runners on a Linux host. It keeps GitHub Actions as the scheduler and log system, while moving the actual compute work to your own server, NAS, mini PC, VM, or VPS.

The project does not replace GitHub Actions, modify application code, or deploy applications. Its job is narrower: make the official GitHub Actions runner easier and safer to provision for multiple repositories.

The current target is Debian 12/13 with systemd. The x86_64 path, including the local-artifact archive flow, has been validated on a real Debian self-hosted runner host. ARM64 support is implemented in the bootstrap path but should still be treated as unvalidated until it has been tested on a real ARM64 host.

## Current validation status

The current implementation has passed the repository test suite and live Debian self-hosted runner validation. The completed-job hook, local archive, manifest/hash publication, GitHub Step Summary, existing-runner migration path, and normal workflow execution have all been exercised successfully on the real runner host.

The optional Web Management V1 frontend has also been installed on the real Debian host and validated for Tailscale-only HTTPS access, authentication, dispatcher startup, privilege drop, and listing the existing runner inventory. Web Management is still collected under **Unreleased** until its create/remove/recover paths complete final live acceptance.

The remaining platform caveat is ARM64: the code path exists, but it has not yet been validated on a real ARM64 runner.

## Quick start

Run the bootstrap as the normal Linux user that should own the runner. Do not run it as root. The default install location is that user's real home directory.

First install the basic tools:

```bash
sudo apt update
sudo apt install -y ca-certificates curl jq tar coreutils rsync util-linux
```

Open the target GitHub repository and go to:

```text
Settings
→ Actions
→ Runners
→ New self-hosted runner
```

Choose Linux and the architecture that matches the host, then copy the temporary registration token from the `config.sh` command GitHub shows.

### One-time local artifact setup

The current version archives completed self-hosted jobs to local Debian storage by default. Run the platform setup once on the runner host before registering new runners.

Use a full checkout for this one-time host setup:

```bash
cd ~
git clone https://github.com/ernestyu/github-runner-tools.git
cd github-runner-tools

bash scripts/setup-local-archive.sh --dry-run
bash scripts/setup-local-archive.sh --apply
```

The default is dry-run. Review the resolved paths and permissions first, then use `--apply` to make the host-level changes.

Run the setup script as the same normal Linux user that owns the runners. Do **not** run the whole script with `sudo`; it requests sudo only for the host-level files under `/srv`, `/etc`, and `/usr/local/lib`.

The default local store is:

```text
/srv/github-actions-archive
```

Existing runners can be inspected and migrated after setup:

```bash
bash scripts/enable-local-archive.sh --dry-run
bash scripts/enable-local-archive.sh --apply
```

The apply command adds the shared `ACTIONS_RUNNER_HOOK_JOB_COMPLETED` hook and restarts only validated runner services. A conflicting existing completed hook is never silently replaced.

### One-line runner bootstrap

After the one-time local artifact setup, future repository runners can still be registered with one command.

For stable use, pin the first public release:

```bash
curl -fsSL https://raw.githubusercontent.com/ernestyu/github-runner-tools/v1.0.1/scripts/register-runner.sh \
  | bash -s -- OWNER/REPO
```

The script reads the registration token from `/dev/tty`, not standard input, so this works correctly with `curl | bash`. The token is entered silently and is not stored by the script.

To follow current development on `main` instead:

```bash
curl -fsSL https://raw.githubusercontent.com/ernestyu/github-runner-tools/main/scripts/register-runner.sh \
  | bash -s -- OWNER/REPO
```

If you prefer to inspect the released script before running it:

```bash
curl -fsSL https://raw.githubusercontent.com/ernestyu/github-runner-tools/v1.0.1/scripts/register-runner.sh \
  -o register-runner.sh

less register-runner.sh
bash register-runner.sh OWNER/REPO
```

### Full management checkout

Clone the repository if you also want the status and removal tools:

```bash
cd ~
git clone https://github.com/ernestyu/github-runner-tools.git
cd github-runner-tools

bash scripts/register-runner.sh OWNER/REPO
bash scripts/status-runners.sh
```

The cloned script and the one-line bootstrap use the same defaults. Both now require the one-time local artifact platform setup to be present.

## Optional Web Management

Web Management V1 is an explicit opt-in component. The default installation remains CLI-only, and the CLI tools continue to work normally without the Web UI.

The Web component is useful when the runner host is remote or you mainly have access from a phone. It provides a small management page for:

- listing existing repository runners;
- creating a runner from `OWNER/REPO` plus a temporary GitHub registration token;
- normal removal with a temporary GitHub removal token;
- local recovery cleanup for a runner that was already removed from GitHub.

The browser frontend never listens directly on the LAN or public Internet. The backend binds only to:

```text
127.0.0.1:8765
```

Remote access is intended to go through **Tailscale Serve**. Do not use Tailscale Funnel for this component.

### Prerequisites and access control

The Web installer requires an installed Tailscale CLI and an active `tailscaled` service, even if you do not immediately enable remote access. Install and join Tailscale first, then verify:

```bash
tailscale status
systemctl is-active tailscaled
```

For remote access, the browsing device must be on the same tailnet (or have explicitly authorized shared access). Tailnet access controls must permit the client to reach the Web host on TCP 443. On tagged hosts, authorize the host's **tag identity**, not an assumed user identity. The Web installer does not edit tailnet policies. Do not leave an unrestricted allow-all policy in place solely to make the Web UI reachable.

### One-time Web installation

Clone or update the repository, then inspect the plan first:

```bash
cd ~/github-runner-tools
git pull --ff-only

bash scripts/setup-web-management.sh --dry-run
bash scripts/setup-web-management.sh --apply
```

Run the setup as the normal runner owner, not as root. The script uses `sudo` internally only for fixed host-level installation steps.

`--apply` is the explicit opt-in point. It installs the Web frontend, privileged dispatcher, fixed lifecycle worker, configuration, systemd units, and Web administrator password configuration.

The first installation prompts for a **single Web administrator password**. V1 has no username field, per-user accounts, or role-based access control. This password is separate from Linux, GitHub, and Tailscale authentication. On repeat `--apply`, an existing authentication file with the expected ownership and permissions is preserved, so the existing password remains in use. Tailscale access control is an additional network boundary, not a substitute for Web login.

It also runs:

```text
systemctl enable github-runner-tools-dispatch.service
systemctl enable github-runner-tools-web.service
```

and starts/restarts both services. Therefore, after a successful one-time `--apply`, the Web backend is a normal persistent systemd service and starts automatically after reboot. You do **not** need to SSH into the Debian host every time you want to use the UI.

Check the services with:

```bash
systemctl status github-runner-tools-dispatch.service
systemctl status github-runner-tools-web.service
```

To stop the Web UI without uninstalling it:

```bash
sudo systemctl stop github-runner-tools-web.service
sudo systemctl stop github-runner-tools-dispatch.service
```

To keep it disabled across reboot:

```bash
sudo systemctl disable --now github-runner-tools-web.service
sudo systemctl disable --now github-runner-tools-dispatch.service
```

To enable it again later:

```bash
sudo systemctl enable --now github-runner-tools-dispatch.service
sudo systemctl enable --now github-runner-tools-web.service
```

### Tailscale access is a separate operator choice

The setup script deliberately does **not** configure Tailscale Serve automatically. After Web setup succeeds, the operator chooses whether to expose the loopback backend to the tailnet:

```bash
sudo tailscale serve --bg http://127.0.0.1:8765
```

The `--bg` form persists after the terminal session ends and resumes after device or Tailscale restarts. To inspect or disable it:

```bash
tailscale serve status
sudo tailscale serve off
```

This separation is intentional:

```text
CLI-only host
    → no Web components

setup-web-management.sh --apply
    → Web services installed + enabled + started locally

tailscale serve --bg ...
    → operator explicitly makes the UI reachable inside the tailnet
```

The Web backend remains loopback-only even when Serve is enabled. Read the exact private HTTPS URL from `tailscale serve status` (typically `https://HOST.TAILNET.ts.net/`); do not append another `.ts.net`. Only authorized tailnet clients should be able to connect.

### Post-install verification and troubleshooting

On the runner host:

```bash
systemctl is-active github-runner-tools-dispatch.service
systemctl is-active github-runner-tools-web.service
curl -i --max-time 5 http://127.0.0.1:8765/
tailscale serve status
```

An unauthenticated request to the loopback backend may redirect to `/login`. From another authorized tailnet device, open the HTTPS URL reported by Serve, log in with the Web administrator password (no username), and confirm the local runner inventory is visible. The inventory reflects **local runners on this host**, not every runner registered on GitHub.

If the page reports `Runner status is unavailable`, that does **not** mean the runner services have stopped. Check the dispatcher and Web logs and the CLI status command before modifying any runner:

```bash
bash scripts/status-runners.sh --json
sudo journalctl -u github-runner-tools-dispatch.service -n 60 --no-pager
sudo journalctl -u github-runner-tools-web.service -n 60 --no-pager
```

### Lifecycle operations and acceptance scope

**Create** requires `OWNER/REPO` and a temporary GitHub registration token. **Remove** uses a temporary GitHub removal token and performs remote unregister plus verified local cleanup. **Recover local** is only for narrowly verified residue after GitHub-side deletion; see [Workflow selection and management](#workflow-selection-and-management) for its fail-closed conditions. These operations can change or remove real runner installations: use a disposable repository for testing.

Live Debian acceptance currently covers Web login and listing existing runners. Web Create / Remove / Recover have automated coverage but **their disposable-runner live acceptance remains pending**; do not interpret CI PASS as proof of those production mutation paths.

### Web security model

Web Management preserves the existing runner lifecycle rules rather than replacing them. The frontend runs as the dedicated `grt-web` user, the root dispatcher has a narrow fixed authority surface, and the lifecycle worker drops to the configured runner owner before runner-owned lifecycle logic runs.

Temporary GitHub registration/removal tokens are request-scoped and are not intentionally stored in argv, environment variables, persistent files, URLs, sessions, or logs.

Tailscale grants control tailnet connectivity; they do not isolate ordinary LAN or public-network routes. Keep CI hosts isolated from sensitive networks where appropriate.

The configured runner-owner account is inside the temporary-token trust boundary. Do not use Web Management V1 on a host where untrusted/public workflows execute as that same runner owner.

## Local artifact archive

Every runner registered by the current tool is configured with the shared completed-job hook. After normal workflow steps finish, the hook copies the final workspace into:

```text
/srv/github-actions-archive/OWNER/REPO/RUN_ID/attempt_N/JOB_KEY/
```

The archive contains `workspace/`, `manifest.json`, and `manifest.sha256`. The default policy excludes reproducible dependency/cache directories such as `.git/`, `node_modules/`, virtual environments, Python bytecode caches, and pytest cache. It does **not** exclude research result directories such as `data/`, `results/`, `reports/`, `artifacts/`, `output/`, or `checkpoints/`.

Archive failure is fail-closed: the hook emits a stable `LOCAL_ARTIFACT_*` error and exits non-zero. The default disk guard refuses a new archive when the archive filesystem has less than 15% free space. The default copy timeout is 3600 seconds.

The hook writes the final GitHub Step Summary after archive finalization. This completed-hook Summary path has now been validated on the real Debian self-hosted runner deployment, so it is the default zero-repository-configuration path. A reusable fallback action is still included at:

```text
.github/actions/local-artifact-summary
```

Retention cleanup is separate from job completion:

```bash
bash scripts/cleanup-local-artifacts.sh --dry-run
bash scripts/cleanup-local-artifacts.sh --apply
```

The default retention is 90 days. A run containing `.keep` at its run root is never removed by automatic cleanup.

Existing GitHub Actions artifacts can be copied locally with:

```bash
bash scripts/migrate-github-artifacts.sh OWNER/REPO
```

The safe default is download + verify without remote deletion. Remote deletion requires the explicit `--delete-after-verified` option and only proceeds after a verified local copy exists.

## What the bootstrap does

For a repository such as `example/project-a`, the default local identity is derived from both owner and repository:

```text
example--project-a
```

The runner is installed under the current user's home:

```text
~/actions-runner-example--project-a
```

The default runner name and custom labels are:

```text
runner name: local-ci-example--project-a
labels:      local-ci,project-a
```

Including the owner in the local identity avoids collisions between repositories such as `example/project-a` and `another/project-a`. Local identities are capped at 64 characters. Longer owner/repository combinations are truncated deterministically and receive an 8-character SHA-256 suffix.

The script performs preflight checks for the current user, sudo, systemd, required commands, the base directory, TTY access, and CPU architecture before downloading the runner. It downloads the official `actions/runner` release for the detected architecture, verifies SHA-256 when GitHub provides a digest, runs the official dependency installer, registers the runner, installs the systemd service, starts it, and prints the final status.

The bootstrap does **not** pass `--replace`. If GitHub already has a runner with the same name, registration should fail instead of silently replacing the remote runner.

## Runner version pinning

By default, the bootstrap installs the latest official GitHub Actions Runner. You can pin the runner binary separately:

```bash
RUNNER_VERSION=2.328.0 \
  bash scripts/register-runner.sh OWNER/REPO
```

or:

```bash
bash scripts/register-runner.sh --runner-version v2.328.0 OWNER/REPO
```

Both `2.328.0` and `v2.328.0` are accepted and normalized internally.

Pinning the `github-runner-tools` URL fixes the bootstrap logic only. It does not pin the GitHub Actions Runner binary unless `RUNNER_VERSION` or `--runner-version` is also supplied.

## Workflow selection and management

After registration, a repository can target its runner with the repository-specific label:

```yaml
runs-on: [self-hosted, Linux, X64, project-a]
```

On ARM64, the architecture label is `ARM64`.

Check all local runners:

```bash
bash scripts/status-runners.sh
```

Remove a runner while its GitHub registration still exists:

```bash
bash scripts/remove-runner.sh OWNER/REPO
```

The normal removal flow asks for GitHub's temporary removal token, unregisters the runner, safely uninstalls the systemd service, and then deletes the verified local runner directory.

If the runner was already deleted in GitHub and Runner.Listener has removed the local `.runner` / `.credentials` files, use the explicit local recovery path:

```bash
bash scripts/remove-runner.sh --recover-local OWNER/REPO
```

Recovery mode never asks for a GitHub removal token and never calls `config.sh remove`. It is deliberately narrow: `.runner` must be completely absent, the directory must be the exact owner+repository path, and the complete normalized repository scope must be provable from the non-truncated `.service` name. Ambiguous legacy directories, existing or malformed `.runner` state, unknown systemd state, and truncated/mismatched service identity all fail closed.

Local recovery removes only the verified runner service residue and runner directory. It does **not** delete archived CI results under `/srv/github-actions-archive`.

The removal tool also understands the old repo-only naming used by earlier versions during normal removal. A legacy directory is never trusted by filename alone: its `.runner` metadata must identify the requested repository before normal removal can continue. Legacy directories with missing `.runner` metadata are not eligible for automatic local recovery.

Service uninstall failure stops local directory deletion unless a post-check proves the unit is already absent. Existing legacy runners are not renamed automatically.

## Custom settings

You can override the base directory, runner name, labels, and runner binary version:

```bash
RUNNER_BASE_DIR=/srv/github-runners \
RUNNER_NAME=my-runner \
RUNNER_LABELS=local-ci,project-a,docker \
RUNNER_VERSION=2.328.0 \
  bash scripts/register-runner.sh OWNER/REPO
```

A custom `RUNNER_BASE_DIR` must already exist and be traversable, readable, and writable by the current user. The bootstrap will not use sudo to create or chown an arbitrary custom directory.

Cross-user provisioning is intentionally unsupported in v1. The user running `config.sh`, the owner of the runner files, and the systemd service user are the same normal Linux user.

If a previous failed registration leaves a non-empty unconfigured target directory, the default behavior is to stop. After inspecting it, you may explicitly request cleanup:

```bash
bash scripts/register-runner.sh --clean-incomplete OWNER/REPO
```

Configured runners containing `.runner` are never removed by that option.

## Tests

The repository includes runner-management tests and local-artifact unit/integration/failure-path tests:

```bash
bash tests/run-all.sh
```

The archive suite covers path validation, hook configuration, workspace archive behavior, exclusions, symlink safety, job-key collisions, failure manifests, disk guard, timeout handling, retention cleanup, existing-runner migration, and GitHub artifact migration.

The full automated suite has been run successfully on the real Debian CI host, and the local-artifact flow has also been exercised end to end with a live self-hosted runner. The validated path includes runner hook installation, completed-job workspace archival, manifest/hash publication, GitHub Step Summary output, systemd runner restart/migration behavior, and real workflow execution.

ARM64 remains implemented but not yet validated on a real ARM64 host.

## Contributing and security

Contributions are welcome. Before changing runner identity, service management, archive publication, cleanup, deletion, or credential handling, please read [CONTRIBUTING.md](CONTRIBUTING.md).

For security-sensitive issues, do not open a public issue with exploit details, tokens, or sensitive logs. Follow [SECURITY.md](SECURITY.md) instead.

Bug reports and feature requests use the repository's structured GitHub issue forms, and pull requests include a checklist for tests, platform validation, and safety impact.

## Security and limitations

A self-hosted runner executes commands from repository workflows. Treat the runner host as a real code-execution environment and isolate it from production where practical.

Do not commit registration/removal tokens, PATs, SSH private keys, production API keys, database passwords, or other long-lived secrets. Avoid exposing a host Docker socket or unrelated production data to CI merely for convenience.

This project currently focuses on repository-level runners on systemd Linux hosts. Organization runner groups, autoscaling fleets, Kubernetes, Windows, macOS, cross-user installation, automatic token generation, workflow rewriting, and production deployment are outside the v1 scope.


## License

This project is licensed under the [MIT License](LICENSE).
