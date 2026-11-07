// The mission detail route. The page is built from the modules in ./mission/ (PRD-037 WP-3).
import { missionPage } from "./mission/page";

export function missionDetail(root: HTMLElement, workerName: string, missionId: string): () => void {
  return missionPage(root, workerName, missionId);
}
