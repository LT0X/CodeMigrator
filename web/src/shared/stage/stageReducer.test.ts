import { describe, expect, it } from "vitest";
import { ALL_EVENT_TYPES, eventMapping } from "./mapping";
import {
  createInitialStageState,
  reduceStage,
  selectActivePersonas,
  toggleFocusLock,
} from "./stageReducer";
import type { RunEvent } from "./types";

const event = (sequence: number, type: string, data: Record<string, unknown>): RunEvent => ({
  schema: "migration.event",
  version: 1,
  sequence,
  type,
  data,
  timestamp_utc: "2026-08-30T00:00:00Z",
});

describe("stage reducer", () => {
  it("maps one slice through run, wait, error and verified exactly once", () => {
    let state = createInitialStageState();
    state = reduceStage(state, event(1, "dispatch.started", { slice_id: "slice-a", generation: 0 }));
    expect(state.slices["slice-a"].zone).toBe("work");
    state = reduceStage(state, event(2, "integration.queued", { slice_id: "slice-a", generation: 0 }));
    expect(state.slices["slice-a"].zone).toBe("waiting");
    state = reduceStage(state, event(3, "test.failure_attributed", { slice_id: "slice-a", generation: 0 }));
    expect(state.slices["slice-a"].zone).toBe("regeneration");
    state = reduceStage(state, event(4, "integration.completed", { slice_id: "slice-a", generation: 0 }));
    state = reduceStage(state, event(5, "verified.advanced", { slice_id: "slice-a", generation: 0, commit_oid: "7f2a91c" }));
    expect(state.slices["slice-a"].zone).toBe("confluence");
    expect(state.celebrations).toHaveLength(1);
    state = reduceStage(state, event(6, "verified.advanced", { slice_id: "slice-a", generation: 0, commit_oid: "7f2a91c" }));
    expect(state.celebrations).toHaveLength(1);
  });

  it("keeps duplicate events inert and marks a gap for catch-up", () => {
    let state = reduceStage(createInitialStageState(), event(1, "dispatch.started", { slice_id: "slice-a" }));
    state = reduceStage(state, event(1, "integration.queued", { slice_id: "slice-a" }));
    expect(state.slices["slice-a"].zone).toBe("work");
    state = reduceStage(state, event(3, "integration.queued", { slice_id: "slice-a" }));
    expect(state.connection).toBe("catching-up");
    expect(state.cursor).toBe(1);
  });

  it("follows the latest slice until a user lock, then pulses the locked card", () => {
    let state = createInitialStageState();
    state = reduceStage(state, event(1, "dispatch.started", { slice_id: "slice-a" }));
    state = toggleFocusLock(state, "slice-a");
    state = reduceStage(state, event(2, "slice.status_changed", { slice_id: "slice-a", status: "LOCAL_VERIFYING" }));
    expect(state.focusedId).toBe("slice-a");
    expect(state.pulseIds).toContain("slice-a");
    state = reduceStage(state, event(3, "dispatch.started", { slice_id: "slice-b" }));
    expect(state.focusedId).toBe("slice-a");
  });

  it("has a mapping or safe default for every API event vocabulary value", () => {
    for (const type of ALL_EVENT_TYPES) {
      expect(eventMapping[type]).toBeDefined();
    }
    expect(eventMapping["future.event"] ?? eventMapping.default).toBeDefined();
  });

  it("caps visible active personas at the host formula", () => {
    let state = createInitialStageState();
    for (let i = 0; i < 6; i += 1) {
      state = reduceStage(state, event(i + 1, "dispatch.started", { slice_id: `slice-${i}` }));
    }
    expect(selectActivePersonas(state, 16, 8)).toHaveLength(4);
    expect(selectActivePersonas(state, 2, 2)).toHaveLength(1);
  });

  it("does not let a late event from an older generation replace the current one", () => {
    let state = createInitialStageState();
    state = reduceStage(state, event(1, "candidate.generation_started", { slice_id: "slice-a", generation: 1 }));
    state = reduceStage(state, event(2, "dispatch.started", { slice_id: "slice-a", generation: 0 }));
    expect(state.slices["slice-a"].generation).toBe(1);
    expect(state.slices["slice-a"].zone).toBe("regeneration");
  });

  it("does not pair an older generation with the current verified candidate", () => {
    let state = createInitialStageState();
    state = reduceStage(state, event(1, "candidate.generation_started", { slice_id: "slice-a", generation: 1 }));
    state = reduceStage(state, event(2, "integration.completed", { slice_id: "slice-a", generation: 0 }));
    state = reduceStage(state, event(3, "verified.advanced", { slice_id: "slice-a", generation: 0 }));
    expect(state.slices["slice-a"].generation).toBe(1);
    expect(state.slices["slice-a"].status).toBe("REGENERATING");
    expect(state.celebrations).toHaveLength(0);
  });
});
