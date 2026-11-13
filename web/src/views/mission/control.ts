// Rotation and settings of a mission (PRD-016, PAR rows `rotation` and `mission set`): the saved provider rotation and the execution settings, read and changed
// through the worker's typed operations `mission.rotation.get/put` and `mission.settings.get/patch`. The block reads only when it is opened, and every change can be undone
// from its toast by writing the earlier value back, but only while the worker still has the value this change wrote. Neither operation has a revision or etag, so that comparison is the only guard.
import * as api from "../../api";
import { h } from "../../dom";
import { reportOutcome, type ToastAction } from "../../ui";
import { record } from "./kit";

export interface ControlCtx {
  workerName: string;
  missionId: string;
  /** "" when changes are possible, otherwise why not. */
  blocked(): string;
}

export interface Control {
  el: HTMLDetailsElement;
  /** Enable or disable the fields and buttons for the current `blocked()`. */
  sync(): void;
}

interface Settings {
  auto_continue: boolean;
  suspend_when_rate_limited: boolean;
  teams: boolean | null;
  token_spend: string;
}

const SPEND = ["low", "medium", "high"];

/** The typed call and its outcome in the words `reportOutcome` expects. */
async function send(ctx: ControlCtx, operation: string, params: Record<string, unknown>): Promise<{ outcome: "applied" | "rejected" | "failed"; message: string; result: Record<string, unknown> }> {
  try {
    const row = await api.command(ctx.workerName, operation, ctx.missionId, params);
    return { outcome: "applied", message: "", result: record(row.result?.result) };
  } catch (e) {
    return { outcome: e instanceof api.ApiError && e.status === 422 ? "rejected" : "failed", message: e instanceof api.ApiError ? e.message : "Unexpected error.", result: {} };
  }
}

/** The rotation names and the settings as the worker has them now, or null when either cannot be read. Neither operation carries a revision, so a value read is the only check there is. */
async function fresh(ctx: ControlCtx): Promise<{ rotation: string[]; settings: Settings; mode: string; selected: string } | null> {
  const [rot, set] = await Promise.all([send(ctx, "mission.rotation.get", {}), send(ctx, "mission.settings.get", {})]);
  if (rot.outcome !== "applied" || set.outcome !== "applied") return null;
  const policy = record(set.result["execution_policy"]);
  return {
    rotation: (Array.isArray(rot.result["entries"]) ? (rot.result["entries"] as unknown[]) : []).map((e) => String(record(e)["name"] ?? "")).filter(Boolean),
    mode: String(rot.result["mode"] ?? "providers"),
    selected: typeof rot.result["selected"] === "string" ? rot.result["selected"] : "",
    settings: {
      auto_continue: set.result["auto_continue"] === true,
      suspend_when_rate_limited: set.result["suspend_when_rate_limited"] === true,
      teams: policy["legacy"] === true ? null : policy["teams_enabled"] === true,
      token_spend: policy["legacy"] === true ? "" : String(policy["token_spend"] ?? ""),
    },
  };
}

const NOT_UNDONE = "Changed elsewhere; not undone.";

