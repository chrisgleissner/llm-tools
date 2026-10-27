# Additional accepted Bridge requirements

These extend the original directive in `prompt.md`; later user instructions
supersede earlier defaults. Implementations and verification remain traceable in
`../../llm-bridge/spec-audit.md` and `HANDOVER.md`.

- Bridge code has a separate **90% branch coverage CI gate**, retaining global
  85%. CI tests Python 3.11 minimum and current 3.15 series.
- Use Sol 6.1 **High** for independent review. Live paid checks currently use
  Codex only; installed Claude command propagation is checked without claiming
  an authenticated completion.
- Before major changes are committed, run the relevant opt-in native probe
  scripts: native model response, exact-owned-ID resume and observable native
  mouse behavior. CI tests their evidence oracles and deterministic terminal
  transport; it never initiates paid requests. Unobserved capabilities remain
  NOT VERIFIED and cannot be converted to passing startup/geometry checks.
- A deliberate outer terminal prefix returns from any provider to Bridge,
  including when zoomed. A persistent clickable Bridge target gives the same
  operation. Provider keystrokes/paste/mouse inside its pane remain native;
  double prefix forwards a literal reserved key. Document nested-tmux conflicts.
- Every standalone operator command uses the same parser/validation/handlers in
  the interactive REPL. The mission may be implicit. Multiline interactive
  entry retains task priority/dependencies/acceptance settings. Direct provider
  input remains available, while durable amendments require Bridge submission.
- Show estimated time to exhaustion for the active model's 5h and weekly scopes,
  plus alternate eligible providers/routes. Reuse llm-usage's sample history and
  confidence rules; show unknown when evidence is insufficient. Distinguish
  runout estimates from resets, configured model from observed model, and data
  freshness from liveness. Opaque routes never receive invented percentages.
- Provide the **full llm-usage dashboard inside Bridge**, using its existing
  Python readers/builders/renderer without launching an llm-usage subprocess.
  Refresh outside the input/render thread, preserving all available columns,
  provider/model scopes, route and budget information through scrolling.
- Dogfood Bridge for its own implementation. Any direct CLI versus facade
  discrepancy becomes a documented implementation/spec issue. README includes
  an actual Bridge screenshot. Merge and release only after all gates pass.
