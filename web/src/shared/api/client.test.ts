import { describe, expect, it } from "vitest";
import { createApiClient, parseSse } from "./client";

describe("API boundary", () => {
  it("builds encoded read-only projection paths", async () => {
    const calls: string[] = [];
    const fetchImpl = async (input: RequestInfo | URL): Promise<Response> => {
      calls.push(String(input));
      return new Response(JSON.stringify({ run_id: "run/1", slices: [], integration_queue: [], latest_sequence: 4 }), { status: 200 });
    };
    await createApiClient({ baseUrl: "/api/v1", fetchImpl }).getWorkspace("run/1");
    expect(calls).toEqual(["/api/v1/migrations/run%2F1/workspace"]);
  });

  it("parses only bounded event envelope fields", () => {
    expect(parseSse('event: migration.event\ndata: {"type":"dispatch.started","sequence":2,"data":{"slice_id":"a"}}')).toEqual({
      type: "dispatch.started",
      sequence: 2,
      data: { slice_id: "a" },
      timestamp_utc: "",
    });
    expect(parseSse("data: {\"sequence\":0}")).toBeNull();
  });
});
