import { execute, modelIds, status } from "./core.mjs";

// stdout is a single JSON envelope, including when SDK diagnostics use console.
console.log = console.info = console.warn = console.error = () => {};
let reply;
const command = process.argv[2] ?? "run";
try {
  const { Agent, Cursor, JsonlLocalAgentStore } = await import("@cursor/sdk");
  if (command === "models") reply = await modelIds(Cursor);
  else if (command === "status") reply = await status(Cursor);
  else if (command === "login") {
    await Cursor.auth.login();
    reply = await status(Cursor);
  } else if (command === "run") {
    let input = "";
    for await (const chunk of process.stdin) input += chunk;
    reply = await execute(JSON.parse(input), { Agent, Cursor, JsonlLocalAgentStore });
  } else throw new Error("command");
} catch {
  reply = command === "models" ? null : command === "status" ? { status: "error", email: null } : { status: "error", result: "", usage: null, model: null, maxMode: false,
    message: "Cursor SDK helper unavailable. Repeat setup and check login." };
}
process.stdout.write(JSON.stringify(reply) + "\n");
process.exit(reply === null || reply.status === "error" ? 1 : 0);
