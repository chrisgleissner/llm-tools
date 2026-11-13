// Helpers shared by the performance specs (uxr-perf.spec.ts and uxr-typing.spec.ts): CPU throttling through CDP and the Event Timing
// and layout-shift observers that stand in for INP and CLS in the lab.
import type { CDPSession, Page } from "@playwright/test";
import { viewport } from "./harness";

export async function throttle(page: Page): Promise<CDPSession> {
  const cdp = await page.context().newCDPSession(page);
  await cdp.send("Emulation.setCPUThrottlingRate", { rate: 4 });
  return cdp;
}
export const onPixel4 = (page: Page, project: string): boolean => project === "chromium-mobile" && viewport(page).width <= 400;

export const OBSERVE = `
(() => {
  const w = window;
  w.__ev = [];
  new PerformanceObserver((list) => { for (const e of list.getEntries()) w.__ev.push({ name: e.name, start: e.startTime, duration: e.duration }); }).observe({ type: "event", durationThreshold: 16, buffered: true });
  w.__cls = 0;
  new PerformanceObserver((list) => { for (const e of list.getEntries()) if (!e.hadRecentInput) w.__cls += e.value; }).observe({ type: "layout-shift", buffered: true });
})();
`;

export const now = (page: Page) => page.evaluate(() => performance.now());
export const p95 = (xs: number[]): number => [...xs].sort((a, b) => a - b)[Math.max(0, Math.ceil(xs.length * 0.95) - 1)] ?? 0;
