import type { EvidenceProjection, ReportProjection } from "../../entities/projections";
import { useEffect, useState } from "react";
import type { ApiClient } from "../../shared/api/client";

const listCount = (items: readonly Record<string, unknown>[] | undefined): string => String(items?.length ?? 0);

function EvidenceBlock({ title, children }: { title: string; children: React.ReactNode }) {
  return <section className="evidence-block"><h2>{title}</h2>{children}</section>;
}

function FactRows({ items }: { items: readonly Record<string, unknown>[] | undefined }) {
  if (!items?.length) return <p className="muted">暂无已提交事实</p>;
  return <ul className="fact-list">{items.slice(0, 8).map((item, index) => <li key={`${String(item.test ?? item.module ?? item.slice_id ?? "fact")}-${index}`}><span>{String(item.test ?? item.module ?? item.slice_id ?? "事实")}</span>{item.generated === true && <b className="generated-badge">GENERATED</b>}<code>{String(item.status ?? item.outcome ?? item.error_code ?? "已记录")}</code></li>)}</ul>;
}

export function ReportView({ report }: { report: ReportProjection }) {
  const evidence: EvidenceProjection = report.evidence ?? {};
  return <section className="page-panel report-page"><div className="panel-heading"><div><span className="eyebrow">REPORT / 语义等价证据</span><h1>{report.run_id}</h1></div><span className="status-chip">{report.status}</span></div><div className="evidence-grid"><EvidenceBlock title="测试通过率"><p className="metric-value">{String(evidence.pass_rate?.passed ?? "—")} / {String(evidence.pass_rate?.total ?? "—")}</p><p className="muted">移植测试与生成测试分开陈述。</p></EvidenceBlock><EvidenceBlock title="失败清单"><p className="muted">{listCount(evidence.failures)} 条后端归因事实</p><FactRows items={evidence.failures} /></EvidenceBlock><EvidenceBlock title="flaky 清单"><p className="muted">{listCount(evidence.flaky)} 条，未触发重生成</p><FactRows items={evidence.flaky} /></EvidenceBlock><EvidenceBlock title="覆盖映射"><p className="muted">{listCount(evidence.coverage)} 条映射记录</p><FactRows items={evidence.coverage} /></EvidenceBlock><EvidenceBlock title="结构守恒与归因"><p className="muted">守恒 {listCount(evidence.structural_conservation)} 条 · 辅助归因 {listCount(evidence.attribution)} 条</p><FactRows items={evidence.attribution} /></EvidenceBlock><EvidenceBlock title="等价信心分级"><FactRows items={evidence.confidence} /></EvidenceBlock><EvidenceBlock title="行为 parity"><p className="muted">{listCount(evidence.parity)} 个已确认场景</p><FactRows items={evidence.parity} /></EvidenceBlock></div><section className="boundary-statement"><strong>验证边界声明</strong><p>{evidence.boundary_statement ?? "测试主证覆盖行为等价（限于源测试覆盖范围）；性能等价、安全等价、生态习惯适配不在主证证明范围。"}</p></section></section>;
}

export function LiveReportView({ client, runId }: { client: ApiClient; runId: string }) {
  const [report, setReport] = useState<ReportProjection | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => { void client.getReport(runId).then(setReport).catch(() => setFailed(true)); }, [client, runId]);
  if (failed) return <section className="page-panel"><p className="diagnostic" role="status">报告投影暂不可用，未将其伪装成成功或空报告。</p></section>;
  return report ? <ReportView report={report} /> : <section className="page-panel"><p className="empty-state">正在读取报告投影…</p></section>;
}
