# github-runner-tools

[中文说明](README.zh-CN.md)

`github-runner-tools` is a small set of scripts for managing GitHub repository-level self-hosted runners. It is designed for a simple setup: you have one always-on Linux host and want several GitHub repositories to use your own compute resources for GitHub Actions, without repeating the same manual runner installation and registration steps for every repository.

This project does not replace GitHub Actions, modify your application code, or deploy your application. GitHub still handles workflow triggers, scheduling, status, and logs. This project only makes it easier to install, register, and manage the official GitHub self-hosted runner on your own machine.

The current setup is mainly intended for Debian 12 / 13, Linux x86_64, and systemd. One host can run multiple independent repository-level runners.

## Why this exists

GitHub Actions can already run tests, builds, and other automated jobs on GitHub-hosted runners. That is convenient for small or occasional workloads. But when several private repositories run tests, Docker builds, or integration tests frequently, GitHub-hosted runners may become less attractive because of usage limits, cost, and limited control over the execution environment.

A self-hosted runner lets you move the actual compute work to your own server, NAS, mini PC, virtual machine, or VPS while keeping the existing GitHub Actions workflow, logs, and status reporting.

If your repositories belong to a personal GitHub account rather than one Organization, each repository normally needs its own repository-level runner registration. The official process is not difficult, but every new repository repeats the same work: download the runner, extract it, run `config.sh`, choose a runner name and labels, install a systemd service, start it, and verify its status.

`github-runner-tools` turns those repeated steps into a standard, reusable process.

## Quick start

### 1. Prepare the host

You need at least one Linux x86_64 host with the following basic tools:

```bash
sudo apt update
sudo apt install -y \
  ca-certificates \
  curl \
  jq \
  tar \
  coreutils
```

Run the runner as a normal user rather than registering it directly as `root`. A dedicated user such as the following works well:

```text
actions
```

If your CI jobs use Docker, install Docker Engine and Docker Compose separately and verify that the current user can use them:

```bash
docker version
docker compose version
docker run --rm hello-world
```

If you add the normal user to the `docker` group, sign out and sign back in before testing again so the new group membership takes effect.

### 2. Clone this repository

```bash
cd ~
git clone https://github.com/ernestyu/github-runner-tools.git
cd github-runner-tools
```

To update it later:

```bash
cd ~/github-runner-tools
git pull
```

The scripts can be run through `bash`, so they do not depend on the executable bit being preserved:

```bash
bash scripts/status-runners.sh
```

### 3. Generate a registration token on GitHub

Open the target repository that you want to connect to a self-hosted runner:

```text
Settings
→ Actions
→ Runners
→ New self-hosted runner
```

Choose:

```text
Linux
x64
```

GitHub will show a `config.sh` command containing a temporary registration token. Copy only the token. Do not save it in a script or commit it to Git.

### 4. Register the runner

Back on the Linux host, run:

```bash
cd ~/github-runner-tools
bash scripts/register-runner.sh OWNER/REPO
```

For example:

```bash
bash scripts/register-runner.sh yourname/project-a
```

The script will prompt:

```text
Paste GitHub registration token:
```

Paste the temporary token and press Enter. The token will not be echoed in the terminal.

After a successful registration, the script installs and starts the corresponding systemd service, then prints the runner status and the recommended workflow label.

## What it creates

`github-runner-tools` is only the management repository. The actual GitHub Actions runners are not installed inside this project directory.

If the tools repository is located at:

```text
/home/actions/github-runner-tools
```

the default runner base directory is:

```text
/home/actions
```

After registering runners for two repositories, the layout may look like this:

```text
/home/actions/
├── github-runner-tools/
├── actions-runner-project-a/
└── actions-runner-project-b/
```

The script resolves its install location from its own path. That means you can call it from any working directory:

```bash
bash ~/github-runner-tools/scripts/register-runner.sh yourname/project-a
```

and the runner will still be installed next to `github-runner-tools`, not inside it.

The default naming scheme is:

```text
repository:   yourname/project-a
runner dir:   actions-runner-project-a
runner name:  unraid-ci-project-a
labels:       unraid-ci,project-a
```

GitHub also adds system labels such as `self-hosted`, `Linux`, and `X64`.

The registration script automatically checks required commands, reads the latest GitHub Actions Runner release, downloads and extracts it, verifies the SHA-256 digest when available, installs the official dependencies, registers the repository runner, creates a systemd service, starts it, and prints the final status.

## Workflow, status, and daily management

After registration, if the target repository currently uses:

```yaml
runs-on: ubuntu-latest
```

you can switch it to the repository-specific label, for example:

```yaml
runs-on: [self-hosted, Linux, X64, project-a]
```

GitHub Actions will then wait for a matching self-hosted runner to pick up the job.

To check all runners on the current host:

```bash
cd ~/github-runner-tools
bash scripts/status-runners.sh
```

You can also inspect systemd directly:

```bash
systemctl --type=service | grep actions.runner
```

To view runner listener processes:

```bash
ps aux | grep Runner.Listener | grep -v grep
```

To inspect logs:

```bash
sudo journalctl -u 'actions.runner*' --since today
```

To follow logs in real time:

```bash
sudo journalctl -u 'actions.runner*' -f
```

Because each runner is installed as a systemd service, it should start again automatically after the host reboots. After a reboot, you can verify the runners with:

```bash
bash ~/github-runner-tools/scripts/status-runners.sh
```

### Remove a runner

Open the target repository on GitHub and go to:

```text
Settings
→ Actions
→ Runners
→ Select the runner
→ Remove
```

GitHub will provide a removal token. Then run:

```bash
cd ~/github-runner-tools
bash scripts/remove-runner.sh OWNER/REPO
```

The script will ask for the removal token. After confirmation, it stops and removes the systemd service, unregisters the runner from GitHub, and deletes the corresponding local runner directory.

### Customize the install directory, runner name, or labels

By default, runners are installed in the parent directory of `github-runner-tools`. To use a different base directory:

```bash
RUNNER_BASE_DIR=/srv/github-runners \
  bash scripts/register-runner.sh yourname/project-a
```

You can also override the runner name and labels:

```bash
RUNNER_NAME=my-runner \
RUNNER_LABELS=self-ci,project-a,docker \
  bash scripts/register-runner.sh yourname/project-a
```

A repository runner can execute one job at a time, but multiple runners on the same host can run different jobs concurrently. If several repositories perform Docker builds, start databases, or run large test suites at the same time, they will compete for the same CPU, memory, and disk resources. Whether you need to limit concurrency depends on the actual workload.

## Security and limitations

A self-hosted runner executes commands defined by GitHub workflows, so the runner host should be treated as a real code-execution environment rather than a read-only client. Keep the CI host separated from production when possible, and give it only the permissions needed for testing.

Do not commit the following into this repository or ordinary CI configuration:

```text
GitHub registration token
GitHub removal token
Personal Access Token
SSH private key
production API keys
production database passwords
other long-lived secrets
```

If the CI host runs next to a NAS, home server, or production host, avoid exposing the host Docker socket, production data directories, or other high-privilege interfaces directly to the runner. In particular, do not mount the host `/var/run/docker.sock` into the CI environment just for convenience.

This project currently handles installation and management of repository-level self-hosted runners. It does not create or modify GitHub Actions workflows automatically, deploy applications, or turn the CI test environment into a production environment.

The current setup is mainly used on Debian 13 with Linux x86_64 and systemd, and is also designed to work with Debian 12. Other Linux distributions, ARM systems, non-systemd environments, and organization-level runners are not primary targets of the current version.
