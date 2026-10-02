import { useEffect, useState, type FormEvent } from "react";
import type { ApiClient, SessionEvent } from "../../shared/api/client";
import { observeSession } from "../../shared/api/observe";
import {
  ARTIFACT_LABELS,
  EMPTY_SESSION_DRAFT_PROJECTION,
  reduceSessionDraft,
  sessionEventLabel,
  type DraftArtifactName,
  type SessionQuestion,
} from "./sessionProjection";

type SessionEventState = "connecting" | "connected" | "ended" | "error" | "idle";
type QuestionStatus = "submitting" | "submitted";
type QuestionError = "invalid" | "submit";
type SessionEventSummary = Pick<SessionEvent, "sequence" | "type" | "timestamp_utc">;

const EVENT_STATE_TEXT: Record<SessionEventState, string> = {
  connecting: "连接会话事件流…",
  connected: "正在接收",
  ended: "事件流已结束",
  error: "事件流连接失败",
  idle: "会话尚未创建",
};

const ARTIFACT_ORDER: readonly DraftArtifactName[] = [
  "spec",
  "understanding_dossier",
  "target_project_blueprint",
  "migration_rulebook",
];

export function SessionInputView({ sessionId, client }: { sessionId: string; client: ApiClient }) {
  const [message, setMessage] = useState("");
  const [sent, setSent] = useState(false);
  const [messageError, setMessageError] = useState(false);
  const [events, setEvents] = useState<SessionEventSummary[]>([]);
  const [draftProjection, setDraftProjection] = useState(EMPTY_SESSION_DRAFT_PROJECTION);
  const [eventState, setEventState] = useState<SessionEventState>("connecting");
  const [questionStatuses, setQuestionStatuses] = useState<Record<string, QuestionStatus>>({});
  const [questionErrors, setQuestionErrors] = useState<Record<string, QuestionError>>({});
  const [reviewedRevision, setReviewedRevision] = useState<number | null>(null);
  const [confirmingRevision, setConfirmingRevision] = useState<number | null>(null);
  const [confirmError, setConfirmError] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    let mounted = true;
    setEvents([]);
    setDraftProjection(EMPTY_SESSION_DRAFT_PROJECTION);
    setQuestionStatuses({});
    setQuestionErrors({});
    setReviewedRevision(null);
    setConfirmError(false);

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
            : [...current, { sequence: event.sequence, type: event.type, timestamp_utc: event.timestamp_utc }].slice(-100));
          setDraftProjection((current) => reduceSessionDraft(current, event));
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

  const sendMessage = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!message.trim()) return;
    setMessageError(false);
    void client.sendSessionMessage(sessionId, message).then(() => {
      setSent(true);
      setMessage("");
    }).catch(() => setMessageError(true));
  };

  const answerQuestion = (question: SessionQuestion, event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (questionStatuses[question.questionId] === "submitting" || questionStatuses[question.questionId] === "submitted") return;
    const values = new FormData(event.currentTarget);
    const selectedOption = String(values.get("selected_option") ?? "").trim();
    const freeText = String(values.get("free_text") ?? "").trim();
    const hasSelectedOption = selectedOption.length > 0;
    const hasFreeText = freeText.length > 0;
    if (hasSelectedOption === hasFreeText || (hasFreeText && !question.allowFreeText)) {
      setQuestionErrors((current) => ({ ...current, [question.questionId]: "invalid" }));
      return;
    }

    setQuestionErrors((current) => {
      const next = { ...current };
      delete next[question.questionId];
      return next;
    });
    setQuestionStatuses((current) => ({ ...current, [question.questionId]: "submitting" }));
    const answer = hasSelectedOption ? { selected_option: selectedOption } : { free_text: freeText };
    void client.answerSession(sessionId, question.questionId, answer, question.revision).then(() => {
      setQuestionStatuses((current) => ({ ...current, [question.questionId]: "submitted" }));
    }).catch(() => {
      setQuestionStatuses((current) => {
        const next = { ...current };
        delete next[question.questionId];
        return next;
      });
      setQuestionErrors((current) => ({ ...current, [question.questionId]: "submit" }));
    });
  };

  const confirmDraft = (revision: number, event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (
      draftProjection.questions.length > 0 ||
      reviewedRevision !== revision ||
      confirmingRevision === revision ||
      draftProjection.confirmationRequestedRevision === revision
    ) return;
    setConfirmError(false);
    setConfirmingRevision(revision);
    void client.confirmSession(sessionId, revision).catch(() => {
      setConfirmError(true);
    }).finally(() => setConfirmingRevision(null));
  };

  const currentDraft = draftProjection.draft;
  const isDraftConfirmed = currentDraft !== null && (
    draftProjection.confirmedRevision === currentDraft.revision
  );
  const isConfirmationRequested = currentDraft !== null &&
    draftProjection.confirmationRequestedRevision === currentDraft.revision;

  return (
    <section className="page-panel">
      <div className="panel-heading">
        <div>
          <span className="eyebrow">DRAFT SESSION</span>
          <h1>迁移起草会话</h1>
        </div>
        <span className="read-only">无 Run 控制</span>
      </div>
      <p className="muted">此通道只提交自然语言消息、AskUser 答案和四件工件确认；不创建 Run、不取消、不修改代码或 Git。</p>
      <form onSubmit={sendMessage}>
        <label htmlFor="session-message">消息</label>
        <textarea id="session-message" value={message} onChange={(event) => setMessage(event.target.value)} rows={5} />
        <button type="submit" disabled={!message.trim()}>发送消息</button>
      </form>
      {sent && <p className="success-note" role="status">消息已提交，等待持久化会话事实。</p>}
      {messageError && <p className="diagnostic" role="alert">消息提交失败；请稍后重试。</p>}

      {draftProjection.questions.map((question) => {
        const status = questionStatuses[question.questionId];
        return (
          <section className="session-question-card" aria-label="待回答问题" key={question.questionId}>
            <div className="panel-kicker">需要确认 · 修订版 {question.revision}</div>
            <h2>{question.prompt}</h2>
            {status === "submitted" ? (
              <p className="success-note" role="status">答案已提交，等待会话继续。</p>
            ) : (
              <form aria-label="AskUser 回答" onSubmit={(event) => answerQuestion(question, event)}>
                <fieldset disabled={status === "submitting"}>
                  <legend>选择一个互斥选项{question.allowFreeText ? "，或填写自己的答案" : ""}</legend>
                  {question.options.map((option) => {
                    const inputId = `question-${question.questionId}-${option.key}`;
                    return (
                      <div className="session-question-option" key={option.key}>
                        <label htmlFor={inputId}>
                          <input id={inputId} type="radio" name="selected_option" value={option.key} />
                          <span>{option.label}</span>
                          {option.recommended && <strong className="recommended-option">推荐</strong>}
                        </label>
                        <p>{option.impact}</p>
                      </div>
                    );
                  })}
                  {question.allowFreeText && (
                    <label htmlFor={`question-free-text-${question.questionId}`}>
                      自由文本答案
                      <textarea id={`question-free-text-${question.questionId}`} name="free_text" rows={3} maxLength={4096} />
                    </label>
                  )}
                </fieldset>
                {questionErrors[question.questionId] === "invalid" && <p className="diagnostic" role="alert">请选择一个选项或只填写自由文本。</p>}
                {questionErrors[question.questionId] === "submit" && <p className="diagnostic" role="alert">答案提交失败；请检查连接后重试。</p>}
                <button type="submit" disabled={status === "submitting"}>{status === "submitting" ? "正在提交…" : "提交答案"}</button>
              </form>
            )}
          </section>
        );
      })}

      {currentDraft && (
        <section className="session-draft-preview" aria-label="四件工件草稿">
          <div className="panel-heading">
            <div><span className="eyebrow">TASK DRAFT · REVISION {currentDraft.revision}</span><h2>四件工件草稿</h2></div>
            {isDraftConfirmed && <span className="read-only">已确认</span>}
          </div>
          {ARTIFACT_ORDER.map((name) => {
            const snapshot = currentDraft.snapshots.find((item) => item.name === name);
            return (
              <article className="draft-artifact" key={name}>
                <h3>{ARTIFACT_LABELS[name]}</h3>
                <pre>{JSON.stringify(currentDraft.artifacts[name], null, 2)}</pre>
                {snapshot && <details><summary>版本与完整性</summary><p>版本 {snapshot.version} · {snapshot.size} 字节 · {snapshot.mediaType}</p><code>{snapshot.sha256}</code></details>}
              </article>
            );
          })}

          {isDraftConfirmed ? (
            <p className="success-note" role="status">Draft 已确认。Run 创建仍由 CLI 完成。</p>
          ) : isConfirmationRequested ? (
            <p className="muted" role="status">确认请求已提交，正在执行只读校准。</p>
          ) : (
            <form aria-label="确认四件工件" onSubmit={(event) => confirmDraft(currentDraft.revision, event)}>
              <label htmlFor={`review-draft-${currentDraft.revision}`}>
                <input
                  id={`review-draft-${currentDraft.revision}`}
                  name="confirm_artifacts"
                  type="checkbox"
                  checked={reviewedRevision === currentDraft.revision}
                  onChange={(event) => setReviewedRevision(event.target.checked ? currentDraft.revision : null)}
                />
                我已审阅四件工件并确认此修订版
              </label>
              <button type="submit" disabled={reviewedRevision !== currentDraft.revision || draftProjection.questions.length > 0 || confirmingRevision === currentDraft.revision}>
                {confirmingRevision === currentDraft.revision ? "正在确认…" : "确认四件工件"}
              </button>
              {confirmError && <p className="diagnostic" role="alert">工件确认未获成功回执，请检查会话进度后重试。</p>}
            </form>
          )}
        </section>
      )}

      <section className="event-timeline session-event-timeline" aria-label="会话持久事件">
        <div className="panel-kicker">
          会话事件
          <span role="status">{EVENT_STATE_TEXT[eventState]}</span>
        </div>
        <ol aria-label="会话持久事件列表">
          {events.map((event) => (
            <li key={event.sequence}>
              <span className="timeline-sequence">{event.sequence}</span>
              <span>{sessionEventLabel(event.type)}</span>
              <time dateTime={event.timestamp_utc}>{event.timestamp_utc}</time>
            </li>
          ))}
        </ol>
        {events.length === 0 && <p className="muted">暂无已提交事件。</p>}
      </section>
    </section>
  );
}
