/* @vitest-environment jsdom */
import { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it } from "vitest";
import { RunListView } from "./RunListView";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

describe("RunListView", () => {
  it("links to Draft creation without exposing Run creation", async () => {
    const host = document.createElement("div");
    document.body.append(host);
    const root = createRoot(host);

    try {
      await act(async () => root.render(<RunListView runs={[]} />));
      expect(host.querySelector('a[href="/sessions/new"]')?.textContent).toContain("新建 Draft 会话");
      expect([...host.querySelectorAll("button")].some((button) => button.textContent?.includes("创建 Run"))).toBe(false);
    } finally {
      await act(async () => root.unmount());
      host.remove();
    }
  });
});
