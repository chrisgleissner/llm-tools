import * as api from "../api";
import { button, copyButton, emptyState, field, h, idChip, keepFocus, timeEl } from "../dom";
import { icon } from "../icons";
import { refresh, store, subscribe, workerState } from "../store";
import { announce, confirmDialog, openPanel } from "../ui";

const NAME_RE = /^[a-z0-9][a-z0-9-]{0,62}$/;

function enrolDialog(name: string, token: string): void {
  const ws = `wss://${location.host}`;
  const cmd = `llm-bridge worker enroll --name ${name} --coordinator ${ws} --token-file - --ca-file ca.pem\nllm-bridge worker run`;
  const body = h(
    "div",
    {},
    h("p", {}, `Worker "${name}" is registered. Copy its token now: it is shown only once.`),
    h("div", { class: "secret" }, h("code", { id: "worker-token" }, token), copyButton(token, "token")),
    h("h3", {}, "Enrol it on the worker machine"),
    h("ol", { class: "steps" }, h("li", {}, "Copy ", h("code", {}, "ca.pem"), " from the coordinator data directory to the worker machine."), h("li", {}, "Run the commands below. Paste the token, press Enter, then Ctrl-D."), h("li", {}, "Add ", h("code", {}, "--allow-control"), " to let this web interface change missions, and ", h("code", {}, "--allow-terminal-view"), " to allow read-only terminals.")),
    h("pre", { class: "cmd" }, cmd),
    h("div", { class: "actions end" }, copyButton(cmd, "commands"), h("button", { type: "button", class: "btn primary", onclick: () => (document.getElementById("enrol-result") as HTMLDialogElement | null)?.close() }, "Done")),
  );
  openPanel({ title: "Worker added", id: "enrol-result", body });
}

function addDialog(): void {
  const name = field("worker-name", "Worker name", "text", { autocapitalize: "none", spellcheck: "false", autocomplete: "off", required: true }, "Lowercase letters, digits and - (up to 63 characters), for example build-1.");
  const error = h("p", { class: "form-error box", role: "alert" });
  const submit = h("button", { type: "submit", class: "btn primary" }, "Add worker");
  const form = h("form", { novalidate: true }, name.wrap, error, h("div", { class: "actions end" }, h("button", { type: "button", class: "btn", onclick: () => (document.getElementById("add-worker") as HTMLDialogElement | null)?.close() }, "Cancel"), submit));
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

export function workers(root: HTMLElement): () => void {
  const body = h("div", {});
  const add = button("Add worker", addDialog, { kind: "primary", icon: "plus" });
  add.id = "add-worker-button";
  root.append(h("div", { class: "head-row" }, h("h1", {}, "Workers"), add), body);

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

  const renderNow = () => {
    if (store.workers.length === 0) {
      body.replaceChildren(
        store.loaded
          ? emptyState({ icon: "workers", title: "No workers enrolled", text: "A worker runs missions on a machine and reports them here. Add one, then enrol it on that machine with the command shown afterwards.", action: h("div", { class: "actions" }, button("Add worker", addDialog, { kind: "primary", icon: "plus" })) })
          : h("p", { class: "muted" }, "Loading..."),
      );
      return;
    }
    body.replaceChildren(
      h(
        "div",
        { class: "table-wrap rt", role: "region", "aria-label": "Workers table", tabindex: "0" },
        h(
          "table",
          { id: "workers-table", class: "rt" },
          h("caption", { class: "sr-only" }, "Workers"),
          h("thead", {}, h("tr", {}, ...["Name", "State", "Last seen", "Version", "Platform", "Missions", "Actions"].map((t) => h("th", { scope: "col" }, t)))),
          h(
            "tbody",
            {},
            ...store.workers.map((w) => {
              const n = store.missions.filter((m) => m.worker === w.name).length;
              const st = workerState(w);
              const plat = w.platform ? [w.platform["system"], w.platform["machine"]].filter(Boolean).join(" ") : "-";
              return h(
                "tr",
                { "data-worker": w.name },
                h("th", { scope: "row", "data-label": "Name" }, h("span", { class: "cell-stack" }, h("span", {}, w.name), w.id ? idChip(w.id, "worker ID") : null)),
                h("td", { "data-label": "State" }, h("span", { class: `badge w-${st}` }, st)),
                h("td", { "data-label": "Last seen" }, w.last_seen ? timeEl(w.last_seen) : h("span", {}, "never")),
                h("td", { "data-label": "Version" }, w.versions.software ?? "-"),
                h("td", { "data-label": "Platform" }, plat || "-"),
                h("td", { "data-label": "Missions" }, n > 0 ? h("a", { class: "cell-link", href: `/missions?worker=${encodeURIComponent(w.name)}` }, String(n)) : "0"),
                h("td", { "data-label": "Actions" }, h("div", { class: "row-actions" }, button("Remove", () => void revoke(w.name), { kind: "danger small", label: `Remove worker ${w.name}`, icon: undefined }))),
              );
            }),
          ),
        ),
      ),
      h("p", { class: "muted small" }, icon("info", 14), " Last seen is when the coordinator last heard from the worker."),
    );
  };
  const render = () => keepFocus(body, renderNow);
  const un = subscribe(render);
  render();
  return un;
}
