import * as api from "../api";
import { absTime, disclosure, emptyState, h, statePill, toDate } from "../dom";
import { deriveState, type DerivedState } from "../state";
import { ago, calm, cap, clock, el, facts, fleetView, plural, when, type Answer, type FleetRender } from "./remote";

type Snapshot = api.UsageSnapshot;
type Provider = api.UsageProvider;
type Scope = api.UsageScope;

const NAMES: Record<string, string> = { opencode: "OpenCode", minimax: "MiniMax", zai: "z.ai" };
const SCOPES: Record<string, string> = { "5h": "5 hours", weekly: "Weekly", monthly: "Monthly", balance: "Balance", budget: "Budget", spend: "Spend" };
/** A provider with this little left in its tightest window is called out as nearly out. */
const LOW = 10;
const EMPTY = "No usage data yet";

const display = (name: string): string => NAMES[name] ?? cap(name);

/** Plain sentences for the reason codes a worker reports (WUX-017); the raw code is never shown. */
export function reasonText(reason: string | null | undefined): string {
  const code = (reason ?? "").toLowerCase();
  if (/^(unavailable|none)?$/.test(code)) return "No usage data";
  if (code.startsWith("read-failed")) return "The usage check failed";
  const map: Record<string, string> = {
    "not-authenticated": "Not signed in",
    "missing-cli": "Its command-line tool is not installed",
    "inconclusive-usage": "No local usage data",
    "stale-usage": "The usage data is out of date",
    "rate-limited": "The provider is limiting usage checks",
    "network-error": "A network error stopped the usage check",
    "subscription-required": "No active subscription",
    "quota-error": "The provider refused the quota query",
    timeout: "The usage check timed out",
  };
  return map[code] ?? cap(code.replace(/[-_]+/g, " "));
}

const measured = (s: Scope): s is Scope & { remaining_percent: number } => typeof s.remaining_percent === "number";
/** The smallest percentage left among the measured scopes, or null when none is measured. */
const tightest = (scopes: Scope[]): number | null => {
  const left = scopes.filter(measured).map((s) => s.remaining_percent);
  return left.length ? Math.min(...left) : null;
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
  const d = toDate(epoch);
  return d ? h("span", { class: "nowrap", title: absTime(d) }, `resets ${clock(epoch!)} (`, when(d), ")") : null;
}

function meterRow(p: Provider, s: Scope): HTMLElement {
  const label = scopeLabel(s);
  const reset = resetText(s.reset_epoch);
  const m = measured(s);
  const left = m ? Math.max(0, Math.min(100, s.remaining_percent)) : 0;
  const text = `${Math.round(left)}% left`;
  const fill = h("i");
  fill.style.setProperty("inline-size", `${left}%`);
  const gauge = h("div", { class: "gauge", role: "meter", "aria-label": `${display(p.provider)}, ${label}: capacity left`, "aria-valuemin": 0, "aria-valuemax": 100, "aria-valuenow": Math.round(left), "aria-valuetext": text, "data-low": left <= LOW ? "true" : null }, fill);
  return el(
    "div",
    "meter-row",
    el("div", "mhead", el("span", "nowrap", label), m ? el("strong", "num nowrap", text) : el("strong", "muted", "Not measured")),
    m ? gauge : null,
    el("div", "mfoot muted small", ...(m ? [reset ?? "No reset time reported"] : [s.guidance ?? "This scope has no percentage.", reset ? " " : null, reset])),
  );
}

function providerCard(p: Provider, level: "h2" | "h3"): HTMLElement {
  const state = providerState(p);
  const guidance = p.scopes.filter((s) => s.guidance);

  return h(
    "li",
    { class: "pcard", "data-entity": `provider:${p.provider}`, "data-l0": "usage.provider-row" },
    el("div", "pcard-head", el(level, "pcard-name", display(p.provider)), statePill(state), p.available ? null : el("span", "pill-meta", reasonText(p.reason))),
    p.scopes.length ? el("div", "gauges", ...p.scopes.map((s) => meterRow(p, s))) : null,
    guidance.length ? disclosure("Details", facts(guidance.map((s) => [scopeLabel(s), s.guidance ?? ""]))) : null,
  );
}

function sortedProviders(snapshot: Snapshot): Provider[] {
  const rank = (p: Provider): number => (!p.available ? 2 : !p.ready ? 0 : 1);
  return [...(snapshot.providers ?? [])].sort((a, b) => rank(a) - rank(b) || (tightest(a.scopes) ?? 101) - (tightest(b.scopes) ?? 101) || a.provider.localeCompare(b.provider));
}

