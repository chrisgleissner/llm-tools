// One locally bundled icon set: 24x24 outline glyphs drawn as inline SVG (no network, no font files).
// Icons are decorative (aria-hidden); the control that contains one carries the accessible name.
const PATHS = {
  overview: "M3 3h7v9H3z M14 3h7v5h-7z M14 12h7v9h-7z M3 16h7v5H3z",
  workers: "M3 4h18v6H3z M3 14h18v6H3z M7 7h.01 M7 17h.01",
  missions: "M5 21V4 M5 4h12l-2 4 2 4H5",
  sessions: "M21 15a2 2 0 0 1-2 2H8l-5 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z",
  usage: "M18 20V10 M12 20V4 M6 20v-6",
  diagnostics: "M22 12h-4l-3 9L9 3l-3 9H2",
  account: "M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2 M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8z",
  more: "M5 12h.01 M12 12h.01 M19 12h.01",
  search: "M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16z M21 21l-4.3-4.3",
  copy: "M9 9h11v11H9z M5 15H4V4h11v1",
  check: "M20 6L9 17l-5-5",
  x: "M18 6L6 18 M6 6l12 12",
  sun: "M12 17a5 5 0 1 0 0-10 5 5 0 0 0 0 10z M12 1v2 M12 21v2 M4.2 4.2l1.4 1.4 M18.4 18.4l1.4 1.4 M1 12h2 M21 12h2 M4.2 19.8l1.4-1.4 M18.4 5.6l1.4-1.4",
  moon: "M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z",
  auto: "M2 4h20v12H2z M8 20h8 M12 16v4",
  eye: "M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z",
  eyeOff: "M17.9 17.9A10.9 10.9 0 0 1 12 20C5 20 1 12 1 12a18 18 0 0 1 5.1-5.9 M9.9 4.2A9 9 0 0 1 12 4c7 0 11 8 11 8a18 18 0 0 1-2.2 3.2 M1 1l22 22",
  alert: "M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z M12 9v4 M12 17h.01",
  info: "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z M12 16v-4 M12 8h.01",
  help: "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z M9.1 9a3 3 0 0 1 5.8 1c0 2-3 3-3 3 M12 17h.01",
  ban: "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z M4.9 4.9l14.2 14.2",
  checkCircle: "M22 11.1V12a10 10 0 1 1-5.9-9.1 M22 4L12 14l-3-3",
  logout: "M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4 M16 17l5-5-5-5 M21 12H9",
  keyboard: "M2 6h20v12H2z M6 10h.01 M10 10h.01 M14 10h.01 M18 10h.01 M7 14h10",
  chevronDown: "M6 9l6 6 6-6",
  chevronUp: "M18 15l-6-6-6 6",
  chevronLeft: "M15 18l-6-6 6-6",
  chevronRight: "M9 18l6-6-6-6",
  refresh: "M21 12a9 9 0 1 1-3-6.7 M21 3v6h-6",
  plus: "M12 5v14 M5 12h14",
  shield: "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z",
  terminal: "M4 17l6-6-6-6 M12 19h8",
  lock: "M5 11h14v10H5z M8 11V7a4 4 0 0 1 8 0v4",
  caps: "M12 3l8 9h-5v6H9v-6H4z M9 21h6",
  clock: "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z M12 6v6l4 2",
  // state shapes (spec 5.1); every state pill uses one of these, so state is never carried by colour alone
  diamondCheck: "M12 2l10 10-10 10L2 12z M8.5 12l2.5 2.5 4.5-5",
  speechBubble: "M21 12a8 8 0 0 1-8 8H4l2.5-3.2A8 8 0 1 1 21 12z M8 11h.01 M12 11h.01 M16 11h.01",
  xOctagon: "M7.9 2h8.2L22 7.9v8.2L16.1 22H7.9L2 16.1V7.9z M9 9l6 6 M15 9l-6 6",
  plugOff: "M9 2v4 M15 2v4 M6 6h12v4a6 6 0 0 1-12 0z M12 16v6 M3 3l18 18",
  hourglass: "M6 2h12 M6 22h12 M7 2v4.5L12 12l-5 5.5V22 M17 2v4.5L12 12l5 5.5V22",
  ring: "M21 12a9 9 0 1 1-9-9",
  pauseBars: "M9 5v14 M15 5v14",
  circle: "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z",
  square: "M5 5h14v14H5z",
  plug: "M9 2v4 M15 2v4 M6 6h12v4a6 6 0 0 1-12 0z M12 16v6",
  slashCircle: "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z M5 19L19 5",
  inbox: "M22 12h-6l-2 3h-4l-2-3H2 M5.5 5.1L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.5-6.9A2 2 0 0 0 16.7 4H7.2a2 2 0 0 0-1.7 1.1z",
} as const;

export type IconName = keyof typeof PATHS;

const NS = "http://www.w3.org/2000/svg";

export function icon(name: IconName, size = 20): SVGSVGElement {
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", String(size));
  svg.setAttribute("height", String(size));
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "2");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  svg.setAttribute("data-icon", name.replace(/[A-Z]/g, (c) => "-" + c.toLowerCase()));
  svg.classList.add("icon");
  const path = document.createElementNS(NS, "path");
  path.setAttribute("d", PATHS[name]);
  svg.append(path);
  return svg;
}
