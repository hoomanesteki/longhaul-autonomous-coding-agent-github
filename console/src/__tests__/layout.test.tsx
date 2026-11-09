import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../App";
import { clearToken, setToken } from "../api/token";

const fetchStub = vi.fn<typeof fetch>();

function renderAt(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** Answer /healthz with a new copy of the given reply (a body reads once) and the task list with an empty page. */
function serve(health: () => Response) {
  fetchStub.mockImplementation((input) =>
    Promise.resolve(
      String(input) === "/healthz"
        ? health()
        : String(input).startsWith("/api/tasks")
          ? json({ items: [], next_cursor: null })
          : json([]),
    ),
  );
}

beforeEach(() => {
  fetchStub.mockReset();
  vi.stubGlobal("fetch", fetchStub);
  // The pages sit behind the login, so these tests start signed in.
  setToken("test-token");
});

afterEach(() => {
  vi.unstubAllGlobals();
  clearToken();
});

describe("layout", () => {
  it("has a nav with Tasks, Memory and Repos", async () => {
    serve(() => json({ status: "ok" }));
    renderAt("/tasks");

    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(nav).toHaveTextContent("Tasks");
    expect(screen.getByRole("link", { name: "Memory" })).toHaveAttribute("href", "/memory");
    expect(screen.getByRole("link", { name: "Repos" })).toHaveAttribute("href", "/repos");
    expect(await screen.findByRole("heading", { name: "Tasks" })).toBeInTheDocument();
  });

  it("shows the API as healthy when /healthz answers", async () => {
    serve(() => json({ status: "ok" }));
    renderAt("/tasks");

    expect(await screen.findByText("API up")).toBeInTheDocument();
  });

  it("shows the API as down when /healthz fails", async () => {
    serve(() => json({ detail: "database unavailable" }, 503));
    renderAt("/tasks");

    expect(await screen.findByText("API down")).toBeInTheDocument();
  });

  it("lists the repos on the repos page", async () => {
    fetchStub.mockImplementation((input) =>
      Promise.resolve(
        String(input) === "/healthz"
          ? json({ status: "ok" })
          : json([
              {
                repo: "acme/widgets",
                allowlisted: true,
                created_at: "2026-11-01T10:00:00Z",
                settings: {},
                tasks: 3,
              },
            ]),
      ),
    );
    renderAt("/repos");

    expect(await screen.findByText("acme/widgets")).toBeInTheDocument();
  });

  it("sends an unknown address to the tasks page", async () => {
    serve(() => json({ status: "ok" }));
    renderAt("/nowhere");

    expect(await screen.findByRole("heading", { name: "Tasks" })).toBeInTheDocument();
  });
});
