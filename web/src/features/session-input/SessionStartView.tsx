import { useEffect, useState } from "react";
import type { RegisteredProjectProjection } from "../../entities/projections";
import type { ApiClient } from "../../shared/api/client";

type LoadState = "loading" | "ready" | "error";

export function SessionStartView({ client }: { client: ApiClient }) {
  const [projects, setProjects] = useState<RegisteredProjectProjection[]>([]);
  const [loadState, setLoadState] = useState<LoadState>("loading");
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState("");
  const [sessionId, setSessionId] = useState("");

  useEffect(() => {
    let mounted = true;
    void client.listProjects().then((items) => {
      if (!mounted) return;
      setProjects(items.filter((project) => project.snapshot_id !== null));
      setLoadState("ready");
    }).catch(() => {
      if (!mounted) return;
      setLoadState("error");
    });
    return () => { mounted = false; };
  }, [client]);

  const submit = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (creating || sessionId) return;
    const form = new FormData(event.currentTarget);
    const projectId = String(form.get("project_id") ?? "");
    const goal = String(form.get("goal") ?? "").trim();
    const project = projects.find((item) => item.project_id === projectId);
    if (!project?.snapshot_id || !goal) return;

    setCreating(true);
    setError("");
    void client.createDraftSession({
      source: { project_id: project.project_id, snapshot_id: project.snapshot_id },
      goal,
    }).then((session) => {
      setSessionId(session.session_id);
    }).catch(() => {
      setError("Draft 会话创建失败；没有创建 Run。");
    }).finally(() => setCreating(false));
  };

  return (
    <section className="page-panel session-start-panel">
      <div className="panel-heading">
        <div>
          <span className="eyebrow">DRAFT SESSION</span>
          <h1>新建 Draft 会话</h1>
        </div>
        <span className="read-only">只读源项目</span>
      </div>
      <p className="muted">选择已注册项目及其托管快照，描述迁移目标。此操作只创建 Draft 会话；Run 仍由 CLI 创建。</p>

      {loadState === "loading" && <p role="status">正在读取已注册项目…</p>}
      {loadState === "error" && <p className="diagnostic" role="alert">项目列表暂不可用，未创建会话。</p>}
      {loadState === "ready" && projects.length === 0 && <p className="empty-state" role="status">没有可用的已注册项目快照。此页面不接受服务器路径或任意目录。</p>}

      {loadState === "ready" && projects.length > 0 && !sessionId && (
        <form onSubmit={submit}>
          <label htmlFor="draft-project">已注册项目与快照</label>
          <select id="draft-project" name="project_id" required defaultValue="">
            <option value="" disabled>选择项目快照</option>
            {projects.map((project) => (
              <option value={project.project_id} key={project.project_id}>
                项目 {project.project_id} · 快照 {project.snapshot_id}
              </option>
            ))}
          </select>
          <label htmlFor="draft-goal">迁移目标</label>
          <textarea id="draft-goal" name="goal" rows={5} required maxLength={4096} placeholder="描述你要迁移的内容和目标语言" />
          <button type="submit" disabled={creating}>{creating ? "正在创建…" : "创建 Draft 会话"}</button>
        </form>
      )}

      {error && <p className="diagnostic" role="alert">{error}</p>}
      {sessionId && (
        <p className="success-note" role="status">
          Draft 会话已创建。<a href={`/sessions/${encodeURIComponent(sessionId)}`}>进入 Draft 会话</a>
        </p>
      )}
    </section>
  );
}
