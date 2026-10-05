# Contributing

Thanks for considering a contribution to `github-runner-tools`.

This project manages GitHub Actions self-hosted runners, systemd services, local archive paths, and optional remote artifact deletion. Changes that look small can affect host safety, runner identity, or stored CI results. Contributions should therefore preserve the project's conservative failure behavior.

## Before making a change

For bug fixes, documentation corrections, and small test improvements, a pull request is fine.

For larger behavior changes, new platform support, archive-layout changes, runner identity changes, or anything that changes deletion or replacement semantics, please open an issue first. This avoids spending time on a design that conflicts with the current safety model.

The current supported target is Debian 12/13 with systemd. The x86_64 path has been validated on a real host. ARM64 support exists but has not yet been validated on a real ARM64 host.

## Development rules

Please preserve these project invariants unless a reviewed design explicitly changes them:

- Do not silently replace an existing remote runner.
- Do not infer legacy runner identity from a directory name alone.
- Do not delete a configured runner when repository identity is ambiguous.
- Do not continue local deletion after a failed systemd service uninstall.
- Do not overwrite an existing finalized archive when execution identity is uncertain.
- Do not follow symlinks outside the archived workspace.
- Do not delete a GitHub Actions artifact unless a verified local copy exists and remote deletion was explicitly requested.
- Treat archive failure as fail-closed.
- Keep runner registration under one normal Linux user; cross-user provisioning is outside the current scope.
- Never commit registration tokens, removal tokens, PATs, SSH private keys, API keys, or other secrets.

Prefer small, reviewable changes over broad refactors.

## Testing

Run the full test suite before submitting a pull request:

```bash
bash tests/run-all.sh
```

The suite includes shell syntax checks, runner-management tests, archive tests, integration tests, and failure-path tests.

If your change affects behavior that depends on a real host, state clearly whether you also tested it on:

- Debian 12 or 13
- x86_64 or ARM64
- a real systemd service
- a real GitHub repository runner
- a live GitHub Actions workflow

Do not describe a platform or path as validated unless it was actually exercised.

## Pull requests

A useful pull request should explain:

1. What changed.
2. Why the change is needed.
3. What tests were run.
4. Which host/platform, if any, was used for live validation.
5. Whether runner identity, service management, archive publication, cleanup, or deletion behavior changed.

Please keep unrelated cleanup out of the same pull request when possible.

## Documentation

User-facing behavior changes should update the relevant README or other documentation in the same pull request.

The English README is the main project page. Keep the Chinese README aligned when changing setup, safety, or operational behavior.

## Security issues

Do not report security-sensitive problems in a public issue. See [SECURITY.md](SECURITY.md) for the reporting path.
