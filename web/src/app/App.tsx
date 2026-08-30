import { useMemo, useState } from "react";
import { ReportView } from "../features/run-report/ReportView";
import { RunListView } from "../features/run-list/RunListView";
import { SessionInputView } from "../features/session-input/SessionInputView";
import { SystemHealthView } from "../features/system-health/SystemHealthView";
import { createApiClient } from "../shared/api/client";
import { mockRunEvents } from "../shared/stage/mockEvents";
import { createInitialStageState, reduceStage, selectActivePersonas, toggleFocusLock } from "../shared/stage/stageReducer";
import type { SliceProjection, StageAction, StageState, StageZone } from "../shared/stage/types";

const phases = ["PLAN", "EXECUTE", "VERIFY", "REPORT"];
const zoneInfo: readonly { zone: StageZone; action: StageAction; title: string; hint: string }[] = [
  { zone: "work", action: "run", title: "作业区", hint: "翻译与局部自检" },
  { zone: "waiting", action: "wait", title: "等待区", hint: "契约阻塞与集成队列" },
  { zone: "regeneration", action: "error", title: "重生成位", hint: "归因后的定向重派" },
  { zone: "confluence", action: "verified", title: "汇流口", hint: "verified 庆祝后汇入主线" },
];

const actionIcon: Record<StageAction, string> = { run: "↗", wait: "◌", error: "!", verified: "✓" };

function StageCard({ slice, locked, pulse, onToggle }: { slice: SliceProjection; locked: boolean; pulse: boolean; onToggle: () => void }) {
  return (
    <button className={`slice-card state-${slice.action}${locked ? " is-locked" : ""}${pulse ? " is-pulsed" : ""}`} onClick={onToggle} type="button">
      <span className="slice-avatar" aria-hidden="true">{actionIcon[slice.action]}</span>
      <span className="slice-card-body">
        <span className="slice-card-topline"><code>{slice.id}</code><span>{slice.kind}</span></span>
        <strong>{slice.action === "run" ? "作业中" : slice.action === "wait" ? "等待中" : slice.action === "error" ? "重生成" : "已汇入"}</strong>
        <span className="slice-card-meta">{slice.status} · g{slice.generation}</span>
      </span>
      {locked && <span className="lock-mark" aria-label="已锁定">锁定</span>}
    </button>
  );
}

function Zone({ info, slices, state, onToggle }: { info: typeof zoneInfo[number]; slices: SliceProjection[]; state: StageState; onToggle: (id: string) => void }) {
  return (
    <section className={`stage-zone zone-${info.zone}`} aria-labelledby={`${info.zone}-title`}>
      <header className="zone-header">
        <div><span className="zone-action">{actionIcon[info.action]} {info.action}</span><h2 id={`${info.zone}-title`}>{info.title}</h2></div>
        <span className="zone-hint">{info.hint}</span>
      </header>
      <div className="zone-cards">
        {slices.length === 0 && info.zone === "waiting" && <div className="contract-placeholder"><span aria-hidden="true">◌</span><span>等待契约集成</span><small>尚未派发，不占用 persona</small></div>}
        {slices.map((slice) => <StageCard key={slice.id} slice={slice} locked={state.lockedId === slice.id} pulse={state.pulseIds.includes(slice.id)} onToggle={() => onToggle(slice.id)} />)}
      </div>
    </section>
  );
}

