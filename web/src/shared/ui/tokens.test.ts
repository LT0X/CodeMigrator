import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { semanticStateTokens } from "./tokens";

describe("visual contract", () => {
  it("defines the four semantic state colors and warm canvas", () => {
    expect(Object.keys(semanticStateTokens)).toEqual(["verified", "agent", "waiting", "failure"]);
    const css = readFileSync(resolve(import.meta.dirname, "tokens.css"), "utf8");
    expect(css).toContain("--cm-background: #FAF9F7");
    expect(css).toContain("--cm-state-verified");
    expect(css).toContain("--cm-state-agent");
    expect(css).toContain("--cm-state-waiting");
    expect(css).toContain("--cm-state-failure");
  });

  it("keeps the public workbench free of legacy interaction vocabulary", () => {
    const css = readFileSync(resolve(import.meta.dirname, "tokens.css"), "utf8");
    expect(css).not.toMatch(/patch_applied|AstPatch|edit intent/i);
  });
});
