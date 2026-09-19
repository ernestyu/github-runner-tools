# Universal Self-Hosted Runner Bootstrap Improvement Spec

**Date:** 2026-09-19 08:29 (+01:00)  
**Repository:** `ernestyu/github-runner-tools`  
**Status:** Draft specification for implementation

## 1. Purpose

The current `register-runner.sh` works well for the original environment, where the Linux user is `actions`, the repository is cloned under that user's home directory, the host is Debian on x86_64, and runners are installed next to the `github-runner-tools` checkout.

That model is too specific for a public tool.

The next version should make runner registration work reliably for users with different Linux usernames, different home directories, different installation locations, and supported CPU architectures. It should also support a one-command bootstrap flow such as:

```bash
curl -fsSL https://raw.githubusercontent.com/OWNER/github-runner-tools/VERSION/scripts/register-runner.sh \
  | bash -s -- OWNER/REPO
```

The goal is not to create another CI platform. The project should remain a small provisioning and management layer around the official GitHub Actions self-hosted runner.

The guiding rule is:

> The script must derive the local execution environment from the machine where it runs instead of assuming the author's directory layout or username.

## 2. User experience and defaults

The common case should require only a repository identifier and a GitHub registration token.

The recommended interactive flow is:

```bash
curl -fsSL https://raw.githubusercontent.com/OWNER/github-runner-tools/VERSION/scripts/register-runner.sh \
  | bash -s -- OWNER/REPO
```

The script then asks:

```text
Paste GitHub registration token:
```

The token must be read silently and must not be passed as a command-line argument by default. Passing the token directly in the shell command would leave it more exposed to shell history, terminal logs, and potentially process inspection.

The default runner user should be the ordinary user executing the script:

```bash
RUNNER_USER="$(id -un)"
```

The default runner base directory should be that user's real home directory rather than a path inferred from the location of `github-runner-tools`.

For example:

```text
user: ubuntu
home: /home/ubuntu
repository: example/project-a

default runner directory:
/home/ubuntu/actions-runner-project-a
```

The same script should work without changes for users such as `actions`, `ubuntu`, `debian`, `ci`, `github`, or any other normal Linux account.

No username should be hard-coded in runner registration or systemd service installation.

The systemd service should therefore be installed with the detected or explicitly configured user:

```bash
sudo ./svc.sh install "$RUNNER_USER"
```

### Explicit overrides

Advanced users should still be able to override the defaults.

Supported environment variables should include:

```text
RUNNER_USER
RUNNER_BASE_DIR
RUNNER_NAME
RUNNER_LABELS
```

For example:

```bash
RUNNER_BASE_DIR=/srv/github-runners \
RUNNER_NAME=build-node-01 \
RUNNER_LABELS=self-ci,docker,project-a \
  bash register-runner.sh example/project-a
```

If `RUNNER_USER` is explicitly supplied, the script must verify that the user exists before continuing.

## 3. Environment discovery and validation

The script should fail early with clear messages rather than reaching GitHub's `config.sh` or `svc.sh` and failing with a less useful error.

### 3.1 Root execution

Direct root execution should be rejected.

```bash
if [[ "$EUID" -eq 0 ]]; then
    die "Do not run this script as root. Run it as the user that should own the runner."
fi
```

Using `sudo` for specific privileged operations is expected, but the runner process itself should belong to an ordinary user.

### 3.2 Reliable home-directory discovery

The script should not rely only on `$HOME`, because unusual sudo, shell, or service environments can carry an unexpected value.

Preferred logic:

1. Determine the user with `id -un`, unless `RUNNER_USER` is explicitly provided.
2. Resolve the user's home directory from the account database, for example with `getent passwd`.
3. Fall back to `$HOME` only if necessary.
4. Verify that the final base directory exists or can be created and is writable by the runner user.

Example:

```bash
USER_HOME="$(getent passwd "$RUNNER_USER" | cut -d: -f6)"
```

If `getent` is unavailable, the script may use a documented fallback.

