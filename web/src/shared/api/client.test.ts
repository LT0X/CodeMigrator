import { describe, expect, it } from "vitest";
import { createApiClient, parseSse } from "./client";

describe("API boundary", () => {
  it("lists only registered projects and their selectable snapshots", async () => {
    const calls: string[] = [];
    const fetchImpl = async (input: RequestInfo | URL): Promise<Response> => {
      calls.push(String(input));
      return new Response(JSON.stringify({ items: [{ project_id: "project-1", snapshot_id: "snapshot-1", status: "READY" }] }), { status: 200 });
    };

    const projects = await createApiClient({ baseUrl: "/api/v1", fetchImpl }).listProjects();

    expect(calls).toEqual(["/api/v1/projects"]);
    expect(projects).toEqual([{ project_id: "project-1", snapshot_id: "snapshot-1", status: "READY" }]);
  });

  it("creates a Draft session from a registered project snapshot and goal", async () => {
    let requestUrl = "";
    let requestInit: RequestInit | undefined;
    const fetchImpl = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      requestUrl = String(input);
      requestInit = init;
      return new Response(JSON.stringify({ session_id: "session-1", status: "OPEN", revision: 0 }), { status: 201 });
    };

    const session = await createApiClient({ baseUrl: "/api/v1", fetchImpl }).createDraftSession({
      source: { project_id: "project-1", snapshot_id: "snapshot-1" },
      goal: "把服务迁移到 Python",
    });

    expect(requestUrl).toBe("/api/v1/sessions");
    expect(requestInit?.method).toBe("POST");
    expect(JSON.parse(String(requestInit?.body))).toEqual({
      kind: "DRAFT",
      payload: {
        source: { project_id: "project-1", snapshot_id: "snapshot-1" },
        goal: "把服务迁移到 Python",
      },
    });
    expect(new Headers(requestInit?.headers).get("Idempotency-Key")).toBeTruthy();
    expect(session).toEqual({ session_id: "session-1", status: "OPEN", revision: 0 });
  });

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

  it("loads the existing Run status projection through its encoded resource path", async () => {
    const calls: string[] = [];
    const fetchImpl = async (input: RequestInfo | URL): Promise<Response> => {
      calls.push(String(input));
      return new Response(JSON.stringify({ run_id: "run/1", status: "EXECUTING", version: 7 }), { status: 200 });
    };

    const migration = await createApiClient({ baseUrl: "/api/v1", fetchImpl }).getMigration("run/1");

    expect(calls).toEqual(["/api/v1/migrations/run%2F1"]);
    expect(migration).toEqual({ run_id: "run/1", status: "EXECUTING", version: 7 });
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

  it("reads session events from the v1 envelope after the supplied replay cursor", async () => {
    let requestUrl = "";
    let requestInit: RequestInit | undefined;
    const fetchImpl = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      requestUrl = String(input);
      requestInit = init;
      return new Response(
        'event: migration.session.event\nid: 8\ndata: {"schema":"migration.session.event","version":1,"type":"agent_run.started","sequence":8,"data":{"agent_run_id":"agent-1"},"timestamp_utc":"2026-10-01T10:00:00Z"}\n\n',
        { status: 200, headers: { "Content-Type": "text/event-stream" } },
      );
    };
    const received = [];

    for await (const event of createApiClient({ baseUrl: "/api/v1", fetchImpl }).streamSessionEvents("session/1", 7)) {
      received.push(event);
    }

    expect(requestUrl).toBe("/api/v1/sessions/session%2F1/events");
    expect(new Headers(requestInit?.headers).get("Accept")).toBe("text/event-stream");
    expect(new Headers(requestInit?.headers).get("Last-Event-ID")).toBe("7");
    expect(received).toEqual([{
      schema: "migration.session.event",
      version: 1,
      type: "agent_run.started",
      sequence: 8,
      data: { agent_run_id: "agent-1" },
      timestamp_utc: "2026-10-01T10:00:00Z",
      sse_id: "8",
    }]);
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
