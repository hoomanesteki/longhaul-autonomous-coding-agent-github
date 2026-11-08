import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { components } from "../../api/schema";
import TaskPage from "../TaskPage";

type Schemas = components["schemas"];
type Item = Schemas["TimelinePage"]["items"][number];
type Detail = Schemas["TaskDetail"];

const fetchStub = vi.fn<typeof fetch>();

const TASK_ID = "lh-acme-widgets-12";
const SHA = "0123456789abcdef0123456789abcdef01234567";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function at(seconds: number): string {
  return new Date(Date.UTC(2026, 10, 1, 10, 0, seconds)).toISOString();
}

function detail(overrides: Partial<Detail> = {}): Detail {
  return {
    task: {
      id: TASK_ID,
      repo: "acme/widgets",
      issue_number: 12,
      title: "Fix the parser",
      status: "active",
      phase: "coding",
      outcome: null,
      pr_url: "https://github.com/acme/widgets/pull/31",
      model_coder: "claude-sonnet",
      cost_usd: 1.2345,
      turns: 7,
      started_at: at(0),
      ended_at: null,
      created_at: at(0),
      settings: {},
      input_tokens_total: 3000,
      output_tokens_total: 1200,
      cache_read_total: 9000,
      compactions: 0,
      max_fraction: 0.2,
    },
    workflow: "running",
    live: null,
    plan: {
      version: 2,
      status: "approved",
      decided_by: "alice",
      decided_at: at(5),
      created_at: at(3),
      plan: {
        summary: "Make the parser accept a trailing comma.",
        steps: [
          { id: "s1", description: "Write a failing test", files: [], done_when: "red", status: "done" },
          { id: "s2", description: "Change the grammar", files: [], done_when: "green", status: "doing" },
          { id: "s3", description: "Update the docs", files: [], done_when: "linked", status: "todo" },
        ],
        acceptance_checks: ["pytest -q"],
        risks: [],
      },
    },
    ...overrides,
  };
}

function turn(index: number, overrides: Partial<Schemas["TimelineTurn"]> = {}): Item {
  return {
    kind: "turn",
    ts: at(10 + index * 2),
    turn_index: index,
    attempt: 1,
    role: "coder",
    input_tokens: 100,
    output_tokens: 20,
    cache_read_tokens: 300,
    cache_write_tokens: 0,
    cost_usd: 0.01,
    stop_reason: "tool_use",
    latency_ms: 900,
    ended_at: null,
    ...overrides,
  };
}

function call(id: string, index: number, overrides: Partial<Schemas["TimelineToolCall"]> = {}): Item {
  return {
    kind: "tool_call",
    ts: at(11 + index * 2),
    id,
    turn_index: index,
    name: "run_command",
    ok: true,
    exit_code: 0,
    duration_ms: 1200,
    has_output: true,
    post_sha: null,
    ...overrides,
  };
}

function event(id: number, type: string, seconds: number, payload: Record<string, unknown> = {}): Item {
  return { kind: "event", ts: at(seconds), id, type, payload };
}

function turnDetail(index: number): Schemas["TurnDetail"] {
  return {
    turn_index: index,
    attempt: 1,
    attempts: [1],
    role: "coder",
    started_at: at(10 + index * 2),
    ended_at: at(11 + index * 2),
    input_tokens: 100,
    output_tokens: 20,
    cache_read_tokens: 300,
    cache_write_tokens: 0,
    cost_usd: 0.01,
    stop_reason: "tool_use",
    latency_ms: 900,
    request: [{ role: "user", content: [{ type: "text", text: `Please fix turn ${index}.` }] }],
    response: {
      content: [{ type: "text", text: `I will run the tests for turn ${index}.` }],
      tool_calls: [{ id: `call-${index}`, name: "run_command", input: { command: "pytest -q tests/unit" } }],
      stop_reason: "tool_use",
      usage: { input_tokens: 100, output_tokens: 20, cache_read_tokens: 300, cache_write_tokens: 0 },
      model: "claude-sonnet-test",
      latency_ms: 900,
      dropped_thinking_blocks: 0,
    },
  };
}

const OUTPUT_LINES = Array.from({ length: 450 }, (_, i) => `line ${i + 1}`);

function toolOutput(callId: string, start: number, end: number): Schemas["ToolOutput"] {
  const last = Math.min(end, OUTPUT_LINES.length);
  return {
    call_id: callId,
    name: "run_command",
    ok: true,
    exit_code: 0,
    text: OUTPUT_LINES.slice(start - 1, last).join("\n"),
    start_line: start,
    end_line: last,
    total_lines: OUTPUT_LINES.length,
    next_start: last < OUTPUT_LINES.length ? last + 1 : null,
  };
}

