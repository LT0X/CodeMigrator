import type { WorkspaceProjection } from "../../entities/projections";
import type { ApiClient } from "./client";
import type { RunEvent } from "../stage/types";

const delay = (milliseconds: number, signal?: AbortSignal): Promise<void> => new Promise((resolve, reject) => {
  const timer = setTimeout(resolve, milliseconds);
  signal?.addEventListener("abort", () => { clearTimeout(timer); reject(signal.reason); }, { once: true });
});

export type ObservationPhase = "start" | "end";
type ObservationLifecycle = (phase: ObservationPhase) => void;

export async function* observeRun(client: ApiClient, runId: string, signal?: AbortSignal, initialSequence?: number, onPhase?: ObservationLifecycle): AsyncIterable<RunEvent> {
  const snapshot: WorkspaceProjection | null = initialSequence === undefined ? await client.getWorkspace(runId) : null;
  let cursor = initialSequence ?? snapshot?.latest_sequence ?? 0;
  let retries = 0;
  let catchUpThrough: number | null = null;

  const beginCatchUp = async (minimumThrough: number): Promise<void> => {
    const latest = await client.getWorkspace(runId);
    const through = Math.max(latest.latest_sequence, minimumThrough);
    if (through > cursor && catchUpThrough === null) {
      catchUpThrough = through;
      onPhase?.("start");
    }
  };

  while (!signal?.aborted && retries < 3) {
    try {
      let needsReconnect = false;
      for await (const event of client.streamEvents(runId, cursor, signal)) {
        if (event.sequence <= cursor) continue;
        if (event.sequence !== cursor + 1) {
          retries += 1;
          await beginCatchUp(event.sequence - 1);
          needsReconnect = true;
          break;
        }
        cursor = event.sequence;
        retries = 0;
        const isCatchUp = catchUpThrough !== null && event.sequence <= catchUpThrough;
        yield event;
        if (isCatchUp && catchUpThrough !== null && event.sequence >= catchUpThrough) {
          catchUpThrough = null;
          onPhase?.("end");
        }
      }
      if (needsReconnect) continue;
      return;
    } catch (error) {
      if (signal?.aborted) return;
      retries += 1;
      if (retries >= 3) throw error;
      await beginCatchUp(cursor);
      await delay(250 * retries, signal);
    }
  }
}
