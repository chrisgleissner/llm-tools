// Confirmation dialogs of the mission page. `confirmDialog` builds the dialog synchronously, so the confirming button can be
// marked `data-confirm` (the hook the acceptance specs use) right after the call.
import { confirmDialog, type ConfirmOptions } from "../../ui";

export function confirm(opts: ConfirmOptions): Promise<string | null> {
  const result = confirmDialog(opts);
  document.getElementById("dialog-confirm")?.setAttribute("data-confirm", "");
  return result;
}
