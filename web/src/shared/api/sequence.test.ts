import { describe, expect, it } from "vitest";
import { SequenceCursor } from "./sequence";

describe("sequence cursor", () => {
  it("accepts contiguous events, rejects duplicates and waits on gaps", () => {
    const cursor = new SequenceCursor(4);
    expect(cursor.accept(5)).toEqual({ kind: "accepted", cursor: 5 });
    expect(cursor.accept(5)).toEqual({ kind: "duplicate", cursor: 5 });
    expect(cursor.accept(7)).toEqual({ kind: "gap", cursor: 5, expected: 6 });
    expect(cursor.state).toBe("catching-up");
  });

  it("exposes the five connection states and terminal transition", () => {
    const cursor = new SequenceCursor();
    expect(cursor.state).toBe("disconnected");
    cursor.connecting();
    cursor.connected();
    cursor.terminal();
    expect(cursor.state).toBe("terminal");
  });
});
