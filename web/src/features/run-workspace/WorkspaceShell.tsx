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
  return (
    <svg className="pixel-cat" viewBox="0 0 40 40" aria-hidden="true">
      <g className="cat-part cat-tail">
        <rect x="29" y="18" width="3" height="3" fill="#ff6b35" />
        <rect x="31" y="15" width="3" height="3" fill="#ff6b35" />
        <rect x="33" y="12" width="3" height="3" fill="#ff6b35" />
        <rect x="34" y="9" width="3" height="3" fill="#ff6b35" />
        <rect x="35" y="7" width="2" height="2" fill="#ffd93d" />
      </g>
      <g className="cat-part cat-body">
        <rect x="10" y="18" width="20" height="11" fill="#ff6b35" />
        <rect x="10" y="27" width="20" height="2" fill="#cc4422" />
        <rect x="12" y="20" width="16" height="6" fill="#ff8855" opacity="0.4" />
      </g>
      <g className="cat-part cat-head">
        <rect x="12" y="7" width="3" height="4" fill="#ff6b35" />
        <rect x="13" y="8" width="1" height="2" fill="#ffd93d" />
        <rect x="25" y="7" width="3" height="4" fill="#ff6b35" />
        <rect x="26" y="8" width="1" height="2" fill="#ffd93d" />
        <g className="cat-stars">
          <path d="M11 2h2v1h2v2h-2v1h-2V5H9V3h2Z" fill="#ffd93d" />
          <path d="M19 0h2v1h2v2h-2v1h-2V3h-2V1h2Z" fill="#ffd93d" />
          <path d="M27 2h2v1h2v2h-2v1h-2V5h-2V3h2Z" fill="#ffd93d" />
        </g>
        <rect x="12" y="10" width="16" height="9" fill="#ff6b35" />
        <rect x="12" y="18" width="16" height="1" fill="#cc4422" />
        <g className="cat-eyes-open">
          <rect x="14" y="13" width="4" height="4" fill="#1a1a2e" />
          <rect x="22" y="13" width="4" height="4" fill="#1a1a2e" />
          <rect x="15" y="14" width="2" height="2" fill="#4ecdc4" />
          <rect x="23" y="14" width="2" height="2" fill="#4ecdc4" />
          <rect x="15" y="14" width="1" height="1" fill="#ffffff" opacity="0.8" />
          <rect x="23" y="14" width="1" height="1" fill="#ffffff" opacity="0.8" />
        </g>
        <g className="cat-eyes-closed">
          <rect x="14" y="15" width="4" height="1" fill="#1a1a2e" />
          <rect x="22" y="15" width="4" height="1" fill="#1a1a2e" />
          <rect x="14" y="14" width="1" height="1" fill="#1a1a2e" />
          <rect x="17" y="14" width="1" height="1" fill="#1a1a2e" />
          <rect x="22" y="14" width="1" height="1" fill="#1a1a2e" />
          <rect x="25" y="14" width="1" height="1" fill="#1a1a2e" />
        </g>
        <g className="cat-glasses">
          <rect x="13" y="12" width="7" height="4" fill="#1a1a2e" />
          <rect x="20" y="12" width="7" height="4" fill="#1a1a2e" />
          <rect x="20" y="13" width="1" height="2" fill="#1a1a2e" />
          <rect x="14" y="13" width="3" height="1" fill="#4ecdc4" />
          <rect x="21" y="13" width="3" height="1" fill="#4ecdc4" />
          <rect x="13" y="12" width="7" height="1" fill="#3a3a5e" />
          <rect x="20" y="12" width="7" height="1" fill="#3a3a5e" />
        </g>
        <rect x="19" y="16" width="2" height="1" fill="#ff8fa3" />
        <rect x="19" y="17" width="1" height="1" fill="#ff8fa3" />
        <rect x="20" y="17" width="1" height="1" fill="#ff8fa3" />
        <rect x="19" y="18" width="2" height="1" fill="#1a1a2e" />
        <rect x="8" y="13" width="4" height="1" fill="#888899" />
        <rect x="8" y="15" width="4" height="1" fill="#888899" />
        <rect x="28" y="13" width="4" height="1" fill="#888899" />
        <rect x="28" y="15" width="4" height="1" fill="#888899" />
        <g className="cat-sweat-drop">
          <path d="M29 10h2v3h-1v1h-2v-2h1Z" fill="#4ecdc4" />
        </g>
      </g>
      <g className="cat-part cat-paw-left">
        <rect x="13" y="28" width="4" height="3" fill="#ffd93d" />
        <rect x="13" y="30" width="4" height="1" fill="#cc4422" />
      </g>
      <g className="cat-part cat-paw-right">
        <rect x="23" y="28" width="4" height="3" fill="#ffd93d" />
        <rect x="23" y="30" width="4" height="1" fill="#cc4422" />
      </g>
      {action === "run" && (
        <g className="cat-keyboard" aria-hidden="true">
          <rect x="12" y="32" width="16" height="3" rx="1" fill="#2a2a3e" />
          <path d="M15 32v3M18 32v3M21 32v3M24 32v3" fill="none" stroke="#4ecdc4" />
        </g>
      )}
      <g className="cat-confetti" aria-hidden="true">
        <rect x="5" y="5" width="2" height="2" fill="#ffd93d" />
        <rect x="33" y="3" width="2" height="2" fill="#4ecdc4" />
        <rect x="4" y="23" width="2" height="2" fill="#ff5e87" />
        <rect x="34" y="26" width="2" height="2" fill="#ffd93d" />
      </g>
    </svg>
  );
}

