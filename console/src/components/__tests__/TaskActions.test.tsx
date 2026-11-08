import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { components } from "../../api/schema";
import TaskActions from "../TaskActions";

type Workflow = components["schemas"]["TaskDetail"]["workflow"];

const fetchStub = vi.fn<typeof fetch>();

const TASK_ID = "lh-acme-widgets-12";
const BASE = `/api/tasks/${TASK_ID}`;

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function sent(signal: string): Response {
  return json({ task_id: TASK_ID, signal, by: "console" });
}

type Posted = { path: string; method: string | undefined; body: unknown };

/** Every call made so far, with the path, the method and the decoded JSON body. */
function calls(): Posted[] {
  return fetchStub.mock.calls.map(([input, init]) => ({
    path: new URL(String(input), "http://console.test").pathname,
    method: init?.method,
    body: typeof init?.body === "string" ? (JSON.parse(init.body) as unknown) : undefined,
  }));
}

function setup(phase: string, workflow: Workflow = "running") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidate = vi.spyOn(client, "invalidateQueries");
  const user = userEvent.setup();
  render(
    <QueryClientProvider client={client}>
      <TaskActions taskId={TASK_ID} phase={phase} workflow={workflow} />
    </QueryClientProvider>,
  );
  return { user, invalidate };
}

const NAMES = ["Approve plan", "Reject plan", "Send hint", "Add budget", "Cancel task"] as const;

function shown(): string[] {
  return NAMES.filter((name) => screen.queryByRole("button", { name }) !== null);
}

beforeEach(() => {
  fetchStub.mockReset();
  fetchStub.mockImplementation(() => Promise.resolve(sent("hint")));
  vi.stubGlobal("fetch", fetchStub);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("which actions are offered", () => {
  it("offers the plan decision only while the task waits for one", () => {
    setup("awaiting_plan_approval");

    expect(shown()).toEqual([...NAMES]);
  });

  it.each(["queued", "preparing", "planning", "coding", "verifying", "paused", "delivering"])(
    "offers a hint, a budget and a cancel in phase %s, and no plan decision",
    (phase) => {
      setup(phase);

      expect(shown()).toEqual(["Send hint", "Add budget", "Cancel task"]);
    },
  );

  it.each(["done", "failed", "cancelled"])("offers nothing to a task that is %s", (phase) => {
    setup(phase);

    expect(shown()).toEqual([]);
    expect(screen.getByText("This task has ended. There is nothing to do.")).toBeInTheDocument();
  });

  it.each<Workflow>(["closed", "not_found"])(
    "offers nothing when the workflow is %s, whatever the row says",
    (workflow) => {
      setup("coding", workflow);

      expect(shown()).toEqual([]);
      expect(screen.getByText(/no running workflow/i)).toBeInTheDocument();
    },
  );

  it("still offers the actions when Temporal cannot be reached, and lets the API say no", () => {
    setup("coding", "unreachable");

    expect(shown()).toEqual(["Send hint", "Add budget", "Cancel task"]);
  });
});

describe("approving and rejecting a plan", () => {
  it("approves with one click, says so and refetches the task", async () => {
    fetchStub.mockImplementation(() => Promise.resolve(sent("approve_plan")));
    const { user, invalidate } = setup("awaiting_plan_approval");

    await user.click(screen.getByRole("button", { name: "Approve plan" }));

    expect(await screen.findByRole("status")).toHaveTextContent("Plan approved.");
    expect(calls()).toEqual([{ path: `${BASE}/approve-plan`, method: "POST", body: undefined }]);
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["task", TASK_ID] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["timeline", TASK_ID] });
  });

  it("rejects with the feedback, and only when there is some", async () => {
    fetchStub.mockImplementation(() => Promise.resolve(sent("reject_plan")));
    const { user } = setup("awaiting_plan_approval");
    const reject = screen.getByRole("button", { name: "Reject plan" });
    expect(reject).toBeDisabled();

    await user.type(screen.getByLabelText("Feedback on the plan"), "   ");
    expect(reject).toBeDisabled();
    await user.type(screen.getByLabelText("Feedback on the plan"), "Split step 2 in two.");
    await user.click(reject);

    expect(await screen.findByRole("status")).toHaveTextContent("Plan rejected.");
    expect(calls()).toEqual([
      { path: `${BASE}/reject-plan`, method: "POST", body: { feedback: "Split step 2 in two." } },
    ]);
    expect(screen.getByLabelText("Feedback on the plan")).toHaveValue("");
  });
});

