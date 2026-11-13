// Interaction counter for the click-path tests (T12, T13, T20, T21, T25, UXR-044).
//
// Counts what the brief counts: a trusted pointerdown is one interaction, a run of consecutive character keydowns is
// one typing interaction, and every other key press that is not a bare modifier (Enter, Tab, Escape, arrows, a
// Ctrl/Cmd combination) is one interaction. Events that the page dispatches itself (isTrusted false) are not counted.
import type { Page } from "@playwright/test";

export interface Interactions {
  /** Start counting from zero (call after the page has loaded). */
  reset(): Promise<void>;
  count(): Promise<number>;
  /** Kinds in order: "pointer", "type", "key". */
  log(): Promise<string[]>;
}

const SCRIPT = `
(() => {
  const w = window;
  w.__ix = { log: [], last: "" };
  const add = (kind) => { w.__ix.log.push(kind); w.__ix.last = kind; };
  addEventListener("pointerdown", (e) => { if (e.isTrusted) add("pointer"); }, true);
  addEventListener("keydown", (e) => {
    if (!e.isTrusted) return;
    if (["Shift", "Control", "Alt", "Meta", "CapsLock"].includes(e.key)) return;
    const printable = e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey;
    if (printable) { if (w.__ix.last !== "type") add("type"); return; }
    add("key");
  }, true);
})();
`;

export async function trackInteractions(page: Page): Promise<Interactions> {
  await page.addInitScript(SCRIPT);
  const get = () => page.evaluate(() => ((window as unknown as { __ix?: { log: string[] } }).__ix?.log ?? []).slice());
  return {
    reset: async () => {
      await page.evaluate(() => { const ix = (window as unknown as { __ix: { log: string[]; last: string } }).__ix; ix.log = []; ix.last = ""; });
    },
    count: async () => (await get()).length,
    log: get,
  };
}
