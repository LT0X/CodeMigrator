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

const parseSse = (chunk: string): RunEvent | null => {
  const data = chunk.split("\n").filter((line) => line.startsWith("data:" )).map((line) => line.slice(5).trim()).join("\n");
  if (!data) return null;
  const candidate: unknown = JSON.parse(data);
  if (typeof candidate !== "object" || candidate === null) return null;
  const record = candidate as Record<string, unknown>;
  if (typeof record.type !== "string" || typeof record.sequence !== "number" || !Number.isInteger(record.sequence) || record.sequence < 1 || typeof record.data !== "object" || record.data === null) return null;
  return {
    type: record.type,
    sequence: record.sequence,
    data: record.data as Record<string, unknown>,
    timestamp_utc: typeof record.timestamp_utc === "string" ? record.timestamp_utc : "",
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
  const request = (path: string, init?: RequestInit) => fetchImpl(`${baseUrl}${path}`, { ...init, headers: { Accept: "application/json", ...init?.headers } });
  return {
    listMigrations: async () => (await readJson<{ items?: MigrationProjection[] }>(request("/migrations"))).items ?? [],
    getWorkspace: async (runId) => readJson<WorkspaceProjection>(request(`/migrations/${encode(runId)}/workspace`)),
    getReport: async (runId) => readJson<ReportProjection>(request(`/migrations/${encode(runId)}/report`)),
    getEvidence: async (runId, receiptId) => readJson<EvidenceProjection>(request(`/migrations/${encode(runId)}/evidence/${encode(receiptId)}`)),
    getHealth: async () => readJson<HealthProjection>(request("/system/health")),
    sendSessionMessage: async (sessionId, message, revision) => readJson<SessionProjection>(request(`/sessions/${encode(sessionId)}/messages`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ message, revision }) })),
    answerSession: async (sessionId, questionId, answer, revision) => readJson<SessionProjection>(request(`/sessions/${encode(sessionId)}/answers`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question_id: questionId, answer, revision }) })),
    confirmSession: async (sessionId, revision) => readJson<SessionProjection>(request(`/sessions/${encode(sessionId)}/confirm`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ revision }) })),
    confirmCorrection: async (sessionId, correctionId, previewHash) => readJson<SessionProjection>(request(`/sessions/${encode(sessionId)}/corrections/${encode(correctionId)}/confirm`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ preview_hash: previewHash }) })),
    streamEvents: async function* (runId, afterSequence, signal) {
      const response = await requireResponse(await request(`/migrations/${encode(runId)}/events`, { headers: { Accept: "text/event-stream", "Last-Event-ID": String(afterSequence) }, signal }));
      yield* readEvents(response);
    },
  };
}

export { parseSse };
