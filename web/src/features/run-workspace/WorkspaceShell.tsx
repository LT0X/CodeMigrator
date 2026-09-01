import { useEffect, useState, type ReactNode } from "react";
import { selectActivePersonas } from "../../shared/stage/stageReducer";
import type { Celebration, SliceProjection, StageAction, StageState } from "../../shared/stage/types";

export const mascotIdentity = (sliceId: string, generation: number): string => `${sliceId}:${generation}`;

export const mascotVisualState = (action: StageAction): "running" | "waiting" | "failed" | "verified" => ({
  run: "running",
  wait: "waiting",
  error: "failed",
  verified: "verified",
} as const)[action];

const actionLabel: Record<StageAction, string> = {
  run: "作业中",
  wait: "等待中",
  error: "重生成",
  verified: "已验证",
};

function PixelCat({ action }: { action: StageAction }) {
  return <svg className="pixel-cat" viewBox="0 0 48 48" aria-hidden="true">
    <g className="cat-tail"><rect x="35" y="25" width="5" height="5" /><rect x="39" y="20" width="4" height="6" /></g>
    <g className="cat-body"><rect x="11" y="22" width="27" height="16" rx="2" /><rect x="15" y="36" width="7" height="5" /><rect x="29" y="36" width="7" height="5" /></g>
    <g className="cat-head"><path d="M12 23V10l7 5h11l7-5v13Z" /><rect x="16" y="17" width="5" height="4" className="cat-eye cat-eye-open" /><rect x="28" y="17" width="5" height="4" className="cat-eye cat-eye-open" /><rect x="16" y="20" width="5" height="1" className="cat-eye cat-eye-closed" /><rect x="28" y="20" width="5" height="1" className="cat-eye cat-eye-closed" /></g>
    <g className="cat-paws"><rect x="16" y="34" width="6" height="5" /><rect x="27" y="34" width="6" height="5" /></g>
    <g className="cat-glasses"><rect x="14" y="16" width="10" height="6" /><rect x="25" y="16" width="10" height="6" /><rect x="23" y="18" width="3" height="2" /></g>
    <g className="cat-sweat"><path d="M39 14c4 5 2 8 0 8s-4-3 0-8Z" /></g>
    <g className="cat-stars"><path d="m8 8 2 4 4 2-4 2-2 4-2-4-4-2 4-2Z" /><path d="m39 5 1 3 3 1-3 1-1 3-1-3-3-1 3-1Z" /></g>
    {action === "run" && <g className="cat-keyboard" aria-hidden="true"><rect x="12" y="42" width="24" height="4" rx="1" /><path d="M17 42v4M22 42v4M27 42v4M32 42v4" /></g>}
  </svg>;
}

function FolderStream() {
  return <div className="folder-stream" aria-hidden="true"><span className="token token-one">&lt;/&gt;</span><span className="token token-two">{ }</span><svg viewBox="0 0 40 32"><path d="M3 8h13l4 4h17v17H3Z" /><path d="M3 8V5h13l4 3" /><path d="M10 19h18M10 24h12" /></svg><small>write_set</small></div>;
}

function Mascot({ slice, action = slice.action, celebration = false }: { slice: SliceProjection; action?: StageAction; celebration?: boolean }) {
  const identity = mascotIdentity(slice.id, slice.generation);
  const visualState = mascotVisualState(action);
  return <article className={`mascot mascot-${visualState}${celebration ? " mascot-celebration" : ""}`} data-persona-key={identity} data-state={visualState} aria-label={`${slice.id} generation ${slice.generation}: ${actionLabel[action]}`}>
    <PixelCat action={action} />
    <div className="mascot-caption"><code>{slice.id}</code><span>g{slice.generation} · {actionLabel[action]}</span></div>
    {!celebration && <FolderStream />}
  </article>;
}

function SliceCapsule({ slice, focused, locked, pulse, onToggle }: { slice: SliceProjection; focused: boolean; locked: boolean; pulse: boolean; onToggle: () => void }) {
  const placeholder = !slice.persona && (slice.status === "CONTRACT_BLOCKED" || slice.status === "READY");
  if (placeholder) return <div className="slice-capsule capsule-placeholder" data-slice-id={slice.id}><span aria-hidden="true">◌</span><code>{slice.id}</code><strong>等待契约集成</strong><small>agent · 未提供</small></div>;
  return <button type="button" aria-pressed={locked} onClick={onToggle} className={`slice-capsule state-${mascotVisualState(slice.action)}${focused ? " is-focused" : ""}${locked ? " is-locked" : ""}${pulse ? " is-pulsed" : ""}`} data-slice-id={slice.id}>
    <span className="capsule-status">{actionLabel[slice.action]}</span><code>{slice.id}</code><span>g{slice.generation} · {slice.kind}</span><small>agent · 未提供 · attempt · 未提供</small>{locked && <b>锁定</b>}
  </button>;
}

function CelebrationFlyover({ item, slice }: { item: Celebration; slice: SliceProjection | undefined }) {
  const [visible, setVisible] = useState(true);
  useEffect(() => { setVisible(true); const timer = window.setTimeout(() => setVisible(false), 1500); return () => window.clearTimeout(timer); }, [item.key]);
  if (!visible || !slice) return null;
  return <div className="celebration-flyover" aria-live="polite"><Mascot slice={slice} action="verified" celebration /><span>已汇入 Verified Spine</span></div>;
}

