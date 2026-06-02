## Work log

- 2026-06-02 13:55 UTC+1: Created `PLANS.md` and started migration from `/home/chris/.local/bin/llm-usage` to `/home/chris/dev/llm-usage`.
- 2026-06-02 13:56 UTC+1: Copied executable and test script into repository root; added SPDX headers to both scripts.
- 2026-06-02 13:58 UTC+1: Replaced tests with fixture-driven local-test script and created README, LICENSE, NOTICE, .gitignore, and `.github/workflows/test.yml`.
- 2026-06-02 14:04 UTC+1: Verified script/test syntax and fixture-driven tests; recorded shellcheck skipped (not installed).
- 2026-06-02 14:05 UTC+1: Linked `/home/chris/.local/bin/llm-usage` to `/home/chris/dev/llm-usage/llm-usage` and initialized a `main` git repository.
- 2026-06-02 14:10 UTC+1: Updated `--watch` to avoid screen clearing and print `Last refreshed` on each refresh; added fixture-driven test coverage.
- 2026-06-02 14:13 UTC+1: Added terminal color output for Remaining/Used values (red→yellow→green by remaining percentage) with tty-only rendering and documented in README.
