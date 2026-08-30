import { SequenceCursor } from "../api/sequence";
import { eventMapping } from "./mapping";
import type {
  EventData,
  RunEvent,
  SliceProjection,
  StageAction,
  StageState,
  StageZone,
} from "./types";

const STATUS_PLACEMENT: Readonly<Record<string, { zone: StageZone; action: StageAction; persona: boolean }>> = {
  RUNNING: { zone: "work", action: "run", persona: true },
  LOCAL_VERIFYING: { zone: "work", action: "run", persona: true },
  LOCALLY_VERIFIED: { zone: "waiting", action: "wait", persona: true },
  INTEGRATION_QUEUED: { zone: "waiting", action: "wait", persona: true },
  INTEGRATING: { zone: "waiting", action: "wait", persona: true },
  REGENERATING: { zone: "regeneration", action: "error", persona: true },
  INTEGRATED: { zone: "confluence", action: "verified", persona: false },
  TERMINAL_FAILED: { zone: "regeneration", action: "error", persona: false },
  CONTRACT_BLOCKED: { zone: "waiting", action: "wait", persona: false },
  CANCELLED: { zone: "waiting", action: "wait", persona: false },
};

const text = (value: unknown, fallback: string): string =>
  typeof value === "string" && value.trim() ? value.trim() : fallback;

const number = (value: unknown, fallback: number): number =>
  typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : fallback;

