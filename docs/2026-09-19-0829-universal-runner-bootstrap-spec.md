# Universal Self-Hosted Runner Bootstrap Improvement Spec

**Date:** 2026-09-19 08:29 (+01:00)  
**Revised:** 2026-09-19  
**Repository:** `ernestyu/github-runner-tools`  
**Status:** Implementation-readiness revision

## 1. Purpose

The current `register-runner.sh` works for the original environment, but it still contains assumptions that are too specific for a public bootstrap tool:

- the script is expected to run from a cloned `github-runner-tools` checkout;
- the install path is derived from `BASH_SOURCE`;
- the runner asset is fixed to Linux x64;
- the systemd service user is taken from shell state rather than a fully defined execution-user model;
- local runner directories are derived from the repository name only;
- failed registration can leave a non-empty directory that blocks the next attempt;
- the current interactive token read is not compatible with `curl | bash`.

The next version should make repository-level runner registration reliable for ordinary Linux users with different usernames, home directories, installation locations, and supported CPU architectures.

It should support both a cloned-tool workflow and a one-command bootstrap workflow such as:

```bash
curl -fsSL https://raw.githubusercontent.com/OWNER/github-runner-tools/VERSION/scripts/register-runner.sh \
  | bash -s -- OWNER/REPO
```

The project remains a small provisioning and management layer around the official GitHub Actions self-hosted runner. It is not a CI platform, deployment system, runner fleet manager, or remote-shell mechanism.

The guiding rule is:

> The bootstrap must derive its environment from the machine and user that execute it, must not depend on the location of the management repository, and must fail conservatively when ownership or installation state is ambiguous.

## 2. Execution identity and defaults

### 2.1 Runner user

For v1, the runner user is always the current non-root user executing the bootstrap:

```bash
RUNNER_USER="$(id -un)"
```

Cross-user provisioning is explicitly out of scope.

The bootstrap must not support a mode where one user runs the script but the runner files, GitHub configuration, or systemd service are intended to belong to another user.

For example, this model is not supported in v1:

```text
script execution user: ernest
target runner user:    actions
```

Supporting that correctly would require a separate ownership model covering `sudo -u`, target-user HOME discovery, file ownership, `config.sh` execution identity, environment propagation, and service permissions.

Therefore, v1 should not expose `RUNNER_USER` as a general-purpose override. If an implementation keeps the variable for internal consistency or future compatibility, any externally supplied value must equal `id -un`; otherwise the script must fail.

The systemd service must be installed for the same user that ran `config.sh` and owns the runner directory:

```bash
sudo ./svc.sh install "$RUNNER_USER"
```

### 2.2 Home directory

The default runner base directory is the current runner user's real home directory.

The script should not rely only on `$HOME`. It should resolve the account home from the system user database when possible, for example:

```bash
USER_HOME="$(getent passwd "$RUNNER_USER" | cut -d: -f6)"
```

A documented fallback to `$HOME` is acceptable if the account database lookup is unavailable.

The resolved home must be non-empty, absolute, and traversable/readable/writable by the runner user.

### 2.3 Default install location

Default:

```text
RUNNER_BASE_DIR = current runner user's home directory
```

The default install path must not depend on where `github-runner-tools` is cloned or where the shell command is invoked.

A cloned script, a downloaded standalone script, and `curl | bash` must all produce the same default install location for the same user and repository.

## 3. Repository identity and local naming

Input remains:

```text
OWNER/REPO
```

The bootstrap must validate the repository identifier before constructing URLs, paths, labels, or names.

### 3.1 Local identity must include owner and repository

Repository name alone is not sufficient because these two repositories must not collide:

```text
example/project-a
another/project-a
```

New registrations must derive their local runner identity from both OWNER and REPO.

A recommended convention is:

```text
safe owner: lowercase sanitized OWNER
safe repo:  lowercase sanitized REPO

local identity:
<safe-owner>--<safe-repo>
```

Example:

```text
ExampleOrg/Project-A
→ exampleorg--project-a
```

The separator is fixed for v1 as a double hyphen:

```text
<safe-owner>--<safe-repo>
```

To keep directory names, runner names, and generated systemd service names bounded, v1 defines:

```text
MAX_LOCAL_ID_LENGTH=64
HASH_LENGTH=8
```

The identity algorithm is deterministic:

1. Normalize OWNER and REPO to lowercase for the hash input.
2. Sanitize OWNER and REPO independently for filesystem/name use.
3. Build `<safe-owner>--<safe-repo>`.
4. If the result is at most 64 characters, use it directly.
5. If it exceeds 64 characters, append an 8-character lowercase hexadecimal SHA-256 prefix and truncate the owner/repo components so the final identity remains at most 64 characters.

The long-name form is:

```text
<truncated-safe-owner>--<truncated-safe-repo>--<8-char-hash>
```

The hash input is the normalized lowercase textual repository identity:

```text
<lowercase-owner>/<lowercase-repo>
```

The truncation algorithm must preserve at least one character from both the owner and repository components. It should divide the available non-hash space as evenly as practical, giving unused space from a short component to the longer component.

The same OWNER/REPO input must always produce the same local identity. Different owners with the same repository name must produce different identities.

### 3.2 New default paths and names

New registrations should use the full local identity:

```text
runner directory:
actions-runner-<local-identity>

runner name:
local-ci-<local-identity>
```

Repository-specific workflow labels do not need to include the owner because a repository-level runner cannot be consumed by unrelated repositories. A default repository label may therefore remain based on the safe repository name.

The historical `unraid-ci` label should not remain the public default because Unraid is not a requirement of this project. A generic shared label such as `local-ci` is preferred for new installations.

This naming decision should be finalized before the first stable public release.

### 3.3 Legacy naming compatibility

Existing configured runners that use the old repo-only naming scheme must not be renamed, moved, overwritten, or deleted automatically.

Legacy examples include:

```text
actions-runner-project-a
unraid-ci-project-a
```

New registrations use the new owner+repo identity.

Management scripts such as `remove-runner.sh` must prefer the new owner+repo path.

A legacy repo-only path must never be accepted solely because its directory name matches the requested repository name. The old naming scheme discarded owner information, so a directory such as:

```text
actions-runner-project-a
```

could represent either:

```text
example/project-a
another/project-a
```

Before a management command treats a legacy path as belonging to the requested `OWNER/REPO`, it must inspect the configured runner metadata, normally `.runner`, and verify that the configured GitHub repository URL matches the requested repository after canonical normalization.

Expected behavior:

```text
new path exists
→ use the new path

new path absent
+ legacy path exists
+ metadata matches requested OWNER/REPO
→ legacy path may be used

new path absent
+ legacy path exists
+ metadata points to another OWNER/REPO
→ reject

new path absent
+ legacy path exists
+ metadata is missing or cannot establish repository identity
→ ambiguous; stop and require explicit user action
```

Metadata field names may vary by runner version. The implementation may inspect fields such as `.gitHubUrl` or `.serverUrl`, but it must validate the resulting URL semantically rather than trusting the directory name.

If both a new-path runner and a verified legacy-path runner are present for the same requested repository, the tool must stop and require explicit user choice. It must not guess.

Automatic migration or renaming of legacy runner directories remains out of scope for v1.

### 3.4 Remote runner-name collisions

The current implementation passes `--replace` to GitHub's `config.sh`. That behavior is not acceptable as the public default.

For v1, registration must not pass `--replace` by default.

If GitHub already has a runner with the same runner name, registration should fail and leave the existing remote runner untouched rather than silently replacing it.

A future explicit recovery or replacement option may be added, but it must be opt-in and clearly named.

This rule keeps remote behavior consistent with the local conservative policy: the bootstrap must not silently replace an existing runner merely because the local directory is absent.

## 4. Preflight validation

The script should fail before downloads or filesystem changes whenever the environment is known to be unsupported.

### 4.1 Root execution

Direct root execution is rejected.

```bash
if [[ "$EUID" -eq 0 ]]; then
    die "Do not run this script as root. Run it as the user that should own the runner."
fi
```

Privileged commands through `sudo` are expected, but the runner itself belongs to the ordinary execution user.

### 4.2 Required commands

The script must verify every command it actually uses before starting the installation.

The expected set includes at least:

```text
bash
curl
tar
jq
sha256sum
sudo
uname
id
ps
sed
tr
awk
find
mktemp
```

If the implementation uses `getent`, it must also check for it or provide a defined fallback.

Docker is not a bootstrap requirement.

### 4.3 sudo

Validate sudo near the start:

```bash
sudo -v
```

If sudo is unavailable or the current user lacks permission, stop before downloading or creating the runner directory.

### 4.4 systemd

The current public target is a systemd-based Linux host.

