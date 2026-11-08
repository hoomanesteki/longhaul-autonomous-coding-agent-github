import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, hint, listTasks, rejectPlan } from "../api/client";
import { clearToken, setToken } from "../api/token";

// The client talks to `fetch` and to nothing else, so a stub of it sees every request.
const fetchStub = vi.fn<typeof fetch>();

function reply(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function lastRequest(): { url: string; init: RequestInit; headers: Headers } {
  const call = fetchStub.mock.calls.at(-1);
  if (call === undefined) throw new Error("fetch was not called");
  const [url, init = {}] = call;
  return { url: String(url), init, headers: new Headers(init.headers) };
}

beforeEach(() => {
  fetchStub.mockReset();
  vi.stubGlobal("fetch", fetchStub);
  clearToken();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("api client", () => {
  it("adds bearer header", async () => {
    fetchStub.mockResolvedValue(reply({ items: [], next_cursor: null }));
    setToken("s3cret");

    await listTasks();

    expect(lastRequest().headers.get("authorization")).toBe("Bearer s3cret");
  });

  it("sends no authorization header without a token", async () => {
    fetchStub.mockResolvedValue(reply({ items: [], next_cursor: null }));

    await listTasks();

    expect(lastRequest().headers.has("authorization")).toBe(false);
  });

  it("puts the filters in the query string and leaves out the empty ones", async () => {
    fetchStub.mockResolvedValue(reply({ items: [], next_cursor: null }));

    await listTasks({ status: "active", repo: undefined, cursor: null, limit: 20 });

    expect(lastRequest().url).toBe("/api/tasks?status=active&limit=20");
  });

  it("sends a JSON body with the path parameter encoded", async () => {
    fetchStub.mockResolvedValue(reply({ task_id: "a/b", signal: "hint", by: "console" }));

    await hint("a/b", { text: "try the other test" });

    const { url, init, headers } = lastRequest();
    expect(url).toBe("/api/tasks/a%2Fb/hint");
    expect(init.method).toBe("POST");
    expect(headers.get("content-type")).toBe("application/json");
    expect(JSON.parse(String(init.body))).toEqual({ text: "try the other test" });
  });

  it("sends an empty JSON object for an action with an optional body", async () => {
    fetchStub.mockResolvedValue(reply({ task_id: "t1", signal: "reject_plan", by: "console" }));

    await rejectPlan("t1", {});

    expect(JSON.parse(String(lastRequest().init.body))).toEqual({});
  });

  it("raises an ApiError with the status and the detail of the API", async () => {
    fetchStub.mockResolvedValue(reply({ detail: "temporal unavailable" }, 503));

    const failure = await listTasks().catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(ApiError);
    expect(failure).toMatchObject({ status: 503, detail: "temporal unavailable" });
  });

  it("raises an ApiError when the reply is not JSON", async () => {
    fetchStub.mockResolvedValue(new Response("Bad Gateway", { status: 502 }));

    const failure = await listTasks().catch((error: unknown) => error);

    expect(failure).toMatchObject({ status: 502, detail: "Bad Gateway" });
  });
});