function FolderStream() {
  return (
    <div className="folder-stream" aria-hidden="true">
      <span className="token token-one">&lt;/&gt;</span>
      <span className="token token-two">{"{}"}</span>
      <svg viewBox="0 0 40 32">
        <path d="M3 8h13l4 4h17v17H3Z" />
        <path d="M3 8V5h13l4 3" />
        <path d="M10 19h18M10 24h12" />
      </svg>
      <small>write_set</small>
    </div>
  );
}

function Mascot({ slice, action = slice.action, celebration = false, compact = false }: { slice: SliceProjection; action?: StageAction; celebration?: boolean; compact?: boolean }) {
  const identity = mascotIdentity(slice.id, slice.generation);
  const visualState = mascotVisualState(action);
  return (
    <article
      className={`mascot mascot-${visualState}${celebration ? " mascot-celebration" : ""}${compact ? " mascot-compact" : ""}`}
      data-persona-key={identity}
      data-state={visualState}
      data-visual-state={visualState}
      aria-label={`${slice.id} generation ${slice.generation}: ${actionLabel[action]}`}
    >
      <PixelCat action={action} />
      <div className="mascot-caption">
        <code>{slice.id}</code>
        <span>g{slice.generation} · {actionLabel[action]}</span>
      </div>
      {!celebration && action === "run" && <FolderStream />}
    </article>
  );
}

interface SliceCapsuleProps {
  readonly slice: SliceProjection;
  readonly focused: boolean;
  readonly locked: boolean;
  readonly pulse: boolean;
  readonly pulseVersion: number;
  readonly onToggle: () => void;
}

function SliceCapsule({ slice, focused, locked, pulse, pulseVersion, onToggle }: SliceCapsuleProps) {
  const placeholder = !slice.persona && (slice.status === "CONTRACT_BLOCKED" || slice.status === "READY");
  if (placeholder) {
    return (
      <div className="slice-capsule capsule-placeholder" data-slice-id={slice.id}>
        <span aria-hidden="true">◌</span>
        <code>{slice.id}</code>
        <strong>等待契约集成</strong>
        <small>agent · 未提供</small>
      </div>
    );
  }
  return (
    <button
      type="button"
      aria-pressed={locked}
      onClick={onToggle}
      className={`slice-capsule state-${mascotVisualState(slice.action)}${focused ? " is-focused" : ""}${locked ? " is-locked" : ""}${pulse ? " is-pulsed" : ""}`}
      data-slice-id={slice.id}
      data-pulse-version={pulseVersion}
    >
      <span className="capsule-status">{actionLabel[slice.action]}</span>
      <code>{slice.id}</code>
      <span>g{slice.generation} · {slice.kind}</span>
      <small>agent · 未提供 · attempt · 未提供</small>
      {locked && <b>锁定</b>}
    </button>
  );
}

function CelebrationFlyover({ item, slice }: { item: Celebration; slice: SliceProjection | undefined }) {
  const [visible, setVisible] = useState(true);
  useEffect(() => {
    setVisible(true);
    const timer = window.setTimeout(() => setVisible(false), 1500);
    return () => window.clearTimeout(timer);
  }, [item.key]);
  if (!visible || !slice) return null;
  return (
    <div className="celebration-flyover" data-celebration={item.key} aria-live="polite">
      <div className="celebration-particles" aria-hidden="true">
        {[0, 1, 2, 3].map((particle) => <i key={particle} data-celebration-particle="true" />)}
      </div>
      <Mascot slice={slice} action="verified" celebration />
      <span>已汇入 Verified Spine</span>
    </div>
  );
}

