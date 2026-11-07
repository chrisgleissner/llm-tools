// "About" (spec 8.1, level 1): folder, worker host and address, created time, and the full mission id with Copy.
import type { WorkerInfo } from "../../api";
import { disclosure, h, idChip, timeEl } from "../../dom";

export interface AboutModel {
  missionId: string;
  folder: string;
  workerName: string;
  worker: WorkerInfo | undefined;
  createdAt: string | null;
}

export interface About {
  el: HTMLDetailsElement;
  update(m: AboutModel): void;
}

export function aboutBlock(): About {
  const el = disclosure("About", h("p", { class: "muted" }, "Loading..."), { id: "mission-about" });
  const body = el.querySelector(".disclosure-body") as HTMLElement;
  return {
    el,
    update(m) {
      const rows: Array<[string, Node | string]> = [];
      if (m.folder) rows.push(["Folder", h("code", { class: "long-text" }, m.folder)]);
      const w = m.worker;
      rows.push(["Worker", [m.workerName, w?.host_name, w?.remote_address].filter(Boolean).join(" · ")]);
      if (m.createdAt) rows.push(["Created", timeEl(m.createdAt)]);
      rows.push(["Mission ID", idChip(m.missionId, "mission ID")]);
      body.replaceChildren(h("dl", { class: "about-fields" }, ...rows.flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v)])));
    },
  };
}