The preflight must verify both:

- `systemctl` exists; and
- PID 1 is systemd, or an equivalent reliable check proves that systemd is the active service manager.

A suitable implementation is conceptually:

```bash
PID1="$(ps -p 1 -o comm= | xargs)"
[[ "$PID1" == "systemd" ]] || die "This version requires a systemd-based Linux host."
```

The script must not use `systemctl is-system-running` as a simple success/failure gate because a usable machine may legitimately report `degraded`.

### 4.5 CPU architecture

Detect architecture with `uname -m`.

Supported mappings for the initial implementation:

```text
x86_64  -> x64
amd64   -> x64
aarch64 -> arm64
arm64   -> arm64
```

Unknown architectures fail before download.

The detected architecture must be used to choose the official GitHub Actions Runner release asset.

ARM64 should only be documented as supported after a real validation run or equivalent CI coverage.

## 5. Base-directory policy

The default base directory is the current user's home and may be created/used normally.

For an explicitly supplied `RUNNER_BASE_DIR`, v1 must be conservative.

The bootstrap must not automatically run `sudo mkdir`, `sudo chown`, or otherwise change ownership of an arbitrary user-selected path such as `/srv/github-runners`.

A custom base directory must already exist and must be traversable, readable, and writable by the current runner user.

If not, stop with a clear message and, where useful, show an example of how the user may prepare the directory manually.

For example:

```bash
sudo mkdir -p /srv/github-runners
sudo chown "$(id -un):$(id -gn)" /srv/github-runners
```

The bootstrap itself must not execute that ownership change automatically.

## 6. curl-pipe compatibility and interactive token input

This requirement is fundamental to the one-line mode.

When the script is executed as:

```bash
curl -fsSL .../register-runner.sh | bash -s -- OWNER/REPO
```

Bash consumes standard input as script source. Therefore, the registration token must not be read from ordinary stdin.

Interactive token input must come from the controlling terminal.

Normative behavior:

```bash
if [[ -r /dev/tty && -w /dev/tty ]]; then
    read -r -s -p "Paste GitHub registration token: " TOKEN < /dev/tty
    printf '\n' > /dev/tty
else
    die "Interactive token input requires a TTY."
fi
```

Using a dedicated file descriptor opened from `/dev/tty` is also acceptable.

The script must perform the TTY availability check before downloading the GitHub runner or making persistent installation changes.

If no controlling TTY is available, the default interactive mode must fail clearly.

A future explicit non-interactive mode may accept a token through a protected environment variable or another defined secret channel, but this is not required for v1 and must not weaken the default behavior.

The recommended public usage must not place the token directly on the command line.

## 7. Safe handling of existing and failed installations

The bootstrap must distinguish four directory states.

### State A: directory does not exist

Create it and continue.

### State B: directory exists and is empty

Reuse it and continue.

### State C: directory contains `.runner`

Treat it as an already configured runner and stop.

Do not overwrite or delete it automatically.

### State D: directory is non-empty but has no `.runner`

Treat this as an incomplete or ambiguous installation.

The default behavior is to stop with a clear explanation.

A future or v1 optional `--clean-incomplete` behavior is acceptable only when `.runner` is absent and the target path has passed all safety checks.

### Cleanup for files created by the current invocation

The script may automatically clean up an incomplete directory only when it can prove all of the following:

- the directory did not exist before the current invocation;
- the current invocation created it;
- registration did not complete;
- no `.runner` file exists.

It must never automatically remove a pre-existing non-empty directory.

This requirement addresses the real failure mode where an invalid or expired token leaves an extracted runner directory that blocks the next attempt.

## 8. Download behavior and version model

The bootstrap script version and the GitHub Actions Runner binary version are separate concepts.

### 8.1 Bootstrap version

A URL such as:

```text
github-runner-tools/v1.0.0/scripts/register-runner.sh
```

pins the provisioning logic only.

It does not by itself pin the GitHub Actions Runner binary.

### 8.2 Runner binary version

The runner binary version is controlled separately.

v1 should support:

```text
RUNNER_VERSION
```

or an equivalent explicit CLI option such as:

```text
--runner-version VERSION
```

If no runner version is supplied, the script may resolve `actions/runner/releases/latest`.

If a runner version is supplied, both of these external forms should be accepted:

```text
2.328.0
v2.328.0
```

The input must be normalized internally to:

```text
VERSION=2.328.0
TAG=v2.328.0
```

The normalized version must match a strict release-version pattern before it is used in any GitHub API request, asset name, or URL. User input must never be concatenated directly into a download URL without validation.

For a pinned runner version, the script should query the specific GitHub release/tag directly, for example the release identified by `v2.328.0`, rather than fetching `latest` and then trying to reconcile it with the requested version.

If the requested release or architecture-specific asset does not exist, fail clearly without falling back to `latest`.

Documentation must state:

> Pinning the github-runner-tools release makes the bootstrap logic reproducible. The installed GitHub Actions Runner is only pinned when RUNNER_VERSION (or its CLI equivalent) is also specified.

Avoid a generic `--version` flag for the runner binary because it is ambiguous with the bootstrap tool's own version.

### 8.3 Integrity

The script should:

1. use official `actions/runner` release assets;
2. fail on HTTP errors;
3. retry transient download failures;
4. verify SHA-256 when reliable digest metadata is available;
5. never silently fall back to an asset for the wrong architecture.

## 9. Token lifetime and cleanup

Registration tokens must not be:

- committed to the repository;
- written to a persistent configuration file;
- echoed back to the terminal;
- recommended as command-line arguments;
- included with real values in documentation.

Token cleanup must be registered before or immediately after the token is read.

The script should use a unified EXIT cleanup handler, for example conceptually:

```bash
cleanup() {
    unset TOKEN
    # remove only temporary files owned by this invocation
}

trap cleanup EXIT
```

This should be combined with temporary-file cleanup rather than replaced by later `trap` calls.

The cleanup logic must not delete pre-existing runner directories or other files that the current invocation cannot prove it created.

If `config.sh` fails under `set -e`, the EXIT trap still runs.

## 10. Clone mode and one-line mode

Both execution methods are first-class and must use the same registration logic.

### Clone mode

```bash
git clone https://github.com/OWNER/github-runner-tools.git
cd github-runner-tools
bash scripts/register-runner.sh OWNER/REPO
```

### One-line mode

For testing or personal use:

```bash
curl -fsSL https://raw.githubusercontent.com/OWNER/github-runner-tools/main/scripts/register-runner.sh \
  | bash -s -- OWNER/REPO
```

For public documentation, prefer a pinned bootstrap release:

```bash
curl -fsSL https://raw.githubusercontent.com/OWNER/github-runner-tools/v1.0.0/scripts/register-runner.sh \
  | bash -s -- OWNER/REPO
```

A review-first alternative should also be documented:

```bash
curl -fsSL https://raw.githubusercontent.com/OWNER/github-runner-tools/v1.0.0/scripts/register-runner.sh \
  -o register-runner.sh

less register-runner.sh
bash register-runner.sh OWNER/REPO
```

All three paths must resolve the same default user, base directory, architecture, local identity, runner name, and labels.

## 11. CLI and configuration surface

The core command remains:

```bash
register-runner.sh OWNER/REPO
```

Supported configuration for the first improved release should include:

```text
RUNNER_BASE_DIR
RUNNER_NAME
RUNNER_LABELS
RUNNER_VERSION
```

Potential CLI flags:

```text
--base-dir PATH
--runner-name NAME
--labels LABEL1,LABEL2
--runner-version VERSION
--clean-incomplete
--help
```

Cross-user options such as `--runner-user` are out of scope for v1.

If both a CLI option and environment variable are supported for the same setting, precedence is:

```text
CLI option
> environment variable
> detected/default value
```

## 12. Diagnostics and output

Before persistent changes, print the resolved non-secret configuration.

Example:

```text
Repository   : example/project-a
Runner user  : ubuntu
Home         : /home/ubuntu
Base dir     : /home/ubuntu
Local ID     : example--project-a
Runner dir   : /home/ubuntu/actions-runner-example--project-a
Runner name  : local-ci-example--project-a
Labels       : local-ci,project-a
Architecture : x64
Runner ver.  : latest
```

Do not print the registration token.

After successful registration, print:

- runner directory;
- runner name;
- architecture;
- installed GitHub Actions Runner version;
- systemd service status;
- GitHub runner settings URL;
- recommended `runs-on` selector.

Failure messages should identify whether the failure occurred during preflight, download, registration, service installation, or startup and should provide the next safe action when possible.

## 13. Management-script compatibility

The bootstrap change affects management scripts and cannot be implemented in isolation.

