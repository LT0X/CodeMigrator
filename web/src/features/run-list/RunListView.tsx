import type { MigrationProjection } from "../../entities/projections";
import { useEffect, useState, type ReactNode } from "react";
import type { ApiClient } from "../../shared/api/client";

export function RunListView({ runs }: { runs: readonly MigrationProjection[] }) {
  return <section className="page-panel"><div className="panel-heading"><div><span className="eyebrow">Run 首页</span><h1>迁移现场</h1></div><span className="read-only">观察模式</span></div><div className="run-list">{runs.length === 0 ? <p className="empty-state">暂无可信 Run 事实。请使用 CLI 发起迁移。</p> : runs.map((run) => <a className="run-row" href={`/runs/${encodeURIComponent(run.run_id)}`} key={run.run_id}><code>{run.run_id}</code><strong>{run.status}</strong><span>版本 {run.version}</span><span>{run.report_delivery_status ?? "报告待定"}</span></a>)}</div><p className="cli-hint"><code>codemigrator migrate start &lt;spec&gt; --follow</code></p></section>;
}

export function LiveRunListView({ client, fallback }: { client: ApiClient; fallback: ReactNode }) {
  const [runs, setRuns] = useState<MigrationProjection[] | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => { void client.listMigrations().then(setRuns).catch(() => setFailed(true)); }, [client]);
  if (failed) return <>{fallback}<p className="diagnostic page-diagnostic" role="status">服务暂不可用，未展示虚构运行事实；可使用 URL 参数 demo=1 查看本地演示。</p></>;
  return <RunListView runs={runs ?? []} />;
}