function sourceNote(s: Snapshot, workers = ""): HTMLElement {
  const source = s.source === "service" ? "the usage service" : s.source === "direct" ? "a direct read" : null;
  return el("p", "muted small foot", s.observed_at ? ago(s.observed_at, "Observed") : "Not observed yet", source ? ` · from ${source}` : null, workers ? ` · ${workers}` : null);
}

function answerFor(providers: Provider[]): Answer {
  const names = [...new Set(providers.map((p) => p.provider))];
  const byName = names.map((n) => {
    const mine = providers.filter((p) => p.provider === n);
    const scopes = mine.flatMap((p) => p.scopes);
    return { name: n, left: tightest(scopes), ready: mine.some((p) => p.available && p.ready), usable: mine.some((p) => p.available), reason: mine.find((p) => !p.available)?.reason, resets: scopes.map((s) => s.reset_epoch).filter((v): v is number => v !== null).sort((a, b) => a - b)[0] ?? null };
  });
  byName.sort((a, b) => (a.left ?? 101) - (b.left ?? 101));
  const figures = byName.map((n) => `${display(n.name)} ${n.left === null ? "not measured" : `${Math.round(n.left)}% left`}`);
  const blocked = byName.find((n) => n.usable && !n.ready);
  const low = byName.find((n) => n.left !== null && n.left <= LOW);
  const unavailable = byName.find((n) => !n.usable);
  const needMe = blocked ? `${display(blocked.name)} is waiting for capacity${blocked.left === null ? "" : `, ${Math.round(blocked.left)}% is left`}.` : low ? `${display(low.name)} is nearly out of capacity.` : unavailable ? `${display(unavailable.name)} is unavailable: ${reasonText(unavailable.reason)}.` : "Every provider has capacity.";
  const focus = blocked ?? low;
  const next = focus && focus.resets !== null ? `Missions that need ${display(focus.name)} wait for its reset at ${clock(focus.resets)}.` : unavailable ? `Sign in to ${display(unavailable.name)} on the worker's machine.` : "Nothing to do.";
  return { what: `${plural(names.length, "provider")} reporting: ${figures.join(", ")}.`, needMe, next };
}

function render(ctx: FleetRender<Snapshot>): { answer: Answer; body: Node } {
  const withData = ctx.results.filter((r) => r.data.available !== false && (r.data.providers ?? []).length > 0);
  if (withData.length === 0) {
    const reason = ctx.results.map((r) => r.data.reason).find(Boolean);
    return {
      answer: calm("No provider usage is available.", "Install and sign in to a provider CLI on a worker."),
      body: emptyState({ icon: "usage", title: EMPTY, text: `A worker reports provider capacity once a provider command-line tool such as Codex or Claude Code is installed and signed in on its machine.${reason ? ` ${reasonText(reason)}.` : ""}`, command: "llm-usage" }),
    };
  }
  const lowest = (r: { data: Snapshot }) => Math.min(101, ...(r.data.providers ?? []).map((p) => tightest(p.scopes) ?? 101));
  const ordered = [...withData].sort((a, b) => lowest(a) - lowest(b) || a.worker.localeCompare(b.worker));
  // Workers that report the same figures share one card set, headed by all their names.
  const groups = new Map<string, { workers: string[]; data: Snapshot }>();
  for (const r of ordered) {
    const key = JSON.stringify(sortedProviders(r.data));
    const g = groups.get(key) ?? { workers: [], data: r.data };
    g.workers.push(r.worker);
    groups.set(key, g);
  }
  const sections = ctx.single || groups.size === 1 ? 1 : groups.size;
  const level = sections === 1 ? "h2" : "h3";
  const parts = [...groups.values()].map(({ workers, data }) => {
    const names = workers.join(", ");
    const cards = h("ul", { class: "pcards", "aria-label": sections === 1 ? "Providers" : `Providers on ${names}` }, ...sortedProviders(data).map((p) => providerCard(p, level)));
    if (sections === 1) return h("div", {}, cards, sourceNote(data, ctx.single ? "" : `reported by ${names}`));
    return h("section", { class: "wsection", "aria-label": `Capacity on ${names}`, "data-worker": workers[0] }, el("h2", "wname", names), cards, sourceNote(data));
  });
  return { answer: answerFor(withData.flatMap((r) => r.data.providers ?? [])), body: h("div", {}, ...parts) };
}

export function usage(root: HTMLElement, query: URLSearchParams): () => void {
  return fleetView<Snapshot>(root, {
    title: "Provider usage",
    query,
    load: (workerName) => api.usageSnapshot(workerName),
    render,
    icon: "usage",
    empty: EMPTY,
    none: "Provider capacity comes from workers, and none is enrolled. Add a worker to see how much of each provider's allowance is left.",
    offline: "provider usage can be read",
    read: "provider capacity",
  });
}
