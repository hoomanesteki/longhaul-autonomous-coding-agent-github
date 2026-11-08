/**
 * The console's only way to the API: `fetch`, the bearer token and the types of `schema.ts`.
 *
 * Every call names a method and a path of the generated schema, and `typed.ts` works out from
 * that what the call may and must pass: the path parameters, the query and the body. When the
 * API renames a path, moves a parameter, or makes a query parameter or a body required, the
 * build of the console fails here and not when someone opens the page. Nothing in this file is
 * typed by hand except the glue.
 */

import type { paths } from "./schema";
import { clearToken, getToken } from "./token";
import type { Args, BodyOf, Method, PathsWith, QueryOf, Reply } from "./typed";

/** A reply that was not a success. `detail` is the message the API gave, or the status text. */
export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(`API answered ${status}: ${detail}`);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

type Params = Record<string, string | number | boolean | null | undefined>;

function queryString(query: Params | undefined): string {
  const search = new URLSearchParams();
  for (const [name, value] of Object.entries(query ?? {})) {
    // An unset filter is left out, so the API applies its own default.
    if (value !== undefined && value !== null) search.set(name, String(value));
  }
  const text = search.toString();
  return text === "" ? "" : `?${text}`;
}

async function detailOf(response: Response): Promise<string> {
  const text = await response.text();
  try {
    const parsed: unknown = JSON.parse(text);
    if (typeof parsed === "object" && parsed !== null && "detail" in parsed) {
      const { detail } = parsed;
      // FastAPI sends a list of problems for a request it could not validate.
      return typeof detail === "string" ? detail : JSON.stringify(detail);
    }
  } catch {
    // Not JSON: a proxy error page, for one.
  }
  return text === "" ? response.statusText : text;
}

async function request<T>(
  method: "GET" | "POST",
  path: string,
  options: { query?: Params; body?: unknown; token?: string } = {},
): Promise<T> {
  const headers = new Headers({ Accept: "application/json" });
  const token = options.token ?? getToken();
  if (token !== null) headers.set("Authorization", `Bearer ${token}`);
  const init: RequestInit = { method, headers };
  if (options.body !== undefined) {
    headers.set("Content-Type", "application/json");
    init.body = JSON.stringify(options.body);
  }
  const response = await fetch(`${path}${queryString(options.query)}`, init);
  if (!response.ok) {
    // A token the API refuses is of no use again: drop it, and the console shows the login. A
    // token that is only being tried (the login screen) is not the stored one, so it stays out.
    if (response.status === 401 && options.token === undefined) clearToken();
    throw new ApiError(response.status, await detailOf(response));
  }
  return (await response.json()) as T;
}

type Runtime = { path?: Params; query?: Params; body?: unknown };

/** Put the path parameters in the template. An id with a slash in it must not change the route. */
function fill(template: string, params: Params = {}): string {
  return template.replace(/\{(\w+)\}/g, (_, name: string) => {
    const value = params[name];
    if (value === undefined || value === null) throw new Error(`Missing path parameter ${name}`);
    return encodeURIComponent(String(value));
  });
}

/** One call to the API, checked against the generated schema. */
export function call<M extends Method, P extends PathsWith<paths, M>>(
  method: M,
  path: P,
  ...args: Args<paths, P, M>
): Promise<Reply<paths, P, M>> {
  const options = (args as unknown as [Runtime?])[0] ?? {};
  return request<Reply<paths, P, M>>(
    method === "get" ? "GET" : "POST",
    fill(path, options.path),
    options,
  );
}

type Query<P extends PathsWith<paths, "get">> = QueryOf<paths, P, "get">;
type Body<P extends PathsWith<paths, "post">> = BodyOf<paths, P, "post">;

export const listTasks = (query?: Query<"/api/tasks">) => call("get", "/api/tasks", { query });

export const getTask = (taskId: string) =>
  call("get", "/api/tasks/{task_id}", { path: { task_id: taskId } });

export const getTimeline = (taskId: string, query?: Query<"/api/tasks/{task_id}/timeline">) =>
  call("get", "/api/tasks/{task_id}/timeline", { path: { task_id: taskId }, query });

export const getTurn = (
  taskId: string,
  turnIndex: number,
  query?: Query<"/api/tasks/{task_id}/turns/{turn_index}">,
) =>
  call("get", "/api/tasks/{task_id}/turns/{turn_index}", {
    path: { task_id: taskId, turn_index: turnIndex },
    query,
  });

export const getToolOutput = (
  taskId: string,
  callId: string,
  query?: Query<"/api/tasks/{task_id}/tool-calls/{call_id}/output">,
) =>
  call("get", "/api/tasks/{task_id}/tool-calls/{call_id}/output", {
    path: { task_id: taskId, call_id: callId },
    query,
  });

export const approvePlan = (taskId: string) =>
  call("post", "/api/tasks/{task_id}/approve-plan", { path: { task_id: taskId } });

export const rejectPlan = (taskId: string, body: Body<"/api/tasks/{task_id}/reject-plan">) =>
  call("post", "/api/tasks/{task_id}/reject-plan", { path: { task_id: taskId }, body });

export const hint = (taskId: string, body: Body<"/api/tasks/{task_id}/hint">) =>
  call("post", "/api/tasks/{task_id}/hint", { path: { task_id: taskId }, body });

export const addBudget = (taskId: string, body: Body<"/api/tasks/{task_id}/budget">) =>
  call("post", "/api/tasks/{task_id}/budget", { path: { task_id: taskId }, body });

export const cancelTask = (taskId: string, body: Body<"/api/tasks/{task_id}/cancel">) =>
  call("post", "/api/tasks/{task_id}/cancel", { path: { task_id: taskId }, body });

export const listMemory = (query?: Query<"/api/memory">) => call("get", "/api/memory", { query });

export const approveMemory = (itemId: string) =>
  call("post", "/api/memory/{item_id}/approve", { path: { item_id: itemId } });

export const rejectMemory = (itemId: string) =>
  call("post", "/api/memory/{item_id}/reject", { path: { item_id: itemId } });

export const listRepos = () => call("get", "/api/repos");

export const healthz = () => call("get", "/healthz");

// The login screen asks an ordinary route whether it takes the token. `satisfies` keeps the path
// tied to the generated schema, as every other call here is.
const CHECK_PATH = "/api/repos" satisfies keyof paths;

/** Whether the API accepts `token`: true for yes, false for a 401, and an ApiError otherwise. */
export async function tokenWorks(token: string): Promise<boolean> {
  try {
    await request("GET", CHECK_PATH, { token });
    return true;
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) return false;
    throw error;
  }
}