const dataText = (data: EventData, ...keys: string[]): string => {
  for (const key of keys) {
    const value = data[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return "";
};

const sliceIdOf = (data: EventData): string | null => {
  const id = dataText(data, "slice_id", "sliceId");
  return id || null;
};

const keyOf = (id: string, generation: number): string => `${id}:${generation}`;

const placementFor = (status: string, fallback: SliceProjection): Pick<SliceProjection, "zone" | "action" | "persona"> =>
  STATUS_PLACEMENT[status] ?? { zone: fallback.zone, action: fallback.action, persona: fallback.persona };

const makeSlice = (id: string, data: EventData, sequence: number, existing?: SliceProjection): SliceProjection => {
  const generation = number(data.generation, existing?.generation ?? 0);
  const status = text(data.status, existing?.status ?? "RUNNING");
  const base: SliceProjection = existing ?? {
    id,
    kind: text(data.kind, "IMPLEMENTATION"),
    status,
    generation,
    zone: "work",
    action: "run",
    persona: true,
    integrationRank: null,
    commitOid: null,
    lastSequence: sequence,
  };
  const placement = placementFor(status, base);
  return {
    ...base,
    kind: text(data.kind, base.kind),
    status,
    generation,
    ...placement,
    integrationRank:
      typeof data.integration_rank === "number" ? data.integration_rank : base.integrationRank,
    commitOid: text(data.commit_oid, base.commitOid ?? "") || null,
    lastSequence: sequence,
  };
};

const withTimeline = (state: StageState, event: RunEvent, label: string, sliceId: string | null): StageState => ({
  ...state,
  timeline: [...state.timeline, { sequence: event.sequence, type: event.type, label, sliceId }].slice(-200),
});

const withSlice = (state: StageState, event: RunEvent, status?: string, placement?: Partial<SliceProjection>): StageState => {
  const id = sliceIdOf(event.data);
  if (!id) return state;
  const existing = state.slices[id];
  if (existing && number(event.data.generation, existing.generation) < existing.generation) return state;
  const next = makeSlice(id, { ...event.data, ...(status ? { status } : {}) }, event.sequence, existing);
  return { ...state, slices: { ...state.slices, [id]: { ...next, ...placement } } };
};

const clearPulseFor = (state: StageState, id: string): readonly string[] =>
  state.pulseIds.includes(id) ? state.pulseIds : [...state.pulseIds, id];

const reduceAcceptedEvent = (state: StageState, event: RunEvent): StageState => {
  const mapping = eventMapping[event.type] ?? eventMapping.default;
  const id = sliceIdOf(event.data);
  let next = { ...state, cursor: event.sequence };
  if (event.type === "run.status_changed") {
    next = { ...next, runStatus: text(event.data.run_status ?? event.data.status, state.runStatus) };
  } else if (event.type === "slice.status_changed") {
    next = withSlice(next, event, text(event.data.status, "UNKNOWN"));
  } else if (event.type === "dispatch.started") {
    next = withSlice(next, event, "RUNNING");
  } else if (event.type === "verification.completed") {
    const outcome = dataText(event.data, "outcome", "status").toUpperCase();
    if (outcome === "PASSED" || outcome === "PASS" || event.data.local === true) {
      next = withSlice(next, event, "LOCALLY_VERIFIED");
    }
  } else if (event.type === "integration.queued") {
    next = withSlice(next, event, "INTEGRATION_QUEUED");
  } else if (event.type === "integration.started") {
    next = withSlice(next, event, "INTEGRATING");
  } else if (event.type === "test.failure_attributed" || event.type === "candidate.generation_started" || event.type === "candidate.generation_invalidated") {
    next = withSlice(next, event, "REGENERATING");
  } else if (event.type === "integration.completed") {
    next = withSlice(next, event, "INTEGRATION_QUEUED");
    if (id) {
      const generation = number(event.data.generation, next.slices[id]?.generation ?? 0);
      const key = keyOf(id, generation);
      const completed = next.completedIntegrations.includes(key)
        ? next.completedIntegrations
        : [...next.completedIntegrations, key];
      next = { ...next, completedIntegrations: completed };
      if (next.advancedVerifications.includes(key)) next = celebrate(next, event, id, generation);
    }
  } else if (event.type === "verified.advanced") {
    if (id) {
      const generation = number(event.data.generation, next.slices[id]?.generation ?? 0);
      const key = keyOf(id, generation);
      const advanced = next.advancedVerifications.includes(key)
        ? next.advancedVerifications
        : [...next.advancedVerifications, key];
      next = { ...next, advancedVerifications: advanced };
      if (next.completedIntegrations.includes(key)) next = celebrate(next, event, id, generation);
    }
  } else if (mapping.kind === "slice" && id && mapping.zone && mapping.action) {
    next = withSlice(next, event, undefined, { zone: mapping.zone, action: mapping.action, persona: mapping.zone !== "confluence" });
  } else if (mapping.kind === "notice") {
    const summary = dataText(event.data, "summary", "decision", "route", "status") || mapping.label;
    next = { ...next, notices: [...next.notices, { sequence: event.sequence, type: event.type, label: mapping.label, summary }].slice(-40) };
  }
  if (event.type === "run.status_changed" && next.runStatus === "CANCELLED") {
    next = { ...next, connection: "terminal" };
  } else if (event.type === "run.status_changed" && ["COMPLETED", "PARTIALLY_COMPLETED", "FAILED"].includes(next.runStatus)) {
    next = { ...next, connection: "terminal" };
  }
  if (id && state.lockedId === id && JSON.stringify(state.slices[id]) !== JSON.stringify(next.slices[id])) {
    next = { ...next, pulseIds: clearPulseFor(next, id) };
  }
  if (!state.lockedId && id && next.slices[id]) next = { ...next, focusedId: id };
  const timelineLabel = mapping.label;
  return withTimeline(next, event, timelineLabel, id);
};

const celebrate = (state: StageState, event: RunEvent, id: string, generation: number): StageState => {
  const key = keyOf(id, generation);
  if (state.celebrations.some((item) => item.key === key)) return state;
  const existing = state.slices[id];
  if (!existing) return state;
  return {
    ...state,
    slices: { ...state.slices, [id]: { ...existing, status: "INTEGRATED", zone: "confluence", action: "verified", persona: false, commitOid: text(event.data.commit_oid, existing.commitOid ?? "") || null } },
    celebrations: [...state.celebrations, { key, sliceId: id, generation, commitOid: text(event.data.commit_oid, existing.commitOid ?? "") || null, sequence: event.sequence }],
  };
};

export const createInitialStageState = (latestSequence = 0): StageState => ({
  cursor: latestSequence,
  connection: "disconnected",
  runStatus: "UNKNOWN",
  slices: {},
  timeline: [],
  celebrations: [],
  completedIntegrations: [],
  advancedVerifications: [],
  focusedId: null,
  lockedId: null,
  pulseIds: [],
  notices: [],
});

export const reduceStage = (state: StageState, event: RunEvent): StageState => {
  const cursor = new SequenceCursor(state.cursor);
  if (state.connection !== "disconnected") cursor.connected();
  if (state.connection === "terminal") cursor.terminal();
  const result = cursor.accept(event.sequence);
  if (result.kind === "duplicate") return state;
  if (result.kind === "gap") return { ...state, connection: "catching-up" };
  const accepted = reduceAcceptedEvent(state, event);
  return { ...accepted, connection: cursor.state };
};

export const toggleFocusLock = (state: StageState, id: string): StageState => ({
  ...state,
  focusedId: id,
  lockedId: state.lockedId === id ? null : id,
});

export const clearFocusLock = (state: StageState): StageState => ({ ...state, lockedId: null });

export const selectActivePersonas = (state: StageState, memoryGib: number, cpuCores: number): SliceProjection[] => {
  const limit = Math.max(1, Math.min(4, Math.floor(memoryGib / 4), Math.floor(cpuCores / 2)));
  return Object.values(state.slices)
    .filter((slice) => slice.persona && (slice.zone === "work" || slice.zone === "regeneration"))
    .sort((left, right) => right.lastSequence - left.lastSequence || left.id.localeCompare(right.id))
    .slice(0, limit);
};
