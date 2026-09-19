# github-runner-tools

[中文说明](README.zh-CN.md)

`github-runner-tools` is a small set of scripts for registering and managing GitHub repository-level self-hosted runners on a Linux host. It keeps GitHub Actions as the scheduler and log system, while moving the actual compute work to your own server, NAS, mini PC, VM, or VPS.

The project does not replace GitHub Actions, modify application code, or deploy applications. Its job is narrower: make the official GitHub Actions runner easier and safer to provision for multiple repositories.

The current target is Debian 12/13 with systemd. x86_64 is the primary tested architecture. ARM64 support is implemented in the bootstrap path but should be treated as unvalidated until it has been tested on a real ARM64 host.

## Quick start

Run the bootstrap as the normal Linux user that should own the runner. Do not run it as root. The default install location is that user's real home directory.

First install the basic tools:

```bash
sudo apt update
sudo apt install -y ca-certificates curl jq tar coreutils
```

Open the target GitHub repository and go to:

```text
Settings
→ Actions
→ Runners
→ New self-hosted runner
```

Choose Linux and the architecture that matches the host, then copy the temporary registration token from the `config.sh` command GitHub shows.

### One-line bootstrap

Before the first tagged release, the development form is:

```bash
curl -fsSL https://raw.githubusercontent.com/ernestyu/github-runner-tools/main/scripts/register-runner.sh \
  | bash -s -- OWNER/REPO
```

The script reads the registration token from `/dev/tty`, not standard input, so this works correctly with `curl | bash`. The token is entered silently and is not stored by the script.

For stable use, prefer a tagged release once one is published:

```bash
curl -fsSL https://raw.githubusercontent.com/ernestyu/github-runner-tools/VERSION/scripts/register-runner.sh \
  | bash -s -- OWNER/REPO
```

If you prefer to inspect the script before running it:

```bash
curl -fsSL https://raw.githubusercontent.com/ernestyu/github-runner-tools/VERSION/scripts/register-runner.sh \
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

The cloned script and the one-line bootstrap use the same defaults.

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

Remove a runner:

```bash
bash scripts/remove-runner.sh OWNER/REPO
```

The removal tool understands both the new owner+repo directory naming and the old repo-only naming used by earlier versions. A legacy directory is never trusted by filename alone: its `.runner` metadata must identify the requested repository before removal can continue. If identity is ambiguous, removal stops.

Service uninstall failure also stops local directory deletion. The removal script does not silently continue to `rm -rf` after a failed systemd uninstall.

Existing legacy runners are not renamed automatically.

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

Pure helper tests currently cover deterministic naming, owner collision avoidance, long-name hashing, and runner-version normalization:

```bash
bash tests/test-pure.sh
```

These tests do not replace real-host validation. Registration, systemd service behavior, TTY input, GitHub token handling, and ARM64 still need environment-specific testing.

## Security and limitations

A self-hosted runner executes commands from repository workflows. Treat the runner host as a real code-execution environment and isolate it from production where practical.

Do not commit registration/removal tokens, PATs, SSH private keys, production API keys, database passwords, or other long-lived secrets. Avoid exposing a host Docker socket or unrelated production data to CI merely for convenience.

This project currently focuses on repository-level runners on systemd Linux hosts. Organization runner groups, autoscaling fleets, Kubernetes, Windows, macOS, cross-user installation, automatic token generation, workflow rewriting, and production deployment are outside the v1 scope.
