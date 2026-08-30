import type { RunEvent } from "./types";

const make = (sequence: number, type: string, data: Record<string, unknown>): RunEvent => ({
  schema: "migration.event",
  version: 1,
  sequence,
  type,
  data,
  timestamp_utc: "2026-08-30T00:00:00Z",
});

export const mockRunEvents: readonly RunEvent[] = [
  make(1, "run.status_changed", { run_status: "EXECUTING" }),
  make(2, "dispatch.started", { slice_id: "slice-a", kind: "IMPLEMENTATION", generation: 0 }),
  make(3, "dispatch.started", { slice_id: "slice-b", kind: "TEST_TRANSLATION", generation: 0 }),
  make(4, "slice.status_changed", { slice_id: "slice-a", status: "LOCAL_VERIFYING" }),
  make(5, "verification.completed", { slice_id: "slice-a", outcome: "PASSED", local: true }),
  make(6, "integration.queued", { slice_id: "slice-a", integration_rank: 1 }),
  make(7, "integration.started", { slice_id: "slice-a", integration_rank: 1 }),
  make(8, "integration.completed", { slice_id: "slice-a", generation: 0 }),
  make(9, "verified.advanced", { slice_id: "slice-a", generation: 0, commit_oid: "7f2a91c" }),
  make(10, "test.failure_attributed", { slice_id: "slice-b", generation: 0, summary: "失败归因至当前 Slice" }),
  make(11, "candidate.generation_started", { slice_id: "slice-b", generation: 1 }),
  make(12, "repair.session.started", { session_kind: "GLOBAL_REPAIR", summary: "联合域修复会话已启动" }),
  make(13, "advice.adopted", { summary: "已收养修复路由结论" }),
  make(14, "run.status_changed", { run_status: "COMPLETED" }),
];
