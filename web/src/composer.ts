// The composer bar of the session pane (spec 8.2): Take control, a message field that types into the live terminal, the Steer
// menu for the typed `mission.steer` kinds, and the chips of steering that is queued but not yet applied.
import type * as api from "./api";
import { button, h } from "./dom";
import { menu } from "./menu";

export interface ComposerOpts {
  workerName: string;
  missionId: string;
  provider?: string;
  /** Send text to the terminal. Returns whether the socket accepted it; the field keeps its text when it did not. */
  send(text: string): boolean;
  takeControl(): void;
}

export interface ComposerView {
  /** The operator holds the writer role and the socket is open. */
  canType: boolean;
  /** Take control is offered (not while controlling). */
  offerControl: boolean;
  /** Why Take control is disabled; empty when it can be pressed. */
  takeReason: string;
  /** The hint under the field. */
  note: string;
}

export interface Composer {
  element: HTMLElement;
  set(view: ComposerView): void;
  focus(): boolean;
  dispose(): void;
}

const KINDS: Array<[api.SteerKind, string]> = [
  ["review", "Review a task"],
  ["verify", "Verify a task"],
  ["replace", "Replace a task prompt"],
  ["pause-after-iteration", "Pause after this iteration"],
  ["stop-after-task", "Stop after a task"],
  ["handoff", "Hand off to a provider"],
];

export function composer(o: ComposerOpts): Composer {
  const input = h("textarea", { class: "composer-field", rows: 1, "aria-label": o.provider ? `Message ${o.provider}` : "Message to the session", "aria-describedby": "composer-note", autocomplete: "off", spellcheck: false });
  const note = h("p", { class: "composer-note small muted", id: "composer-note" });
  const take = button("Take control", () => o.takeControl(), { kind: "primary", action: "take-control", id: "terminal-control" });
  const sendBtn = button("Send", () => submit(), { disabled: true });
  const chips = h("div", { class: "cluster", "data-queued-steer": "", hidden: true });
  const grow = (): void => {
    input.style.blockSize = "auto";
    input.style.blockSize = `${Math.min(input.scrollHeight, 100)}px`;
  };
  function submit(): void {
    const text = input.value;
    if (input.disabled || !text.trim() || !o.send(text)) return;
    input.value = "";
    if (!sizes) grow();
    sendBtn.disabled = true;
  }
  // Chromium sizes the field itself (field-sizing in session.css); elsewhere measure on input.
  const sizes = CSS.supports("field-sizing", "content");
  input.addEventListener("input", () => {
    if (!sizes) grow();
    sendBtn.disabled = input.disabled || !input.value.trim();
  });
  input.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || e.shiftKey || e.isComposing) return;
    e.preventDefault();
    submit();
  });

  const stops: Array<() => void> = [];
  // Fetched when the composer mounts, so a dialog opens in the same task as the click that chose the item.
  const loading = import("./session-extras");
  const steer = (kind: api.SteerKind, label: string): void => void loading.then((m) => m.runSteer({ ...o, chips, own: (stop) => stops.push(stop) }, kind, label));
  const steerMenu = menu({ label: "Steer the mission", text: "Steer" }, KINDS.map(([kind, label]) => ({ label, id: `steer-${kind}`, onSelect: () => steer(kind, label) })));
  // The menu builds its items when it opens; tag them with the steering kind they send.
  const tag = (): void => document.querySelectorAll('[role="menuitem"][id^="steer-"]').forEach((e) => e.setAttribute("data-steer", e.id.slice(6)));
  steerMenu.addEventListener("click", tag);
  steerMenu.addEventListener("keydown", tag);

  const element = h("div", { class: "composer", "data-composer": "" }, chips, h("div", { class: "cluster" }, take, input, sendBtn, steerMenu), note);
  return {
    element,
    set(v): void {
      const wasTyping = !input.disabled;
      input.disabled = !v.canType;
      input.placeholder = v.canType ? "Message the session" : "";
      sendBtn.disabled = !v.canType || !input.value.trim();
      take.hidden = !v.offerControl;
      take.disabled = Boolean(v.takeReason);
      take.title = v.takeReason;
      note.textContent = v.note;
      note.hidden = !v.note;
      if (v.canType && !wasTyping) input.focus();
    },
    focus(): boolean {
      if (input.disabled) return false;
      input.focus();
      return true;
    },
    dispose(): void {
      for (const stop of stops) stop();
    },
  };
}