type World = { detail?: Detail | null; items?: Item[] };

/** Answer the four task routes like the API does, with the timeline paged by `limit` and `cursor`. */
function serve(world: World) {
  const body = world.detail === undefined ? detail() : world.detail;
  const items = world.items ?? [];
  fetchStub.mockImplementation((input) => {
    const url = new URL(String(input), "http://console.test");
    const path = url.pathname;
    if (path === `/api/tasks/${TASK_ID}`) {
      return Promise.resolve(body === null ? json({ detail: "task not found" }, 404) : json(body));
    }
    if (path === `/api/tasks/${TASK_ID}/timeline`) {
      const limit = Number(url.searchParams.get("limit") ?? 100);
      const from = Number(url.searchParams.get("cursor") ?? 0);
      const next = from + limit;
      return Promise.resolve(
        json({ items: items.slice(from, next), next_cursor: next < items.length ? String(next) : null }),
      );
    }
    const turnMatch = /\/turns\/(\d+)$/.exec(path);
    if (turnMatch !== null) return Promise.resolve(json(turnDetail(Number(turnMatch[1]))));
    const outputMatch = /\/tool-calls\/([^/]+)\/output$/.exec(path);
    if (outputMatch !== null) {
      const start = Number(url.searchParams.get("start") ?? 1);
      const end = Number(url.searchParams.get("end") ?? start + 199);
      return Promise.resolve(json(toolOutput(decodeURIComponent(outputMatch[1] ?? ""), start, end)));
    }
    return Promise.resolve(json({ detail: "not found" }, 404));
  });
}

