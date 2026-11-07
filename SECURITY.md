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

- Dependabot opens weekly version-update pull requests for Cargo, npm, GitHub Actions and pip
  (`.github/dependabot.yml`). Dependabot security updates open a pull request for a known
  vulnerable dependency outside that weekly schedule, when the owner enables them (see
  Repository settings).
- The `audit` workflow (`.github/workflows/audit.yml`) runs `cargo audit`, `npm audit
  --audit-level=moderate` and `pip-audit` every week, on demand, and on every pull request that
  changes a lock file or `pyproject.toml`. A low-severity npm advisory does not fail the npm job;
  it is still listed in the job log and handled in the routine update.
- The maintainer triages every advisory reported by the audit workflow or by a Dependabot alert.
  The maintainer decides whether the advisory affects a shipped component, which fix applies, and
  who merges it. Severity is the one the advisory source gives (RustSec, the npm advisory or the
  PyPI advisory). When a source gives none, the maintainer assigns one from the CVSS 3.1 base
  score.
- A dependency counts as shipped when it is part of the Python package, the coordinator binary,
  the web console or the container image, or when it runs in a release or deploy workflow with
  release credentials. Any other dependency is development-only. Development-only dependencies,
  for example test and lint tools, use the same triage but may take the routine update.

Response times for a dependency advisory. Triage is counted in working days from the report. Fix
and mitigation times are counted in calendar days from the triage decision:

| Severity | Shipped component | Development-only |
|---|---|---|
| Critical or high | triage in 3 working days; fix or mitigation within 7 days | triage in 3 working days; fix in the next routine update (a dependency that runs with release credentials counts as shipped) |
| Medium | triage in 10 working days; fix or mitigation within 30 days | triage in 10 working days; next routine update |
| Low | triage in 10 working days; next routine update | next routine update |

Development-only fixes are recorded in the pull request that updates the dependency.

When no fixed version exists, the maintainer tries these in order and records the choice in the
pull request:

1. Move to a patched version of a transitive dependency with a lock-file override (npm
   `overrides`, Cargo `[patch]` or a `cargo update -p`), when the patched release exists.
2. Apply a documented workaround that removes the vulnerable code path from the shipped
   component, for example by disabling the affected feature.
3. Accept the risk for a stated period. The exception names the advisory identifier, the reason,
   the affected path and a review date no more than 30 days away. The exception is removed when
   a fix is released, and the audit command ignores that identifier only for the period
   stated.

A failing audit run is an advisory to triage, not a background warning. The maintainer triages
the highest severity the run reports within the times above. A failure caused by the advisory
database or the network, not by an advisory, is rerun once; if it fails again, the maintainer
opens an issue.

## Repository settings (owner actions)

These settings are not stored in the repository and are set by the owner:

- **Private vulnerability reporting**: Settings, Code security, Private vulnerability reporting,
  Enable. The reporting link above depends on it.
- **Dependabot alerts and security updates**: Settings, Code security, enable both.
- **Required audit check**: Settings, Branches, branch protection for the default branch, require the
  `audit` workflow jobs (`cargo`, `npm`, `python`) to pass before merging. Without this the audit
  job reports a failure but does not block a merge.

## Scope

The threat model is in `docs/distributed/threat-model.md`. Standalone `llm-bridge` opens no
network listener; the distributed mode's trust boundaries are listed there.
