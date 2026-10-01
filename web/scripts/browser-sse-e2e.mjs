import { spawn } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

const chromiumPath = process.env.CODEMIGRATOR_BROWSER_CHROMIUM;
const baseUrl = process.env.CODEMIGRATOR_BROWSER_URL;
const runId = process.env.CODEMIGRATOR_BROWSER_RUN_ID;
const sensitiveMarkers = JSON.parse(process.env.CODEMIGRATOR_BROWSER_SENSITIVE_MARKERS ?? "[]");

function requireEnvironment() {
  if (!chromiumPath || !baseUrl || !runId || !Array.isArray(sensitiveMarkers)) {
    throw new Error("browser E2E environment is incomplete");
  }
}

function waitForDevTools(chrome) {
  return new Promise((resolve, reject) => {
    let output = "";
    const timeout = setTimeout(() => reject(new Error("Chromium did not start")), 12000);
    chrome.stderr.setEncoding("utf8");
    chrome.stderr.on("data", (chunk) => {
      output += chunk;
      const match = output.match(/DevTools listening on ws:\/\/127\.0\.0\.1:(\d+)\//);
      if (!match) return;
      clearTimeout(timeout);
      resolve(Number(match[1]));
    });
    chrome.once("error", () => {
      clearTimeout(timeout);
      reject(new Error("Chromium failed to start"));
    });
    chrome.once("exit", () => {
      clearTimeout(timeout);
      reject(new Error("Chromium exited before DevTools started"));
    });
  });
}

function connectCdp(socket) {
  let nextId = 0;
  const pending = new Map();
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (typeof message.id !== "number") return;
    const operation = pending.get(message.id);
    if (!operation) return;
    pending.delete(message.id);
    if (message.error) operation.reject(new Error("DevTools command failed"));
    else operation.resolve(message.result);
  });
  return {
    call(method, params = {}) {
      const id = ++nextId;
      return new Promise((resolve, reject) => {
        pending.set(id, { resolve, reject });
        socket.send(JSON.stringify({ id, method, params }));
      });
    },
    close() {
      socket.close();
    },
  };
}

async function evaluate(cdp, expression) {
  const result = await cdp.call("Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
  });
  if (result.exceptionDetails) throw new Error("browser evaluation failed");
  return result.result?.value;
}

