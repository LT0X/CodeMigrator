export type EventData = Record<string, unknown>;

export interface RunEvent {
  readonly type: string;
  readonly data: EventData;
  readonly sequence: number;
  readonly timestamp_utc: string;
}

export type StageZone = "work" | "waiting" | "regeneration" | "confluence";
export type StageAction = "run" | "wait" | "error" | "verified";
export type ConnectionState =
  | "disconnected"
  | "connecting"
  | "connected"
  | "catching-up"
  | "terminal";

export interface SliceProjection {
  readonly id: string;
  readonly kind: string;
  readonly status: string;
  readonly generation: number;
  readonly zone: StageZone;
  readonly action: StageAction;
  readonly persona: boolean;
  readonly integrationRank: number | null;
  readonly commitOid: string | null;
  readonly lastSequence: number;
}

export interface TimelineEntry {
  readonly sequence: number;
  readonly type: string;
  readonly label: string;
  readonly sliceId: string | null;
}

export interface Celebration {
  readonly key: string;
  readonly sliceId: string;
  readonly generation: number;
  readonly commitOid: string | null;
  readonly sequence: number;
}

export interface SessionNotice {
  readonly sequence: number;
  readonly type: string;
  readonly label: string;
  readonly summary: string;
}

export interface StageState {
  readonly cursor: number;
  readonly connection: ConnectionState;
  readonly runStatus: string;
  readonly slices: Readonly<Record<string, SliceProjection>>;
  readonly timeline: readonly TimelineEntry[];
  readonly celebrations: readonly Celebration[];
  readonly completedIntegrations: readonly string[];
  readonly advancedVerifications: readonly string[];
  readonly focusedId: string | null;
  readonly lockedId: string | null;
  readonly pulseIds: readonly string[];
  readonly notices: readonly SessionNotice[];
}
