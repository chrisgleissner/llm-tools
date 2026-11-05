import { h, isoTime, timeEl } from "../dom";
import { table, workerReadView } from "./remote";

interface Scope {
  scope: string;
  model: string;
  remaining_percent: number | null;
  used_percent: number | null;
  reset_epoch: number | null;
  guidance: string | null;
}
interface Provider {
  provider: string;
  available: boolean;
  reason: string | null;
  ready: boolean;
  scopes: Scope[];
}

const pct = (v: number | null) => (v === null ? "-" : `${v}%`);

export function usage(root: HTMLElement, query: URLSearchParams): () => void {
  return workerReadView(root, {
    title: "Provider usage",
    operation: "usage.snapshot",
    query,
    render: (result, workerName) => {
      if (result["available"] === false) return h("p", { class: "notice", role: "status" }, `Usage is not available from ${workerName}: ${String(result["reason"] ?? "unknown reason")}.`);
      const providers = (result["providers"] as Provider[] | undefined) ?? [];
      if (providers.length === 0) return h("p", { class: "muted" }, "No providers are enabled on this worker.");
      const rows = providers.flatMap((p) => {
        if (p.scopes.length === 0) return [[h("span", {}, p.provider), h("span", {}, p.available ? (p.ready ? "yes" : "no") : "unavailable"), h("span", {}, "-"), h("span", {}, "-"), h("span", {}, "-"), h("span", {}, "-"), h("span", {}, p.reason ?? "-")]];
        return p.scopes.map((s, i) => [
          h("span", {}, i === 0 ? p.provider : `${p.provider} (${s.scope})`),
          h("span", {}, i === 0 ? (p.available ? (p.ready ? "yes" : "no") : "unavailable") : ""),
          h("span", {}, s.model ? `${s.scope} / ${s.model}` : s.scope),
          h("span", {}, pct(s.remaining_percent)),
          h("span", {}, pct(s.used_percent)),
          s.reset_epoch === null ? h("span", {}, "-") : timeEl(s.reset_epoch),
          h("span", {}, s.guidance ?? (i === 0 && p.reason ? p.reason : "-")),
        ]);
      });
      return h(
        "div",
        {},
        h("p", { class: "muted", id: "usage-observed" }, `Observed ${isoTime(String(result["observed_at"] ?? ""))} (source: ${String(result["source"] ?? "-")}).`),
        table("usage-table", `Provider usage on ${workerName}`, ["Provider", "Ready", "Scope", "Remaining", "Used", "Resets", "Guidance / reason"], rows),
      );
    },
  });
}
