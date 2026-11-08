// "About" (spec 8.1, level 1): folder, worker host and address, created time, and the full mission id with Copy.
import type { WorkerInfo } from "../../api";
import { h, idChip, timeEl } from "../../dom";
import { fields } from "./kit";

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
  // Built here, not with `disclosure()`, so the one-word label sits in its own span: a summary that holds bare text is as tall as its tap target.
  const body = h("div", { class: "disclosure-body" }, h("p", { class: "muted" }, "Loading..."));
  const el = h("details", { class: "disclosure", id: "mission-about" }, h("summary", {}, h("span", {}, "About")), body);
  return {
    el,
    update(m) {
      const rows: Array<[string, Node | string]> = [];
      if (m.folder) rows.push(["Folder", h("code", { class: "long-text" }, m.folder)]);
      const w = m.worker;
      rows.push(["Worker", [m.workerName, w?.host_name, w?.remote_address].filter(Boolean).join(" · ")]);
      if (m.createdAt) rows.push(["Created", timeEl(m.createdAt)]);
      rows.push(["Mission ID", idChip(m.missionId, "mission ID")]);
      body.replaceChildren(fields("about-fields", rows));
    },
  };
}
