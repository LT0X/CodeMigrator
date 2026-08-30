import { describe, expect, it } from "vitest";
import { createApiClient, parseSse } from "./client";

describe("API boundary", () => {
  it("builds encoded read-only projection paths", async () => {
    const calls: string[] = [];
    const initValues: RequestInit[] = [];
    const fetchImpl = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      calls.push(String(input));
      initValues.push(init ?? {});
      return new Response(JSON.stringify({ run_id: "run/1", slices: [], integration_queue: [], latest_sequence: 4 }), { status: 200 });
    };
    await createApiClient({ baseUrl: "/api/v1", fetchImpl, token: "test-token" }).getWorkspace("run/1");
    expect(calls).toEqual(["/api/v1/migrations/run%2F1/workspace"]);
    expect(new Headers(initValues[0].headers).get("Authorization")).toBe("Bearer test-token");
  });

  it("adds an idempotency key to session writes", async () => {
    let init: RequestInit | undefined;
    const fetchImpl = async (_input: RequestInfo | URL, requestInit?: RequestInit): Promise<Response> => {
      init = requestInit;
      return new Response(JSON.stringify({ session_id: "session-1", status: "OPEN", revision: 2 }), { status: 200 });
    };
    await createApiClient({ fetchImpl }).sendSessionMessage("session-1", "继续", 1);
    expect(new Headers(init?.headers).get("Idempotency-Key")).toBeTruthy();
  });

  it("parses only bounded event envelope fields", () => {
    expect(parseSse('event: migration.event\nid: 2\ndata: {"schema":"migration.event","version":1,"type":"dispatch.started","sequence":2,"data":{"slice_id":"a"}}')).toEqual({
      type: "dispatch.started",
      sequence: 2,
      data: { slice_id: "a" },
      timestamp_utc: "",
      schema: "migration.event",
      version: 1,
      sse_id: "2",
    });
    expect(parseSse('id: 2\ndata: {"schema":"migration.event","version":1,"sequence":2,"type":"x","data":{}}')).toEqual(expect.objectContaining({ sequence: 2 }));
    expect(parseSse('id: 3\ndata: {"schema":"migration.event","version":1,"sequence":2,"type":"x","data":{}}')).toBeNull();
    expect(parseSse("data: {\"sequence\":0}")).toBeNull();
    expect(parseSse("data: not-json")).toBeNull();
  });
});
