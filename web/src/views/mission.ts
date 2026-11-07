// The mission detail route (PRD-037 WP-3). The page is built from the modules in ./mission/ and loaded as its own chunk when the
// route opens, so it stays out of the first-load budget; skeleton rows show only if the chunk takes longer than 150 ms.
import { deferSkeleton } from "../dom";

export function missionDetail(root: HTMLElement, workerName: string, missionId: string): () => void {
  let dispose = (): void => {};
  let cancelled = false;
  const done = deferSkeleton(root, 4);
  void import("./mission/page")
    .then(({ missionPage }) => {
      done();
      if (!cancelled) dispose = missionPage(root, workerName, missionId);
    })
    .catch(() => done());
  return () => {
    cancelled = true;
    done();
    dispose();
  };
}