### 3.3 Required commands

The script should verify all commands it actually needs before doing any download or partial installation.

Likely required commands include:

```text
bash
curl
tar
jq
sha256sum
sudo
uname
id
sed
tr
awk
find
mktemp
```

If the implementation uses `getent`, it should also be checked.

Docker must not be a hard requirement for runner registration. Some repositories do not use Docker at all.

Docker checks belong in diagnostics or documentation, not in the core bootstrap requirement.

### 3.4 sudo

The script should validate sudo access near the start:

```bash
sudo -v
```

If the current user cannot use sudo, registration should stop with a direct explanation that system dependency installation and systemd service setup require elevated privileges.

### 3.5 systemd

The current project targets systemd-based Linux hosts. The script should make that assumption explicit and verify it before registration.

At minimum, check that `systemctl` exists and that the system is actually running systemd.

If not, stop with a message such as:

```text
This version currently requires a systemd-based Linux host.
```

Do not let the failure occur much later inside `svc.sh install`.

### 3.6 CPU architecture

The current script assumes Linux x64. A public version should detect architecture with:

```bash
uname -m
```

Initial supported mappings should include:

```text
x86_64  -> x64
amd64   -> x64
aarch64 -> arm64
arm64   -> arm64
```

Unknown architectures should fail explicitly rather than downloading an incompatible runner.

The selected architecture must be used when locating the GitHub Actions Runner release asset.

## 4. Installation path and curl-pipe compatibility

The current implementation derives `TOOL_ROOT` from `BASH_SOURCE[0]` and then installs runners in the parent directory of the repository checkout.

That behavior is convenient when the project is cloned, but it breaks the one-line `curl | bash` model because the script may be executing from stdin and does not have a meaningful repository path.

The revised rule should be:

> Runner installation must not depend on the physical location of the management script.

Default:

```text
RUNNER_BASE_DIR = the runner user's home directory
```

Therefore:

```bash
curl ... | bash -s -- example/project-a
```

would create:

```text
/home/<current-user>/actions-runner-project-a
```

A cloned `github-runner-tools` repository should behave the same way unless `RUNNER_BASE_DIR` is explicitly set.

This change makes the execution method irrelevant: cloned repository, direct script path, curl to file, and `curl | bash` should all produce the same default runner location.

## 5. Safe handling of existing and failed installations

A public bootstrap script must distinguish between a configured runner and a failed or incomplete previous attempt.

### State A: directory does not exist

Create it and continue.

### State B: directory exists and is empty

Reuse it and continue.

### State C: directory contains `.runner`

Treat it as an already configured GitHub runner.

Default behavior should be to stop and report:

```text
A configured runner already exists at ...
```

The script must not delete or overwrite it automatically.

### State D: directory is non-empty but has no `.runner`

This often means a previous attempt downloaded and extracted the runner but failed before registration completed, for example because the GitHub registration token was invalid or expired.

The script should identify this state clearly.

A safe first version may stop with:

```text
An unconfigured runner directory already exists.
It may be left from a failed registration attempt.
Use --clean-incomplete to remove it, or inspect it manually.
```

A later implementation may support `--clean-incomplete`, but only when `.runner` is absent.

It must never automatically delete an existing directory merely because registration failed.

### Cleanup after a failure created by the current invocation

If the current invocation created a new runner directory and registration later fails before `.runner` exists, the script may offer to remove only the files created by that invocation.

Automatic cleanup is acceptable only if the script can prove that it created the directory during the current run and that no successful runner configuration exists.

This avoids the failure pattern already observed in practice: a bad token leaves an extracted runner directory, and the next invocation fails because the directory is non-empty.

## 6. Repository names, labels, and sanitization

Repository input should remain:

```text
OWNER/REPO
```

The script must validate this input before constructing URLs or paths.

The repository name may contain upper-case letters, dots, underscores, or hyphens. A safe local identifier should be derived before using it in directory names, runner names, custom labels, or service-related output.

For example:

```text
My_Project -> my_project
foo.bar    -> foo.bar
my-repo    -> my-repo
```

The transformation should be deterministic and documented.

The script should not silently produce an empty identifier after sanitization.

Default conventions may remain:

```text
runner directory: actions-runner-<safe-repo-name>
runner name:      unraid-ci-<safe-repo-name>
labels:           unraid-ci,<safe-repo-name>
```

However, the implementation should not imply that Unraid is required. The default `unraid-ci` label is historical and should be reconsidered before a stable public release. A more generic label such as `self-ci` or `local-ci` may be preferable.

This naming decision should be made before the first stable release because changing default labels later can break existing workflows.

## 7. Download and integrity behavior

The script should continue to obtain the GitHub Actions Runner from the official `actions/runner` release.

It should:

1. Fetch the latest release metadata, unless a version is explicitly pinned.
2. Select the correct Linux asset for the detected CPU architecture.
3. Download from the official GitHub release URL.
4. Verify SHA-256 when reliable digest metadata is available.
5. Fail on HTTP errors.
6. Use retry behavior for transient network failures.

A future option should allow an explicit runner version through `RUNNER_VERSION`. This would support reproducible installations and avoid unexpected behavior if a newly released GitHub Runner has a regression.

## 8. One-line installation and release pinning

For convenience, the README may show a one-line installation command.

For personal use or testing:

```bash
curl -fsSL https://raw.githubusercontent.com/OWNER/github-runner-tools/main/scripts/register-runner.sh \
  | bash -s -- OWNER/REPO
```

For public documentation, the recommended form should eventually pin a release tag:

```bash
curl -fsSL https://raw.githubusercontent.com/OWNER/github-runner-tools/v1.0.0/scripts/register-runner.sh \
  | bash -s -- OWNER/REPO
```

The reason is simple: `curl | bash` executes remote code immediately. Pinning a release provides a reproducible script instead of silently executing whatever happens to be on `main` that day.

The README should also offer a review-first alternative:

```bash
curl -fsSL https://raw.githubusercontent.com/OWNER/github-runner-tools/v1.0.0/scripts/register-runner.sh \
  -o register-runner.sh

less register-runner.sh
bash register-runner.sh OWNER/REPO
```

Both modes should use the same script and the same defaults.

## 9. Security requirements

Registration tokens must not be stored in the repository, written to a configuration file, printed back to the terminal, passed as the recommended command-line argument, or included in shell examples with real values.

They should be read interactively with silent input.

The script must unset the token variable after registration.

The runner should be treated as a code-execution environment. The project documentation should continue to advise users not to expose production secrets, host Docker sockets, unrelated data directories, or other high-privilege resources to runner jobs unless explicitly required.

The project must not suggest that registering a runner is equivalent to granting ChatGPT direct SSH access or deployment access. Runner provisioning, CI validation, and production deployment are separate concerns.

## 10. Scope and non-goals

The first improved public version should support:

```text
Linux
Debian 12 / 13 as the primary tested distributions
systemd
repository-level self-hosted runners
x86_64
arm64, if validated during implementation
interactive GitHub registration token input
multiple independent runners on one host
clone-based use
curl-based use
```

The first version does not need to solve:

```text
organization-level runner groups
autoscaling runner fleets
ephemeral runners
Kubernetes runners
containerized runner daemons
Windows
macOS
automatic GitHub token generation through PAT/OAuth
automatic workflow modification
production deployment
arbitrary non-systemd service managers
```

Keeping these out of scope is intentional. The value of this project is its small size and predictable behavior.

## 11. CLI behavior

The initial CLI should remain simple:

```bash
register-runner.sh OWNER/REPO
```

Useful future flags may include:

```text
--base-dir PATH
--runner-name NAME
--labels LABEL1,LABEL2
--runner-user USER
--version VERSION
--clean-incomplete
--non-interactive
--help
```

Environment variables may remain supported, but explicit flags are easier to document for public use.

