import { useEffect, useMemo, useState } from "react";
import { ReportView } from "../features/run-report/ReportView";
import { RunListView } from "../features/run-list/RunListView";
import { SessionInputView } from "../features/session-input/SessionInputView";
import { createApiClient } from "../shared/api/client";
import { LiveRunView } from "../features/run-workspace/LiveRunView";
import { LiveReportView } from "../features/run-report/ReportView";
import { LiveRunListView } from "../features/run-list/RunListView";
import { LiveSystemHealthView } from "../features/system-health/SystemHealthView";
import { mockRunEvents } from "../shared/stage/mockEvents";
import { clearFocusLock, toggleFocusLock } from "../shared/stage/stageReducer";
import { advanceDemoPlayback, createDemoPlayback, replayDemoPlayback, toggleDemoPlayback } from "../features/run-workspace/demoPlayback";
import { WorkspaceShell } from "../features/run-workspace/WorkspaceShell";

function DemoWorkspace() {
  const events = useMemo(() => mockRunEvents, []);
  const [playback, setPlayback] = useState(() => createDemoPlayback(events));
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => { if (event.key === "Escape") setPlayback((current) => ({ ...current, state: clearFocusLock(current.state) })); };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);
  useEffect(() => {
    if (!playback.isPlaying) return;
    const timer = window.setInterval(() => setPlayback(advanceDemoPlayback), 750);
    return () => window.clearInterval(timer);
  }, [playback.isPlaying]);
  return <WorkspaceShell state={playback.state} runId="run-mock-001" presentationCelebrationKey={playback.presentationCelebrationKey} onToggle={(id) => setPlayback((current) => ({ ...current, state: toggleFocusLock(current.state, id) }))} onClearFocus={() => setPlayback((current) => ({ ...current, state: clearFocusLock(current.state) }))} demoControls={<span className="demo-controls"><button type="button" onClick={() => setPlayback(toggleDemoPlayback)}>{playback.isPlaying ? "暂停" : "继续"}</button><button type="button" onClick={() => setPlayback(replayDemoPlayback)}>重播</button></span>} />;
}

function RouteNotFound() {
  return <main className="app-shell"><section className="page-panel page-diagnostic" role="status"><span className="eyebrow">404 / 页面不存在</span><h1>没有可展示的运行事实</h1><p className="diagnostic">请检查地址，或从 Run 首页进入已提交的运行投影。</p><a href="/">返回 Run 首页</a></section></main>;
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
  return <RouteNotFound />;
}
