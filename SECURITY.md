# Security policy

This policy covers the `llm-tools` Python package (including `llm-bridge` and its worker), the
Bridge coordinator (`coordinator/`), its web console (`web/`) and the container image.

## Reporting a vulnerability

Report vulnerabilities privately through GitHub:
<https://github.com/chrisgleissner/llm-tools/security/advisories/new>
(the repository's **Security** tab, then **Report a vulnerability**).

Do not open a public issue, pull request or discussion for a suspected vulnerability.

Please include:

- the affected component and version (`llm-bridge --version`, or the coordinator image tag or
  digest);
- the steps to reproduce, and what an attacker gains;
- whether the issue is already public.

Remove tokens, passwords, prompts and private addresses from any log you attach. Do not attach
database files or passkey material.

## What happens next

| Step | Target time |
|---|---|
| Acknowledge the report | 3 working days |
| Confirm or reject it, with a severity (CVSS 3.1) | 10 working days |
| Release a fix for critical and high severity | 30 days from confirmation |
| Release a fix for medium and low severity | the next scheduled release, at most 90 days |

The maintainer works on a fix in a private security advisory and credits the reporter unless they
ask not to be named. The fixed release and the advisory are published together. A CVE is requested
through GitHub for every confirmed vulnerability of medium severity or higher.

## Supported versions

Security fixes are made for the latest released minor version. The worker protocol and REST API
are versioned (`v1`), so a coordinator fix does not require a worker upgrade unless the advisory
says so.

| Version | Supported |
|---|---|
| latest minor release | yes |
| older releases | no; upgrade with the documented path in `docs/distributed/install.md` |

## Dependencies and advisories

- Dependabot opens weekly update pull requests for Cargo, npm, GitHub Actions and pip
  (`.github/dependabot.yml`).
- The `audit` workflow (`.github/workflows/audit.yml`) runs `cargo audit`, `npm audit` and
  `pip-audit` every week, on demand, and on every pull request that changes a lock file.
- Response policy for an advisory in a dependency: critical or high severity that affects a shipped
  component is fixed or mitigated within 7 days; medium within 30 days; low with the next routine
  update. An advisory that does not affect a shipped component (for example a test-only
  dependency) is recorded in the pull request that updates it.

## Repository settings (owner actions)

These settings are not stored in the repository and are set by the owner:

- **Private vulnerability reporting**: Settings, Code security, Private vulnerability reporting,
  Enable. The reporting link above depends on it.
- **Dependabot alerts and security updates**: Settings, Code security, enable both.

## Scope

The threat model is in `docs/distributed/threat-model.md`. Standalone `llm-bridge` opens no
network listener; the distributed mode's trust boundaries are listed there.
