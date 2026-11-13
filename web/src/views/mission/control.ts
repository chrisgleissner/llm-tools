// Rotation and settings of a mission (PRD-016, PAR rows `rotation` and `mission set`): the saved provider rotation and the execution settings, read and changed
// through the worker's typed operations `mission.rotation.get/put` and `mission.settings.get/patch`. The block reads only when it is opened, and every change can be undone
// from its toast by writing the earlier value back.
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

export function controlBlock(ctx: ControlCtx): Control {
  let loaded = false;
  let rotation: string[] = [];
  let settings: Settings = { auto_continue: false, suspend_when_rate_limited: false, teams: null, token_spend: "" };
  const body = h("div", { class: "disclosure-body stack" }, h("p", { class: "muted" }, "Loading..."));
  const el = h("details", { class: "disclosure", id: "mission-control" }, h("summary", {}, h("span", {}, "Rotation and settings")), body);

  async function load(): Promise<void> {
    const [rot, set] = await Promise.all([send(ctx, "mission.rotation.get", {}), send(ctx, "mission.settings.get", {})]);
    if (rot.outcome !== "applied" || set.outcome !== "applied") {
      body.replaceChildren(h("p", { class: "notice", role: "status" }, `Cannot load the rotation and settings. ${rot.message || set.message}`));
      loaded = false;
      return;
    }
    rotation = (Array.isArray(rot.result["entries"]) ? (rot.result["entries"] as unknown[]) : []).map((e) => String(record(e)["name"] ?? "")).filter(Boolean);
    const policy = record(set.result["execution_policy"]);
    settings = {
      auto_continue: set.result["auto_continue"] === true,
      suspend_when_rate_limited: set.result["suspend_when_rate_limited"] === true,
      teams: policy["legacy"] === true ? null : policy["teams_enabled"] === true,
      token_spend: policy["legacy"] === true ? "" : String(policy["token_spend"] ?? ""),
    };
    loaded = true;
    draw(String(rot.result["mode"] ?? "providers"), typeof rot.result["selected"] === "string" ? rot.result["selected"] : "");
  }

  function draw(mode: string, selected: string): void {
    const rotationInput = h("input", { id: "rotation-input", type: "text", value: rotation.join(","), autocomplete: "off", spellcheck: false });
    const rotationNote = h("p", { class: "muted small", id: "rotation-note", role: "status" }, `Order of the ${mode === "routes" ? "routes" : "providers"}, separated by commas.${selected ? ` In use now: ${selected}.` : ""}`);
    const saveRotation = h("button", { type: "button", class: "btn small", id: "rotation-save", "data-action": "rotation-save" }, h("span", {}, "Save rotation"));
    saveRotation.addEventListener("click", async () => {
      const next = rotationInput.value.split(",").map((s) => s.trim()).filter(Boolean);
      if (!next.length || next.join(",") === rotation.join(",")) return;
      const before = rotation.join(",");
      const r = await send(ctx, "mission.rotation.put", { set: next.join(",") });
      const undo: ToastAction = { label: "Undo", run: () => void send(ctx, "mission.rotation.put", { set: before }).then((u) => (reportOutcome(u, "Rotation restored", "restore the rotation"), load())) };
      reportOutcome(r, "Rotation saved", "save the rotation", undo);
      if (r.outcome === "applied" && typeof r.result["applies"] === "string") rotationNote.textContent = r.result["applies"];
      await load();
    });

    const auto = h("input", { id: "setting-auto-continue", type: "checkbox", checked: settings.auto_continue });
    const suspend = h("input", { id: "setting-suspend", type: "checkbox", checked: settings.suspend_when_rate_limited });
    const teams = h("input", { id: "setting-teams", type: "checkbox", checked: settings.teams === true, disabled: settings.teams === null });
    const spend = h("select", { id: "setting-spend", disabled: settings.token_spend === "" }, ...(settings.token_spend === "" ? [h("option", { value: "" }, "Not set for this mission")] : []), ...SPEND.map((v) => h("option", { value: v, selected: v === settings.token_spend }, v)));
    const saveSettings = h("button", { type: "button", class: "btn small", id: "settings-save", "data-action": "settings-save" }, h("span", {}, "Save settings"));
    saveSettings.addEventListener("click", async () => {
      const patch: Record<string, unknown> = {};
      const back: Record<string, unknown> = {};
      const note = (key: string, now: unknown, was: unknown): void => {
        if (now !== was) [patch[key], back[key]] = [now, was];
      };
      note("auto_continue", auto.checked, settings.auto_continue);
      note("suspend_when_rate_limited", suspend.checked, settings.suspend_when_rate_limited);
      if (settings.teams !== null) note("teams", teams.checked, settings.teams);
      if (settings.token_spend !== "") note("token_spend", spend.value, settings.token_spend);
      if (!Object.keys(patch).length) return;
      const r = await send(ctx, "mission.settings.patch", patch);
      const undo: ToastAction = { label: "Undo", run: () => void send(ctx, "mission.settings.patch", back).then((u) => (reportOutcome(u, "Settings restored", "restore the settings"), load())) };
      reportOutcome(r, "Settings saved", "save the settings", undo);
      await load();
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
