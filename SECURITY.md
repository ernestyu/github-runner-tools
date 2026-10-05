# Security Policy

## Supported versions

Until the project publishes versioned releases, security fixes are made on the current `main` branch.

After tagged releases are established, this section will be updated with an explicit support window.

## Reporting a vulnerability

Please do **not** open a public GitHub issue for a vulnerability that could expose credentials, execute unintended code, delete runner data, bypass repository identity checks, or remove artifacts incorrectly.

Preferred reporting path:

1. Use GitHub's private vulnerability reporting / Security Advisory flow for this repository if it is available.
2. If private vulnerability reporting is not available, use a private contact method listed on the maintainer's GitHub profile.
3. If no private channel is available, open a minimal public issue asking for a private security contact **without including exploit details, secrets, tokens, private repository names, or sensitive logs**.

Please include, when possible:

- affected commit or version;
- operating system and architecture;
- relevant script or component;
- impact;
- minimal reproduction steps;
- whether credentials or stored artifacts may have been exposed;
- any mitigation already tested.

## Sensitive data

Before sharing logs, remove:

- GitHub registration or removal tokens;
- personal access tokens;
- SSH private keys;
- API keys;
- passwords;
- private repository URLs or names when sensitive;
- environment variables containing secrets;
- internal hostnames or network details that are not needed to reproduce the issue.

## Security model

A self-hosted runner executes commands from GitHub Actions workflows on your own machine. This project does not turn untrusted workflow code into trusted code.

Users should isolate runner hosts from production systems where practical and avoid exposing unrelated production credentials, host Docker sockets, or sensitive filesystem paths to CI.

The project intentionally favors conservative behavior:

- ambiguous identity stops the operation;
- existing runners are not silently replaced;
- archive failures fail closed;
- cleanup avoids uncertain or incomplete archive states;
- remote artifact deletion requires explicit opt-in and a verified local copy.

A report that shows one of these guarantees can be bypassed is considered security-relevant.
