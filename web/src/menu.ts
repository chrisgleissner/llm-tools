// Overflow menu (spec 6.2). One component for the account menu, the mission header menu, the task row menu and the Inbox row menu.
// A menu holds level-1 actions only: anything needed in the current state (Approve, Pause, Stop, Retry) stays a visible button.
import { h, liveOf } from "./dom";
import { icon, type IconName } from "./icons";
import { openPanel } from "./ui";

export interface MenuItem {
  label: string;
  onSelect: () => void;
  icon?: IconName;
  /** Destructive items are placed last, after a separator, and drawn in the danger colour. */
  danger?: boolean;
  disabled?: boolean;
  /** Why the item is disabled; shown beside it. */
  reason?: string;
  id?: string;
  /** Extra attributes on the item button, for example `{ "data-steer": "replace" }`. */
  attrs?: Record<string, string>;
}

export interface MenuTrigger {
  /** Accessible name; also the visible text when `text` is set. */
  label: string;
  /** Visible text instead of an icon-only button. */
  text?: string;
  icon?: IconName;
  id?: string;
  /** Extra classes for the trigger button (`ghost`, `small`). */
  kind?: string;
}

const PHONE = "(max-width: 47.99rem)";

function ordered(items: MenuItem[]): Array<MenuItem | "separator"> {
  const normal = items.filter((i) => !i.danger);
  const danger = items.filter((i) => i.danger);
  return danger.length && normal.length ? [...normal, "separator", ...danger] : [...normal, ...danger];
}

function itemButton(item: MenuItem, close: () => void): HTMLButtonElement {
  const b = h(
    "button",
    { type: "button", role: "menuitem", class: item.danger ? "menu-item danger" : "menu-item", id: item.id, disabled: item.disabled, tabindex: "-1" },
    item.icon ? icon(item.icon, 18) : null,
    h("span", {}, item.label),
    item.disabled && item.reason ? h("span", { class: "why" }, item.reason) : null,
  );
  if (item.disabled && item.reason) b.title = item.reason;
  for (const [name, value] of Object.entries(item.attrs ?? {})) b.setAttribute(name, value);
  b.addEventListener("click", () => {
    close();
    item.onSelect();
  });
  return b;
}

/** Arrow, Home and End keys over the enabled items of a menu element. Escape and Tab are handled by the caller. */
function navigate(list: HTMLElement, e: KeyboardEvent): void {
  const items = [...list.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not(:disabled)')];
  if (items.length === 0) return;
  const at = items.indexOf(document.activeElement as HTMLButtonElement);
  let next = -1;
  if (e.key === "ArrowDown") next = at < 0 ? 0 : (at + 1) % items.length;
  else if (e.key === "ArrowUp") next = at <= 0 ? items.length - 1 : at - 1;
  else if (e.key === "Home") next = 0;
  else if (e.key === "End") next = items.length - 1;
  if (next < 0) return;
  e.preventDefault();
  items[next]?.focus();
}

/**
 * `menu(trigger, items)`: a trigger button with `aria-haspopup="menu"` and `aria-expanded`, and a popover with `role="menu"`.
 * Enter, Space or ArrowDown opens it; arrows, Home and End move; Escape closes and returns focus to the trigger. Below 48rem
 * it opens as a bottom sheet.
 */
export function menu(trigger: MenuTrigger, items: MenuItem[]): HTMLElement {
  const btn = trigger.text
    ? h("button", { type: "button", class: `btn ${trigger.kind ?? ""}`.trim(), id: trigger.id, "aria-haspopup": "menu", "aria-expanded": "false", "aria-label": trigger.label }, trigger.icon ? icon(trigger.icon, 18) : null, h("span", {}, trigger.text))
    : h("button", { type: "button", class: `btn icon ${trigger.kind ?? "ghost"}`.trim(), id: trigger.id, "aria-haspopup": "menu", "aria-expanded": "false", "aria-label": trigger.label, title: trigger.label }, icon(trigger.icon ?? "more", 20));
  const wrap = h("div", { class: "menu" }, btn);
  // A refresh that patches the row in place hands the new items to this menu (see `morph`): the items are read when the menu opens.
  liveOf(wrap)["items"] = items;
  let pop: HTMLElement | null = null;
  let sheet: HTMLDialogElement | null = null;

  const build = (close: () => void): HTMLElement => {
    const list = h("div", { role: "menu", "aria-label": trigger.label });
    for (const entry of ordered(liveOf(wrap)["items"] as MenuItem[])) list.append(entry === "separator" ? h("hr", { class: "menu-sep", role: "separator" }) : itemButton(entry, close));
    list.addEventListener("keydown", (e) => navigate(list, e));
    return list;
  };

  const close = (restoreFocus = true): void => {
    if (sheet) {
      const dlg = sheet;
      sheet = null;
      dlg.close();
    }
    if (pop) {
      pop.remove();
      pop = null;
      document.removeEventListener("pointerdown", outside, true);
      document.removeEventListener("keydown", onKey, true);
    }
    btn.setAttribute("aria-expanded", "false");
    if (restoreFocus) btn.focus({ preventScroll: true });
  };
  const outside = (e: Event) => {
    if (!wrap.contains(e.target as Node)) close(false);
  };
  const onKey = (e: KeyboardEvent) => {
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      close();
    } else if (e.key === "Tab") close(false);
  };

  const open = (focusLast = false): void => {
    if (pop || sheet) return;
    btn.setAttribute("aria-expanded", "true");
    if (window.matchMedia(PHONE).matches) {
      const list = build(() => close(false));
      sheet = openPanel({ title: trigger.label, id: "menu-sheet", body: list, sheet: true });
      sheet.addEventListener("close", () => {
        sheet = null;
        btn.setAttribute("aria-expanded", "false");
      });
      (list.querySelector<HTMLElement>('[role="menuitem"]:not(:disabled)'))?.focus();
      return;
    }
    pop = h("div", { class: "menu-popover", "data-keep": "" }, build(() => close(false)));
    wrap.append(pop);
    document.addEventListener("pointerdown", outside, true);
    document.addEventListener("keydown", onKey, true);
    const enabled = [...pop.querySelectorAll<HTMLElement>('[role="menuitem"]:not(:disabled)')];
    (focusLast ? enabled.at(-1) : enabled[0])?.focus();
  };

  btn.addEventListener("click", () => (pop || sheet ? close() : open()));
  btn.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" && !pop && !sheet) {
      e.preventDefault();
      open();
    } else if (e.key === "ArrowUp" && !pop && !sheet) {
      e.preventDefault();
      open(true);
    }
  });
  return wrap;
}
