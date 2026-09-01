import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { createInitialStageState, reduceStage } from "../../shared/stage/stageReducer";
import type { RunEvent } from "../../shared/stage/types";
import { mascotIdentity, mascotVisualState, WorkspaceShell } from "./WorkspaceShell";

const event = (sequence: number, type: string, data: Record<string, unknown>): RunEvent => ({
  schema: "migration.event",
  version: 1,
  sequence,
  type,
  data,
  timestamp_utc: "2026-09-01T00:00:00Z",
});

const workspaceState = () => [
  event(1, "dispatch.started", { slice_id: "slice-running", kind: "IMPLEMENTATION", generation: 0 }),
  event(2, "slice.status_changed", { slice_id: "slice-blocked", kind: "CONTRACT", status: "CONTRACT_BLOCKED", generation: 0 }),
  event(3, "candidate.generation_started", { slice_id: "slice-regenerating", kind: "TEST_TRANSLATION", generation: 1 }),
  event(4, "dispatch.started", { slice_id: "slice-verified", kind: "IMPLEMENTATION", generation: 0 }),
  event(5, "integration.completed", { slice_id: "slice-verified", generation: 0 }),
  event(6, "verified.advanced", { slice_id: "slice-verified", generation: 0, commit_oid: "7f2a91c" }),
].reduce(reduceStage, createInitialStageState());

describe("WorkspaceShell", () => {
  it("renders the v14 three-column shell from reducer facts without fixed personas", () => {
    const html = renderToStaticMarkup(<WorkspaceShell state={workspaceState()} onToggle={() => undefined} />);

    expect(html).toContain('data-workspace-shell="true"');
    expect(html).toContain('aria-label="迁移工作台导航"');
    expect(html).toContain('aria-label="中央迁移舞台"');
    expect(html).toContain('aria-label="只读上下文检查器"');
    expect(html).toContain('aria-label="运行事件活动条"');
    expect(html).toContain('data-persona-key="slice-running:0"');
    expect(html).toContain('data-persona-key="slice-regenerating:1"');
    expect(html).toContain("等待契约集成");
    expect(html).toContain("未提供");
    expect(html).not.toContain("coder-v2");
  });

  it("binds mascot identity and every visual action to the slice generation", () => {
    expect(mascotIdentity("slice-a", 2)).toBe("slice-a:2");
    expect(mascotVisualState("run")).toBe("running");
    expect(mascotVisualState("wait")).toBe("waiting");
    expect(mascotVisualState("error")).toBe("failed");
    expect(mascotVisualState("verified")).toBe("verified");
  });

  it("renders an explicit queue placeholder when the projection has no integration facts", () => {
    const html = renderToStaticMarkup(<WorkspaceShell state={createInitialStageState()} onToggle={() => undefined} />);

    expect(html).toContain("无已提供的冻结集成序");
  });
});