function StageObject({ zone, slice }: { zone: "waiting" | "regeneration"; slice?: SliceProjection }) {
  const isWaiting = zone === "waiting";
  return (
    <div
      className={`stage-object stage-object-${zone}${slice ? "" : " is-empty"}`}
      data-stage-object={zone}
      aria-label={isWaiting ? "等待对象" : "重生成对象"}
    >
      <span className="stage-object-icon" aria-hidden="true">{isWaiting ? "◌" : "!"}</span>
      {isWaiting && slice && <Mascot slice={slice} action="wait" compact />}
      <span>
        <strong>{isWaiting ? "等待对象" : "重生成对象"}</strong>
        <small>{slice ? `${slice.id} · g${slice.generation}` : isWaiting ? "等待后端事实" : "等待失败归因"}</small>
      </span>
    </div>
  );
}

function MobileSliceList({ slices, state, onToggle }: { slices: readonly SliceProjection[]; state: StageState; onToggle: (id: string) => void }) {
  const ordered = [...slices].sort(
    (left, right) => (left.integrationRank ?? Number.MAX_SAFE_INTEGER) - (right.integrationRank ?? Number.MAX_SAFE_INTEGER) || left.id.localeCompare(right.id),
  );
  return (
    <section className="mobile-slice-list" aria-label="移动端 Slice 列表">
      <div className="panel-kicker">Mobile Slice List <span>{ordered.length} 个对象</span></div>
      <ol>
        {ordered.map((slice) => (
            <li key={`${mascotIdentity(slice.id, slice.generation)}:pulse-${state.pulseVersion[slice.id] ?? 0}`} data-mobile-slice-id={slice.id}>
            <SliceCapsule
              slice={slice}
              focused={state.focusedId === slice.id}
              locked={state.lockedId === slice.id}
              pulse={state.pulseIds.includes(slice.id)}
              pulseVersion={state.pulseVersion[slice.id] ?? 0}
              onToggle={() => onToggle(slice.id)}
            />
          </li>
        ))}
      </ol>
    </section>
  );
}

function Inspector({ state }: { state: StageState }) {
  const slice = state.focusedId ? state.slices[state.focusedId] : undefined;
  return (
    <aside className="workspace-inspector" aria-label="只读上下文检查器">
      <div className="panel-kicker">Context Inspector <span>只读</span></div>
      <h2>{slice?.id ?? "未选择对象"}</h2>
      {slice ? (
        <dl>
          <div><dt>当前动作</dt><dd>{actionLabel[slice.action]}</dd></div>
          <div><dt>generation</dt><dd>g{slice.generation}</dd></div>
          <div><dt>agent</dt><dd>未提供</dd></div>
          <div><dt>attempt</dt><dd>未提供</dd></div>
          <div><dt>checks</dt><dd>未提供</dd></div>
          <div><dt>状态</dt><dd>{slice.status}</dd></div>
        </dl>
      ) : <p>选择 Slice 查看已提供的运行事实。</p>}
    </aside>
  );
}

interface WorkspaceShellProps {
  readonly state: StageState;
  readonly onToggle: (id: string) => void;
  readonly onClearFocus?: () => void;
  readonly runId?: string;
  readonly demoControls?: ReactNode;
  readonly presentationCelebrationKey?: string | null;
}

