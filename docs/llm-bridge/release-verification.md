# LLM Bridge 1.1.0 publication verification

Verified 2026-10-27 on the final production/test changes extending `41cff55`.
Documentation-only record updates follow the source freeze. GitHub PR26 checks
provide the publication-head CI verdict; mark ready/merge/tag only after they
pass. The release workflow uses the plain numeric tag `1.1.0`.

| Gate | Result |
| --- | --- |
| Python3.11.14 full suite | 1,262 passed in196.73s |
| Python3.15.1 full suite | 1,262 passed in188.98s |
| Global branch-coverage report, both versions | 89%, required85% |
| Bridge/terminal branch-coverage report, both versions | 92%, required90% |
| Independent source review | Sol6.1 High; A1–A24 closed |
| Terminal repeated runs | Implementor12×7 tests per version; reviewer12×2 plus48 contracts |
| Native Codex full-controller reply/resume | PASS, two-prompt budget, same owned UUID/new controller/context recall |
| Codex `/model`, Claude `/help` | PASS, zero prompts, direct and Bridge paths |
| Known-marker native wheel parity | PASS, equal measured120×32 viewports, pane mode0 |
| Native current-model click selection | NOT VERIFIED, no visible effect in either path |
| Paid Claude/Copilot reply or restoration | NOT VERIFIED; not attempted |
| Final wheel/sdist | Built; isolated3.11 wheel/3.15 sdist installation and CLI help passed |
| Installed artifact lifecycle smoke | Private create/verbatim prompt/pending amend/show passed outside source tree |
| Original directive | Unchanged |
| Whitespace/syntax | `git diff --check` and compileall passed |

Exact full-suite command, once per interpreter and unique directory:

```sh
COVERAGE_FILE=/tmp/bridge-publication-311/.coverage \
  /tmp/llm-bridge-release-venv/bin/python -m coverage run -m pytest \
  -o addopts='' -q --basetemp=/tmp/bridge-publication-311/tests
```

Combine/report with that same `COVERAGE_FILE`; the3.15 run uses
`/tmp/llm-bridge-release-315-venv/bin/python` and its own `315` directory.
CI commands remain `coverage report --fail-under=85` and
`coverage report --include='llm_tools/bridge*.py,llm_tools/terminal.py' --fail-under=90`.
No exclusions or thresholds were changed. Logs/coverage artifacts are outside
the repository. Live structured evidence is `final-native-mission.json` and
`final-native-input.json`; no raw screens, credentials or provider transcripts
are included. Long-run simulation is measured test evidence, not multiweek or
physical-reboot reliability proof.