describe("sending a hint", () => {
  it("posts the text, clears the box and refetches", async () => {
    const { user, invalidate } = setup("paused");
    const send = screen.getByRole("button", { name: "Send hint" });
    expect(send).toBeDisabled();

    await user.type(screen.getByLabelText("Hint"), "Edit the copy in vendor/.");
    await user.click(send);

    expect(await screen.findByRole("status")).toHaveTextContent("Hint sent.");
    expect(calls()).toEqual([
      { path: `${BASE}/hint`, method: "POST", body: { text: "Edit the copy in vendor/." } },
    ]);
    expect(screen.getByLabelText("Hint")).toHaveValue("");
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["task", TASK_ID] });
  });

  it("keeps the text and says why when the API refuses", async () => {
    fetchStub.mockImplementation(() =>
      Promise.resolve(json({ detail: "the workflow has ended" }, 409)),
    );
    const { user, invalidate } = setup("coding");

    await user.type(screen.getByLabelText("Hint"), "Try again.");
    await user.click(screen.getByRole("button", { name: "Send hint" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not send the hint: API answered 409: the workflow has ended",
    );
    expect(screen.getByLabelText("Hint")).toHaveValue("Try again.");
    // A refused action changed nothing, so there is nothing new to read.
    expect(invalidate).not.toHaveBeenCalled();
  });
});

describe("adding budget", () => {
  it("posts dollars and turns as numbers", async () => {
    fetchStub.mockImplementation(() => Promise.resolve(sent("add_budget")));
    const { user } = setup("paused");

    await user.type(screen.getByLabelText("Dollars"), "2.5");
    await user.type(screen.getByLabelText("Turns"), "10");
    await user.click(screen.getByRole("button", { name: "Add budget" }));

    expect(await screen.findByRole("status")).toHaveTextContent("Budget added.");
    expect(calls()).toEqual([
      { path: `${BASE}/budget`, method: "POST", body: { usd: 2.5, turns: 10 } },
    ]);
  });

  it("takes an empty field as zero, and does not send two zeros", async () => {
    fetchStub.mockImplementation(() => Promise.resolve(sent("add_budget")));
    const { user } = setup("paused");
    const add = screen.getByRole("button", { name: "Add budget" });
    expect(add).toBeDisabled();

    await user.type(screen.getByLabelText("Turns"), "0");
    expect(add).toBeDisabled();
    await user.clear(screen.getByLabelText("Turns"));
    await user.type(screen.getByLabelText("Dollars"), "1");
    await user.click(add);

    await waitFor(() => expect(calls()).toHaveLength(1));
    expect(calls()[0]?.body).toEqual({ usd: 1, turns: 0 });
  });

  it("does not send an amount the API would refuse", async () => {
    const { user } = setup("paused");

    await user.type(screen.getByLabelText("Dollars"), "-1");
    expect(screen.getByRole("button", { name: "Add budget" })).toBeDisabled();
    await user.clear(screen.getByLabelText("Dollars"));
    await user.type(screen.getByLabelText("Turns"), "2.5");

    expect(screen.getByRole("button", { name: "Add budget" })).toBeDisabled();
  });
});

describe("cancelling", () => {
  it("waits for the task id to be typed in full", async () => {
    fetchStub.mockImplementation(() => Promise.resolve(sent("cancel")));
    const { user, invalidate } = setup("coding");
    const cancel = screen.getByRole("button", { name: "Cancel task" });
    const box = screen.getByLabelText(`Type ${TASK_ID} to confirm`);
    expect(cancel).toBeDisabled();

    await user.type(box, TASK_ID.slice(0, -1));
    expect(cancel).toBeDisabled();
    await user.type(box, TASK_ID.slice(-1));
    expect(cancel).toBeEnabled();
    await user.click(cancel);

    expect(await screen.findByRole("status")).toHaveTextContent("Cancel sent.");
    expect(calls()).toEqual([{ path: `${BASE}/cancel`, method: "POST", body: null }]);
    expect(box).toHaveValue("");
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["task", TASK_ID] });
  });
});