function Inspector({ state }: { state: StageState }) {
  const slice = state.focusedId ? state.slices[state.focusedId] : undefined;
  return <aside className="workspace-inspector" aria-label="只读上下文检查器"><div className="panel-kicker">Context Inspector <span>只读</span></div><h2>{slice?.id ?? "未选择对象"}</h2>{slice ? <dl><div><dt>当前动作</dt><dd>{actionLabel[slice.action]}</dd></div><div><dt>generation</dt><dd>g{slice.generation}</dd></div><div><dt>agent</dt><dd>未提供</dd></div><div><dt>attempt</dt><dd>未提供</dd></div><div><dt>checks</dt><dd>未提供</dd></div><div><dt>状态</dt><dd>{slice.status}</dd></div></dl> : <p>选择 Slice 查看已提供的运行事实。</p>}</aside>;
}

export function WorkspaceShell({ state, onToggle, onClearFocus, runId = "run-mock-001", demoControls, presentationCelebrationKey }: { state: StageState; onToggle: (id: string) => void; onClearFocus?: () => void; runId?: string; demoControls?: ReactNode; presentationCelebrationKey?: string | null }) {
  const slices = Object.values(state.slices).sort((left, right) => right.lastSequence - left.lastSequence || left.id.localeCompare(right.id));
  const active = selectActivePersonas(state, 16, 8);
  const activeIds = new Set(active.map((slice) => slice.id));
  const capsules = slices.filter((slice) => slice.zone !== "confluence" || slice.persona).slice(0, 6);
  const queued = slices.filter((slice) => slice.integrationRank !== null).sort((left, right) => (left.integrationRank ?? 0) - (right.integrationRank ?? 0));
  const celebration = state.celebrations.find((item) => item.key === presentationCelebrationKey) ?? null;
  return <main className="workspace-shell" data-workspace-shell="true">
    <header className="workspace-header"><div className="brand"><span className="brand-mark" aria-hidden="true">⌁</span><span>CodeMigrator</span></div><div className="workspace-run"><span>当前迁移 Run</span><code>{runId}</code><b>{state.runStatus}</b></div><div className="workspace-connection" data-state={state.connection}><i aria-hidden="true" />{state.connection}{demoControls}</div></header>
    <nav className="workspace-nav" aria-label="迁移工作台导航"><span className="nav-active">EX</span><span>迁移汇流场</span><span>Slice DAG</span><span>证据</span></nav>
    <section className="workspace-stage" aria-label="中央迁移舞台"><div className="stage-toolbar"><div><span className="eyebrow">Migration Confluence</span><h1>中央迁移汇流场</h1></div><span>{active.length} 个活动 persona · 最大 4 个</span></div><div className="stage-surround"><div className="capsule-flank">{capsules.filter((_, index) => index % 2 === 0).map((slice) => <SliceCapsule key={slice.id} slice={slice} focused={state.focusedId === slice.id} locked={state.lockedId === slice.id} pulse={state.pulseIds.includes(slice.id)} onToggle={() => onToggle(slice.id)} />)}</div><div className="mascot-stage">{active.length ? active.map((slice) => <Mascot key={mascotIdentity(slice.id, slice.generation)} slice={slice} />) : <div className="stage-empty">等待后端派发 persona</div>}{celebration && <CelebrationFlyover item={celebration} slice={state.slices[celebration.sliceId]} />}</div><div className="capsule-flank">{capsules.filter((_, index) => index % 2 === 1).map((slice) => <SliceCapsule key={slice.id} slice={slice} focused={state.focusedId === slice.id} locked={state.lockedId === slice.id} pulse={state.pulseIds.includes(slice.id)} onToggle={() => onToggle(slice.id)} />)}</div></div><section className="workspace-details"><section className="verified-spine"><div className="panel-kicker">Verified Spine <span>{state.celebrations.length} 条</span></div><h2>唯一已验证主线</h2>{state.celebrations.length ? state.celebrations.map((item) => <div className="spine-item" key={item.key}><b>✓</b><code>{item.commitOid ?? "未提供"}</code><span>{item.sliceId} · g{item.generation}</span></div>) : <p>等待第一条 verified 事实</p>}</section><section className="integration-queue"><div className="panel-kicker">Frozen Integration Queue <span>只读</span></div><h2>集成队列</h2>{queued.length ? queued.map((slice) => <div className="queue-item" key={slice.id}><b>#{slice.integrationRank}</b><code>{slice.id}</code><span>{slice.status}</span></div>) : <p>无已提供的冻结集成序</p>}</section></section><section className="event-timeline"><div className="panel-kicker">Event Timeline <span>sequence {state.cursor}</span></div><ol>{state.timeline.slice(-8).reverse().map((item) => <li key={`${item.sequence}:${item.type}`}><code>{item.sequence}</code><strong>{item.label}</strong>{item.sliceId && <span>{item.sliceId}</span>}</li>)}</ol></section></section>
    <Inspector state={state} />
    <footer className="workspace-activity" aria-label="运行事件活动条"><span aria-live="polite">sequence {state.cursor} · {state.connection}</span><span>{state.lockedId ? `已锁定 ${state.lockedId}` : "自动跟随最新状态变化"}</span>{state.lockedId && <button type="button" onClick={onClearFocus}>按 Esc 解锁</button>}</footer>
  </main>;
}
