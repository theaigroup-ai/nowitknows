import { mkdir, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

function hasTool(value) {
  if (!value || typeof value !== "object") return false;
  if (typeof value.type === "string" && /tool/i.test(value.type)) return true;
  return Object.values(value).some(v => Array.isArray(v) ? v.some(hasTool) : hasTool(v));
}

function parameters(model) {
  const params = model?.params;
  return Array.isArray(params) && params.every(p =>
    p && typeof p.id === "string" && typeof p.value === "string") ?
    params.map(p => ({ id: p.id, value: p.value })) : null;
}

function isGrok(model) {
  return typeof model?.id === "string" && /^grok-(\d+(?:[.-]\d+)*)$/.test(model.id);
}

export async function status(Cursor) {
  const value = await Cursor.auth.status();
  return { status: value.status, email: typeof value.email === "string" ? value.email : null };
}

// Catalog metadata and account data never cross the helper boundary.
export async function modelIds(Cursor) {
  const models = await Cursor.models.list();
  return [...new Set(models.map(row => row.id).filter(id => typeof id === "string" && id.length > 0))];
}

export async function execute(request, { Agent, Cursor, JsonlLocalAgentStore }) {
  let directory, agent, run, timer, timedOut = false;
  let model = null, usage = null, maxMode = false;
  let reportedParameters = null, parameterSource = "unreported";
  const metadata = () => ({
    modelParameters: {
      requested: parameters(request?.model),
      reported: reportedParameters,
      source: parameterSource,
    },
  });
  const error = message => ({ status: "error", result: "", usage, model, maxMode, message, ...metadata() });
  try {
    if (!request || typeof request.prompt !== "string" || !request.prompt.trim() ||
        !request.model || typeof request.model.id !== "string" || !request.model.id.trim() ||
        !Array.isArray(request.model.params) || request.model.params.some(p =>
          !p || typeof p.id !== "string" || typeof p.value !== "string") ||
        !Number.isFinite(request.timeout) || request.timeout <= 0) {
      return error("Invalid Cursor runner request.");
    }
    if (!isGrok(request.model)) {
      return error("Cursor background requests require a Grok model.");
    }
    // No caller-supplied options can expand this tools-free boundary.
    const requestedModel = { id: request.model.id, params: request.model.params };
    if ((await status(Cursor)).status !== "logged-in") return error("Cursor SDK is not logged in. Run Cursor.auth.login() first.");
    directory = await mkdtemp(join(tmpdir(), "nowitknows-cursor-"));
    const cwd = join(directory, "work");
    const home = join(directory, "home");
    const temp = join(directory, "tmp");
    for (const path of [cwd, home, temp]) await mkdir(path);
    const isolatedEnv = {
      HOME: home, USERPROFILE: home,
      APPDATA: join(home, "appdata"), LOCALAPPDATA: join(home, "localappdata"),
      XDG_CONFIG_HOME: join(home, "config"), XDG_CACHE_HOME: join(home, "cache"),
      XDG_DATA_HOME: join(home, "data"), XDG_STATE_HOME: join(home, "state"),
      CURSOR_DATA_DIR: join(home, ".cursor"),
      CURSOR_SANDBOX_POLICY_DIR: join(home, "sandbox-policies"),
      TMPDIR: temp, TMP: temp, TEMP: temp,
    };
    const work = async () => {
      const options = {
        model: requestedModel,
        tools: [],
        disallowedTools: ["mcp"],
        local: { cwd, store: new JsonlLocalAgentStore(join(directory, "store")),
          settingSources: [], sandboxOptions: { enabled: true } },
      };
      // Agent.create resolves the SDK login and retains it on the agent. The
      // local executor is loaded by send; its caches, policies and other writes
      // must use this run's home as well as the explicit store above.
      // This helper handles one request per process. Keep late SDK callbacks
      // isolated even after a timeout, until runner.mjs exits the process.
      try {
        agent = await Agent.create(options);
      } finally {
        Object.assign(process.env, isolatedEnv);
      }
      if (timedOut) { agent.close(); throw new Error("Cursor run timed out."); }
      run = await agent.send(request.prompt);
      if (timedOut) { void run.cancel().catch(() => {}); agent.close(); return error("Cursor run timed out."); }
      let streamUsage = null;
      for await (const event of run.stream()) {
        const params = parameters(event.model);
        if (params !== null) {
          reportedParameters = params;
          parameterSource = "sdk_stream";
        }
        if (hasTool(event)) throw new Error("Cursor run refused: a tool-call event appeared.");
        if (event.model?.maxMode === true) {
          maxMode = true;
          throw new Error("Cursor run refused: Max Mode is enabled.");
        }
        if (event.type === "usage") {
          const counts = event.usage;
          if (counts && ["inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens"].every(
            key => Number.isSafeInteger(counts[key]) && counts[key] >= 0)) {
            streamUsage ??= { inputTokens: 0, outputTokens: 0, cacheReadTokens: 0, cacheWriteTokens: 0 };
            for (const key of Object.keys(streamUsage)) streamUsage[key] += counts[key];
            usage = streamUsage;
          }
        }
        if (typeof event.model === "string" || event.model?.id !== undefined) {
          model = typeof event.model === "string" ? { id: event.model } : event.model;
          if (!isGrok(model)) throw new Error("Cursor run refused: model identity is not proven Grok.");
        }
      }
      const reply = await run.wait();
      const params = parameters(reply.model);
      if (params !== null) {
        reportedParameters = params;
        parameterSource = "sdk_reply";
      }
      model = typeof reply.model === "string" ? { id: reply.model } : reply.model ?? model;
      usage = reply.usage ?? streamUsage;
      maxMode = reply.maxMode === true || model?.maxMode === true;
      if (maxMode) return error("Cursor run refused: Max Mode is enabled.");
      if (hasTool(reply)) return error("Cursor run refused: a tool-call event appeared.");
      if (!isGrok(model)) return error("Cursor run refused: model identity is not proven Grok.");
      if (reply.status !== "finished" || typeof reply.result !== "string") return error("Cursor run did not finish successfully.");
      return { status: "finished", result: reply.result, usage, model, maxMode, ...metadata() };
    };
    return await Promise.race([
      work(),
      new Promise((_, reject) => {
        timer = setTimeout(() => {
          timedOut = true;
          reject(new Error("Cursor run timed out."));
        }, request.timeout * 1000);
      }),
    ]);
  } catch (e) {
    // SDK exception messages may contain account or transport details.
    const message = ["Cursor run timed out.", "Cursor run refused: a tool-call event appeared.",
      "Cursor run refused: Max Mode is enabled.",
      "Cursor run refused: model identity is not proven Grok."].includes(e?.message) ? e.message : "Cursor SDK run failed. Check setup and login.";
    return error(message);
  } finally {
    clearTimeout(timer);
    try { if (run && run.status === "running") void run.cancel().catch(() => {}); } catch {}
    try { agent?.close(); } catch {}
    if (directory) await rm(directory, { recursive: true, force: true });
  }
}
