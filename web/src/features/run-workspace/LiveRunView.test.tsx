/* @vitest-environment jsdom */
import { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it } from "vitest";
import type { ApiClient } from "../../shared/api/client";
import { LiveRunView } from "./LiveRunView";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

describe("LiveRunView", () => {
  it("loads snapshot then Run status before resuming SSE at the snapshot cursor", async () => {
    const calls: string[] = [];
    const client = {
      getWorkspace: async () => {
        calls.push("workspace");
        return { run_id: "run-1", slices: [], integration_queue: [], latest_sequence: 42 };
      },
      getMigration: async () => {
        calls.push("migration");
        return { run_id: "run-1", status: "EXECUTING", version: 7 };
      },
      streamEvents: async function* (_runId: string, afterSequence: number) {
        calls.push(`events:${afterSequence}`);
      },
    } as unknown as ApiClient;
    const host = document.createElement("div");
    document.body.append(host);
    const root = createRoot(host);

    await act(async () => {
      root.render(<LiveRunView client={client} runId="run-1" />);
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(calls).toEqual(["workspace", "migration", "events:42"]);
    expect(host.textContent).toContain("EXECUTING");

    await act(async () => root.unmount());
    host.remove();
  });
});
