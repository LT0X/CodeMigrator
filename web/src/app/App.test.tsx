/* @vitest-environment jsdom */
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

afterEach(() => {
  window.history.pushState({}, "", "/");
  vi.unstubAllGlobals();
});

describe("App session routes", () => {
  it.each(["/sessions/new", "/sessions/new/"])("opens the project chooser at %s instead of treating new as a session id", async (path) => {
    window.history.pushState({}, "", path);
    vi.stubGlobal("fetch", async () => new Response(JSON.stringify({
      items: [{ project_id: "project-1", snapshot_id: "snapshot-1", status: "READY" }],
    }), { status: 200 }));
    const host = document.createElement("div");
    document.body.append(host);
    const root = createRoot(host);

    try {
      await act(async () => root.render(<App />));
      await act(async () => new Promise((resolve) => setTimeout(resolve, 0)));

      expect(host.querySelector("select")?.textContent).toContain("project-1");
      expect(host.textContent).toContain("新建 Draft 会话");
      expect(host.textContent).not.toContain("受限会话输入");
    } finally {
      await act(async () => root.unmount());
      host.remove();
    }
  });
});