async function waitForUi(cdp) {
  const expression = `(() => {
    const summary = document.querySelector('[aria-label="AgentRun 执行摘要"] span[data-agent-run-state="TERMINAL"]');
    const activity = document.querySelector('[aria-label="运行事件活动条"] [aria-live="polite"]');
    return { summary: summary?.textContent?.replace(/\\s+/g, " ").trim() ?? "", activity: activity?.textContent ?? "" };
  })()`;
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) {
    const value = await evaluate(cdp, expression);
    if (value?.summary === "PLAN · PLAN_AUXILIARY · COMPLETED" && value.activity.includes("sequence 3")) {
      return value;
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error("AgentRun summary did not appear");
}

async function inspectReplay(cdp, afterSequence, expectedIds) {
  const result = await evaluate(cdp, `(() => (async () => {
    const response = await fetch("/api/v1/migrations/${runId}/events", {
      headers: { Accept: "text/event-stream", "Last-Event-ID": "${afterSequence}" }
    });
    const raw = await response.text();
    const blocks = raw.split(/\\r?\\n\\r?\\n/).filter((block) => block.includes("data:"));
    const events = blocks.map((block) => {
      const id = block.match(/^id:\\s*(.*)$/m)?.[1]?.trim() ?? "";
      const data = block.split(/\\r?\\n/).filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).trim()).join("\\n");
      return { id, envelope: JSON.parse(data) };
    });
    const markers = ${JSON.stringify(sensitiveMarkers)};
    const json = JSON.stringify(events);
    const forbiddenNames = ["prompt", "source", "tool_body", "provider_response", "thread_id", "checkpoint_ref"];
    const expectedIds = ${JSON.stringify(expectedIds)};
    const allowedKeys = {
      "agent_run.started": ["agent_run_id", "phase", "session_kind", "slice_id", "generation"],
      "agent_run.terminal": ["agent_run_id", "phase", "session_kind", "slice_id", "generation", "exit", "receipt_category"],
      "run.status_changed": ["run_status"]
    };
    return {
      ok: response.ok,
      contentType: response.headers.get("content-type") ?? "",
      ids: events.map((item) => item.id),
      types: events.map((item) => item.envelope.type),
      envelopeValid: events.length === expectedIds.length && events.every((item) =>
        item.id === String(item.envelope.sequence) &&
        item.envelope.schema === "migration.event" && item.envelope.version === 1 &&
        typeof item.envelope.type === "string" &&
        typeof item.envelope.timestamp_utc === "string" && item.envelope.timestamp_utc.length > 0 &&
        item.envelope.data !== null && typeof item.envelope.data === "object" && !Array.isArray(item.envelope.data) &&
        Array.isArray(allowedKeys[item.envelope.type]) &&
        Object.keys(item.envelope.data).every((key) => allowedKeys[item.envelope.type].includes(key))
      ),
      idsMatch: events.map((item) => item.id).every((id, index) => id === expectedIds[index]),
      sensitiveMarkerInBody: markers.some((marker) => json.includes(marker)),
      forbiddenFieldNameInBody: forbiddenNames.some((name) => json.includes(name))
    };
  })())()`);
  return result;
}

async function main() {
  requireEnvironment();
  const profile = await mkdtemp(join(tmpdir(), "codemigrator-browser-e2e-"));
  const chrome = spawn(chromiumPath, [
    "--headless=new",
    "--no-sandbox",
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--remote-allow-origins=*",
    "--remote-debugging-port=0",
    `--user-data-dir=${profile}`,
    "about:blank",
  ], { stdio: ["ignore", "ignore", "pipe"] });
  let cdp;
  try {
    const devToolsPort = await waitForDevTools(chrome);
    const targetResponse = await fetch(`http://127.0.0.1:${devToolsPort}/json/new?about:blank`, { method: "PUT" });
    if (!targetResponse.ok) throw new Error("Chromium did not create a page");
    const target = await targetResponse.json();
    const socket = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      socket.addEventListener("open", resolve, { once: true });
      socket.addEventListener("error", () => reject(new Error("DevTools connection failed")), { once: true });
    });
    cdp = connectCdp(socket);
    await cdp.call("Page.enable");
    await cdp.call("Runtime.enable");
    await cdp.call("Page.navigate", { url: `${baseUrl}/runs/${runId}` });
    await waitForUi(cdp);

    const full = await inspectReplay(cdp, 0, ["1", "2", "3"]);
    const replay = await inspectReplay(cdp, 1, ["2", "3"]);
    const ui = await evaluate(cdp, `(() => {
      const summary = document.querySelector('[aria-label="AgentRun 执行摘要"] span[data-agent-run-state="TERMINAL"]');
      const activity = document.querySelector('[aria-label="运行事件活动条"] [aria-live="polite"]');
      const body = document.body.innerText;
      const markers = ${JSON.stringify(sensitiveMarkers)};
      return {
        summary: summary?.textContent?.replace(/\\s+/g, " ").trim() ?? "",
        cursor: Number(activity?.textContent?.match(/sequence (\\d+)/)?.[1] ?? -1),
        sensitiveMarkerVisible: markers.some((marker) => body.includes(marker))
      };
    })()`);
    const result = {
      ui_summary: ui.summary,
      ui_cursor: ui.cursor,
      sensitive_marker_visible: ui.sensitiveMarkerVisible,
      full_envelope_valid: full.ok && full.envelopeValid && full.idsMatch && full.contentType.startsWith("text/event-stream"),
      full_event_ids: full.ids,
      forbidden_field_name_in_full_sse: full.forbiddenFieldNameInBody,
      replay_envelope_valid: replay.ok && replay.envelopeValid && replay.idsMatch && replay.contentType.startsWith("text/event-stream"),
      replay_event_ids: replay.ids,
      replay_event_types: replay.types,
      sensitive_marker_in_sse: full.sensitiveMarkerInBody || replay.sensitiveMarkerInBody,
      forbidden_field_name_in_sse: replay.forbiddenFieldNameInBody,
    };
    process.stdout.write(JSON.stringify(result));
  } finally {
    cdp?.close();
    chrome.kill("SIGTERM");
    await new Promise((resolve) => {
      if (chrome.exitCode !== null) resolve();
      else chrome.once("exit", resolve);
      setTimeout(() => {
        if (chrome.exitCode === null) chrome.kill("SIGKILL");
        resolve();
      }, 3000).unref();
    });
    await rm(profile, { recursive: true, force: true });
  }
}

main().catch(() => {
  process.stderr.write("browser E2E scenario failed\n");
  process.exitCode = 1;
});
