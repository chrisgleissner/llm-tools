// The route table (main.ts) still imports `overview`; the page is now the Inbox. The lead repoints main.ts and deletes this file.
export { inbox as overview } from "./inbox";
