export interface MigrationProjection {
  readonly run_id: string;
  readonly status: string;
  readonly version: number;
  readonly verification_outcome?: unknown;
  readonly report_delivery_status?: string;
  readonly code_delivery_status?: string;
}

export interface SliceProjectionView {
  readonly slice_id: string;
  readonly kind: string;
  readonly status: string;
  readonly generation: number;
  readonly write_scope: Record<string, readonly string[]>;
  readonly integration_rank: number;
}

export interface WorkspaceProjection {
  readonly run_id: string;
  readonly slices: readonly SliceProjectionView[];
  readonly integration_queue: readonly Record<string, unknown>[];
  readonly latest_sequence: number;
}

export interface ReportProjection {
  readonly run_id: string;
  readonly status: string;
  readonly report_ref?: string;
  readonly evidence?: EvidenceProjection;
}

export interface EvidenceProjection {
  readonly pass_rate?: Record<string, unknown>;
  readonly failures?: readonly Record<string, unknown>[];
  readonly flaky?: readonly Record<string, unknown>[];
  readonly coverage?: readonly Record<string, unknown>[];
  readonly structural_conservation?: readonly Record<string, unknown>[];
  readonly attribution?: readonly Record<string, unknown>[];
  readonly confidence?: readonly Record<string, unknown>[];
  readonly boundary_statement?: string;
  readonly parity?: readonly Record<string, unknown>[];
}

export interface HealthProjection {
  readonly app: string;
  readonly postgres: string;
  readonly sandbox: string;
  readonly optional_profiles: Record<string, string>;
}

export interface SessionProjection {
  readonly session_id: string;
  readonly status: string;
  readonly revision: number;
}
