import fs from "node:fs";
import path from "node:path";

// Ephemeral, private counters contain no messages or customer identifiers.
// The supervisor stays awake if a counter cannot be read or validated.
export function trackStandbyActivity(handler, environment = process.env) {
  const directory = environment.GENAIE_STANDBY_RUNTIME;
  if (environment.GENAIE_OVERNIGHT_STANDBY !== "1" || !directory) return handler;
  let active = 0;
  const file = path.join(directory, "requests.json");
  function save() {
    const temporary = `${file}.${process.pid}.tmp`;
    fs.writeFileSync(temporary, JSON.stringify({ pid: process.pid, active, updatedAtMs: Date.now() }), { mode: 0o600 });
    fs.renameSync(temporary, file);
  }
  save();
  return async (...args) => {
    if (fs.existsSync(path.join(directory, "closing"))) {
      return { handled: true, text: "We're offline overnight. Please try again after 5:45 a.m. Algeria time." };
    }
    active++;
    save();
    try { return await handler(...args); }
    finally { active--; save(); }
  };
}
