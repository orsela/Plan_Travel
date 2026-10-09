// Plan_Travel Edge Function `invite-manager` entry point · version 3.0.0-alpha.3.1 · F03
// CHANGE 2026-10-09 F03-FN-02: new file. Deployed entry point; starts the server defined in index.ts (which stays
//   importable by tests without starting a server).
import { startServer } from "./index.ts";

startServer();
