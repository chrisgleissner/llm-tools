// Gating for the PRD-037 acceptance specs (WP-6).
//
// Every spec is written against the DOM contract in docs/distributed/web-design-spec.md (section 11.3) before the pages
// exist. A test whose pages have not landed is registered with `test.fixme` and carries the work packages that unblock
// it, so it is listed (never silently skipped) and a page builder only has to flip it on:
//
//   touch web/e2e/redesign/landed/WP-3        # in the commit that lands work package WP-3
//
// A test runs when every work package it needs has a file in `landed/` (one file per work package, so parallel
// builders never edit the same line). Overrides for local checks:
//   UXR_LANDED=WP-0,WP-1   treat these packages as landed
//   UXR_FORCE=1            run every fixme test (validation: shows which contract assertion fails first)
// Select by package with `--grep @WP-3`, by test with `--grep @T10`, by row with `--grep @UXR-004`.
import { test as base, type PlaywrightTestArgs, type PlaywrightTestOptions, type PlaywrightWorkerArgs, type PlaywrightWorkerOptions, type TestInfo } from "@playwright/test";
import { existsSync } from "node:fs";
import { join } from "node:path";
import { test as withCoordinator, type Coordinator } from "../fixtures";

export type Wp = "WP-0" | "WP-1" | "WP-2" | "WP-3" | "WP-4" | "WP-5" | "WP-6" | "lead";

const landedDir = join(import.meta.dirname, "landed");
const fromEnv = new Set((process.env["UXR_LANDED"] ?? "").split(",").map((s) => s.trim()).filter(Boolean));

export function isLanded(wp: Wp): boolean {
  if (wp === "WP-6") return true; // this harness
  return process.env["UXR_FORCE"] === "1" || fromEnv.has(wp) || existsSync(join(landedDir, wp));
}

export interface Meta {
  /** Acceptance test id from the spec, "T10". */
  t: string;
  /** UXR rows this test proves, ["UXR-004"]. */
  uxr: string[];
  /** Work packages that must have landed before the test can pass. */
  needs: Wp[];
}

type Base = PlaywrightTestArgs & PlaywrightTestOptions & PlaywrightWorkerArgs & PlaywrightWorkerOptions;
type Args = Base & { coord: Coordinator };
type StaticArgs = Base;

function details(meta: Meta) {
  return {
    tag: [`@${meta.t}`, ...meta.uxr.map((u) => `@${u}`), ...meta.needs.map((w) => `@${w}`)],
    annotation: meta.needs.map((w) => ({ type: "needs", description: w })),
  };
}

function titleOf(meta: Meta, title: string): string {
  return `${meta.t} ${meta.uxr.join(" ")}: ${title}`;
}

/** A test that needs the real coordinator (it serves the built application) and a page. */
export function uxr(meta: Meta, title: string, body: (args: Args, info: TestInfo) => Promise<void>): void {
  const run = meta.needs.every(isLanded);
  const name = run ? titleOf(meta, title) : `${titleOf(meta, title)} [fixme until ${meta.needs.filter((w) => !isLanded(w)).join(", ")}]`;
  if (run) withCoordinator(name, details(meta), body);
  else withCoordinator.fixme(name, details(meta), body);
}

/** A test that reads files only (no coordinator, no browser page). */
export function uxrStatic(meta: Meta, title: string, body: (args: StaticArgs, info: TestInfo) => Promise<void>): void {
  const run = meta.needs.every(isLanded);
  const name = run ? titleOf(meta, title) : `${titleOf(meta, title)} [fixme until ${meta.needs.filter((w) => !isLanded(w)).join(", ")}]`;
  if (run) base(name, details(meta), body);
  else base.fixme(name, details(meta), body);
}

export { expect } from "../fixtures";
