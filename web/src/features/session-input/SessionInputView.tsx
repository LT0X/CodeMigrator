import { useEffect, useState } from "react";
import type { ApiClient, SessionEvent } from "../../shared/api/client";
import { observeSession } from "../../shared/api/observe";

type SessionEventState = "connecting" | "connected" | "ended" | "error" | "idle";

const EVENT_STATE_TEXT: Record<SessionEventState, string> = {
  connecting: "连接会话事件流…",
  connected: "正在接收",
  ended: "事件流已结束",
  error: "事件流连接失败",
  idle: "会话尚未创建",
};

export function SessionInputView({ sessionId, client }: { sessionId: string; client: ApiClient }) {
  const [message, setMessage] = useState("");
  const [sent, setSent] = useState(false);
  const [events, setEvents] = useState<SessionEvent[]>([]);
  const [eventState, setEventState] = useState<SessionEventState>("connecting");

  useEffect(() => {
    const controller = new AbortController();
    let mounted = true;
    setEvents([]);

    if (sessionId === "new") {
      setEventState("idle");
      return () => controller.abort();
    }

    setEventState("connecting");
    void (async () => {
      try {
        for await (const event of observeSession(client, sessionId, controller.signal)) {
          if (!mounted) return;
          setEvents((current) => current.some((item) => item.sequence === event.sequence)
            ? current
            : [...current, event].slice(-100));
          setEventState("connected");
        }
        if (mounted) setEventState("ended");
      } catch {
        if (mounted && !controller.signal.aborted) setEventState("error");
      }
    })();

    return () => {
      mounted = false;
      controller.abort();
    };
  }, [client, sessionId]);

  return (
    <section className="page-panel">
      <div className="panel-heading">
        <div>
          <span className="eyebrow">SESSION INPUT</span>
          <h1>受限会话输入</h1>
        </div>
        <span className="read-only">无 Run 控制</span>
      </div>
      <p className="muted">此通道只提交会话消息；不创建 Run、不取消、不修改代码或 Git。</p>
      <form onSubmit={(event) => {
        event.preventDefault();
        if (!message.trim()) return;
        void client.sendSessionMessage(sessionId, message).then(() => {
          setSent(true);
          setMessage("");
        });
      }}>
        <label htmlFor="session-message">消息</label>
        <textarea id="session-message" value={message} onChange={(event) => setMessage(event.target.value)} rows={5} />
        <button type="submit">发送消息</button>
      </form>
      {sent && <p className="success-note" role="status">消息已提交，等待持久化会话事实。</p>}
      <section className="event-timeline session-event-timeline" aria-label="会话持久事件">
        <div className="panel-kicker">
          会话事件
          <span role="status">{EVENT_STATE_TEXT[eventState]}</span>
        </div>
        <ol aria-label="会话持久事件列表">
          {events.map((event) => (
            <li key={event.sequence}>
              <span className="timeline-sequence">{event.sequence}</span>
              <code>{event.type}</code>
              <time dateTime={event.timestamp_utc}>{event.timestamp_utc}</time>
            </li>
          ))}
        </ol>
        {events.length === 0 && <p className="muted">暂无已提交事件。</p>}
      </section>
    </section>
  );
}
