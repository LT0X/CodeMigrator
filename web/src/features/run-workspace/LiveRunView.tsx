import { useEffect, useState } from "react";
import type { ApiClient } from "../../shared/api/client";
import { observeRun } from "../../shared/api/observe";
import { hydrateStage, reduceStage, toggleFocusLock } from "../../shared/stage/stageReducer";
import type { StageState } from "../../shared/stage/types";
import { StageBoard } from "./StageBoard";

export function LiveRunView({ client, runId }: { client: ApiClient; runId: string }) {
  const [state, setState] = useState<StageState | null>(null);
  const [diagnostic, setDiagnostic] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      try {
        const snapshot = await client.getWorkspace(runId);
        setState(hydrateStage(snapshot));
        for await (const event of observeRun(client, runId, controller.signal, snapshot.latest_sequence)) {
          setState((current) => current ? reduceStage(current, event) : current);
        }
      } catch {
        setDiagnostic("无法连接服务，未展示虚构运行事实；请检查 server status。 ");
      }
    })();
    return () => controller.abort();
  }, [client, runId]);
  return <main className="app-shell live-app-shell">
    <header className="product-header">
      <div className="brand"><span className="brand-mark" aria-hidden="true">⌁</span><span>CodeMigrator</span></div>
      <div className="run-heading"><span className="eyebrow">当前迁移 Run</span><code>{runId}</code><span className="status-chip">{state?.runStatus ?? "读取中"}</span></div>
      <div className="connection" data-state={state?.connection ?? "connecting"}><span className="connection-dot" aria-hidden="true" />{state?.connection ?? "connecting"}</div>
    </header>
    <section className="workbench-heading">
      <div><p className="eyebrow">迁移汇流场 / 实时投影</p><h1>看见事实如何汇入唯一主线</h1><p className="lede">舞台只归约已提交事件；点击卡片只锁定观察对象，不改变 Run。</p></div>
      <div className="activity-summary"><strong>{state ? Object.values(state.slices).filter((slice) => slice.persona && slice.zone === "work").length : "—"}</strong><span>活动 persona<br />最多 4 个</span></div>
    </section>
    {diagnostic && <p className="diagnostic" role="status">{diagnostic}</p>}
    {state ? <StageBoard state={state} onToggle={(id) => setState((current) => current ? toggleFocusLock(current, id) : current)} /> : !diagnostic && <p className="empty-state">正在读取 workspace 快照…</p>}
  </main>;
}
