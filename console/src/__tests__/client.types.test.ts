import { describe, expect, it } from "vitest";

import { call } from "../api/client";
import type { Args, PathsWith } from "../api/typed";

// These checks run in `npm run typecheck`, not in vitest: each `@ts-expect-error` line fails the
// build when the client stops rejecting the call under it. That is how an API change that breaks
// a call (a renamed path, a body that became required) stops the console build.

// A small spec of our own, because the real API has no operation with a required query yet.
type Json<T> = { content: { "application/json": T } };
type Reply = { 200: Json<{ ok: true }> };

interface Spec {
  "/needs-query": {
    get: {
      parameters: { query: { kind: string }; path?: never };
      requestBody?: never;
      responses: Reply;
    };
    post?: never;
  };
  "/needs-body": {
    get?: never;
    post: {
      parameters: { query?: never; path?: never };
      requestBody: Json<{ text: string }>;
      responses: Reply;
    };
  };
  "/optional-body": {
    get?: never;
    post: {
      parameters: { query?: never; path?: never };
      requestBody?: Json<{ text: string }>;
      responses: Reply;
    };
  };
  "/by/{id}": {
    get: {
      parameters: { query?: { limit?: number }; path: { id: string } };
      requestBody?: never;
      responses: Reply;
    };
    post?: never;
  };
}

declare function specGet<P extends PathsWith<Spec, "get">>(
  path: P,
  ...args: Args<Spec, P, "get">
): void;
declare function specPost<P extends PathsWith<Spec, "post">>(
  path: P,
  ...args: Args<Spec, P, "post">
): void;

function specChecks(): void {
  specGet("/by/{id}", { path: { id: "a" } });
  specGet("/by/{id}", { path: { id: "a" }, query: { limit: 5 } });
  specGet("/needs-query", { query: { kind: "x" } });
  specPost("/needs-body", { body: { text: "hi" } });
  specPost("/optional-body");
  specPost("/optional-body", { body: { text: "hi" } });

  // @ts-expect-error a required query parameter cannot be left out
  specGet("/needs-query");
  // @ts-expect-error a required query parameter cannot be left out of the options either
  specGet("/needs-query", {});
  // @ts-expect-error a required body cannot be left out
  specPost("/needs-body");
  // @ts-expect-error a required body cannot be left out of the options either
  specPost("/needs-body", {});
  // @ts-expect-error the path parameters must be given
  specGet("/by/{id}");
  // @ts-expect-error the path parameter has another name
  specGet("/by/{id}", { path: { other: "a" } });
  // @ts-expect-error the path has no POST
  specPost("/by/{id}", { path: { id: "a" } });
}

function apiChecks(): void {
  call("get", "/api/repos");
  call("get", "/api/tasks");
  call("get", "/api/tasks", { query: { status: "active" } });
  call("post", "/api/tasks/{task_id}/approve-plan", { path: { task_id: "t1" } });
  call("post", "/api/tasks/{task_id}/hint", { path: { task_id: "t1" }, body: { text: "go" } });

  // @ts-expect-error a path that the API does not have, as when /api/repos is renamed
  call("get", "/api/repositories");
  // @ts-expect-error a path whose parameter was renamed
  call("get", "/api/tasks/{task_id}/turns/{index}", { path: { task_id: "t", index: 1 } });
  // @ts-expect-error /api/repos has no POST
  call("post", "/api/repos");
  // @ts-expect-error the hint action takes a body
  call("post", "/api/tasks/{task_id}/hint", { path: { task_id: "t1" } });
  // @ts-expect-error the task id is a path parameter and must be given
  call("get", "/api/tasks/{task_id}");
  // @ts-expect-error the body of a hint is a text
  call("post", "/api/tasks/{task_id}/hint", { path: { task_id: "t1" }, body: { note: "go" } });
}

describe("client types", () => {
  it("rejects calls that the generated schema does not allow", () => {
    // The checks above are compile-time only. They are never called: the calls would use fetch.
    expect([specChecks, apiChecks]).toHaveLength(2);
  });
});
