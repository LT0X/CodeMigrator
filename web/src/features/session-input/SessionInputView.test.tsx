/* @vitest-environment jsdom */
import { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it } from "vitest";
import type { ApiClient } from "../../shared/api/client";
import { SessionInputView } from "./SessionInputView";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const sessionEvent = (sequence: number, type: string) => ({
  schema: "migration.session.event" as const,
  version: 1 as const,
  type,
  sequence,
  data: {},
  timestamp_utc: "2026-10-01T10:00:00Z",
  sse_id: String(sequence),
});

describe("SessionInputView", () => {
  it("displays persisted session events and resumes replay after the last displayed sequence", async () => {
    const cursors: number[] = [];
    let attempts = 0;
    const client = {
      sendSessionMessage: async () => ({ session_id: "session-1", status: "OPEN", revision: 1 }),
      streamSessionEvents: async function* (_sessionId: string, afterSequence: number) {
        cursors.push(afterSequence);
        attempts += 1;
        if (attempts === 1) {
          yield sessionEvent(1, "assistant.message.completed");
          throw new Error("connection dropped");
        }
        yield sessionEvent(2, "session.closed");
      },
    } as unknown as ApiClient;
    const host = document.createElement("div");
    document.body.append(host);
    const root = createRoot(host);

    try {
      await act(async () => root.render(<SessionInputView sessionId="session-1" client={client} />));
      await act(async () => new Promise((resolve) => setTimeout(resolve, 400)));

      expect(cursors).toEqual([0, 1]);
      expect(host.querySelector('[aria-label="会话持久事件"]')?.textContent).toContain("assistant.message.completed");
      expect(host.querySelector('[aria-label="会话持久事件"]')?.textContent).toContain("session.closed");
    } finally {
      await act(async () => root.unmount());
      host.remove();
    }
  });
});
