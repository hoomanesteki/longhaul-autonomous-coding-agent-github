/**
 * The console's only way to the API: `fetch`, the bearer token and the types of `schema.ts`.
 *
 * Every function below takes its parameter and body types from `operations` in the generated
 * schema, by operation id. When the API renames an operation or changes a parameter, the build
 * of the console fails here and not when someone opens the page. Nothing in this file is typed
 * by hand except the glue.
 */

import type { operations } from "./schema";
import { getToken } from "./token";

type Operation = keyof operations;

type Query<K extends Operation> = operations[K]["parameters"]["query"];

type Json<T> = T extends { content: { "application/json": infer B } } ? B : never;

/** The body of the 200 reply of an operation. */
export type Reply<K extends Operation> = operations[K]["responses"] extends { 200: infer R }
  ? Json<R>
  : never;

/** The JSON body an operation takes. */
export type Body<K extends Operation> = operations[K] extends {
  requestBody?: { content: { "application/json": infer B } };
}
  ? B
  : never;

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
  options: { query?: Params; body?: unknown } = {},
): Promise<T> {
  const headers = new Headers({ Accept: "application/json" });
  const token = getToken();
  if (token !== null) headers.set("Authorization", `Bearer ${token}`);
  const init: RequestInit = { method, headers };
  if (options.body !== undefined) {
    headers.set("Content-Type", "application/json");
    init.body = JSON.stringify(options.body);
  }
  const response = await fetch(`${path}${queryString(options.query)}`, init);
  if (!response.ok) throw new ApiError(response.status, await detailOf(response));
  return (await response.json()) as T;
}

/** A path segment from user data: an id with a slash in it must not change the route. */
const seg = encodeURIComponent;

const get = <K extends Operation>(path: string, query?: Query<K>) =>
  request<Reply<K>>("GET", path, { query: query as Params | undefined });

const post = <K extends Operation>(path: string, body?: Body<K>) =>
  request<Reply<K>>("POST", path, { body });

export const listTasks = (query?: Query<"list_tasks">) => get<"list_tasks">("/api/tasks", query);

export const getTask = (taskId: string) => get<"get_task">(`/api/tasks/${seg(taskId)}`);

export const getTimeline = (taskId: string, query?: Query<"get_timeline">) =>
  get<"get_timeline">(`/api/tasks/${seg(taskId)}/timeline`, query);

export const getTurn = (taskId: string, turnIndex: number, query?: Query<"get_turn">) =>
  get<"get_turn">(`/api/tasks/${seg(taskId)}/turns/${turnIndex}`, query);

export const getToolOutput = (
  taskId: string,
  callId: string,
  query?: Query<"get_tool_output">,
) =>
  get<"get_tool_output">(`/api/tasks/${seg(taskId)}/tool-calls/${seg(callId)}/output`, query);

export const approvePlan = (taskId: string) =>
  post<"approve_plan">(`/api/tasks/${seg(taskId)}/approve-plan`);

export const rejectPlan = (taskId: string, body: Body<"reject_plan">) =>
  post<"reject_plan">(`/api/tasks/${seg(taskId)}/reject-plan`, body);

export const hint = (taskId: string, body: Body<"hint">) =>
  post<"hint">(`/api/tasks/${seg(taskId)}/hint`, body);

export const addBudget = (taskId: string, body: Body<"budget">) =>
  post<"budget">(`/api/tasks/${seg(taskId)}/budget`, body);

export const cancelTask = (taskId: string, body: Body<"cancel">) =>
  post<"cancel">(`/api/tasks/${seg(taskId)}/cancel`, body);

export const listMemory = (query?: Query<"list_memory">) =>
  get<"list_memory">("/api/memory", query);

export const approveMemory = (itemId: string) =>
  post<"approve">(`/api/memory/${seg(itemId)}/approve`);

export const rejectMemory = (itemId: string) =>
  post<"reject">(`/api/memory/${seg(itemId)}/reject`);

export const listRepos = () => get<"list_repos">("/api/repos");

export const healthz = () => get<"healthz">("/healthz");