At minimum, review and update:

```text
scripts/remove-runner.sh
scripts/status-runners.sh
```

Requirements:

- default base-directory resolution must match `register-runner.sh`;
- new owner+repo local identities must be recognized;
- legacy repo-only directories must remain manageable only after repository identity is verified from runner metadata;
- a legacy repo-only path must not be accepted solely because its directory name matches the requested repo name;
- no management script may silently delete a legacy runner because a new naming scheme exists;
- token input in removal flows should use `/dev/tty` when stdin may be occupied or redirected;
- token cleanup should follow the same EXIT-trap rule.

### 13.1 Safe service removal

`remove-runner.sh` must not silently ignore failure to uninstall the systemd service and then delete the runner directory.

Stopping the service may tolerate a narrowly defined "already stopped/not running" state, but service-uninstall failure is different: if the systemd service cannot be removed successfully, local runner-directory deletion must stop.

The safe default order is:

```text
identify and verify runner
→ stop service
→ uninstall service
→ remove GitHub runner registration
→ delete local runner directory
```

If service uninstall fails, the command must stop before local directory deletion and explain the recovery action.

A future explicit force/recovery mode may allow an administrator to continue after inspecting the failed service cleanup, but v1 must not hide the failure with `|| true` and proceed to `rm -rf`.

## 14. Security boundaries

The runner host is a code-execution environment.

Documentation should continue to advise users not to expose production secrets, host Docker sockets, unrelated data directories, or high-privilege resources to runner jobs unless explicitly required.

The project must not imply that registering a runner grants ChatGPT direct SSH access or deployment access.

Runner provisioning, CI validation, and production deployment are separate concerns.

The bootstrap must not:

- auto-change ownership of arbitrary custom directories;
- auto-delete ambiguous pre-existing runner directories;
- accept cross-user provisioning in v1;
- print or persist registration tokens;
- silently install an architecture different from the detected host;
- silently replace an existing remote GitHub runner with the same name;
- construct runner download URLs from unvalidated version input.

## 15. Scope and non-goals

The first improved public version should support:

```text
Linux
Debian 12 / 13 as primary tested distributions
systemd
repository-level self-hosted runners
x86_64
arm64 after validation
interactive token input from /dev/tty
multiple independent runners on one host
clone-based use
curl-based use
optional GitHub Runner version pinning
legacy repo-only runner detection for management
```

Out of scope for v1:

```text
cross-user runner provisioning
organization-level runner groups
autoscaling runner fleets
ephemeral runner orchestration
Kubernetes runners
containerized runner daemons
Windows
macOS
automatic token generation through PAT/OAuth
automatic workflow modification
production deployment
arbitrary non-systemd service managers
automatic migration/renaming of existing runner directories
```

## 16. README changes after implementation

Both `README.md` and `README.zh-CN.md` must be updated to reflect the implemented behavior.

The Quick Start should present:

1. a release-pinned one-line registration command;
2. a review-first download-and-run alternative;
3. the full cloned-tool workflow for registration, status, and removal.

Documentation must:

- avoid assuming the username `actions`;
- explain that default install location is the current user's home;
- explain owner+repo local naming;
- describe legacy naming behavior;
- explain the `curl | bash` security tradeoff;
- explain that token input comes from the terminal rather than stdin;
- distinguish bootstrap release pinning from GitHub Runner binary pinning;
- document `RUNNER_VERSION` or `--runner-version`;
- avoid claiming that pinning the bootstrap alone makes the entire installation reproducible.

## 17. Acceptance criteria

The improvement is implementation-ready only when the following behaviors are covered.