export function App() {
  const path = window.location.pathname;
  const client = useMemo(() => createApiClient(), []);
  const initial = useMemo(() => mockRunEvents.reduce(reduceStage, createInitialStageState()), []);
  const [state, setState] = useState<StageState>(initial);
  if (path === "/") return <RunListView runs={[{ run_id: "run-mock-001", status: "COMPLETED", version: 14, report_delivery_status: "PENDING", code_delivery_status: "PENDING" }]} />;
  if (path.endsWith("/report")) return <ReportView report={{ run_id: "run-mock-001", status: "COMPLETED" }} />;
  if (path === "/system") return <SystemHealthView health={{ app: "healthy", postgres: "healthy", sandbox: "ready", optional_profiles: { "模型会话池": "ready", "沙箱执行池": "ready", "描述符资源": "ready" } }} />;
  if (path.startsWith("/sessions/")) return <SessionInputView sessionId={path.split("/").at(-1) ?? "new"} client={client} />;
  const activePersonas = selectActivePersonas(state, 16, 8);
  const activeIds = new Set(activePersonas.map((slice) => slice.id));
  const slicesByZone = (zone: StageZone) => Object.values(state.slices).filter((slice) => {
    if (slice.zone !== zone) return false;
    return zone !== "work" && zone !== "regeneration" || !slice.persona || activeIds.has(slice.id);
  });

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
        <div className="activity-summary"><strong>{activePersonas.length}</strong><span>活动 persona<br />最多 4 个</span></div>
      </section>
      <section className="stage-grid" aria-label="四场舞台">
        {zoneInfo.map((info) => <Zone key={info.zone} info={info} slices={slicesByZone(info.zone)} state={state} onToggle={(id) => setState((current) => toggleFocusLock(current, id))} />)}
      </section>
      <section className="workspace-lower">
        <section className="spine-panel panel"><div className="panel-heading"><div><span className="eyebrow">Verified Spine</span><h2>唯一已验证主线</h2></div><span className="count-badge">{state.celebrations.length} 条</span></div>{state.celebrations.length === 0 ? <p className="empty-state">等待第一条 verified 事实</p> : state.celebrations.map((item) => <div className="spine-row" key={item.key}><span className="spine-check">✓</span><code>{item.commitOid ?? "commit"}</code><span>{item.sliceId} · g{item.generation}</span></div>)}</section>
        <aside className="inspector panel"><div className="panel-heading"><div><span className="eyebrow">Context Inspector</span><h2>{state.focusedId ?? "未选择对象"}</h2></div><span className="read-only">只读</span></div>{state.focusedId && state.slices[state.focusedId] ? <dl className="facts"><div><dt>当前动作</dt><dd>{state.slices[state.focusedId].action}</dd></div><div><dt>代次</dt><dd>g{state.slices[state.focusedId].generation}</dd></div><div><dt>状态</dt><dd>{state.slices[state.focusedId].status}</dd></div></dl> : <p className="empty-state">选择 persona 查看受限事实</p>}</aside>
      </section>
      <section className="panel queue-panel"><div className="panel-heading"><div><span className="eyebrow">Frozen Integration Queue</span><h2>冻结集成顺序</h2></div><span className="read-only">只读排序</span></div><div className="queue-list">{Object.values(state.slices).filter((slice) => slice.integrationRank !== null).sort((left, right) => (left.integrationRank ?? 0) - (right.integrationRank ?? 0) || left.id.localeCompare(right.id)).map((slice) => <div className="queue-row" key={slice.id}><b>#{slice.integrationRank}</b><code>{slice.id}</code><span>{slice.status}</span></div>)}</div></section>
      <section className="timeline panel"><div className="panel-heading"><div><span className="eyebrow">Event Timeline</span><h2>事件时间线</h2></div><code>sequence {state.cursor}</code></div><ol>{state.timeline.slice().reverse().map((item) => <li key={`${item.sequence}-${item.type}`}><span className="timeline-sequence">{item.sequence}</span><span><strong>{item.label}</strong>{item.sliceId && <code>{item.sliceId}</code>}</span></li>)}</ol></section>
      {state.notices.length > 0 && <section className="notice-strip" aria-live="polite">{state.notices.map((notice) => <span key={`${notice.sequence}-${notice.type}`}><b>{notice.label}</b>{notice.summary}</span>)}</section>}
      <footer className="app-footer"><span>Wave 1 mock · 事件驱动展示</span><span>Web 只读运行投影</span></footer>
    </main>
  );
}
