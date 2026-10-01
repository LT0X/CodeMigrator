import type { SessionEvent } from "../../shared/api/client";

export interface SessionQuestionOption {
  readonly key: string;
  readonly label: string;
  readonly impact: string;
  readonly recommended: boolean;
}

export interface SessionQuestion {
  readonly questionId: string;
  readonly revision: number;
  readonly prompt: string;
  readonly options: readonly SessionQuestionOption[];
  readonly allowFreeText: boolean;
}

export type JsonValue = null | boolean | number | string | readonly JsonValue[] | { readonly [key: string]: JsonValue };
export type DraftArtifactName = "spec" | "understanding_dossier" | "target_project_blueprint" | "migration_rulebook";

export interface DraftArtifactSnapshot {
  readonly name: DraftArtifactName;
  readonly version: number;
  readonly sha256: string;
  readonly size: number;
  readonly mediaType: string;
}

export interface SessionDraftRevision {
  readonly revision: number;
  readonly artifacts: Readonly<Record<DraftArtifactName, JsonValue>>;
  readonly snapshots: readonly DraftArtifactSnapshot[];
}

export interface SessionDraftProjection {
  readonly questions: readonly SessionQuestion[];
  readonly draft: SessionDraftRevision | null;
  readonly confirmationRequestedRevision: number | null;
  readonly confirmedRevision: number | null;
}

export const EMPTY_SESSION_DRAFT_PROJECTION: SessionDraftProjection = {
  questions: [],
  draft: null,
  confirmationRequestedRevision: null,
  confirmedRevision: null,
};

const ARTIFACT_NAMES: readonly DraftArtifactName[] = [
  "spec",
  "understanding_dossier",
  "target_project_blueprint",
  "migration_rulebook",
];

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const positiveInteger = (value: unknown): value is number =>
  typeof value === "number" && Number.isSafeInteger(value) && value > 0;

const isJsonValue = (value: unknown): value is JsonValue => {
  if (value === null || typeof value === "string" || typeof value === "boolean") return true;
  if (typeof value === "number") return Number.isFinite(value);
  if (Array.isArray(value)) return value.every(isJsonValue);
  return isRecord(value) && Object.values(value).every(isJsonValue);
};

const parseQuestion = (event: SessionEvent): SessionQuestion | null => {
  const data = event.data;
  if (
    typeof data.question_id !== "string" || !data.question_id ||
    !positiveInteger(data.revision) ||
    typeof data.prompt !== "string" || !data.prompt.trim() ||
    !Array.isArray(data.options) || data.options.length < 2 ||
    typeof data.allow_free_text !== "boolean"
  ) return null;

  const options: SessionQuestionOption[] = [];
  const keys = new Set<string>();
  for (const candidate of data.options) {
    if (
      !isRecord(candidate) ||
      typeof candidate.key !== "string" || !candidate.key || keys.has(candidate.key) ||
      typeof candidate.label !== "string" || !candidate.label.trim() ||
      typeof candidate.impact !== "string" || !candidate.impact.trim() ||
      typeof candidate.recommended !== "boolean"
    ) return null;
    keys.add(candidate.key);
    options.push({
      key: candidate.key,
      label: candidate.label,
      impact: candidate.impact,
      recommended: candidate.recommended,
    });
  }
  if (options.filter((option) => option.recommended).length !== 1) return null;
  return {
    questionId: data.question_id,
    revision: data.revision,
    prompt: data.prompt,
    options,
    allowFreeText: data.allow_free_text,
  };
};

