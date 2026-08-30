import type { SliceProjection, StageAction, StageState, StageZone } from "../../shared/stage/types";
import { selectActivePersonas } from "../../shared/stage/stageReducer";

const zoneInfo: readonly { zone: StageZone; action: StageAction; title: string; hint: string }[] = [
  { zone: "work", action: "run", title: "作业区", hint: "翻译与局部自检" },
  { zone: "waiting", action: "wait", title: "等待区", hint: "契约阻塞与集成队列" },
  { zone: "regeneration", action: "error", title: "重生成位", hint: "归因后的定向重派" },
  { zone: "confluence", action: "verified", title: "汇流口", hint: "verified 庆祝后汇入主线" },
];

const actionIcon: Record<StageAction, string> = { run: "↗", wait: "◌", error: "!", verified: "✓" };

function StageCard({ slice, locked, pulse, onToggle }: { slice: SliceProjection; locked: boolean; pulse: boolean; onToggle: () => void }) {
  return (
    <button aria-pressed={locked} className={`slice-card state-${slice.action}${locked ? " is-locked" : ""}${pulse ? " is-pulsed" : ""}`} onClick={onToggle} type="button">
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
        {slices.map((slice) => slice.status === "CONTRACT_BLOCKED" || slice.status === "READY" ? <div className="contract-placeholder" key={slice.id}><span aria-hidden="true">◌</span><span>{slice.id} · 等待契约集成</span><small>由后端 readiness 事实提供</small></div> : <StageCard key={`${slice.id}-${state.pulseVersion[slice.id] ?? 0}`} slice={slice} locked={state.lockedId === slice.id} pulse={state.pulseIds.includes(slice.id)} onToggle={() => onToggle(slice.id)} />)}
      </div>
    </section>
  );
}

export function StageBoard({ state, onToggle }: { state: StageState; onToggle: (id: string) => void }) {
  const activeIds = new Set(selectActivePersonas(state, 16, 8).map((slice) => slice.id));
  const slicesByZone = (zone: StageZone) => Object.values(state.slices).filter((slice) => {
    if (slice.zone !== zone) return false;
    if (zone === "confluence" && !slice.persona) return false;
    return zone !== "work" && zone !== "regeneration" || !slice.persona || activeIds.has(slice.id);
  });

  const mobileSlices = Object.values(state.slices).sort((left, right) => (left.integrationRank ?? Number.MAX_SAFE_INTEGER) - (right.integrationRank ?? Number.MAX_SAFE_INTEGER) || left.id.localeCompare(right.id));
  return <>
    <div className="mobile-slice-list" aria-label="按冻结集成序排列的 Slice 列表">{mobileSlices.map((slice) => <div key={slice.id} className="queue-row"><code>{slice.id}</code><span>{slice.status}</span><b>g{slice.generation}</b></div>)}</div>
    <section className="stage-grid" aria-label="四场舞台">
      {zoneInfo.map((info) => <Zone key={info.zone} info={info} slices={slicesByZone(info.zone)} state={state} onToggle={onToggle} />)}
    </section>
    <section className="workspace-lower">
      <section className="spine-panel panel"><div className="panel-heading"><div><span className="eyebrow">Verified Spine</span><h2>唯一已验证主线</h2></div><span className="count-badge">{state.celebrations.length} 条</span></div>{state.celebrations.length === 0 ? <p className="empty-state">等待第一条 verified 事实</p> : state.celebrations.map((item) => <div className="spine-row" key={item.key}><span className="spine-check">✓</span><code>{item.commitOid ?? "commit"}</code><span>{item.sliceId} · g{item.generation}</span></div>)}</section>
      <aside className="inspector panel"><div className="panel-heading"><div><span className="eyebrow">Context Inspector</span><h2>{state.focusedId ?? "未选择对象"}</h2></div><span className="read-only">只读</span></div>{state.focusedId && state.slices[state.focusedId] ? <dl className="facts"><div><dt>当前动作</dt><dd>{state.slices[state.focusedId].action}</dd></div><div><dt>代次</dt><dd>g{state.slices[state.focusedId].generation}</dd></div><div><dt>状态</dt><dd>{state.slices[state.focusedId].status}</dd></div></dl> : <p className="empty-state">选择 persona 查看受限事实</p>}</aside>
    </section>
    <section className="panel queue-panel"><div className="panel-heading"><div><span className="eyebrow">Slice DAG / Frozen Integration Queue</span><h2>Slice 依赖与冻结集成序摘要</h2></div><span className="read-only">只读排序</span></div><div className="queue-list" aria-label="Slice DAG 冻结集成序摘要">{Object.values(state.slices).filter((slice) => slice.integrationRank !== null).sort((left, right) => (left.integrationRank ?? 0) - (right.integrationRank ?? 0) || left.id.localeCompare(right.id)).map((slice) => <div className="queue-row" key={slice.id}><b>#{slice.integrationRank}</b><code>{slice.id}</code><span>{slice.kind}</span><span>{slice.status}</span></div>)}</div></section>
    <section className="timeline panel"><div className="panel-heading"><div><span className="eyebrow">Event Timeline</span><h2>事件时间线</h2></div><code>sequence {state.cursor}</code></div><ol>{state.timeline.slice().reverse().map((item) => <li key={`${item.sequence}-${item.type}`}><span className="timeline-sequence">{item.sequence}</span><span><strong>{item.label}</strong>{item.sliceId && <code>{item.sliceId}</code>}</span></li>)}</ol></section>
    {state.notices.length > 0 && <section className="notice-strip" aria-live="polite">{state.notices.map((notice) => <span key={`${notice.sequence}-${notice.type}`}><b>{notice.label}</b>{notice.summary}</span>)}</section>}
  </>;
}
