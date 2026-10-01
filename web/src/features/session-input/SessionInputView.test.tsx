/* @vitest-environment jsdom */
import { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../../shared/api/client";
import { SessionInputView } from "./SessionInputView";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const sessionEvent = (sequence: number, type: string) => ({
  schema: "migration.session.event" as const,
  version: 1 as const,
  type,
  sequence,
  data: {},
  timestamp_utc: "2026-10-01T10:00:00Z",
  sse_id: String(sequence),
});

const projectedSessionEvent = (sequence: number, type: string, data: Record<string, unknown>) => ({
  ...sessionEvent(sequence, type),
  data,
});

describe("SessionInputView", () => {
  it("displays persisted session events and resumes replay after the last displayed sequence", async () => {
    const cursors: number[] = [];
    let attempts = 0;
    const client = {
      sendSessionMessage: async () => ({ session_id: "session-1", status: "OPEN", revision: 1 }),
      streamSessionEvents: async function* (_sessionId: string, afterSequence: number) {
        cursors.push(afterSequence);
        attempts += 1;
        if (attempts === 1) {
          yield sessionEvent(1, "assistant.message.completed");
          throw new Error("connection dropped");
        }
        yield sessionEvent(2, "session.closed");
      },
    } as unknown as ApiClient;
    const host = document.createElement("div");
    document.body.append(host);
    const root = createRoot(host);

    try {
      await act(async () => root.render(<SessionInputView sessionId="session-1" client={client} />));
      await act(async () => new Promise((resolve) => setTimeout(resolve, 400)));

      expect(cursors).toEqual([0, 1]);
      expect(host.querySelector('[aria-label="会话持久事件"]')?.textContent).toContain("会话回复已保存");
      expect(host.querySelector('[aria-label="会话持久事件"]')?.textContent).toContain("Draft 会话已关闭");
    } finally {
      await act(async () => root.unmount());
      host.remove();
    }
  });

  it("renders the projected AskUser card and submits one selected option with its bound revision", async () => {
    const answerSession = vi.fn(async () => ({ session_id: "session-1", status: "OPEN", revision: 2 }));
    const client = {
      answerSession,
      streamSessionEvents: async function* () {
        yield projectedSessionEvent(1, "session.question.asked", {
          question_id: "question-1",
          revision: 2,
          prompt: "如何冻结当前未提交工作树？",
          options: [
            { key: "head", label: "只采用当前 HEAD", impact: "未提交改动不进入翻译输入", recommended: true },
            { key: "snapshot", label: "复制为托管快照", impact: "把确认的工作树状态作为输入", recommended: false },
          ],
          allow_free_text: true,
        });
      },
    } as unknown as ApiClient;
    const host = document.createElement("div");
    document.body.append(host);
    const root = createRoot(host);

    try {
      await act(async () => root.render(<SessionInputView sessionId="session-1" client={client} />));
      await act(async () => new Promise((resolve) => setTimeout(resolve, 0)));

      expect(host.textContent).toContain("如何冻结当前未提交工作树？");
      expect(host.textContent).toContain("推荐");
      expect(host.textContent).toContain("未提交改动不进入翻译输入");
      const option = host.querySelector('input[name="selected_option"][value="snapshot"]') as HTMLInputElement;
      await act(async () => {
        option.checked = true;
        option.dispatchEvent(new Event("change", { bubbles: true }));
        host.querySelector('[aria-label="AskUser 回答"]')?.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
      });

      expect(answerSession).toHaveBeenCalledWith("session-1", "question-1", { selected_option: "snapshot" }, 2);
      expect(host.textContent).toContain("答案已提交");
    } finally {
      await act(async () => root.unmount());
      host.remove();
    }
  });

  it("submits free-text AskUser answers in the owner model's single-answer form", async () => {
    const answerSession = vi.fn(async () => ({ session_id: "session-1", status: "OPEN", revision: 2 }));
    const client = {
      answerSession,
      streamSessionEvents: async function* () {
        yield projectedSessionEvent(1, "session.question.asked", {
          question_id: "question-free-text",
          revision: 2,
          prompt: "还有什么需要补充？",
          options: [
            { key: "default", label: "按推荐方案继续", impact: "使用默认决策", recommended: true },
            { key: "defer", label: "暂不决定", impact: "等待后续对齐", recommended: false },
          ],
          allow_free_text: true,
        });
      },
    } as unknown as ApiClient;
    const host = document.createElement("div");
    document.body.append(host);
    const root = createRoot(host);

    try {
      await act(async () => root.render(<SessionInputView sessionId="session-1" client={client} />));
      await act(async () => new Promise((resolve) => setTimeout(resolve, 0)));
      const freeText = host.querySelector('textarea[name="free_text"]') as HTMLTextAreaElement;
      await act(async () => {
        freeText.value = "保留公开 API 名称";
        freeText.dispatchEvent(new Event("input", { bubbles: true }));
        host.querySelector('[aria-label="AskUser 回答"]')?.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
      });

      expect(answerSession).toHaveBeenCalledWith("session-1", "question-free-text", { free_text: "保留公开 API 名称" }, 2);
    } finally {
      await act(async () => root.unmount());
      host.remove();
    }
  });

  it("previews all four persisted draft artifacts and confirms that revision without creating a Run", async () => {
    let markConfirmationRequested!: () => void;
    let finishCalibration!: () => void;
    const confirmationRequested = new Promise<void>((resolve) => { markConfirmationRequested = resolve; });
    const calibrationFinished = new Promise<void>((resolve) => { finishCalibration = resolve; });
    const confirmSession = vi.fn(async () => {
      markConfirmationRequested();
      return { session_id: "session-1", status: "DRAFTING", revision: 3 };
    });
    const client = {
      confirmSession,
      streamSessionEvents: async function* () {
        yield projectedSessionEvent(1, "session.draft_revision.created", {
          revision: 3,
          artifacts: {
            spec: { target_language_id: "python", goal: "迁移服务层" },
            understanding_dossier: { summary: "服务层依赖两个数据模块" },
            target_project_blueprint: { package_layout: "src/service/" },
            migration_rulebook: { naming: "保持公开接口语义" },
          },
          artifact_snapshots: [
            { name: "spec", version: 3, sha256: "a".repeat(64), size: 120, media_type: "application/json" },
            { name: "understanding_dossier", version: 3, sha256: "b".repeat(64), size: 120, media_type: "application/json" },
            { name: "target_project_blueprint", version: 3, sha256: "c".repeat(64), size: 120, media_type: "application/json" },
            { name: "migration_rulebook", version: 3, sha256: "d".repeat(64), size: 120, media_type: "application/json" },
          ],
        });
        await confirmationRequested;
        yield projectedSessionEvent(2, "session.draft_revision.confirmation_requested", { revision: 3 });
        await calibrationFinished;
        yield projectedSessionEvent(3, "session.draft_revision.confirmed", { revision: 3 });
      },
    } as unknown as ApiClient;
    const host = document.createElement("div");
    document.body.append(host);
    const root = createRoot(host);

    try {
      await act(async () => root.render(<SessionInputView sessionId="session-1" client={client} />));
      await act(async () => new Promise((resolve) => setTimeout(resolve, 0)));

      expect(host.textContent).toContain("任务规格");
      expect(host.textContent).toContain("迁移服务层");
      expect(host.textContent).toContain("理解档案");
      expect(host.textContent).toContain("目标项目蓝图");
      expect(host.textContent).toContain("迁移规则手册");
      const confirmButton = [...host.querySelectorAll("button")].find((button) => button.textContent === "确认四件工件");
      expect(confirmButton?.disabled).toBe(true);

      const confirmation = host.querySelector('input[name="confirm_artifacts"]') as HTMLInputElement;
      await act(async () => confirmation.click());
      expect(confirmButton?.disabled).toBe(false);
      await act(async () => confirmButton?.dispatchEvent(new MouseEvent("click", { bubbles: true })));
      await act(async () => new Promise((resolve) => setTimeout(resolve, 0)));

      expect(confirmSession).toHaveBeenCalledWith("session-1", 3);
      expect(host.textContent).toContain("确认请求已提交，正在执行只读校准。");
      expect(host.textContent).not.toContain("Draft 已确认");
      expect(host.querySelector('[aria-label="确认四件工件"]')).toBeNull();

      await act(async () => {
        finishCalibration();
        await new Promise((resolve) => setTimeout(resolve, 0));
      });
      expect(host.textContent).toContain("Draft 已确认");
      expect([...host.querySelectorAll("button")].some((button) => button.textContent?.includes("Run"))).toBe(false);
    } finally {
      await act(async () => root.unmount());
      host.remove();
    }
  });

  it("does not render non-projectable internal data from unknown session events", async () => {
    const client = {
      streamSessionEvents: async function* () {
        yield projectedSessionEvent(1, "agent_run.internal_detail", { prompt: "hidden-prompt-sentinel", source: "hidden-source-sentinel" });
      },
    } as unknown as ApiClient;
    const host = document.createElement("div");
    document.body.append(host);
    const root = createRoot(host);

    try {
      await act(async () => root.render(<SessionInputView sessionId="session-1" client={client} />));
      await act(async () => new Promise((resolve) => setTimeout(resolve, 0)));

      expect(host.textContent).not.toContain("hidden-prompt-sentinel");
      expect(host.textContent).not.toContain("hidden-source-sentinel");
    } finally {
      await act(async () => root.unmount());
      host.remove();
    }
  });

  it("keeps the latest artifact projection available when its event falls outside the visible timeline window", async () => {
    const client = {
      streamSessionEvents: async function* () {
        yield projectedSessionEvent(1, "session.draft_revision.created", {
          revision: 4,
          artifacts: {
            spec: { goal: "long session draft" },
            understanding_dossier: {},
            target_project_blueprint: {},
            migration_rulebook: {},
          },
          artifact_snapshots: [
            { name: "spec", version: 4, sha256: "a".repeat(64), size: 1, media_type: "application/json" },
            { name: "understanding_dossier", version: 4, sha256: "b".repeat(64), size: 1, media_type: "application/json" },
            { name: "target_project_blueprint", version: 4, sha256: "c".repeat(64), size: 1, media_type: "application/json" },
            { name: "migration_rulebook", version: 4, sha256: "d".repeat(64), size: 1, media_type: "application/json" },
          ],
        });
        for (let sequence = 2; sequence <= 106; sequence += 1) {
          yield projectedSessionEvent(sequence, "session.progress", {});
        }
      },
    } as unknown as ApiClient;
    const host = document.createElement("div");
    document.body.append(host);
    const root = createRoot(host);

    try {
      await act(async () => root.render(<SessionInputView sessionId="session-1" client={client} />));
      await act(async () => new Promise((resolve) => setTimeout(resolve, 0)));

      expect(host.textContent).toContain("long session draft");
      expect(host.querySelectorAll('[aria-label="会话持久事件列表"] li')).toHaveLength(100);
    } finally {
      await act(async () => root.unmount());
      host.remove();
    }
  });
});
