import * as api from "../api";
import { button, copyButton, copyText, emptyState, field, h, hooks, keepFocus, statePill } from "../dom";
import { menu } from "../menu";
import { refresh, store, subscribe, workerDerived } from "../store";
import { shortId } from "../state";
import { announce, confirmDialog, openPanel } from "../ui";
import { ago, answerLine, onceDialog, plural, toggleDetails } from "./remote";

const NAME_RE = /^[a-z0-9][a-z0-9-]{0,62}$/;

function enrolDialog(name: string, token: string): void {
  const ws = `wss://${location.host}`;
  const cmd = `llm-bridge worker enroll --name ${name} --coordinator ${ws} --token-file - --ca-file ca.pem\nllm-bridge worker run`;
  const done = h("button", { type: "button", class: "btn primary", id: "enrol-done" }, "Done");
  const body = h(
    "div",
    {},
    h("p", {}, `Worker "${name}" is registered. Copy its token now: it is shown only once.`),
    h("div", { class: "secret" }, h("code", { id: "worker-token" }, token), copyButton(token, "token")),
    h("h3", {}, "Enrol it on the worker machine"),
    h("ol", { class: "steps" }, h("li", {}, "Copy ", h("code", {}, "ca.pem"), " from the coordinator data directory to the worker machine."), h("li", {}, "Run the commands below. Paste the token, press Enter, then Ctrl-D."), h("li", {}, "Add ", h("code", {}, "--allow-control"), " to let this web interface change missions, and ", h("code", {}, "--allow-terminal-view"), " to allow read-only terminals.")),
    h("pre", { class: "cmd" }, cmd),
    h("div", { class: "actions end" }, button("Copy commands", () => void copyText(cmd).then((ok) => hooks.say(ok ? "Copied." : "Could not copy.")), { icon: "copy" }), done),
  );
  const dlg = onceDialog({ id: "enrol-result", title: "Worker added", body, warning: "This token is shown once. If you close this window before copying it, remove the worker and add it again to get a new token." });
  done.addEventListener("click", () => dlg.close());
}

function addDialog(): void {
  const name = field("worker-name", "Worker name", "text", { autocapitalize: "none", spellcheck: "false", autocomplete: "off", required: true }, "Lowercase letters, digits and - (up to 63 characters), for example build-1.");
  const error = h("p", { class: "form-error box", role: "alert" });
  const submit = h("button", { type: "submit", class: "btn primary" }, "Add worker");
  const form = h("form", { novalidate: true }, h("div", { class: "dialog-body" }, name.wrap, error), h("div", { class: "actions end" }, h("button", { type: "button", class: "btn", onclick: () => (document.getElementById("add-worker") as HTMLDialogElement | null)?.close() }, "Cancel"), submit));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    error.textContent = "";
    const v = name.input.value.trim();
    if (!NAME_RE.test(v)) {
      name.setError("Use lowercase letters, digits and - only, starting with a letter or digit.");
      name.input.focus();
      return;
    }
    name.setError("");
    submit.disabled = true;
    try {
      const r = await api.post<{ name: string; token: string }>("/api/v1/workers", { name: v });
      (document.getElementById("add-worker") as HTMLDialogElement | null)?.close();
      announce(`Worker ${r.name} added.`, "applied");
      void refresh().catch(() => {});
      enrolDialog(r.name, r.token);
    } catch (err) {
      submit.disabled = false;
      if (err instanceof api.ApiError && err.code === "worker-exists") {
        name.setError("A worker with this name already exists. Choose another name.");
        name.input.focus();
      } else error.textContent = err instanceof api.ApiError ? err.message : "Unexpected error. Try again.";
    }
  });
  openPanel({ title: "Add a worker", id: "add-worker", body: form });
  name.input.focus();
}

async function revoke(name: string): Promise<void> {
  const ok = await confirmDialog({ title: `Remove worker ${name}?`, message: `Its connection is closed and its token stops working. Its missions disappear from this list. To use the machine again, add and enrol a worker again.`, confirmLabel: "Remove worker", danger: true });
  if (ok === null) return;
  try {
    await api.del(`/api/v1/workers/${encodeURIComponent(name)}`);
    announce(`Worker ${name} removed.`, "applied");
  } catch (e) {
    announce(`Worker ${name} was not removed: ${e instanceof api.ApiError ? e.message : "unexpected error"}`, "failed");
  }
  void refresh().catch(() => {});
}

function platformText(w: api.WorkerInfo): string {
  return w.platform ? [w.platform["system"], w.platform["machine"]].filter(Boolean).join(" ") : "";
}

function protocolText(p: unknown): string {
  return Array.isArray(p) ? p.join(", ") : p === undefined || p === null ? "" : String(p);
}

