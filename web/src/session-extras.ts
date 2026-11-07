// Parts of the session pane that are needed only after an interaction: full screen, the soft-keyboard layout, the resize
// confirmation and the transcript text. A separate chunk keeps them out of the terminal chunk's size budget.
import type { Terminal } from "@xterm/xterm";
import { h } from "./dom";
import { confirmDialog } from "./ui";

export const transcriptText = (t: Terminal): string => {
  const b = t.buffer.active;
  const lines: string[] = [];
  for (let i = 0; i < b.length; i++) lines.push(b.getLine(i)?.translateToString(true) ?? "");
  while (lines.length && !lines[lines.length - 1]) lines.pop();
  return lines.join("\n");
};

/** The explicit "resize the shared session to this screen" confirmation. */
export async function confirmResize(): Promise<boolean> {
  const answer = confirmDialog({ title: "Resize the session to this screen?", message: "Other viewers and the tmux station will change size.", confirmLabel: "Resize" });
  document.getElementById("dialog-confirm")?.setAttribute("data-confirm", "");
  return (await answer) !== null;
}

export interface Mode {
  dispose(): void;
}

/**
 * Full screen: the pane covers the viewport and the shell chrome is removed (CSS on `body[data-session-fs]`). Leave with the button,
 * Back or Escape; focus returns to the button. `keepEscape` says when Escape belongs to the terminal.
 */
export function fullScreen(button: HTMLButtonElement, keepEscape: () => boolean): Mode & { set(on: boolean): void; on(): boolean } {
  let on = false;
  let pushed = false;
  let expecting = false; // our own history.back() is in flight
  const set = (next: boolean): void => {
    if (next === on) return;
    on = next;
    document.body.toggleAttribute("data-session-fs", on);
    button.querySelector("span")!.textContent = on ? "Exit full screen" : "Expand";
    button.setAttribute("aria-label", on ? "Exit full screen" : "Expand to full screen");
    button.setAttribute("aria-pressed", String(on));
    if (on) {
      history.pushState({ sessionFs: true }, "");
      pushed = true;
    } else if (pushed) {
      pushed = false;
      expecting = true;
      history.back();
    }
    button.focus({ preventScroll: true });
  };
  const onKey = (e: KeyboardEvent): void => {
    if (e.key === "Escape" && on && !document.querySelector("dialog[open], .menu-popover") && !keepEscape()) set(false);
  };
  const onPop = (e: PopStateEvent): void => {
    if (!on && !expecting) return;
    e.stopImmediatePropagation(); // Back leaves full screen; it must not re-render the page behind it
    if (expecting) return void (expecting = false);
    pushed = false;
    set(false);
  };
  window.addEventListener("popstate", onPop, true);
  document.addEventListener("keydown", onKey);
  return {
    set,
    on: () => on,
    dispose(): void {
      window.removeEventListener("popstate", onPop, true);
      document.removeEventListener("keydown", onKey);
      document.body.removeAttribute("data-session-fs");
    },
  };
}

/**
 * While the composer or the terminal has focus on a touch device the soft keyboard is open: the shell hides its bottom bar and
 * collapses the header (`body[data-kb]`). On iOS the layout viewport does not shrink, so `--vvh` carries the visible height.
 * `--composer-h` is the composer's height, for scroll padding.
 */
export function keyboardLayout(pane: HTMLElement, composer: HTMLElement, narrow: MediaQueryList): Mode {
  const root = document.documentElement;
  const vv = window.visualViewport;
  const paint = (): void => {
    if (vv && document.body.hasAttribute("data-kb")) root.style.setProperty("--vvh", `${vv.height}px`);
  };
  const update = (): void => {
    const open = narrow.matches && pane.contains(document.activeElement) && Boolean(document.activeElement?.matches("textarea, input"));
    if (open === document.body.hasAttribute("data-kb")) return;
    document.body.toggleAttribute("data-kb", open);
    if (open) paint();
    else root.style.removeProperty("--vvh");
  };
  const out = (): number => window.setTimeout(update, 0);
  pane.addEventListener("focusin", update);
  pane.addEventListener("focusout", out);
  vv?.addEventListener("resize", paint);
  const sizer = new ResizeObserver(() => root.style.setProperty("--composer-h", `${composer.offsetHeight}px`));
  sizer.observe(composer);
  update();
  return {
    dispose(): void {
      pane.removeEventListener("focusin", update);
      pane.removeEventListener("focusout", out);
      vv?.removeEventListener("resize", paint);
      sizer.disconnect();
      document.body.removeAttribute("data-kb");
      root.style.removeProperty("--vvh");
      root.style.removeProperty("--composer-h");
    },
  };
}

