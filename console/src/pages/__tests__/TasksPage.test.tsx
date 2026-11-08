import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { components } from "../../api/schema";
import TasksPage from "../TasksPage";

type Task = components["schemas"]["TaskSummary"];

const fetchStub = vi.fn<typeof fetch>();

function task(overrides: Partial<Task> & Pick<Task, "id">): Task {
  return {
    repo: "acme/widgets",
    issue_number: 12,
    title: "Fix the parser",
    status: "active",
    phase: "coding",
    outcome: null,
    pr_url: null,
    model_coder: "claude-sonnet",
    cost_usd: 0.4321,
    turns: 7,
    started_at: null,
    ended_at: null,
    created_at: new Date().toISOString(),
    ...overrides,
  };
}

const CODING = task({ id: "lh-acme-widgets-12", title: "Fix the parser" });
const PAUSED = task({
  id: "lh-acme-widgets-13",
  issue_number: 13,
  title: "Speed up the build",
  phase: "paused",
});
const APPROVAL = task({
  id: "lh-acme-gadgets-4",
  repo: "acme/gadgets",
  issue_number: 4,
  title: "Add a retry",
  phase: "awaiting_plan_approval",
});
const DONE = task({
  id: "lh-acme-gadgets-3",
  repo: "acme/gadgets",
  issue_number: 3,
  title: "Rename the flag",
  status: "done",
  phase: "done",
  outcome: "pr_opened",
  cost_usd: 1.5,
  turns: 21,
});

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

const REPOS = [
  { repo: "acme/gadgets", allowlisted: true, created_at: "2026-11-01T10:00:00Z", settings: {}, tasks: 2 },
  { repo: "acme/widgets", allowlisted: true, created_at: "2026-11-01T10:00:00Z", settings: {}, tasks: 2 },
];

/** Answer the task list like the API does for `status` and `repo`, and the repo list. */
function serve(all: Task[]) {
  fetchStub.mockImplementation((input) => {
    const url = new URL(String(input), "http://console.test");
    if (url.pathname === "/api/repos") return Promise.resolve(json(REPOS));
    if (url.pathname === "/api/tasks") {
      const status = url.searchParams.get("status");
      const repo = url.searchParams.get("repo");
      const items = all.filter(
        (t) => (status === null || t.status === status) && (repo === null || t.repo === repo),
      );
      return Promise.resolve(json({ items, next_cursor: null }));
    }
    return Promise.resolve(json({ detail: "not found" }, 404));
  });
}

function taskCalls(): URL[] {
  return fetchStub.mock.calls
    .map(([input]) => new URL(String(input), "http://console.test"))
    .filter((url) => url.pathname === "/api/tasks");
}

function renderPage(path = "/tasks") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <TasksPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function rowOf(title: string): HTMLElement {
  return screen.getByText(title).closest("tr") as HTMLElement;
}

