// Security notifications for the administrator (new sign-in address or device, API token created, blocked sign-in failures, passkeys reset).
// A separate chunk: the shell loads it after the first paint. The notices are read from the coordinator, never from a worker. They are shown
// behind a header button with their count, not as a banner, so a late notice cannot move anything that is already on screen.
import * as api from "./api";
import { h } from "./dom";
import { icon } from "./icons";
import { announce, openPanel } from "./ui";

interface Notice { id: string; category: string; created_at: number }
const read = async (): Promise<Notice[]> => (await api.get<{ notifications: Notice[] }>("/api/v1/security/notifications")).notifications;
const dismissOne = (id: string): Promise<unknown> => api.del(`/api/v1/security/notifications/${encodeURIComponent(id)}`);

const LABELS: Record<string, string> = {
  new_sign_in_address: "A sign-in came from a new address.",
  new_sign_in_device: "A sign-in came from a new device.",
  api_token_created: "An API token was created.",
  repeated_sign_in_failures: "Repeated sign-in failures were blocked.",
  passkey_origin_reenrolled: "Passkeys were reset for a new trusted address.",
};

/** Keep `button` (hidden while there are no notices) current: after a token or passkey change and every minute. Returns the cleanup. */
export function mountSecurityNotice(button: HTMLElement): () => void {
  let items: Notice[] = [];
  let panel: HTMLDialogElement | null = null;
  const list = h("section", { "aria-label": "Security notifications" });
  const paint = (): void => {
    button.hidden = items.length === 0;
    button.setAttribute("aria-label", `Security notifications, ${items.length}`);
    button.replaceChildren(icon("alert", 18), h("span", { class: "num" }, String(items.length)));
    list.replaceChildren(...items.map((item) => {
      const dismiss = h("button", { class: "btn small ghost", type: "button", "aria-label": "Dismiss security notification" }, "Dismiss");
      dismiss.addEventListener("click", () => {
        void dismissOne(item.id).then(() => {
          items = items.filter((x) => x.id !== item.id);
          paint();
          if (items.length === 0) panel?.close();
        }).catch(() => announce("Could not dismiss security notification", "error"));
      });
      return h("div", { class: "actions" }, h("span", {}, LABELS[item.category] ?? "A security event occurred."), dismiss);
    }));
  };
  const refresh = (): void => {
    void read().then((next) => {
      items = next;
      paint();
    }).catch(() => {});
  };
  const open = (): void => {
    panel = openPanel({ title: "Security notifications", id: "security-panel", body: h("div", { class: "dialog-body" }, list) });
    panel.addEventListener("close", () => (panel = null));
  };
  button.addEventListener("click", open);
  window.addEventListener("bridge-security-notification", refresh);
  const timer = window.setInterval(refresh, 60_000);
  refresh();
  return () => {
    button.removeEventListener("click", open);
    window.removeEventListener("bridge-security-notification", refresh);
    window.clearInterval(timer);
  };
}
