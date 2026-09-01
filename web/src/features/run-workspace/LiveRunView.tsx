import { useEffect, useRef, useState } from "react";
import type { ApiClient } from "../../shared/api/client";
import { observeRun } from "../../shared/api/observe";
import { clearFocusLock, hydrateStage, reduceStage, toggleFocusLock } from "../../shared/stage/stageReducer";
import type { StageState } from "../../shared/stage/types";
import { presentationCelebration } from "./demoPlayback";
import { WorkspaceShell } from "./WorkspaceShell";

export function LiveRunView({ client, runId }: { client: ApiClient; runId: string }) {
  const [state, setState] = useState<StageState | null>(null);
  const [diagnostic, setDiagnostic] = useState<string | null>(null);
  const [presentationCelebrationKey, setPresentationCelebrationKey] = useState<string | null>(null);
  const stageRef = useRef<StageState | null>(null);
  const catchingUpRef = useRef(false);
  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      try {
        const snapshot = await client.getWorkspace(runId);
        const hydrated = hydrateStage(snapshot);
        stageRef.current = hydrated;
        setState(hydrated);
        for await (const event of observeRun(client, runId, controller.signal, snapshot.latest_sequence, (phase) => {
          catchingUpRef.current = phase === "start";
        })) {
          const current = stageRef.current;
          if (!current) continue;
          const next = reduceStage(current, event);
          stageRef.current = next;
          setState(next);
          const celebration = catchingUpRef.current ? null : presentationCelebration(current, next);
          if (celebration) setPresentationCelebrationKey(celebration);
        }
      } catch {
        setDiagnostic("无法连接服务，未展示虚构运行事实；请检查 server status。 ");
      }
    })();
    return () => controller.abort();
  }, [client, runId]);
  if (state) return <WorkspaceShell state={state} runId={runId} presentationCelebrationKey={presentationCelebrationKey} onToggle={(id) => {
    const next = toggleFocusLock(stageRef.current ?? state, id);
    stageRef.current = next;
    setState(next);
  }} onClearFocus={() => {
    const next = clearFocusLock(stageRef.current ?? state);
    stageRef.current = next;
    setState(next);
  }} />;
  return <main className="app-shell live-app-shell">{diagnostic ? <p className="diagnostic" role="status">{diagnostic}</p> : <p className="empty-state">正在读取 workspace 快照…</p>}</main>;
}
