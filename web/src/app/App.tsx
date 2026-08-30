import { useEffect, useMemo, useState } from "react";
import { ReportView } from "../features/run-report/ReportView";
import { RunListView } from "../features/run-list/RunListView";
import { SessionInputView } from "../features/session-input/SessionInputView";
import { createApiClient } from "../shared/api/client";
import { LiveRunView } from "../features/run-workspace/LiveRunView";
import { LiveReportView } from "../features/run-report/ReportView";
import { LiveRunListView } from "../features/run-list/RunListView";
import { LiveSystemHealthView } from "../features/system-health/SystemHealthView";
import { StageBoard } from "../features/run-workspace/StageBoard";
import { mockRunEvents } from "../shared/stage/mockEvents";
import { clearFocusLock, createInitialStageState, reduceStage, toggleFocusLock } from "../shared/stage/stageReducer";
import type { StageState } from "../shared/stage/types";

const phases = ["PLAN", "EXECUTE", "VERIFY", "REPORT"];

function DemoWorkspace() {
  const initial = useMemo(() => mockRunEvents.reduce(reduceStage, createInitialStageState()), []);
  const [state, setState] = useState<StageState>(initial);
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => { if (event.key === "Escape") setState((current) => clearFocusLock(current)); };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);
  return (
    <main className="app-shell">
      <header className="product-header">
        <div className="brand"><span className="brand-mark" aria-hidden="true">⌁</span><span>CodeMigrator</span></div>
        <div className="run-heading"><span className="eyebrow">当前迁移 Run</span><code>run-mock-001</code><span className="status-chip">{state.runStatus}</span></div>
        <div className="connection" data-state={state.connection}><span className="connection-dot" aria-hidden="true" />{state.connection}</div>
      </header>
      <nav className="phase-bar" aria-label="迁移阶段">
        {phases.map((phase, index) => <span className={`phase ${index < 2 ? "is-active" : ""}`} key={phase}><b>{String(index + 1).padStart(2, "0")}</b>{phase}</span>)}
      </nav>
      <section className="workbench-heading">
        <div><p className="eyebrow">迁移汇流场 / TypeScript → Python</p><h1>看见事实如何汇入唯一主线</h1><p className="lede">所有舞台变化来自已提交事件；点击卡片只锁定观察对象，不改变 Run。</p></div>
        <div className="activity-summary"><strong>{Object.values(state.slices).filter((slice) => slice.persona && slice.zone === "work").length}</strong><span>活动 persona<br />最多 4 个</span></div>
      </section>
      <StageBoard state={state} onToggle={(id) => setState((current) => toggleFocusLock(current, id))} />
      <footer className="app-footer"><span>本地演示事件源 · 事件驱动展示</span><span>Web 只读运行投影</span></footer>
    </main>
  );
}

export function App() {
  const path = window.location.pathname;
  const client = useMemo(() => createApiClient(), []);
  const isDemo = path === "/demo" || new URLSearchParams(window.location.search).get("demo") === "1";
  if (isDemo) {
    const reportMatch = path.match(/^\/runs\/([^/]+)\/report$/);
    return reportMatch ? <ReportView report={{ run_id: reportMatch[1], status: "COMPLETED" }} /> : <DemoWorkspace />;
  }
  if (path === "/") return <LiveRunListView client={client} fallback={<RunListView runs={[]} />} />;
  const reportMatch = path.match(/^\/runs\/([^/]+)\/report$/);
  if (reportMatch) return <LiveReportView client={client} runId={reportMatch[1]} />;
  if (path === "/system") return <LiveSystemHealthView client={client} />;
  const runMatch = path.match(/^\/runs\/([^/]+)$/);
  if (runMatch) return <LiveRunView client={client} runId={runMatch[1]} />;
  if (path.startsWith("/sessions/")) return <SessionInputView sessionId={path.split("/").at(-1) ?? "new"} client={client} />;
  return <DemoWorkspace />;
}
