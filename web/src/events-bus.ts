// New events of the open mission, as the mission page reads them from the coordinator after a change notice. Only the lazy mission page and the steer chips use it.
import type { BridgeEvent } from "./api";

const listeners = new Set<(events: BridgeEvent[]) => void>();
export function onEvents(fn: (events: BridgeEvent[]) => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}
export function publishEvents(events: BridgeEvent[]): void {
  for (const listener of [...listeners]) listener(events);
}
