// Security notifications for the administrator (new sign-in address or device, API token created, blocked sign-in failures, passkeys reset).
// A separate chunk: the shell loads it after the first paint and the notices are read from the coordinator, never from a worker.
import * as api from "./api";
import { h } from "./dom";
import { announce } from "./ui";

const LABELS: Record<string, string> = {
  new_sign_in_address: "A sign-in came from a new address.",
  new_sign_in_device: "A sign-in came from a new device.",
  api_token_created: "An API token was created.",
  repeated_sign_in_failures: "Repeated sign-in failures were blocked.",
  passkey_origin_reenrolled: "Passkeys were reset for a new trusted address.",
};

/** Fill `box` with the active notices and keep it current (after a token or passkey change, and every minute). Returns the cleanup. */
export function mountSecurityNotice(box: HTMLElement): () => void {
  const refresh = (): void => {
    void api.securityNotifications().then((items) => {
      box.replaceChildren(...items.map((item) => {
        const dismiss = h("button", { class: "btn small ghost", type: "button", "aria-label": "Dismiss security notification" }, "Dismiss");
        const row = h("div", { class: "actions" }, h("span", {}, LABELS[item.category] ?? "A security event occurred."), dismiss);
        dismiss.addEventListener("click", () => {
          void api.dismissSecurityNotification(item.id).then(() => row.remove()).catch(() => announce("Could not dismiss security notification", "error"));
        });
        return row;
      }));
      box.hidden = items.length === 0;
    }).catch(() => {});
  };
  window.addEventListener("bridge-security-notification", refresh);
  const timer = window.setInterval(refresh, 60_000);
  refresh();
  return () => {
    window.removeEventListener("bridge-security-notification", refresh);
    window.clearInterval(timer);
  };
}