const parseDraftRevision = (event: SessionEvent): SessionDraftRevision | null => {
  const data = event.data;
  if (!positiveInteger(data.revision) || !isRecord(data.artifacts) || !Array.isArray(data.artifact_snapshots)) return null;

  const artifacts = {} as Record<DraftArtifactName, JsonValue>;
  for (const name of ARTIFACT_NAMES) {
    const value = data.artifacts[name];
    if (value === undefined || !isJsonValue(value)) return null;
    artifacts[name] = value;
  }

  if (data.artifact_snapshots.length !== ARTIFACT_NAMES.length) return null;
  const snapshots: DraftArtifactSnapshot[] = [];
  const names = new Set<string>();
  for (const candidate of data.artifact_snapshots) {
    if (
      !isRecord(candidate) ||
      typeof candidate.name !== "string" || !ARTIFACT_NAMES.includes(candidate.name as DraftArtifactName) || names.has(candidate.name) ||
      !positiveInteger(candidate.version) ||
      typeof candidate.sha256 !== "string" || !/^[0-9a-f]{64}$/.test(candidate.sha256) ||
      typeof candidate.size !== "number" || !Number.isSafeInteger(candidate.size) || candidate.size < 0 ||
      typeof candidate.media_type !== "string" || !candidate.media_type
    ) return null;
    names.add(candidate.name);
    snapshots.push({
      name: candidate.name as DraftArtifactName,
      version: candidate.version,
      sha256: candidate.sha256,
      size: candidate.size,
      mediaType: candidate.media_type,
    });
  }
  if (names.size !== ARTIFACT_NAMES.length || ARTIFACT_NAMES.some((name) => !(name in artifacts))) return null;
  return { revision: data.revision, artifacts, snapshots };
};

export function reduceSessionDraft(projection: SessionDraftProjection, event: SessionEvent): SessionDraftProjection {
  if (event.type === "session.question.asked") {
    const question = parseQuestion(event);
    if (!question) return projection;
    return {
      ...projection,
      questions: [...projection.questions.filter((item) => item.questionId !== question.questionId), question],
    };
  }
  if (event.type === "session.question.answered") {
    const questionId = event.data.question_id;
    if (typeof questionId !== "string" || !projection.questions.some((item) => item.questionId === questionId)) return projection;
    return { ...projection, questions: projection.questions.filter((item) => item.questionId !== questionId) };
  }
  if (event.type === "session.draft_revision.created") {
    const draft = parseDraftRevision(event);
    if (!draft || (projection.draft !== null && draft.revision < projection.draft.revision)) return projection;
    return { ...projection, draft, confirmationRequestedRevision: null, confirmedRevision: null };
  }
  if (event.type === "session.draft_revision.confirmation_requested") {
    const revision = event.data.revision;
    if (!projection.draft || revision !== projection.draft.revision) return projection;
    return { ...projection, confirmationRequestedRevision: projection.draft.revision };
  }
  if (event.type === "session.draft_revision.confirmed") {
    const revision = event.data.revision;
    if (!projection.draft || revision !== projection.draft.revision) return projection;
    return {
      ...projection,
      confirmationRequestedRevision: null,
      confirmedRevision: projection.draft.revision,
    };
  }
  return projection;
}

export function projectSessionDraft(events: readonly SessionEvent[]): SessionDraftProjection {
  return [...events]
    .sort((left, right) => left.sequence - right.sequence)
    .reduce(reduceSessionDraft, EMPTY_SESSION_DRAFT_PROJECTION);
}

export const ARTIFACT_LABELS: Readonly<Record<DraftArtifactName, string>> = {
  spec: "任务规格",
  understanding_dossier: "理解档案",
  target_project_blueprint: "目标项目蓝图",
  migration_rulebook: "迁移规则手册",
};

export function sessionEventLabel(type: string): string {
  switch (type) {
    case "assistant.message.completed": return "会话回复已保存";
    case "session.question.asked": return "需要回答的问题";
    case "session.question.answered": return "问题答案已提交";
    case "session.draft_revision.created": return "四件工件草稿已生成";
    case "session.draft_revision.confirmation_requested": return "四件工件正在执行确认校准";
    case "session.draft_revision.confirmed": return "四件工件已确认";
    case "agent_run.started": return "探索 Agent 已开始";
    case "agent_run.completed": return "探索 Agent 已完成";
    case "session.closed": return "Draft 会话已关闭";
    case "session.attached_to_run": return "Draft 已关联后续 Run";
    default: return "会话进度已更新";
  }
}
