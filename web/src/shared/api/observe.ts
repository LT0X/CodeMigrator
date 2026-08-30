import type { WorkspaceProjection } from "../../entities/projections";
import type { ApiClient } from "./client";
import type { RunEvent } from "../stage/types";

const delay = (milliseconds: number, signal?: AbortSignal): Promise<void> => new Promise((resolve, reject) => {
  const timer = setTimeout(resolve, milliseconds);
  signal?.addEventListener("abort", () => { clearTimeout(timer); reject(signal.reason); }, { once: true });
});

export async function* observeRun(client: ApiClient, runId: string, signal?: AbortSignal, initialSequence?: number): AsyncIterable<RunEvent> {
  const snapshot: WorkspaceProjection | null = initialSequence === undefined ? await client.getWorkspace(runId) : null;
  let cursor = initialSequence ?? snapshot?.latest_sequence ?? 0;
  let retries = 0;
  while (!signal?.aborted && retries < 3) {
    try {
      let needsReconnect = false;
      for await (const event of client.streamEvents(runId, cursor, signal)) {
        if (event.sequence <= cursor) continue;
        if (event.sequence !== cursor + 1) {
          retries += 1;
          needsReconnect = true;
          break;
        }
        cursor = event.sequence;
        retries = 0;
        yield event;
      }
      if (needsReconnect) continue;
      return;
    } catch (error) {
      if (signal?.aborted) return;
      retries += 1;
      if (retries >= 3) throw error;
      await delay(250 * retries, signal);
    }
  }
}