function urls(): URL[] {
  return fetchStub.mock.calls.map(([input]) => new URL(String(input), "http://console.test"));
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[`/tasks/${TASK_ID}`]}>
        <Routes>
          <Route path="/tasks/:taskId" element={<TaskPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function rowOf(label: string | HTMLElement): HTMLElement {
  const element = typeof label === "string" ? screen.getByText(label) : label;
  return element.closest("[data-testid='timeline-row']") as HTMLElement;
}

beforeEach(() => {
  fetchStub.mockReset();
  vi.stubGlobal("fetch", fetchStub);
  // The virtualizer scrolls the window when the list moves, and jsdom does not implement that.
  vi.stubGlobal("scrollTo", vi.fn());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("task header", () => {
  it("shows the phase, cost, turns, tokens, cache ratio, pull request and plan", async () => {
    serve({ items: [] });
    renderPage();

    expect(await screen.findByRole("heading", { name: "Fix the parser" })).toBeInTheDocument();
    const header = screen.getByRole("region", { name: "Task summary" });
    expect(within(header).getByText("coding")).toBeInTheDocument();
    expect(within(header).getByText("$1.23")).toBeInTheDocument();
    expect(within(header).getByText("7")).toBeInTheDocument();
    expect(within(header).getByText("3,000 in, 1,200 out")).toBeInTheDocument();
    // 9,000 cache reads out of 12,000 input tokens that could have been cached.
    expect(within(header).getByText("75%")).toBeInTheDocument();
    expect(within(header).getByRole("link", { name: "Pull request" })).toHaveAttribute(
      "href",
      "https://github.com/acme/widgets/pull/31",
    );

    const plan = screen.getByRole("region", { name: "Plan" });
    expect(within(plan).getByText("Make the parser accept a trailing comma.")).toBeInTheDocument();
    const steps = within(plan).getAllByRole("listitem");
    expect(steps.map((step) => step.textContent)).toEqual([
      expect.stringContaining("Write a failing test"),
      expect.stringContaining("Change the grammar"),
      expect.stringContaining("Update the docs"),
    ]);
    expect(within(steps[0] as HTMLElement).getByText("done")).toBeInTheDocument();
    expect(within(steps[1] as HTMLElement).getByText("doing")).toBeInTheDocument();
    expect(within(steps[2] as HTMLElement).getByText("todo")).toBeInTheDocument();
  });

  it("prefers the answer of the workflow to the row, which can lag", async () => {
    serve({
      detail: detail({
        live: {
          task_id: TASK_ID,
          phase: "paused",
          turn_index: 12,
          cost_usd: 2.5,
          last_checkpoint: null,
          plan_version: 2,
          paused_reason: "the same test failed three times",
        },
      }),
    });
    renderPage();

    const header = await screen.findByRole("region", { name: "Task summary" });
    expect(within(header).getByText("paused")).toBeInTheDocument();
    expect(within(header).getByText("$2.50")).toBeInTheDocument();
    expect(within(header).getByText("the same test failed three times")).toBeInTheDocument();
  });

  it("says when the live status is not available", async () => {
    serve({ detail: detail({ workflow: "unreachable" }) });
    renderPage();

    expect(await screen.findByText(/Temporal does not answer/)).toBeInTheDocument();
    expect(screen.getByText("coding")).toBeInTheDocument();
  });

  it("leaves out the pull request and the plan when the task has neither", async () => {
    const bare = detail({ plan: null });
    bare.task.pr_url = null;
    serve({ detail: bare });
    renderPage();

    await screen.findByRole("heading", { name: "Fix the parser" });
    expect(screen.queryByRole("link", { name: "Pull request" })).not.toBeInTheDocument();
    expect(screen.getByText("No plan yet.")).toBeInTheDocument();
  });

  it("says so for a task that does not exist", async () => {
    serve({ detail: null });
    renderPage();

    expect(await screen.findByText("This task does not exist.")).toBeInTheDocument();
  });
});

describe("timeline", () => {
  it("shows each turn with its tool calls and what they did", async () => {
    serve({
      items: [
        turn(0),
        call("call-0", 0, { post_sha: SHA }),
        turn(1, { cost_usd: 0.0123 }),
        call("call-1", 1, { ok: false, exit_code: 2, duration_ms: 75_000, has_output: false }),
      ],
    });
    renderPage();

    const first = rowOf(await screen.findByText("Turn 0"));
    expect(within(first).getByText("coder")).toBeInTheDocument();
    expect(within(first).getByText("claude-sonnet")).toBeInTheDocument();
    expect(within(first).getByText("100 in, 20 out")).toBeInTheDocument();
    expect(within(first).getByText("$0.0100")).toBeInTheDocument();
    expect(within(first).getByText("run_command")).toBeInTheDocument();
    expect(within(first).getByText("ok")).toBeInTheDocument();
    expect(within(first).getByText("1.2 s")).toBeInTheDocument();
    expect(within(first).getByText("0123456")).toBeInTheDocument();

    const second = rowOf(screen.getByText("Turn 1"));
    expect(within(second).getByText("$0.0123")).toBeInTheDocument();
    expect(within(second).getByText("error")).toBeInTheDocument();
    expect(within(second).getByText("1 m 15 s")).toBeInTheDocument();
    expect(within(second).queryByRole("button", { name: "Show output" })).not.toBeInTheDocument();
  });

  it("colours event rows by their type", async () => {
    serve({
      items: [
        event(1, "compaction", 5, { dropped_turns: 6 }),
        event(2, "paused", 6, { reason: "stuck" }),
        event(3, "hint", 7, { by: "alice" }),
        event(4, "verdict", 8, { passed: false }),
        event(5, "sandbox_recreated", 9),
      ],
    });
    renderPage();

    await screen.findByText("compaction");
    const tones = ["compaction", "paused", "hint", "verdict", "sandbox_recreated"].map((type) =>
      rowOf(type).getAttribute("data-tone"),
    );
    expect(tones).toEqual(["compaction", "pause", "hint", "verdict", "recovery"]);
    expect(within(rowOf("paused")).getByText("reason: stuck")).toBeInTheDocument();
  });

  it("shows every attempt of a turn that was asked again", async () => {
    serve({ items: [turn(3), turn(3, { attempt: 2, stop_reason: "end_turn" })] });
    renderPage();

    const row = rowOf(await screen.findByText("Turn 3"));
    expect(within(row).getByText("attempt 1")).toBeInTheDocument();
    expect(within(row).getByText("attempt 2")).toBeInTheDocument();
  });

  it("follows the cursor until the whole timeline is loaded", async () => {
    const items = Array.from({ length: 12 }, (_, i) => turn(i));
    serve({ items });
    renderPage();

    expect(await screen.findByText("Turn 11")).toBeInTheDocument();
    const timelineCalls = urls().filter((url) => url.pathname.endsWith("/timeline"));
    expect(timelineCalls.length).toBeGreaterThan(0);
    expect(timelineCalls.every((url) => url.searchParams.get("limit") === "500")).toBe(true);
  });

  it("draws the cost per turn as a line with one point for each turn", async () => {
    serve({ items: [turn(0, { cost_usd: 0.1 }), turn(1, { cost_usd: 0.2 }), turn(2, { cost_usd: 0.3 })] });
    renderPage();

    const chart = await screen.findByRole("img", { name: /Cost over turns/ });
    expect(chart).toHaveAccessibleName(expect.stringContaining("$0.60"));
    const line = chart.querySelector("polyline");
    expect(line?.getAttribute("points")?.trim().split(/\s+/)).toHaveLength(3);
  });

  it("says when nothing has been recorded", async () => {
    serve({ items: [] });
    renderPage();

    expect(await screen.findByText("Nothing recorded yet.")).toBeInTheDocument();
  });

  it("renders a 300-turn fixture with fewer than 100 rows in the page", async () => {
    const items: Item[] = [];
    for (let i = 0; i < 300; i += 1) {
      items.push(turn(i), call(`call-${i}`, i));
      if (i % 50 === 0) items.push(event(i + 1, "compaction", 10 + i * 2));
    }
    serve({ items });
    renderPage();

    await screen.findByText("Turn 0");
    // The last page has to arrive before the page counts as loaded: the sparkline covers all 300.
    await screen.findByRole("img", { name: /Cost over turns, \$3\.00/ });
    expect(screen.queryByText("Turn 299")).not.toBeInTheDocument();
    const rows = screen.getAllByTestId("timeline-row");
    expect(rows.length).toBeGreaterThan(0);
    expect(rows.length).toBeLessThan(100);
    expect(document.querySelectorAll("li").length).toBeLessThan(100);
  });
});

describe("expanders", () => {
  it("loads the request and reply of one turn when it is opened", async () => {
    serve({ items: [turn(0), call("call-0", 0), turn(1), call("call-1", 1)] });
    renderPage();
    const user = userEvent.setup();
    const row = rowOf(await screen.findByText("Turn 1"));
    expect(urls().some((url) => url.pathname.includes("/turns/"))).toBe(false);

    await user.click(within(row).getByRole("button", { name: "Details" }));

    expect(await within(row).findByText("I will run the tests for turn 1.")).toBeInTheDocument();
    expect(within(row).getByText("Please fix turn 1.")).toBeInTheDocument();
    expect(within(row).getByText("claude-sonnet-test")).toBeInTheDocument();
    // The arguments of the call come with the reply, so they show once the turn is open.
    expect(within(row).getByText("pytest -q tests/unit")).toBeInTheDocument();
    const turnCalls = urls().filter((url) => url.pathname.includes("/turns/"));
    expect(turnCalls.map((url) => url.pathname)).toEqual([`/api/tasks/${TASK_ID}/turns/1`]);
    expect(turnCalls[0]?.searchParams.get("attempt")).toBe("1");

    await user.click(within(row).getByRole("button", { name: "Hide details" }));
    expect(within(row).queryByText("I will run the tests for turn 1.")).not.toBeInTheDocument();
  });

  it("fetches only the output of the call that was opened, a page of lines at a time", async () => {
    serve({ items: [turn(0), call("call-0", 0), turn(1), call("call-1", 1)] });
    renderPage();
    const user = userEvent.setup();
    const row = rowOf(await screen.findByText("Turn 1"));

    await user.click(within(row).getByRole("button", { name: "Show output" }));

    expect(await within(row).findByText(/line 1\b/)).toBeInTheDocument();
    const outputCalls = urls().filter((url) => url.pathname.includes("/tool-calls/"));
    expect(outputCalls.map((url) => url.pathname)).toEqual([
      `/api/tasks/${TASK_ID}/tool-calls/call-1/output`,
    ]);
    expect(outputCalls[0]?.searchParams.get("start")).toBe("1");
    expect(urls().some((url) => url.pathname.includes("/turns/"))).toBe(false);
    expect(within(row).getByText(/Lines 1 to 200 of 450/)).toBeInTheDocument();
    expect(within(row).queryByText(/line 201\b/)).not.toBeInTheDocument();

    await user.click(within(row).getByRole("button", { name: "Show more lines" }));

    expect(await within(row).findByText(/line 201\b/)).toBeInTheDocument();
    const next = urls().filter((url) => url.pathname.includes("/tool-calls/")).at(-1);
    expect(next?.searchParams.get("start")).toBe("201");
    expect(within(row).getByText(/Lines 1 to 400 of 450/)).toBeInTheDocument();

    await user.click(within(row).getByRole("button", { name: "Hide output" }));
    expect(within(row).queryByText(/line 1\b/)).not.toBeInTheDocument();
  });

  it("says so when the output cannot be read", async () => {
    serve({ items: [turn(0), call("call-0", 0)] });
    const base = fetchStub.getMockImplementation();
    fetchStub.mockImplementation((input, init) =>
      String(input).includes("/output")
        ? Promise.resolve(json({ detail: "stored output for this tool call not found" }, 404))
        : (base?.(input, init) ?? Promise.reject(new Error("no stub"))),
    );
    renderPage();
    const user = userEvent.setup();
    const row = rowOf(await screen.findByText("Turn 0"));

    await user.click(within(row).getByRole("button", { name: "Show output" }));

    await waitFor(() => expect(within(row).getByRole("alert")).toHaveTextContent(/not found/));
  });
});
