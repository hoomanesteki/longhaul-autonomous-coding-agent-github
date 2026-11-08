import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../App";
import { clearToken, getToken, setToken } from "../api/token";

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

function authorizationOf(call: Parameters<typeof fetch>): string | null {
  return new Headers(call[1]?.headers).get("authorization");
}

/** An API that accepts one token: the probes are public and every other path checks it. */
function apiWithToken(accepted: string) {
  fetchStub.mockImplementation((input, init) => {
    if (String(input) === "/healthz") return Promise.resolve(json({ status: "ok" }));
    const given = new Headers(init?.headers).get("authorization");
    return Promise.resolve(
      given === `Bearer ${accepted}`
        ? json([])
        : json({ detail: "a valid bearer token is required" }, 401),
    );
  });
}

beforeEach(() => {
  fetchStub.mockReset();
  vi.stubGlobal("fetch", fetchStub);
  clearToken();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("login", () => {
  it("shows the login screen instead of a page when there is no token", async () => {
    fetchStub.mockResolvedValue(json({ status: "ok" }));
    renderAt("/memory");

    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "Main" })).not.toBeInTheDocument();
  });

  it("stores the token and opens the page that was asked for", async () => {
    apiWithToken("s3cret");
    renderAt("/repos");

    await userEvent.type(await screen.findByLabelText("Admin token"), "s3cret");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByRole("navigation", { name: "Main" })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Repos" })).toBeInTheDocument();
    expect(getToken()).toBe("s3cret");
  });

  it("checks the token with the API before it keeps it", async () => {
    apiWithToken("s3cret");
    renderAt("/login");

    await userEvent.type(await screen.findByLabelText("Admin token"), "s3cret");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await screen.findByRole("heading", { name: "Tasks" });

    const checked = fetchStub.mock.calls.find((call) => String(call[0]) === "/api/repos");
    expect(checked).toBeDefined();
    expect(authorizationOf(checked as Parameters<typeof fetch>)).toBe("Bearer s3cret");
  });

  it("says so when the token is wrong, and keeps nothing", async () => {
    apiWithToken("s3cret");
    renderAt("/login");

    await userEvent.type(await screen.findByLabelText("Admin token"), "guess");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("The API did not accept that token.");
    expect(getToken()).toBeNull();
    expect(screen.getByRole("heading", { name: "Sign in" })).toBeInTheDocument();
  });

  it("says what the API answered when it cannot check the token", async () => {
    fetchStub.mockResolvedValue(json({ detail: "database unavailable" }, 503));
    renderAt("/login");

    await userEvent.type(await screen.findByLabelText("Admin token"), "s3cret");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("database unavailable");
    expect(getToken()).toBeNull();
  });

  it("says so when the API cannot be reached at all", async () => {
    fetchStub.mockRejectedValue(new TypeError("Failed to fetch"));
    renderAt("/login");

    await userEvent.type(await screen.findByLabelText("Admin token"), "s3cret");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Could not reach the API");
  });

  it("does not send an empty token", async () => {
    renderAt("/login");

    await userEvent.click(await screen.findByRole("button", { name: "Sign in" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Enter the admin token.");
    expect(fetchStub).not.toHaveBeenCalledWith("/api/repos", expect.anything());
  });

  it("works when localStorage throws", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    apiWithToken("s3cret");
    renderAt("/login");

    await userEvent.type(await screen.findByLabelText("Admin token"), "s3cret");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByRole("navigation", { name: "Main" })).toBeInTheDocument();
    expect(getToken()).toBe("s3cret");
  });

  it("leaves the login screen for the tasks page when a token is stored", async () => {
    apiWithToken("s3cret");
    setToken("s3cret");
    renderAt("/login");

    expect(await screen.findByRole("heading", { name: "Tasks" })).toBeInTheDocument();
  });

  it("signs out", async () => {
    apiWithToken("s3cret");
    setToken("s3cret");
    renderAt("/tasks");

    await userEvent.click(await screen.findByRole("button", { name: "Sign out" }));

    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(getToken()).toBeNull();
  });

  it("returns to the login screen when the API stops accepting the token", async () => {
    // The token was changed on the server, or the page was left open for a long time.
    apiWithToken("the new one");
    setToken("the old one");
    renderAt("/repos");

    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    await waitFor(() => expect(getToken()).toBeNull());
  });
});
