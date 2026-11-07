import type { Screen } from "./types";
import { SCREENS as auth } from "./auth";
import { SCREENS as shell } from "./shell";
import { SCREENS as session } from "./session";
import { SCREENS as mission } from "./mission";
import { SCREENS as inbox } from "./inbox";
import { SCREENS as fleet } from "./fleet";

export type { Screen } from "./types";
/** Every screen of every package, in a fixed order. A package adds or changes screens only in its own file. */
export const SCREENS: Screen[] = [...auth, ...shell, ...session, ...mission, ...inbox, ...fleet];