function workerRow(w: api.WorkerInfo): HTMLElement {
  const state = workerDerived(w);
  const offline = state.id === "offline";
  const missions = store.missions.filter((m) => m.worker === w.name).length;
  const plat = platformText(w);
  const seen = w.last_seen ? ago(w.last_seen, offline ? "last seen" : "seen") : h("span", { class: "nowrap muted" }, "never connected");
  const trigger = menu({ label: `Actions for worker ${w.name}`, kind: "ghost" }, [
    { label: "Copy ID", icon: "copy", onSelect: () => void copyText(w.id).then((ok) => hooks.say(ok ? "Copied worker ID." : "Could not copy the worker ID.")) },
    { label: "Remove worker", danger: true, onSelect: () => void revoke(w.name) },
  ]);
  const why = !offline
    ? null
    : w.last_seen
      ? h("p", {}, `The coordinator last heard from ${w.name} `, ago(w.last_seen), ". It reconnects by itself once its machine is on and ", h("code", {}, "llm-bridge worker run"), " is running there.")
      : h("p", {}, `${w.name} has never connected. Enrol it on its machine, then start it with `, h("code", {}, "llm-bridge worker run"), ".");
  const facts = h(
    "dl",
    { class: "facts" },
    h("dt", {}, "Software version"),
    h("dd", {}, w.versions.software ?? "-"),
    h("dt", {}, "Protocol"),
    h("dd", {}, protocolText(w.versions.protocol) || "-"),
    h("dt", {}, "Platform"),
    h("dd", {}, plat || "-"),
    h("dt", {}, "Worker ID"),
    h("dd", {}, w.id ? h("span", { class: "idchip" }, h("code", { title: w.id }, shortId(w.id)), copyButton(w.id, "worker ID")) : "-"),
  );
  const details = toggleDetails(offline ? "Why offline?" : "Details", h("div", {}, ...(why ? [why, facts] : [facts])));
  return h(
    "li",
    { class: "frow", "data-worker": w.name, "data-entity": `worker:${w.name}`, "data-l0": "workers.worker-row" },
    h("div", { class: "row-main" }, statePill(state), h("strong", { class: "nowrap" }, w.name), h("span", { class: "muted small row-seen" }, seen), h("span", { class: "row-end" }, trigger)),
    h(
      "div",
      { class: "row-sub small" },
      h(
        "span",
        { class: "row-facts" },
        h("span", { class: "nowrap muted" }, w.host_name || "unknown host"),
        h("span", { class: "nowrap muted" }, w.remote_address || "no address"),
        plat ? h("span", { class: "nowrap muted" }, plat) : null,
        missions > 0 ? h("a", { class: "row-link nowrap", href: `/missions?worker=${encodeURIComponent(w.name)}` }, plural(missions, "mission")) : h("span", { class: "nowrap muted" }, "No missions"),
      ),
      h("span", { class: "row-end" }, details.button),
    ),
    details.panel,
  );
}

export function workers(root: HTMLElement): () => void {
  const body = h("div", { id: "workers-body" });
  const add = button("Add worker", addDialog, { icon: "plus" });
  add.id = "add-worker-button";
  const answers = answerLine({ what: "Reading workers.", needMe: "Nothing needs you.", next: "This takes a moment." });
  root.append(h("div", { class: "fleet-head" }, h("h1", {}, "Workers"), add), answers.el, body);

  const renderNow = () => {
    // One primary button per page: the header's while there are workers, else the empty state's.
    add.classList.toggle("primary", store.workers.length > 0);
    if (store.workers.length === 0) {
      answers.set(store.loaded ? { what: "No workers are enrolled.", needMe: "A worker has to be enrolled before missions can run.", next: "Add one, then enrol it on its machine." } : { what: "Reading workers.", needMe: "Nothing needs you.", next: "This takes a moment." });
      body.replaceChildren(
        store.loaded
          ? emptyState({ icon: "workers", title: "No workers enrolled", text: "A worker runs missions on a machine and reports them here. Add one, then enrol it on that machine with the command shown afterwards.", action: h("div", { class: "actions" }, button("Add worker", addDialog, { kind: "primary", icon: "plus" })) })
          : h("p", { class: "muted" }, "Loading..."),
      );
      return;
    }
    const sorted = [...store.workers].sort((a, b) => workerDerived(a).rank - workerDerived(b).rank || a.name.localeCompare(b.name));
    const offline = sorted.filter((w) => workerDerived(w).id === "offline");
    const online = sorted.length - offline.length;
    answers.set({
      what: `${online} of ${plural(sorted.length, "worker")} online.`,
      needMe: offline.length === 0 ? "Nothing needs you." : `${offline.map((w) => w.name).join(", ")} ${offline.length === 1 ? "is" : "are"} offline.`,
      next: offline.length === 0 ? "Nothing to do." : `Start llm-bridge worker run on ${offline[0]?.host_name || offline[0]?.name}.`,
    });
    body.replaceChildren(h("ul", { class: "frows", "aria-label": "Workers", "data-section": "workers" }, ...sorted.map(workerRow)));
  };
  const render = () => keepFocus(body, renderNow);
  const un = subscribe(render);
  render();
  return un;
}
