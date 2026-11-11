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

async function findRow(title: string): Promise<HTMLElement> {
  return (await screen.findByText(title)).closest("tr") as HTMLElement;
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

    const row = await findRow("Rename the flag");
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

    expect(within(await findRow("An old task")).getByText("3h")).toBeInTheDocument();
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

  it("repeats the phase and what needs you under the repo, for a screen too narrow for the columns", async () => {
    serve([CODING, PAUSED, APPROVAL]);
    renderPage();

    await screen.findByText("Fix the parser");
    // The table scrolls sideways on a phone, and Phase and Needs you are the last columns. The
    // line under the repo, in the first column, is what a person sees without scrolling. CSS hides it on wide screens.
    const line = (title: string) => within(rowOf(title).querySelectorAll("td")[0] as HTMLElement);
    expect(line("Fix the parser").getByTestId("status-line")).toHaveTextContent("Phase: coding");
    expect(line("Fix the parser").queryByText(/Needs you/)).not.toBeInTheDocument();
    expect(line("Speed up the build").getByTestId("status-line")).toHaveTextContent("Phase: paused");
    expect(line("Speed up the build").getByText("Needs you: Paused")).toBeInTheDocument();
    expect(line("Add a retry").getByText("Needs you: Approve the plan")).toBeInTheDocument();
    expect(line("Add a retry").getByTestId("status-line").className).toContain("lg:hidden");
  });

  it("writes the phase in words and wraps the status line between words", async () => {
    serve([APPROVAL]);
    renderPage();

    await screen.findByText("Add a retry");
    const line = within(rowOf("Add a retry").querySelectorAll("td")[0] as HTMLElement).getByTestId("status-line");
    // awaiting_plan_approval has no space to wrap at and broke as "awaiting_plan_appr / oval".
    expect(line).toHaveTextContent("Phase: awaiting plan approval");
    expect(line.className).toContain("break-words");
    expect(line.className).not.toContain("break-all");
  });

  it("leaves out the issue link for a task without an issue number", async () => {
    serve([task({ id: "t-1", title: "Manual task", issue_number: null })]);
    renderPage();

    const row = await findRow("Manual task");
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
    const url = taskCalls()[0] as URL;
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
      await vi.advanceTimersByTimeAsync(9_000);
    });
    expect(taskCalls().length).toBe(before);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_500);
    });
    await waitFor(() => expect(taskCalls().length).toBe(before + 1));
  });

  it("keeps the ages current while the list does not change", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const fiveMinutes = 5 * 60 * 1000;
    serve([task({ id: "lh-acme-widgets-20", title: "Old news", created_at: new Date(Date.now() - fiveMinutes).toISOString() })]);
    renderPage();
    const row = await findRow("Old news");
    expect(within(row).getByText("5m")).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2 * 60 * 60 * 1000);
    });

    expect(within(rowOf("Old news")).getByText("2h")).toBeInTheDocument();
  });

  it("lets the filters shrink so a long repo name cannot widen the page", async () => {
    const longRepo = "hoomanesteki/longhaul-autonomous-coding-agent-github";
    serve([task({ id: "lh-long-1", repo: longRepo, title: "Long one" })]);
    renderPage();
    await screen.findByText("Long one");

    const label = screen.getByLabelText("Repo").closest("label") as HTMLElement;
    expect(label).toHaveClass("min-w-0", "max-w-full");
    expect(screen.getByLabelText("Repo")).toHaveClass("w-full", "max-w-full");
  });

  it("warns that the needs-you toggle sees only the loaded tasks while more can be loaded", async () => {
    fetchStub.mockImplementation((input) => {
      const url = new URL(String(input), "http://console.test");
      if (url.pathname === "/api/repos") return Promise.resolve(json(REPOS));
      return Promise.resolve(json({ items: [CODING, PAUSED], next_cursor: "c2" }));
    });
    renderPage("/tasks?needs_you=1");

    await screen.findByText("Speed up the build");
    expect(
      screen.getByText("Only the tasks loaded so far are checked. Load more to check the rest."),
    ).toBeInTheDocument();
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
