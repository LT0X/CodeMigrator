import type { HealthProjection } from "../../entities/projections";
import { useEffect, useState } from "react";
import type { ApiClient } from "../../shared/api/client";

export function SystemHealthView({ health }: { health: HealthProjection }) {
  const facts = [["应用", health.app], ["PostgreSQL", health.postgres], ["沙箱", health.sandbox], ...Object.entries(health.optional_profiles)];
  return <section className="page-panel"><div className="panel-heading"><div><span className="eyebrow">SYSTEM</span><h1>系统状态</h1></div><span className="read-only">安全摘要</span></div><dl className="health-list">{facts.map(([name, value]) => <div key={name}><dt>{name}</dt><dd>{value}</dd></div>)}</dl><p className="muted">此页面不显示 DSN、socket 路径、凭据或宿主文件路径。</p></section>;
}

export function LiveSystemHealthView({ client }: { client: ApiClient }) {
  const [health, setHealth] = useState<HealthProjection | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => { void client.getHealth().then(setHealth).catch(() => setFailed(true)); }, [client]);
  if (failed) return <section className="page-panel"><p className="diagnostic" role="status">系统状态暂不可用，未展示虚构健康结论。</p></section>;
  return health ? <SystemHealthView health={health} /> : <section className="page-panel"><p className="empty-state">正在读取系统状态…</p></section>;
}
