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

The cloned script and the one-line bootstrap use the same defaults. Both now require the one-time local artifact platform setup to be present.

## Local artifact archive

Every runner registered by the current tool is configured with the shared completed-job hook. After normal workflow steps finish, the hook copies the final workspace into:

```text
/srv/github-actions-archive/OWNER/REPO/RUN_ID/attempt_N/JOB_KEY/
```

The archive contains `workspace/`, `manifest.json`, and `manifest.sha256`. The default policy excludes reproducible dependency/cache directories such as `.git/`, `node_modules/`, virtual environments, Python bytecode caches, and pytest cache. It does **not** exclude research result directories such as `data/`, `results/`, `reports/`, `artifacts/`, `output/`, or `checkpoints/`.

Archive failure is fail-closed: the hook emits a stable `LOCAL_ARTIFACT_*` error and exits non-zero. The default disk guard refuses a new archive when the archive filesystem has less than 15% free space. The default copy timeout is 3600 seconds.

The hook attempts to write the final GitHub Step Summary after archive finalization. This completed-hook Summary path still requires live validation on the actual Debian runner version before it is treated as the stable V1 default. A reusable fallback action is included at:

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

The repository includes runner-management tests and local-artifact unit/integration/failure-path tests:

```bash
bash tests/run-all.sh
```

The archive suite covers path validation, hook configuration, workspace archive behavior, exclusions, symlink safety, job-key collisions, failure manifests, disk guard, timeout handling, retention cleanup, existing-runner migration, and GitHub artifact migration.

These automated tests do not replace real-host validation. In particular, completed-hook `$GITHUB_STEP_SUMMARY` behavior, hook failure propagation through GitHub's `Complete runner` stage, systemd restart behavior, and large real-workspace copies must be validated on the actual Debian runner before the local-artifact release is declared complete.

## Security and limitations

A self-hosted runner executes commands from repository workflows. Treat the runner host as a real code-execution environment and isolate it from production where practical.

Do not commit registration/removal tokens, PATs, SSH private keys, production API keys, database passwords, or other long-lived secrets. Avoid exposing a host Docker socket or unrelated production data to CI merely for convenience.

This project currently focuses on repository-level runners on systemd Linux hosts. Organization runner groups, autoscaling fleets, Kubernetes, Windows, macOS, cross-user installation, automatic token generation, workflow rewriting, and production deployment are outside the v1 scope.