If both a flag and an environment variable are supplied, the precedence should be documented. A reasonable rule is:

```text
CLI flag
> environment variable
> detected/default value
```

The token should remain interactive by default. A non-interactive token mechanism, if ever added for automation, should use stdin or a protected environment variable and should be clearly marked as an advanced use case.

## 12. Diagnostics and output

The script should print a short resolved configuration before making changes, without printing secrets.

For example:

```text
Repository   : example/project-a
Runner user  : ubuntu
Home         : /home/ubuntu
Base dir     : /home/ubuntu
Runner dir   : /home/ubuntu/actions-runner-project-a
Runner name  : local-ci-project-a
Labels       : local-ci,project-a
Architecture : x64
```

After successful registration, it should print the systemd service status, GitHub runner settings URL, recommended `runs-on` selector, and local runner directory.

Failure messages should explain the next action whenever possible. For example, an expired registration token should not leave the user wondering whether the installation succeeded.

## 13. README changes required after implementation

Once the script has been updated, both the English and Chinese README files should be revised.

The Quick Start should offer two paths.

### One-line registration

```bash
curl -fsSL <pinned-release-url>/register-runner.sh \
  | bash -s -- OWNER/REPO
```

### Full management checkout

```bash
git clone https://github.com/OWNER/github-runner-tools.git
cd github-runner-tools
bash scripts/register-runner.sh OWNER/REPO
bash scripts/status-runners.sh
bash scripts/remove-runner.sh OWNER/REPO
```

The README should avoid examples tied to a specific Linux username.

Examples should use generic paths such as:

```text
/home/<user>/actions-runner-project-a
```

or explain that the actual path is derived from the current user's home directory.

The README should also explain the security tradeoff of `curl | bash` and recommend release-pinned URLs for public use.

## 14. Acceptance criteria

The improvement is complete when the following cases pass:

1. A Debian 13 x86_64 user named `actions` can register a repository runner.
2. A Debian user with a different username, such as `ubuntu`, can run the same command without editing the script.
3. The runner installs under that user's home directory by default.
4. A cloned script and a `curl | bash` execution produce the same default runner location.
5. The script refuses to run directly as root.
6. The script detects missing sudo access before downloading the runner.
7. The script detects a non-systemd host and exits with a clear message.
8. The script detects x86_64 correctly.
9. The script selects arm64 correctly if arm64 support is included in this release.
10. A valid registration token completes registration and starts the systemd service.
11. An invalid or expired token does not create a false-success state.
12. A failed registration does not make the next attempt impossible without explanation.
13. An existing configured runner is never deleted or overwritten automatically.
14. A non-empty unconfigured directory is detected and reported separately.
15. Repository names with uppercase letters, underscores, dots, and hyphens produce safe deterministic local names.
16. Registration tokens are never echoed or stored.
17. Docker is not required unless the user's own CI workflow needs Docker.
18. `RUNNER_BASE_DIR`, `RUNNER_NAME`, and `RUNNER_LABELS` overrides continue to work.
19. The final output gives the correct GitHub settings URL and recommended `runs-on` selector.
20. English and Chinese documentation describe the actual behavior of the implemented script.

## 15. Implementation order

The recommended implementation sequence is:

1. Remove dependence on the repository checkout path.
2. Detect runner user and home directory safely.
3. Replace hard-coded user assumptions in systemd installation.
4. Add root, sudo, systemd, and required-command preflight checks.
5. Add architecture detection and release-asset selection.
6. Refactor directory-state handling for configured versus incomplete installs.
7. Preserve silent interactive token input.
8. Make the same script work from a clone and from stdin.
9. Add or revise tests for path, user, architecture, and incomplete-install logic.
10. Test manually on the existing Debian host.
11. Test with a second non-`actions` username if possible.
12. Update `README.md` and `README.zh-CN.md`.
13. Tag the first version intended for pinned `curl` installation.

The priority should remain reliability over convenience. A one-line installer is useful only if it is predictable, conservative with existing files, and clear when it cannot continue.
