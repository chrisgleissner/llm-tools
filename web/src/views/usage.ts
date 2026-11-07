import * as api from "../api";
import { absTime, disclosure, emptyState, h, relFromMs, statePill, toDate } from "../dom";
import { deriveState, type DerivedState } from "../state";
import { ago, clock, fleetView, plural, type Answer, type FleetRender } from "./remote";

type Snapshot = api.UsageSnapshot;
type Provider = api.UsageProvider;
type Scope = api.UsageScope;

const NAMES: Record<string, string> = { codex: "Codex", claude: "Claude", copilot: "Copilot", kilo: "Kilo", opencode: "OpenCode", minimax: "MiniMax", zai: "z.ai" };
const SCOPES: Record<string, string> = { "5h": "5 hours", weekly: "Weekly", monthly: "Monthly", balance: "Balance", budget: "Budget", spend: "Spend" };
/** A provider with this little left in its tightest window is called out as nearly out. */
const LOW = 10;

const display = (name: string): string => NAMES[name] ?? name.charAt(0).toUpperCase() + name.slice(1);

/** Plain sentences for the reason codes a worker reports (WUX-017); the raw code is never shown. */
export function reasonText(reason: string | null | undefined): string {
  const code = (reason ?? "").toLowerCase();
  if (!code || code === "unavailable") return "No usage data";
  if (code.startsWith("read-failed")) return "The usage check failed";
  const map: Record<string, string> = {
    "not-authenticated": "Not signed in",
    "missing-cli": "Its command-line tool is not installed",
    "inconclusive-usage": "No local usage data",
    "no-local-usage": "No local usage data",
    "stale-usage": "The usage data is out of date",
    "rate-limited": "The provider is limiting usage checks",
    "network-error": "A network error stopped the usage check",
    "subscription-required": "No active subscription",
    "quota-error": "The provider refused the quota query",
    hidden: "Hidden by configuration",
    timeout: "The usage check timed out",
    none: "No usage data",
  };
  return map[code] ?? code.replace(/[-_]+/g, " ").replace(/^./, (c) => c.toUpperCase());
}

const measured = (s: Scope): s is Scope & { remaining_percent: number } => typeof s.remaining_percent === "number";
const tightest = (p: Provider): number | null => {
  const left = p.scopes.filter(measured).map((s) => s.remaining_percent);
  return left.length === 0 ? null : Math.min(...left);
};

function providerState(p: Provider): DerivedState {
  if (!p.available) return deriveState({ entity: "provider", status: "unavailable", reason: reasonText(p.reason) });
  if (!p.ready) return deriveState({ status: "QUEUED", needsYou: { kind: "capacity", message: "" } });
  return deriveState({ entity: "provider", status: "ready" });
}

function scopeLabel(s: Scope): string {
  const base = SCOPES[s.scope] ?? s.scope;
  return s.model ? `${base} · ${s.model}` : base;
}

function resetText(epoch: number | null): HTMLElement | null {
  if (epoch === null) return null;
  const d = toDate(epoch);
  if (!d) return null;
  return h("span", { class: "nowrap", title: absTime(d) }, `resets ${clock(epoch)} (`, h("time", { class: "ts", datetime: d.toISOString(), "data-ms": d.getTime() }, h("span", { class: "rel" }, relFromMs(d.getTime()))), ")");
}

function meterRow(p: Provider, s: Scope): HTMLElement {
  const label = scopeLabel(s);
  const reset = resetText(s.reset_epoch);
  if (!measured(s)) {
    return h("div", { class: "mrow" }, h("div", { class: "mhead" }, h("span", { class: "nowrap" }, label), h("strong", { class: "muted" }, "Not measured")), h("div", { class: "mfoot muted small" }, s.guidance ?? "This scope has no percentage.", reset ? " " : null, reset));
  }
  const left = Math.max(0, Math.min(100, s.remaining_percent));
  const text = `${Math.round(left)}% left`;
  const gauge = h("div", { class: "gauge", role: "meter", "aria-label": `${display(p.provider)}, ${label}: capacity left`, "aria-valuemin": 0, "aria-valuemax": 100, "aria-valuenow": Math.round(left), "aria-valuetext": text, "data-low": left <= LOW ? "true" : null }, h("i"));
  (gauge.firstElementChild as HTMLElement).style.setProperty("inline-size", `${left}%`);
  return h("div", { class: "mrow" }, h("div", { class: "mhead" }, h("span", { class: "nowrap" }, label), h("strong", { class: "num nowrap" }, text)), gauge, h("div", { class: "mfoot muted small" }, reset ?? "No reset time reported"));
}

function providerCard(p: Provider, level: "h2" | "h3"): HTMLElement {
  const state = providerState(p);
  const guidance = p.scopes.filter((s) => s.guidance);
  const meters = p.scopes.length > 0 ? h("div", { class: "gauges" }, ...p.scopes.map((s) => meterRow(p, s))) : null;
  const detail = p.available ? null : h("span", { class: "pill-meta" }, reasonText(p.reason));
  return h(
    "li",
    { class: "pcard", "data-entity": `provider:${p.provider}`, "data-l0": "usage.provider-row" },
    h("div", { class: "pcard-head" }, h(level, { class: "pcard-name" }, display(p.provider)), statePill(state), detail),
    meters,
    guidance.length > 0 ? disclosure("Details", h("dl", { class: "facts" }, ...guidance.flatMap((s) => [h("dt", {}, scopeLabel(s)), h("dd", {}, s.guidance ?? "")]))) : null,
  );
}