beforeEach(() => {
  fetchStub.mockReset();
  vi.stubGlobal("fetch", fetchStub);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("tasks page", () => {
  it("shows a row for every task with its columns", async () => {
    serve([CODING, DONE]);
    renderPage();

    const row = rowOf(await screen.findByText("Rename the flag"));
    expect(within(row).getByText("acme/gadgets")).toBeInTheDocument();
    expect(within(row).getByRole("link", { name: "#3" })).toHaveAttribute(
      "href",
      "https://github.com/acme/gadgets/issues/3",
    );
    expect(within(row).getByText("done")).toBeInTheDocument();
    expect(within(row).getByText("pr_opened")).toBeInTheDocument();
    expect(within(row).getByText("21")).toBeInTheDocument();
    expect(within(row).getByText("$1.50")).toBeInTheDocument();
    expect(within(row).getByText("now")).toBeInTheDocument();

    const headers = screen.getAllByRole("columnheader").map((h) => h.textContent);
    expect(headers).toEqual([
      "Repo",
      "Issue",
      "Title",
      "Phase",
      "Outcome",
      "Turns",
      "Cost",
      "Age",
      "Needs you",
    ]);
  });

  it("shows the age of a task in the largest unit that fits", async () => {
    const hoursAgo = new Date(Date.now() - 3 * 3600 * 1000 - 5000).toISOString();
    serve([task({ id: "t-old", title: "An old task", created_at: hoursAgo })]);
    renderPage();

    expect(await within(rowOf(await screen.findByText("An old task"))).findByText("3h")).toBeInTheDocument();
  });

  it("marks only paused and awaiting-approval tasks as needing you", async () => {
    serve([CODING, PAUSED, APPROVAL, DONE]);
    renderPage();

    await screen.findByText("Fix the parser");
    expect(within(rowOf("Speed up the build")).getByText("Paused")).toBeInTheDocument();
    expect(within(rowOf("Add a retry")).getByText("Approve the plan")).toBeInTheDocument();
    expect(within(rowOf("Fix the parser")).queryByText("Paused")).not.toBeInTheDocument();
    expect(within(rowOf("Rename the flag")).queryByText("Paused")).not.toBeInTheDocument();
  });

  it("leaves out the issue link for a task without an issue number", async () => {
    serve([task({ id: "t-1", title: "Manual task", issue_number: null })]);
    renderPage();

    const row = rowOf(await screen.findByText("Manual task"));
    expect(within(row).queryByRole("link", { name: /#/ })).not.toBeInTheDocument();
  });

  it("narrows the list to the tasks that need you with the toggle", async () => {
    serve([CODING, PAUSED, APPROVAL, DONE]);
    renderPage();
    const user = userEvent.setup();
    await screen.findByText("Fix the parser");

    await user.click(screen.getByRole("checkbox", { name: "Needs you" }));

    expect(screen.queryByText("Fix the parser")).not.toBeInTheDocument();
    expect(screen.queryByText("Rename the flag")).not.toBeInTheDocument();
    expect(screen.getByText("Speed up the build")).toBeInTheDocument();
    expect(screen.getByText("Add a retry")).toBeInTheDocument();

    await user.click(screen.getByRole("checkbox", { name: "Needs you" }));
    expect(screen.getByText("Fix the parser")).toBeInTheDocument();
  });

  it("says so when nothing needs you", async () => {
    serve([CODING, DONE]);
    renderPage("/tasks?needs_you=1");

    expect(await screen.findByText("Nothing needs you right now.")).toBeInTheDocument();
  });

  it("asks the API for one status and shows what it returns", async () => {
    serve([CODING, PAUSED, DONE]);
    renderPage();
    const user = userEvent.setup();
    await screen.findByText("Fix the parser");

    await user.selectOptions(screen.getByLabelText("Status"), "done");

    await waitFor(() => expect(screen.queryByText("Fix the parser")).not.toBeInTheDocument());
    expect(screen.getByText("Rename the flag")).toBeInTheDocument();
    expect(taskCalls().at(-1)?.searchParams.get("status")).toBe("done");
  });

  it("asks the API for one repo and shows what it returns", async () => {
    serve([CODING, APPROVAL]);
    renderPage();
    const user = userEvent.setup();
    await screen.findByText("Fix the parser");
    await screen.findByRole("option", { name: "acme/gadgets" });

    await user.selectOptions(screen.getByLabelText("Repo"), "acme/gadgets");

    await waitFor(() => expect(screen.queryByText("Fix the parser")).not.toBeInTheDocument());
    expect(screen.getByText("Add a retry")).toBeInTheDocument();
    expect(taskCalls().at(-1)?.searchParams.get("repo")).toBe("acme/gadgets");
  });

  it("starts from the filters in the address", async () => {
    serve([CODING, DONE]);
    renderPage("/tasks?status=done&repo=acme%2Fgadgets");

    expect(await screen.findByText("Rename the flag")).toBeInTheDocument();
    const url = taskCalls()[0];
    expect(url.searchParams.get("status")).toBe("done");
    expect(url.searchParams.get("repo")).toBe("acme/gadgets");
    expect(screen.getByLabelText("Status")).toHaveValue("done");
  });

  it("polls the list every 10 seconds", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    serve([CODING]);
    renderPage();
    await screen.findByText("Fix the parser");
    const before = taskCalls().length;

    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_500);
    });

    await waitFor(() => expect(taskCalls().length).toBeGreaterThan(before));
  });

  it("shows an empty state when there are no tasks", async () => {
    serve([]);
    renderPage();

    expect(await screen.findByText("No tasks yet.")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("says when the filters match no task", async () => {
    serve([CODING]);
    renderPage("/tasks?status=failed");

    expect(await screen.findByText("No task matches these filters.")).toBeInTheDocument();
  });

  it("shows an error and retries on request", async () => {
    fetchStub.mockImplementation((input) => {
      const url = new URL(String(input), "http://console.test");
      return Promise.resolve(
        url.pathname === "/api/repos" ? json(REPOS) : json({ detail: "database unavailable" }, 503),
      );
    });
    renderPage();
    const user = userEvent.setup();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Could not load the tasks");
    expect(alert).toHaveTextContent("database unavailable");

    serve([CODING]);
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Fix the parser")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("loads the next page on request", async () => {
    fetchStub.mockImplementation((input) => {
      const url = new URL(String(input), "http://console.test");
      if (url.pathname === "/api/repos") return Promise.resolve(json(REPOS));
      return Promise.resolve(
        url.searchParams.get("cursor") === "c2"
          ? json({ items: [DONE], next_cursor: null })
          : json({ items: [CODING], next_cursor: "c2" }),
      );
    });
    renderPage();
    const user = userEvent.setup();
    await screen.findByText("Fix the parser");

    await user.click(screen.getByRole("button", { name: "Load more" }));

    expect(await screen.findByText("Rename the flag")).toBeInTheDocument();
    expect(screen.getByText("Fix the parser")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();
  });

  it("scrolls the table sideways and not the page", async () => {
    serve([CODING]);
    renderPage();

    const table = await screen.findByRole("table");
    // At 375 px the table keeps its width and its wrapper scrolls, so the page itself does not.
    expect(table.parentElement).toHaveClass("overflow-x-auto");
    expect(table).toHaveClass("min-w-[56rem]");
  });
});
