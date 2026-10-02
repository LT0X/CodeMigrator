/* @vitest-environment jsdom */
import { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../../shared/api/client";
import { SessionStartView } from "./SessionStartView";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

describe("SessionStartView", () => {
  it("creates a Draft from the selected registered snapshot and navigates to its session", async () => {
    const createDraftSession = vi.fn(async () => ({ session_id: "session-1", status: "OPEN", revision: 0 }));
    const client = {
      listProjects: async () => [
        { project_id: "project-1", snapshot_id: "snapshot-1", status: "READY" },
        { project_id: "project-2", snapshot_id: "snapshot-2", status: "READY" },
      ],
      createDraftSession,
    } as unknown as ApiClient;
    const host = document.createElement("div");
    document.body.append(host);
    const root = createRoot(host);

    try {
      await act(async () => root.render(<SessionStartView client={client} />));

      const project = host.querySelector("select") as HTMLSelectElement;
      const goal = host.querySelector("textarea") as HTMLTextAreaElement;
      expect(project.options).toHaveLength(3);
      expect(goal.maxLength).toBe(4096);
      expect(host.querySelector('input[type="file"]')).toBeNull();

      await act(async () => {
        project.value = "project-2";
        project.dispatchEvent(new Event("change", { bubbles: true }));
        goal.value = "把服务迁移到 Python";
        goal.dispatchEvent(new Event("input", { bubbles: true }));
      });
      await act(async () => host.querySelector("form")?.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));

      expect(createDraftSession).toHaveBeenCalledWith({
        source: { project_id: "project-2", snapshot_id: "snapshot-2" },
        goal: "把服务迁移到 Python",
      });
      expect(host.querySelector('a[href="/sessions/session-1"]')?.textContent).toContain("进入 Draft 会话");
      expect(host.textContent).not.toContain("创建 Run");
      expect(host.textContent).not.toContain("取消 Run");
    } finally {
      await act(async () => root.unmount());
      host.remove();
    }
  });
});
