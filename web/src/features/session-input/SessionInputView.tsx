import { useState } from "react";
import type { ApiClient } from "../../shared/api/client";

export function SessionInputView({ sessionId, client }: { sessionId: string; client: ApiClient }) {
  const [message, setMessage] = useState("");
  const [sent, setSent] = useState(false);
  return <section className="page-panel"><div className="panel-heading"><div><span className="eyebrow">SESSION INPUT</span><h1>受限会话输入</h1></div><span className="read-only">无 Run 控制</span></div><p className="muted">此通道只提交会话消息；不创建 Run、不取消、不修改代码或 Git。</p><form onSubmit={(event) => { event.preventDefault(); if (!message.trim()) return; void client.sendSessionMessage(sessionId, message).then(() => { setSent(true); setMessage(""); }); }}><label htmlFor="session-message">消息</label><textarea id="session-message" value={message} onChange={(event) => setMessage(event.target.value)} rows={5} /><button type="submit">发送消息</button></form>{sent && <p className="success-note" role="status">消息已提交，等待持久化会话事实。</p>}</section>;
}
