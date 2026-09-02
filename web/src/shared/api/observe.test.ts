import { describe, expect, it } from "vitest";
import { observeRun } from "./observe";
import type { ApiClient } from "./client";

const client = (streamEvents: ApiClient["streamEvents"]): ApiClient => ({
  listMigrations: async () => [],
  getWorkspace: async () => ({ run_id: "run", slices: [], integration_queue: [], latest_sequence: 4 }),
  getReport: async () => ({ run_id: "run", status: "COMPLETED" }),
  getEvidence: async () => ({}),
  getHealth: async () => ({ app: "healthy", postgres: "healthy", sandbox: "ready", optional_profiles: {} }),
  sendSessionMessage: async () => ({ session_id: "s", status: "OPEN", revision: 1 }),
  answerSession: async () => ({ session_id: "s", status: "OPEN", revision: 1 }),
  confirmSession: async () => ({ session_id: "s", status: "OPEN", revision: 1 }),
  confirmCorrection: async () => ({ session_id: "s", status: "OPEN", revision: 1 }),
  streamEvents,
});

async function* events() {
  yield { schema: "migration.event" as const, version: 1 as const, type: "dispatch.started", sequence: 5, data: { slice_id: "a" }, timestamp_utc: "" };
}

describe("live observation", () => {
  it("starts after the snapshot sequence and yields only later contiguous events", async () => {
    const seen: number[] = [];
    const source = client(async function* (_runId, after) { seen.push(after); yield* events(); });
    const received: number[] = [];
    for await (const event of observeRun(source, "run")) received.push(event.sequence);
    expect(seen).toEqual([4]);
    expect(received).toEqual([5]);
  });

  it("reconnects from the last contiguous sequence after a gap", async () => {
    const seen: number[] = [];
    let attempts = 0;
    const source = client(async function* (_runId, after) {
      seen.push(after);
      attempts += 1;
      if (attempts === 1) {
        yield { schema: "migration.event" as const, version: 1 as const, type: "dispatch.started", sequence: 6, data: { slice_id: "a" }, timestamp_utc: "" };
        return;
      }
      yield { schema: "migration.event" as const, version: 1 as const, type: "dispatch.started", sequence: 5, data: { slice_id: "a" }, timestamp_utc: "" };
    });
    const received: number[] = [];
    for await (const event of observeRun(source, "run")) received.push(event.sequence);
    expect(seen).toEqual([4, 4]);
    expect(received).toEqual([5]);
  });

  it("marks reconnect history so presentation layers can suppress replay effects", async () => {
    const phases: string[] = [];
    let attempts = 0;
    const source = client(async function* (_runId, after) {
      attempts += 1;
      if (attempts === 1) {
        yield { schema: "migration.event" as const, version: 1 as const, type: "dispatch.started", sequence: 6, data: { slice_id: "a" }, timestamp_utc: "" };
        return;
      }
      expect(after).toBe(4);
      yield { schema: "migration.event" as const, version: 1 as const, type: "dispatch.started", sequence: 5, data: { slice_id: "a" }, timestamp_utc: "" };
    });
    const received: number[] = [];
    for await (const event of observeRun(source, "run", undefined, undefined, (phase) => phases.push(phase))) received.push(event.sequence);

    expect(received).toEqual([5]);
    expect(phases).toEqual(["start", "end"]);
  });
});
