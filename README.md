# github-runner-tools

Manage GitHub Actions self-hosted Runners on Debian through one shared lifecycle authority.

The installed product provides two interfaces:

- **CLI** for interactive Create, Status, Remove, and bounded recovery.
- **Web** for authenticated Create, Status, and Remove through the bounded root Executor.

Both interfaces use the same installed CLI, lifecycle state, mutation lock, canonical systemd Unit rules, and structured operational log.

## Fresh Debian installation

Supported release installation is one command, run as the intended **non-root Runner owner**:

```bash
curl -fsSL https://github.com/ernestyu/github-runner-tools/releases/latest/download/install.sh | bash
```

Do **not** use `sudo bash` or `curl ... | sudo bash`. The installer invokes `sudo` itself only for bounded host-level changes.

A normal installation does not require `git clone`. It installs the verified release payload under `/usr/local/lib/github-runner-tools`, initializes lifecycle/log/archive state, installs and starts the Executor and Web services, and prompts for the Web administrator password.

The installer selects a concrete RFC1918 IPv4 address and prints the Web URL:

```text
http://192.168.x.x:8765/
```

The selected address is persisted. The Web service never silently switches to another interface after reboot or a DHCP/VPN change. If that address disappears, reconfigure `WEB_BIND_ADDRESS` explicitly.

### LAN HTTP security boundary

The default Web endpoint is plain HTTP for a **trusted home/lab/management LAN**. Administrator passwords and temporary GitHub registration/removal tokens are not protected by TLS on the network.

Do not expose the default endpoint to the public Internet, public Wi-Fi, or an untrusted shared LAN.

Application controls remain active:

- administrator password authentication;
- server-side sessions;
- `HttpOnly; SameSite=Strict` session cookie;
- login rate limiting;
- CSRF protection;
- one-time confirmation nonces;
- bounded request sizes;
- token redaction and no token persistence.

Tailscale, a reverse proxy, or TLS can be added separately. None is required for installation or normal LAN use.

## CLI lifecycle

Installed commands are under:

```text
/usr/local/lib/github-runner-tools/cli/
```

Create:

```bash
/usr/local/lib/github-runner-tools/cli/register-runner.sh OWNER/REPO
```

Status:

```bash
/usr/local/lib/github-runner-tools/cli/status-runners.sh
```

Remove:

```bash
/usr/local/lib/github-runner-tools/cli/remove-runner.sh OWNER/REPO
```

Temporary GitHub registration/removal tokens are entered interactively and are not stored in lifecycle state or operational logs.

## Web lifecycle

After installation, open the LAN URL printed by the installer and sign in with the administrator password chosen during setup.

The Web frontend never executes lifecycle logic itself. It sends bounded requests to:

```text
github-runner-tools-web
        ↓
github-runner-tools-executor
        ↓
installed CLI + lifecycle authority
```

There is no alternative privileged Web path.

## State and logs

Lifecycle authority state:

```text
/var/lib/github-runner-tools
```

Operational JSONL:

```text
/var/log/github-runner-tools/operational.jsonl
```

Shared mutation lock:

```text
/run/lock/github-runner-tools/mutation.lock
```

The installer also records its own release-bound progress in:

```text
/var/lib/github-runner-tools/install-state.json
```

A failed fresh installation of the same verified release can be rerun only when the recorded partial installation and every managed object still verify exactly. Unknown or conflicting state fails closed.

## Development

Git checkout is for development, testing, and contribution only.

Run the fast checks:

```bash
bash tests/run-all.sh
```

Release packaging:

```bash
bash scripts/build-release.sh vX.Y.Z
```

The generated release contains a minimal runtime payload and a bootstrap with an embedded immutable version, payload URL, and SHA-256.
