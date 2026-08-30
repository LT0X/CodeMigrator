import type { StageAction, StageZone } from "./types";

export type MappingKind = "slice" | "run" | "notice" | "noop";

export interface EventMapping {
  readonly kind: MappingKind;
  readonly zone?: StageZone;
  readonly action?: StageAction;
  readonly label: string;
}

export const ALL_EVENT_TYPES = [
  "run.status_changed",
  "slice.status_changed",
  "execute.contract_wave_completed",
  "candidate.generation_started",
  "candidate.generation_invalidated",
  "dispatch.started",
  "dispatch.interrupted",
  "dispatch.discarded",
  "dispatch.completed",
  "verification.completed",
  "test.failure_attributed",
  "test.flaky_observed",
  "tool.call.pre",
  "tool.call.post",
  "checkpoint.pre",
  "integration.queued",
  "integration.started",
  "integration.completed",
  "verified.advanced",
  "report.completed",
  "delivery.status_changed",
  "slice.segment_continued",
  "advice.proposed",
  "advice.adopted",
  "repair.decision",
  "repair.session.started",
  "repair.session.completed",
  "repair.session.blocked",
  "repair.session.failed",
] as const;

export const eventMapping: Readonly<Record<string, EventMapping>> = {
  "run.status_changed": { kind: "run", label: "Run 状态变化" },
  "slice.status_changed": { kind: "slice", label: "Slice 状态变化" },
  "execute.contract_wave_completed": { kind: "notice", label: "契约波次完成" },
  "candidate.generation_started": { kind: "slice", zone: "regeneration", action: "error", label: "重生成代次开始" },
  "candidate.generation_invalidated": { kind: "slice", zone: "regeneration", action: "error", label: "候选代次失效" },
  "dispatch.started": { kind: "slice", zone: "work", action: "run", label: "persona 上场" },
  "dispatch.interrupted": { kind: "slice", label: "派发中断" },
  "dispatch.discarded": { kind: "slice", label: "迟到结果丢弃" },
  "dispatch.completed": { kind: "slice", label: "派发完成" },
  "verification.completed": { kind: "slice", label: "验证完成" },
  "test.failure_attributed": { kind: "slice", zone: "regeneration", action: "error", label: "失败归因" },
  "test.flaky_observed": { kind: "notice", label: "观察到 flaky" },
  "tool.call.pre": { kind: "slice", label: "工具调用开始" },
  "tool.call.post": { kind: "slice", label: "工具调用完成" },
  "checkpoint.pre": { kind: "slice", label: "checkpoint 提交" },
  "integration.queued": { kind: "slice", zone: "waiting", action: "wait", label: "排队集成" },
  "integration.started": { kind: "slice", zone: "waiting", action: "wait", label: "集成开始" },
  "integration.completed": { kind: "slice", zone: "waiting", action: "wait", label: "集成完成" },
  "verified.advanced": { kind: "slice", zone: "confluence", action: "verified", label: "verified 主线推进" },
  "report.completed": { kind: "notice", label: "报告完成" },
  "delivery.status_changed": { kind: "notice", label: "交付状态变化" },
  "slice.segment_continued": { kind: "slice", label: "Slice 分段续作" },
  "advice.proposed": { kind: "notice", label: "判断层建议" },
  "advice.adopted": { kind: "notice", label: "建议已收养" },
  "repair.decision": { kind: "notice", label: "修复决策" },
  "repair.session.started": { kind: "notice", label: "全局修复会话开始" },
  "repair.session.completed": { kind: "notice", label: "全局修复会话完成" },
  "repair.session.blocked": { kind: "notice", label: "全局修复会话阻塞" },
  "repair.session.failed": { kind: "notice", label: "全局修复会话失败" },
  default: { kind: "noop", label: "未分类事件" },
};
