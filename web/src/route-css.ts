// Page stylesheets of lazy routes (css/fleet.css: workers, sessions, usage, diagnostics; css/auth.css: sign-in, first-run setup,
// password reset and account; css/route-mission.css: session.css and mission.css for the mission page). build.mjs emits each as its own hashed file and defines their URLs. A route's loader starts the
// fetch together with its chunk and waits for both, so the page never renders unstyled and the first load does not carry them.
export type RouteCss = "fleet" | "auth" | "mission";

declare const __ROUTE_CSS__: Record<RouteCss, string>;
const loading = new Map<RouteCss, Promise<void>>();

export function ensureRouteCss(name: RouteCss): Promise<void> {
  let p = loading.get(name);
  if (!p) {
    p = new Promise<void>((done) => {
      const link = document.createElement("link");
      link.rel = "stylesheet";
      link.href = __ROUTE_CSS__[name];
      link.addEventListener("load", () => done());
      link.addEventListener("error", () => done());
      document.head.append(link);
    });
    loading.set(name, p);
  }
  return p;
}

/** A dynamic import that also waits for the route's stylesheet; both downloads run in parallel. */
export function withCss<T>(css: RouteCss, load: () => Promise<T>): () => Promise<T> {
  return () => Promise.all([ensureRouteCss(css), load()]).then(([, module]) => module);
}