export interface KeyIo {
  write(bytes: string): void;
  appKeys(): boolean;
  focus(): void;
  writer(): boolean;
}

/** The on-screen key bar: six keys and "...", which opens a second row. An empty byte string marks a sticky modifier. */
export function keyBar(bar: HTMLElement, io: KeyIo): { enable(on: boolean, why: string): void; typed(d: string): string } {
  const extra = h("div", { class: "keybar-row", id: "terminal-keys-more", hidden: true });
  let armed = "";
  const arm = (next: string): void => {
    armed = next;
    for (const k of ["ctrl", "alt"]) document.getElementById(`key-${k}`)?.setAttribute("aria-pressed", String(armed === k));
  };
  const add = (to: HTMLElement, keys: string[][]): void => {
    for (const [id = "", text = "", label = "", bytes = ""] of keys) {
      const b = h("button", { type: "button", class: "btn key", id: `key-${id}`, "aria-label": label }, text);
      b.addEventListener("mousedown", (e) => e.preventDefault());
      b.addEventListener("click", () => {
        if (!io.writer()) return;
        if (!bytes) arm(armed === id ? "" : id);
        else io.write(/^[A-D]$/.test(bytes) ? `${io.appKeys() ? "\x1bO" : "\x1b["}${bytes}` : bytes);
        io.focus();
      });
      if (!bytes) b.setAttribute("aria-pressed", "false");
      to.append(b);
    }
  };
  add(bar, [["esc", "Esc", "Escape", "\x1b"], ["tab", "Tab", "Tab", "\t"], ["ctrl", "Ctrl", "Ctrl, applies to the next key"], ["alt", "Alt", "Alt, applies to the next key"], ["up", "\u2191", "Arrow up", "A"], ["down", "\u2193", "Arrow down", "B"]]);
  const more = h("button", { type: "button", class: "btn more", id: "key-more", "aria-label": "More keys", "aria-expanded": "false", "aria-controls": "terminal-keys-more" }, "...");
  more.addEventListener("mousedown", (e) => e.preventDefault());
  more.addEventListener("click", () => more.setAttribute("aria-expanded", String((extra.hidden = !extra.hidden) === false)));
  add(extra, [["left", "\u2190", "Arrow left", "D"], ["right", "\u2192", "Arrow right", "C"], ["pipe", "|", "Pipe", "|"], ["tilde", "~", "Tilde", "~"], ["slash", "/", "Slash", "/"], ["dash", "-", "Dash", "-"], ["pgup", "PgUp", "Page up", "\x1b[5~"], ["pgdn", "PgDn", "Page down", "\x1b[6~"]]);
  bar.append(more, extra);
  return {
    enable(on, why): void {
      for (const b of bar.querySelectorAll<HTMLButtonElement>(".key")) {
        b.disabled = !on;
        b.title = on ? "" : why;
      }
      if (!on) arm("");
    },
    /** Typed text with an armed Ctrl (Ctrl+C is 0x03) or Alt (Escape prefix) applied to a single character. */
    typed(d): string {
      if (d.length !== 1) return d;
      const out = armed === "ctrl" && /[A-Za-z@[\\\]^_]/.test(d) ? String.fromCharCode(d.toUpperCase().charCodeAt(0) & 0x1f) : armed === "alt" ? `\x1b${d}` : d;
      arm("");
      return out;
    },
  };
}