export function controlBlock(ctx: ControlCtx): Control {
  let loaded = false;
  let rotation: string[] = [];
  let settings: Settings = { auto_continue: false, suspend_when_rate_limited: false, teams: null, token_spend: "" };
  const body = h("div", { class: "disclosure-body stack" }, h("p", { class: "muted" }, "Loading..."));
  const el = h("details", { class: "disclosure", id: "mission-control" }, h("summary", {}, h("span", {}, "Rotation and settings")), body);

  async function load(): Promise<void> {
    const now = await fresh(ctx);
    if (!now) {
      body.replaceChildren(h("p", { class: "notice", role: "status" }, "Cannot load the rotation and settings."));
      loaded = false;
      return;
    }
    rotation = now.rotation;
    settings = now.settings;
    loaded = true;
    draw(now.mode, now.selected);
  }

  function draw(mode: string, selected: string): void {
    const rotationInput = h("input", { id: "rotation-input", type: "text", value: rotation.join(","), autocomplete: "off", spellcheck: false });
    const rotationNote = h("p", { class: "muted small", id: "rotation-note", role: "status" }, `Order of the ${mode === "routes" ? "routes" : "providers"}, separated by commas.${selected ? ` In use now: ${selected}.` : ""}`);
    const saveRotation = h("button", { type: "button", class: "btn small", id: "rotation-save", "data-action": "rotation-save" }, h("span", {}, "Save rotation"));
    saveRotation.addEventListener("click", async () => {
      const next = rotationInput.value.split(",").map((s) => s.trim()).filter(Boolean);
      if (!next.length || next.join(",") === rotation.join(",")) return;
      // The earlier value is the worker's now, not what the form was drawn from.
      const before = await fresh(ctx);
      if (!before) return void reportOutcome({ outcome: "failed", message: "The current rotation could not be read." }, "", "save the rotation");
      const r = await send(ctx, "mission.rotation.put", { set: next.join(",") });
      const undo: ToastAction = {
        label: "Undo",
        run: () =>
          void (async () => {
            const now = await fresh(ctx);
            if (!now || now.rotation.join(",") !== next.join(",")) return void reportOutcome({ outcome: "rejected", message: NOT_UNDONE }, "", "restore the rotation");
            const u = await send(ctx, "mission.rotation.put", { set: before.rotation.join(",") });
            reportOutcome(u, "Rotation restored", "restore the rotation");
            if (u.outcome === "applied") await load();
          })(),
      };
      reportOutcome(r, "Rotation saved", "save the rotation", undo);
      // A failed save leaves the form, its focus and the typed text as they are.
      if (r.outcome !== "applied") return;
      rotation = next;
      if (typeof r.result["applies"] === "string") rotationNote.textContent = r.result["applies"];
    });

    const auto = h("input", { id: "setting-auto-continue", type: "checkbox", checked: settings.auto_continue });
    const suspend = h("input", { id: "setting-suspend", type: "checkbox", checked: settings.suspend_when_rate_limited });
    const teams = h("input", { id: "setting-teams", type: "checkbox", checked: settings.teams === true, disabled: settings.teams === null });
    const spend = h("select", { id: "setting-spend", disabled: settings.token_spend === "" }, ...(settings.token_spend === "" ? [h("option", { value: "" }, "Not set for this mission")] : []), ...SPEND.map((v) => h("option", { value: v, selected: v === settings.token_spend }, v)));
    const saveSettings = h("button", { type: "button", class: "btn small", id: "settings-save", "data-action": "settings-save" }, h("span", {}, "Save settings"));
    saveSettings.addEventListener("click", async () => {
      const before = await fresh(ctx);
      if (!before) return void reportOutcome({ outcome: "failed", message: "The current settings could not be read." }, "", "save the settings");
      const was = before.settings;
      const patch: Record<string, unknown> = {};
      const back: Record<string, unknown> = {};
      const note = (key: keyof Settings, value: unknown): void => {
        if (value !== was[key]) [patch[key], back[key]] = [value, was[key]];
      };
      note("auto_continue", auto.checked);
      note("suspend_when_rate_limited", suspend.checked);
      if (was.teams !== null) note("teams", teams.checked);
      if (was.token_spend !== "") note("token_spend", spend.value);
      if (!Object.keys(patch).length) return;
      const r = await send(ctx, "mission.settings.patch", patch);
      const undo: ToastAction = {
        label: "Undo",
        run: () =>
          void (async () => {
            const now = await fresh(ctx);
            if (!now || Object.entries(patch).some(([k, v]) => now.settings[k as keyof Settings] !== v)) return void reportOutcome({ outcome: "rejected", message: NOT_UNDONE }, "", "restore the settings");
            const u = await send(ctx, "mission.settings.patch", back);
            reportOutcome(u, "Settings restored", "restore the settings");
            if (u.outcome === "applied") await load();
          })(),
      };
      reportOutcome(r, "Settings saved", "save the settings", undo);
      if (r.outcome === "applied") settings = { ...settings, ...(patch as Partial<Settings>) };
    });
    const check = (input: HTMLElement, label: string): HTMLElement => h("label", { class: "check" }, input, ` ${label}`);

    body.replaceChildren(
      h("div", { class: "field" }, h("label", { for: "rotation-input" }, "Provider rotation"), rotationInput, rotationNote, saveRotation),
      h("div", { class: "stack" }, check(auto, "Continue with the next task automatically"), check(suspend, "Suspend the machine while every provider is rate-limited"), check(teams, "Allow agent teams"), h("div", { class: "field" }, h("label", { for: "setting-spend" }, "Token spend"), spend), saveSettings),
    );
    sync();
  }

  function sync(): void {
    const why = ctx.blocked();
    for (const control of body.querySelectorAll<HTMLInputElement | HTMLButtonElement | HTMLSelectElement>("input, select, button")) {
      const locked = control.dataset["locked"] === "1";
      if (why) {
        if (!control.disabled) control.dataset["locked"] = "1";
        control.disabled = true;
        control.title = why;
      } else if (locked) {
        control.disabled = false;
        control.title = "";
        delete control.dataset["locked"];
      }
    }
  }

  el.addEventListener("toggle", () => {
    if (el.open && !loaded) void load();
  });
  return { el, sync };
}