1. A Debian 13 x86_64 user named `actions` can register a repository runner.
2. A Debian user with a different username, such as `ubuntu`, can run the same command without modifying the script.
3. The runner user is always the current non-root execution user in v1.
4. Any attempted cross-user override is rejected before filesystem changes.
5. The runner installs under the execution user's real home directory by default.
6. A cloned script and `curl | bash` resolve the same default runner location.
7. `curl | bash` can successfully prompt for and read the registration token through `/dev/tty`.
8. Interactive mode without a controlling TTY fails before download or persistent installation changes.
9. The script refuses direct root execution.
10. Missing sudo access is detected before runner download.
11. A non-systemd host fails with a clear message.
12. A systemd host in `degraded` state is not rejected merely because it is degraded.
13. x86_64 maps to the correct official x64 runner asset.
14. arm64/aarch64 maps to the correct official arm64 asset when arm64 support is declared.
15. Unsupported architectures fail before download.
16. `example/project-a` and `another/project-a` produce different local runner directories.
17. New registrations use owner+repo-derived local identity.
18. Existing repo-only configured runners are not renamed, overwritten, or deleted automatically.
19. A legacy repo-only path is not accepted solely because its repository-name component matches the requested repository.
20. Management scripts only accept a legacy repo-only runner after metadata verifies the requested OWNER/REPO.
21. A legacy runner whose metadata points to another owner/repository is never removed for the requested repository.
22. A legacy runner with missing or unparseable repository identity is treated as ambiguous and is not removed automatically.
23. If both legacy and new runner paths exist for one repository request, management stops rather than guessing.
24. A valid token completes GitHub registration and starts the systemd service.
25. An invalid or expired token does not produce a false-success state.
26. An invalid token entered through `curl | bash` does not consume script stdin or corrupt script parsing.
27. A failed registration created by the current invocation can be cleaned safely without touching pre-existing directories.
28. A pre-existing non-empty directory without `.runner` is never deleted automatically.
29. A configured runner containing `.runner` is never overwritten automatically.
30. A custom `RUNNER_BASE_DIR` that is not accessible and writable by the current user is rejected.
31. The bootstrap does not run `sudo chown` or equivalent ownership changes on arbitrary custom base directories.
32. Repository and owner names are converted to deterministic safe local identifiers.
33. Registration tokens are not echoed, stored, or recommended as command-line arguments.
34. An EXIT cleanup trap removes temporary files and unsets the token on success and failure paths.
35. Docker is not required for runner bootstrap.
36. `RUNNER_BASE_DIR`, `RUNNER_NAME`, and `RUNNER_LABELS` continue to work.
37. `RUNNER_VERSION` or `--runner-version` accepts both `2.328.0` and `v2.328.0`, normalizes them to one internal VERSION/TAG representation, and rejects malformed version strings.
38. A pinned runner version queries and installs that exact official GitHub release rather than resolving `latest`.
39. A missing pinned release or missing architecture asset fails without falling back to `latest`.
40. An existing remote GitHub runner with the same runner name is not silently replaced; default registration does not use `--replace`.
41. A failed systemd service uninstall prevents local runner-directory deletion unless an explicit recovery/force path is used.
42. A pinned bootstrap URL with no runner-version override is documented as pinning bootstrap logic only, not the runner binary.
43. Final output reports the actual installed runner version, runner directory, settings URL, and recommended `runs-on` selector.
44. Local identities never exceed 64 characters; overlong OWNER/REPO values are truncated deterministically and include the defined 8-character hash suffix.
45. English and Chinese README files match the implemented behavior.

## 18. Recommended implementation order

1. Introduce shared deterministic OWNER/REPO parsing and the fixed 64-character local-identity/truncation/hash algorithm.
2. Change the default base directory to the current execution user's real home.
3. Remove clone-path dependence from registration.
4. Define current-user-only runner ownership and remove cross-user behavior.
5. Add root, sudo, systemd, TTY, base-directory, and required-command preflight checks.
6. Change interactive token reads to `/dev/tty` and add unified EXIT cleanup.
7. Add architecture detection.
8. Separate bootstrap version semantics from GitHub Runner binary version selection, normalize/validate runner-version input, and query pinned release tags directly.
9. Refactor directory-state handling for configured, empty, incomplete, and newly-created paths.
10. Adopt owner+repo naming for new registrations and remove default `--replace` behavior.
11. Add metadata-verified legacy repo-only discovery to removal/status management.
12. Make runner removal stop on systemd uninstall failure before local directory deletion.
13. Make clone mode, standalone-file mode, and stdin mode use the same registration logic.
14. Add tests for user/home detection, TTY behavior, naming collisions, remote-name collisions, legacy metadata verification, custom base-directory permissions, architecture selection, version pinning, service-uninstall failure, and incomplete-install handling.
15. Manually validate on the existing Debian host.
16. Validate with a second non-`actions` username if possible.
17. Validate ARM64 before documenting it as supported.
18. Update English and Chinese README files.
19. Tag the first release intended for pinned one-line installation.

The priority remains conservative behavior over convenience. A one-line bootstrap is useful only if its ownership model, token input, path selection, version semantics, and failure cleanup are all explicit and predictable.