export function WorkspaceShell({
  state,
  onToggle,
  onClearFocus,
  runId = "run-mock-001",
  demoControls,
  presentationCelebrationKey,
}: WorkspaceShellProps) {
  useEffect(() => {
    if (!onClearFocus) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClearFocus();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onClearFocus]);

  const slices = Object.values(state.slices).sort(
    (left, right) => right.lastSequence - left.lastSequence || left.id.localeCompare(right.id),
  );
  const active = selectActivePersonas(state, 16, 8);
  const capsules = slices.filter((slice) => slice.zone !== "confluence" || slice.persona).slice(0, 6);
  const queued = slices
    .filter((slice) => slice.integrationRank !== null)
    .sort((left, right) => (left.integrationRank ?? 0) - (right.integrationRank ?? 0));
  const waitingSlice = slices.find((slice) => slice.zone === "waiting" && slice.persona);
  const regenerationSlice = slices.find((slice) => slice.zone === "regeneration");
  const celebration = state.celebrations.find((item) => item.key === presentationCelebrationKey) ?? null;

  return (
    <main className="workspace-shell" data-workspace-shell="true">
      <header className="workspace-header">
        <div className="brand"><span className="brand-mark" aria-hidden="true">⌁</span><span>CodeMigrator</span></div>
        <div className="workspace-run"><span>当前迁移 Run</span><code>{runId}</code><b>{state.runStatus}</b></div>
        <div className="workspace-connection" data-state={state.connection}>
          <i aria-hidden="true" />{state.connection}{demoControls}
        </div>
      </header>

      <nav className="workspace-nav" aria-label="迁移工作台导航">
        <span className="nav-active">EX</span>
        <span>迁移汇流场</span>
        <span>Slice DAG</span>
        <span>证据</span>
      </nav>

      <section className="workspace-stage" aria-label="中央迁移舞台">
        <div className="stage-toolbar">
          <div><span className="eyebrow">Migration Confluence</span><h1>中央迁移汇流场</h1></div>
          <span>{active.length} 个活动 persona · 最大 4 个</span>
        </div>

        <div className="stage-surround" data-stage-layout="1fr-auto-1fr">
          <div className="capsule-flank">
            {capsules.filter((_, index) => index % 2 === 0).map((slice) => (
              <SliceCapsule
                key={`${mascotIdentity(slice.id, slice.generation)}:pulse-${state.pulseVersion[slice.id] ?? 0}`}
                slice={slice}
                focused={state.focusedId === slice.id}
                locked={state.lockedId === slice.id}
                pulse={state.pulseIds.includes(slice.id)}
                pulseVersion={state.pulseVersion[slice.id] ?? 0}
                onToggle={() => onToggle(slice.id)}
              />
            ))}
          </div>
          <div className="mascot-stage">
            {active.length ? active.map((slice) => (
              <Mascot key={mascotIdentity(slice.id, slice.generation)} slice={slice} />
            )) : <div className="stage-empty">等待后端派发 persona</div>}
            {celebration && <CelebrationFlyover item={celebration} slice={state.slices[celebration.sliceId]} />}
          </div>
          <div className="capsule-flank">
            {capsules.filter((_, index) => index % 2 === 1).map((slice) => (
              <SliceCapsule
                key={`${mascotIdentity(slice.id, slice.generation)}:pulse-${state.pulseVersion[slice.id] ?? 0}`}
                slice={slice}
                focused={state.focusedId === slice.id}
                locked={state.lockedId === slice.id}
                pulse={state.pulseIds.includes(slice.id)}
                pulseVersion={state.pulseVersion[slice.id] ?? 0}
                onToggle={() => onToggle(slice.id)}
              />
            ))}
          </div>
        </div>

        <div className="stage-bottom">
          <StageObject zone="waiting" slice={waitingSlice} />
          <div className="token-bridge" aria-hidden="true"><span>·</span><span>→</span><span>·</span></div>
          <StageObject zone="regeneration" slice={regenerationSlice} />
        </div>

        <MobileSliceList slices={slices} state={state} onToggle={onToggle} />

        <section className="workspace-details">
          <section className="verified-spine">
            <div className="panel-kicker">Verified Spine <span>{state.celebrations.length} 条</span></div>
            <h2>唯一已验证主线</h2>
            {state.celebrations.length ? state.celebrations.map((item) => (
              <div className="spine-item" key={item.key}>
                <b>✓</b><code>{item.commitOid ?? "未提供"}</code><span>{item.sliceId} · g{item.generation}</span>
              </div>
            )) : <p>等待第一条 verified 事实</p>}
          </section>
          <section className="integration-queue">
            <div className="panel-kicker">Frozen Integration Queue <span>只读</span></div>
            <h2>集成队列</h2>
            {queued.length ? queued.map((slice) => (
              <div className="queue-item" key={mascotIdentity(slice.id, slice.generation)}>
                <b>#{slice.integrationRank}</b><code>{slice.id}</code><span>{slice.status}</span>
              </div>
            )) : <p>无已提供的冻结集成序</p>}
          </section>
        </section>

        <section className="event-timeline">
          <div className="panel-kicker">Event Timeline <span>sequence {state.cursor}</span></div>
          <ol>
            {state.timeline.slice(-8).reverse().map((item) => (
              <li key={`${item.sequence}:${item.type}`}>
                <code>{item.sequence}</code><strong>{item.label}</strong>{item.sliceId && <span>{item.sliceId}</span>}
              </li>
            ))}
          </ol>
        </section>
      </section>

      <Inspector state={state} />
      <footer className="workspace-activity" aria-label="运行事件活动条">
        <span aria-live="polite">sequence {state.cursor} · {state.connection}</span>
        <span>{state.lockedId ? `已锁定 ${state.lockedId}` : "自动跟随最新状态变化"}</span>
        {state.lockedId && <button type="button" onClick={onClearFocus}>按 Esc 解锁</button>}
      </footer>
    </main>
  );
}
