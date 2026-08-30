import type {
  EvidenceProjection,
  HealthProjection,
  MigrationProjection,
  ReportProjection,
  SessionProjection,
  WorkspaceProjection,
} from "../../entities/projections";
import type { RunEvent } from "../stage/types";

export interface ApiClientOptions {
  readonly baseUrl?: string;
  readonly fetchImpl?: typeof fetch;
  readonly token?: string;
}

export interface ApiClient {
  listMigrations(): Promise<MigrationProjection[]>;
  getWorkspace(runId: string): Promise<WorkspaceProjection>;
  getReport(runId: string): Promise<ReportProjection>;
  getEvidence(runId: string, receiptId: string): Promise<EvidenceProjection>;
  getHealth(): Promise<HealthProjection>;
  sendSessionMessage(sessionId: string, message: string, revision?: number): Promise<SessionProjection>;
  answerSession(sessionId: string, questionId: string, answer: unknown, revision: number): Promise<SessionProjection>;
  confirmSession(sessionId: string, revision: number): Promise<SessionProjection>;
  confirmCorrection(sessionId: string, correctionId: string, previewHash: string): Promise<SessionProjection>;
  streamEvents(runId: string, afterSequence: number, signal?: AbortSignal): AsyncIterable<RunEvent>;
}

const requireResponse = async (response: Response): Promise<Response> => {
  if (!response.ok) throw new Error(`API request failed: ${response.status}`);
  return response;
};

const readJson = async <T>(response: Response | Promise<Response>): Promise<T> =>
  (await requireResponse(await response)).json() as Promise<T>;

const encode = (value: string): string => encodeURIComponent(value);

const idempotencyKey = (): string => {
  if (typeof globalThis.crypto?.randomUUID === "function") return globalThis.crypto.randomUUID();
  return `cm-${Date.now()}-${Math.random().toString(36).slice(2)}`;
};

const parseSse = (chunk: string): RunEvent | null => {
  const lines = chunk.split("\n");
  const id = lines.find((line) => line.startsWith("id:"))?.slice(3).trim();
  const data = lines
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).trim())
    .join("\n");
  if (!data) return null;
  let candidate: unknown;
  try {
    candidate = JSON.parse(data);
  } catch {
    return null;
  }
  if (typeof candidate !== "object" || candidate === null) return null;
  const record = candidate as Record<string, unknown>;
  if (
    record.schema !== "migration.event" ||
    record.version !== 1 ||
    typeof record.type !== "string" ||
    typeof record.sequence !== "number" ||
    !Number.isInteger(record.sequence) ||
    record.sequence < 1 ||
    typeof record.data !== "object" ||
    record.data === null ||
    Array.isArray(record.data)
  ) return null;
  if (id !== undefined && id !== String(record.sequence)) return null;
  return {
    type: record.type,
    sequence: record.sequence,
    data: record.data as Record<string, unknown>,
    timestamp_utc: typeof record.timestamp_utc === "string" ? record.timestamp_utc : "",
    schema: "migration.event",
    version: 1,
    sse_id: id ?? String(record.sequence),
  };
};

async function* readEvents(response: Response): AsyncIterable<RunEvent> {
  if (!response.body) return;
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const result = await reader.read();
    if (result.done) break;
    buffer += decoder.decode(result.value, { stream: true }).replaceAll("\r\n", "\n");
    const chunks = buffer.split("\n\n");
    buffer = chunks.pop() ?? "";
    for (const chunk of chunks) {
      const event = parseSse(chunk);
      if (event) yield event;
    }
  }
  const finalEvent = parseSse(buffer);
  if (finalEvent) yield finalEvent;
}

export function createApiClient(options: ApiClientOptions = {}): ApiClient {
  const baseUrl = (options.baseUrl ?? "/api/v1").replace(/\/$/, "");
  const fetchImpl = options.fetchImpl ?? fetch;
  const request = (path: string, init?: RequestInit) => {
    const headers = new Headers(init?.headers);
    headers.set("Accept", headers.get("Accept") ?? "application/json");
    if (options.token) headers.set("Authorization", `Bearer ${options.token}`);
    return fetchImpl(`${baseUrl}${path}`, {
      ...init,
      credentials: init?.credentials ?? "same-origin",
      headers,
    });
  };
  const writeRequest = (path: string, body: object) => request(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "Idempotency-Key": idempotencyKey() },
    body: JSON.stringify(body),
  });
  return {
    listMigrations: async () => (await readJson<{ items?: MigrationProjection[] }>(request("/migrations"))).items ?? [],
    getWorkspace: async (runId) => readJson<WorkspaceProjection>(request(`/migrations/${encode(runId)}/workspace`)),
    getReport: async (runId) => readJson<ReportProjection>(request(`/migrations/${encode(runId)}/report`)),
    getEvidence: async (runId, receiptId) => readJson<EvidenceProjection>(request(`/migrations/${encode(runId)}/evidence/${encode(receiptId)}`)),
    getHealth: async () => readJson<HealthProjection>(request("/system/health")),
    sendSessionMessage: async (sessionId, message, revision) => readJson<SessionProjection>(writeRequest(`/sessions/${encode(sessionId)}/messages`, { message, revision })),
    answerSession: async (sessionId, questionId, answer, revision) => readJson<SessionProjection>(writeRequest(`/sessions/${encode(sessionId)}/answers`, { question_id: questionId, answer, revision })),
    confirmSession: async (sessionId, revision) => readJson<SessionProjection>(writeRequest(`/sessions/${encode(sessionId)}/confirm`, { revision })),
    confirmCorrection: async (sessionId, correctionId, previewHash) => readJson<SessionProjection>(writeRequest(`/sessions/${encode(sessionId)}/corrections/${encode(correctionId)}/confirm`, { preview_hash: previewHash })),
    streamEvents: async function* (runId, afterSequence, signal) {
      const response = await requireResponse(await request(`/migrations/${encode(runId)}/events`, { headers: { Accept: "text/event-stream", "Last-Event-ID": String(afterSequence) }, signal }));
      yield* readEvents(response);
    },
  };
}

export { parseSse };
