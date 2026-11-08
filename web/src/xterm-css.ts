// The xterm.js stylesheet is a separate hashed file (build.mjs defines its URL), so the first load does not carry it.
// The mission page starts the fetch together with the terminal chunk, after its first paint; the terminal module awaits it.
declare const __XTERM_CSS__: string;
let loading: Promise<void> | null = null;

export function ensureXtermCss(): Promise<void> {
  loading ??= new Promise<void>((done) => {
    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = __XTERM_CSS__;
    link.addEventListener("load", () => done());
    link.addEventListener("error", () => done());
    document.head.append(link);
  });
  return loading;
}
