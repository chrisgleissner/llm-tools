// Every lazy route except the mission page: sign-in, first-run setup, forced password change and the account page (auth stylesheet), and the workers, sessions, usage and diagnostics pages (fleet stylesheet).
// One chunk: the account and fleet pages share the answer line and dialog code, and the pages compress better together than as several chunks. main.ts loads the stylesheet that belongs to the route.
export { loginView } from "./login";
export { forcedChangeView, resetNotice } from "./reset";
export { account } from "./account";
export { workers } from "./workers";
export { sessions } from "./sessions";
export { usage } from "./usage";
export { diagnostics } from "./diagnostics";
