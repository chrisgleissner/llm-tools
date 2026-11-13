# Provider capabilities

Generated from `llm_tools/bridge_providers.py` and `llm_tools/provider_evidence/*.json` by
`python -m llm_tools.bridge_capabilities --write-doc`; `tests/test_bridge_capabilities.py` fails when this file differs.
Do not edit it by hand. Earlier hand-written discovery notes are in `provider-capabilities-history.md`.

`unverified` means Bridge implements the capability but no evidence file records it for a live CLI.

| Provider | launch | reply | stop | exact_resume | context_recall | finish | limit_notice | question_detection | task_notice | prompt_outside_argv | Evidence |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Codex | unverified | unverified | unverified | unverified | unverified | supported | supported | supported | supported | unsupported | none |
| Claude Code | unverified | unverified | unverified | unverified | unverified | supported | supported | supported | supported | unsupported | none |
| Copilot | unverified | unverified | unverified | unverified | unverified | supported | unsupported | unsupported | supported | unsupported | none |
| Kilo | unverified | unverified | unverified | unverified | unverified | supported | unsupported | unsupported | unsupported | unsupported | none |
| OpenCode | unverified | unverified | unverified | unverified | unverified | supported | unsupported | unsupported | unsupported | unsupported | none |
| MiniMax | unsupported | unsupported | unsupported | unsupported | unsupported | unsupported | unsupported | unsupported | unsupported | unsupported | none |
| z.ai | unsupported | unsupported | unsupported | unsupported | unsupported | unsupported | unsupported | unsupported | unsupported | unsupported | none |

Notes

- Kilo: prompt channel `argv`; kilo 7.3.45 help: `run [message..]` positional; --file attaches, no prompt-file or stdin option documented
- OpenCode: prompt channel `argv`; opencode 1.18.3 help: `run [message..]` positional; --file attaches, no prompt-file or stdin option documented
- MiniMax: no native mission. MiniMax mmx exposes text chat, not a native engineering session for LLM Bridge. Run MiniMax models through a route on Kilo or OpenCode instead (e.g. [routes.kilo-minimax-m3] with provider = "kilo" and model = "minimax-m3")
- z.ai: no native mission. --provider zai has no launch CLI. Launch Kilo or OpenCode instead: use a route (e.g. [routes.kilo-zai-glm-47] with capacity.policy = "delegate" and provider = "zai") or set [providers.kilo] capacity_provider = "zai"
