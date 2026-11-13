// Groups of the Missions list (PRD-014): the question the overview answers first is "what needs me, what is rate-limited, which worker is unhealthy".
// Pure (no DOM): the list draws one heading per non-empty group, in the order of `GROUPS`.
import type { DerivedState, StateId } from "./state";

export type GroupId = "attention" | "rate-limited" | "unhealthy" | "running" | "idle";

/** Display order and heading of each group. */
export const GROUPS: ReadonlyArray<{ id: GroupId; title: string }> = [
  { id: "attention", title: "Needs you" },
  { id: "unhealthy", title: "Worker unhealthy" },
  { id: "rate-limited", title: "Rate-limited" },
  { id: "running", title: "Running" },
  { id: "idle", title: "Paused and queued" },
];

const BY_STATE: Partial<Record<StateId, GroupId>> = { waiting: "rate-limited", offline: "unhealthy", running: "running" };

/** The group of a mission state. Finished states (done, stopped) have their own collapsed section and never reach this. */
export function groupOf(state: DerivedState): GroupId {
  if (state.tier === "alert") return "attention";
  return BY_STATE[state.id] ?? "idle";
}

export const groupRank = (id: GroupId): number => GROUPS.findIndex((g) => g.id === id);

/** One entry of the flat list: a group heading or a mission. */
export type Flat<T> = { kind: "head"; group: GroupId; title: string; count: number } | { kind: "row"; item: T };

/** The items (already in display order, so the members of a group are adjacent) with a heading in front of each group. */
export function withHeadings<T>(items: T[], groupOfItem: (item: T) => GroupId): Flat<T>[] {
  const counts = new Map<GroupId, number>();
  for (const i of items) counts.set(groupOfItem(i), (counts.get(groupOfItem(i)) ?? 0) + 1);
  const out: Flat<T>[] = [];
  let current: GroupId | null = null;
  for (const item of items) {
    const g = groupOfItem(item);
    if (g !== current) {
      current = g;
      out.push({ kind: "head", group: g, title: GROUPS[groupRank(g)]!.title, count: counts.get(g) ?? 0 });
    }
    out.push({ kind: "row", item });
  }
  return out;
}