function sortedProviders(snapshot: Snapshot): Provider[] {
  const rank = (p: Provider): number => (!p.available ? 2 : !p.ready ? 0 : 1);
  return [...(snapshot.providers ?? [])].sort((a, b) => rank(a) - rank(b) || (tightest(a) ?? 101) - (tightest(b) ?? 101) || a.provider.localeCompare(b.provider));
}

function sourceNote(s: Snapshot): HTMLElement {
  const source = s.source === "service" ? "the usage service" : s.source === "direct" ? "a direct read" : null;
  return h("p", { class: "muted small foot" }, s.observed_at ? ago(s.observed_at, "Observed") : "Not observed yet", source ? ` · from ${source}` : null);
}

function answerFor(all: Array<{ worker: string; snap: Snapshot }>): Answer {
  const providers = all.flatMap((a) => a.snap.providers ?? []);
  const names = [...new Set(providers.map((p) => p.provider))];
  const byName = names.map((n) => {
    const mine = providers.filter((p) => p.provider === n);
    const lows = mine.map(tightest).filter((v): v is number => v !== null);
    return { name: n, left: lows.length ? Math.min(...lows) : null, ready: mine.some((p) => p.available && p.ready), usable: mine.some((p) => p.available), reason: mine.find((p) => !p.available)?.reason, resets: mine.flatMap((p) => p.scopes.map((s) => s.reset_epoch)).filter((v): v is number => v !== null).sort((a, b) => a - b)[0] ?? null };
  });
  byName.sort((a, b) => (a.left ?? 101) - (b.left ?? 101));
  const figures = byName.map((n) => `${display(n.name)} ${n.left === null ? "not measured" : `${Math.round(n.left)}% left`}`);
  const blocked = byName.find((n) => n.usable && !n.ready);
  const low = byName.find((n) => n.left !== null && n.left <= LOW);
  const unavailable = byName.find((n) => !n.usable);
  const needMe = blocked ? `${display(blocked.name)} has no capacity to start new work.` : low ? `${display(low.name)} is nearly out of capacity.` : unavailable ? `${display(unavailable.name)} is unavailable: ${reasonText(unavailable.reason)}.` : "Every provider has capacity.";
  const focus = blocked ?? low;
  const next = focus && focus.resets !== null ? `${display(focus.name)} resets at ${clock(focus.resets)}. Missions that need it wait until then.` : unavailable ? `Sign in to ${display(unavailable.name)} on the worker's machine.` : "Nothing to do.";
  return { what: `${plural(names.length, "provider")} reporting: ${figures.join(", ")}.`, needMe, next };
}

function render(ctx: FleetRender<Snapshot>): { answer: Answer; body: Node } {
  const withData = ctx.results.filter((r) => r.data.available !== false && (r.data.providers ?? []).length > 0);
  if (withData.length === 0) {
    const reason = ctx.results.map((r) => r.data.reason).find(Boolean);
    return {
      answer: { what: "No provider usage is available.", needMe: "Nothing needs you.", next: "Install and sign in to a provider CLI on a worker." },
      body: emptyState({ icon: "usage", title: "No usage data yet", text: `A worker reports provider capacity once a provider command-line tool such as Codex or Claude Code is installed and signed in on its machine.${reason ? ` ${reasonText(reason)}.` : ""}`, command: "llm-usage" }),
    };
  }
  const sections = ctx.single || withData.length === 1 ? 1 : withData.length;
  const level = sections === 1 ? "h2" : "h3";
  const lowest = (r: { data: Snapshot }) => Math.min(101, ...(r.data.providers ?? []).map((p) => tightest(p) ?? 101));
  const ordered = [...withData].sort((a, b) => lowest(a) - lowest(b) || a.worker.localeCompare(b.worker));
  const parts = ordered.map((r) => {
    const cards = h("ul", { class: "pcards", "aria-label": sections === 1 ? "Providers" : `Providers on ${r.worker}` }, ...sortedProviders(r.data).map((p) => providerCard(p, level)));
    if (sections === 1) return h("div", {}, cards, sourceNote(r.data));
    return h("section", { class: "wsection", "aria-label": `Capacity on ${r.worker}`, "data-worker": r.worker }, h("h2", { class: "wname" }, r.worker), cards, sourceNote(r.data));
  });
  return { answer: answerFor(withData.map((r) => ({ worker: r.worker, snap: r.data }))), body: h("div", {}, ...parts) };
}

export function usage(root: HTMLElement, query: URLSearchParams): () => void {
  return fleetView<Snapshot>(root, {
    title: "Provider usage",
    query,
    load: (workerName) => api.usageSnapshot(workerName),
    render,
    noWorkers: () => emptyState({ icon: "usage", title: "No usage data yet", text: "Provider capacity comes from workers, and none is enrolled. Add a worker to see how much of each provider's allowance is left.", action: h("div", { class: "actions" }, h("a", { class: "btn primary", href: "/workers" }, "Go to Workers")) }),
    allOffline: (names) => emptyState({ icon: "workers", title: "No worker is online", text: `${names.join(", ")} must reconnect before provider usage can be read.`, action: h("div", { class: "actions" }, h("a", { class: "btn", href: "/workers" }, "Check Workers")) }),
    idle: (reason) => ({
      what: reason === "loading" ? "Reading provider capacity from the workers." : reason === "offline" ? "No worker is online, so capacity cannot be read." : "No worker is enrolled yet.",
      needMe: reason === "loading" ? "Nothing needs you." : "A worker has to be online first.",
      next: reason === "loading" ? "This takes a moment." : "Open Workers to add or check one.",
    }),
  });
}
