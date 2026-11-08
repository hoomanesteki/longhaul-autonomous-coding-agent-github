import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import MemoryPage from "../MemoryPage";

type Item = {
  id: string;
  repo: string;
  kind: "repo_map" | "command" | "convention" | "pitfall" | "lesson";
  status: "candidate" | "approved" | "rejected";
  source: "default_branch" | "merged_pr" | "human" | "task";
  source_ref: string | null;
  approved_by: string | null;
  uses: number;
  last_used_at: string | null;
  created_at: string;
  content: string;
};

function item(fields: Partial<Item> & Pick<Item, "id">): Item {
  return {
    repo: "acme/widgets",
    kind: "lesson",
    status: "candidate",
    source: "task",
    source_ref: "lh-0001",
    approved_by: null,
    uses: 0,
    last_used_at: null,
    created_at: "2026-11-01T10:00:00Z",
    content: `content of ${fields.id}`,
    ...fields,
  };
}

const REPOS = ["acme/widgets", "acme/gadgets"].map((repo) => ({
  repo,
  allowlisted: true,
  settings: {},
  tasks: 1,
  created_at: "2026-11-01T10:00:00Z",
}));

// The stub plays the API: it keeps the items, lists them by repo and applies approve and reject,
// so a page that refetches after a decision sees the item in its new status.
let items: Item[];
const fetchStub = vi.fn<typeof fetch>();

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function api(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const url = new URL(String(input), "http://console.test");
  const decision = /^\/api\/memory\/([^/]+)\/(approve|reject)$/.exec(url.pathname);
  if (url.pathname === "/api/repos") return Promise.resolve(json(REPOS));
  if (url.pathname === "/api/memory" && init?.method !== "POST") {
    const repo = url.searchParams.get("repo");
    return Promise.resolve(json(items.filter((entry) => entry.repo === repo)));
  }
  if (decision !== null && init?.method === "POST") {
    const entry = items.find((candidate) => candidate.id === decodeURIComponent(decision[1]!));
    if (entry === undefined) return Promise.resolve(json({ detail: "memory item not found" }, 404));
    entry.status = decision[2] === "approve" ? "approved" : "rejected";
    entry.approved_by = "console";
    return Promise.resolve(json(entry));
  }
  return Promise.resolve(json({ detail: "not found" }, 404));
}

function renderPage(path = "/memory") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <MemoryPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function requests(): { method: string; url: string }[] {
  return fetchStub.mock.calls.map(([input, init]) => ({
    method: init?.method ?? "GET",
    url: String(input),
  }));
}

beforeEach(() => {
  items = [
    item({ id: "m-1", status: "approved", kind: "command", content: "run make check" }),
    item({ id: "m-2", status: "candidate", content: "avoid the global cache" }),
    item({ id: "m-3", status: "candidate", kind: "pitfall", content: "tests need docker" }),
    item({ id: "m-4", status: "rejected", content: "always use tabs" }),
    item({ id: "g-1", repo: "acme/gadgets", status: "approved", content: "gadgets use bun" }),
  ];
  fetchStub.mockReset();
  fetchStub.mockImplementation(api);
  vi.stubGlobal("fetch", fetchStub);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("memory page", () => {
  it("picks the first repo and lists its approved items with kind, uses and last used", async () => {
    items[0]!.uses = 7;
    items[0]!.last_used_at = "2026-11-05T08:30:00Z";
    renderPage();

    expect(await screen.findByText("run make check")).toBeInTheDocument();
    const row = screen.getByRole("listitem", { name: /run make check/ });
    expect(within(row).getByText("command")).toBeInTheDocument();
    expect(within(row).getByText("7")).toBeInTheDocument();
    expect(within(row).getByText("2026-11-05 08:30 UTC")).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Repo" })).toHaveValue("acme/widgets");
    expect(screen.queryByText("avoid the global cache")).not.toBeInTheDocument();
    expect(screen.queryByText("gadgets use bun")).not.toBeInTheDocument();
  });

  it("says never for an item that was not used", async () => {
    renderPage();

    const row = await screen.findByRole("listitem", { name: /run make check/ });
    expect(within(row).getByText("never")).toBeInTheDocument();
  });

  it("asks the API for the items of the chosen repo only", async () => {
    renderPage();
    await screen.findByText("run make check");

    const memory = requests().find((call) => call.url.startsWith("/api/memory"));
    expect(memory?.method).toBe("GET");
    expect(new URL(memory!.url, "http://console.test").searchParams.get("repo")).toBe(
      "acme/widgets",
    );
  });

  it("loads the items of another repo when it is chosen", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("run make check");

    await user.selectOptions(screen.getByRole("combobox", { name: "Repo" }), "acme/gadgets");

    expect(await screen.findByText("gadgets use bun")).toBeInTheDocument();
    expect(screen.queryByText("run make check")).not.toBeInTheDocument();
  });

  it("opens on the repo and the tab named in the address", async () => {
    renderPage("/memory?repo=acme%2Fgadgets&tab=rejected");

    expect(await screen.findByRole("combobox", { name: "Repo" })).toHaveValue("acme/gadgets");
    expect(screen.getByRole("tab", { name: /^Rejected/ })).toHaveAttribute("aria-selected", "true");
  });

  it("counts the items of each tab", async () => {
    renderPage();
    await screen.findByText("run make check");

    expect(screen.getByRole("tab", { name: "Approved (1)" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Candidates (2)" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Rejected (1)" })).toBeInTheDocument();
  });

  it("shows candidates and rejected items under their own tabs", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("run make check");

    await user.click(screen.getByRole("tab", { name: /^Candidates/ }));
    expect(screen.getByText("avoid the global cache")).toBeInTheDocument();
    expect(screen.getByText("tests need docker")).toBeInTheDocument();
    expect(screen.queryByText("run make check")).not.toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: /^Rejected/ }));
    expect(screen.getByText("always use tabs")).toBeInTheDocument();
    expect(screen.queryByText("avoid the global cache")).not.toBeInTheDocument();
  });

  it("offers Approve and Reject on candidates only", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("run make check");
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: /^Candidates/ }));
    expect(screen.getAllByRole("button", { name: "Approve" })).toHaveLength(2);
    expect(screen.getAllByRole("button", { name: "Reject" })).toHaveLength(2);

    await user.click(screen.getByRole("tab", { name: /^Rejected/ }));
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reject" })).not.toBeInTheDocument();
  });

  it("approves a candidate through the API and moves it to Approved", async () => {
    const user = userEvent.setup();
    renderPage("/memory?tab=candidates");
    const row = await screen.findByRole("listitem", { name: /avoid the global cache/ });

    await user.click(within(row).getByRole("button", { name: "Approve" }));

    expect(await screen.findByRole("tab", { name: "Candidates (1)" })).toBeInTheDocument();
    expect(requests()).toContainEqual({ method: "POST", url: "/api/memory/m-2/approve" });
    expect(screen.queryByText("avoid the global cache")).not.toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: /^Approved/ }));
    expect(screen.getByText("avoid the global cache")).toBeInTheDocument();
  });

  it("rejects a candidate through the API and moves it to Rejected", async () => {
    const user = userEvent.setup();
    renderPage("/memory?tab=candidates");
    const row = await screen.findByRole("listitem", { name: /tests need docker/ });

    await user.click(within(row).getByRole("button", { name: "Reject" }));

    expect(await screen.findByRole("tab", { name: "Rejected (2)" })).toBeInTheDocument();
    expect(requests()).toContainEqual({ method: "POST", url: "/api/memory/m-3/reject" });
    await user.click(screen.getByRole("tab", { name: /^Rejected/ }));
    expect(screen.getByText("tests need docker")).toBeInTheDocument();
  });

  it("keeps the item and shows the error when the API refuses", async () => {
    const user = userEvent.setup();
    fetchStub.mockImplementation((input, init) =>
      String(input).endsWith("/approve")
        ? Promise.resolve(json({ detail: "memory item not found" }, 404))
        : api(input, init),
    );
    renderPage("/memory?tab=candidates");
    const row = await screen.findByRole("listitem", { name: /avoid the global cache/ });

    await user.click(within(row).getByRole("button", { name: "Approve" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("memory item not found");
    expect(screen.getByText("avoid the global cache")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Candidates (2)" })).toBeInTheDocument();
  });

  describe("source links", () => {
    it("sends a pull request number to the pull request on GitHub", async () => {
      items = [item({ id: "m-1", status: "approved", source: "task", source_ref: "42" })];
      renderPage();

      const link = await screen.findByRole("link", { name: "PR #42" });
      expect(link).toHaveAttribute("href", "https://github.com/acme/widgets/pull/42");
    });

    it("sends a merged pull request to the pull request on GitHub", async () => {
      items = [item({ id: "m-1", status: "approved", source: "merged_pr", source_ref: "7" })];
      renderPage();

      const link = await screen.findByRole("link", { name: "PR #7" });
      expect(link).toHaveAttribute("href", "https://github.com/acme/widgets/pull/7");
    });

    it("sends a task id to the task page of the console", async () => {
      items = [item({ id: "m-1", status: "approved", source: "task", source_ref: "lh-0042" })];
      renderPage();

      const link = await screen.findByRole("link", { name: "Task lh-0042" });
      expect(link).toHaveAttribute("href", "/tasks/lh-0042");
    });

    it("sends a default branch sha to the commit on GitHub", async () => {
      items = [
        item({
          id: "m-1",
          status: "approved",
          source: "default_branch",
          source_ref: "0123456789abcdef0123456789abcdef01234567",
        }),
      ];
      renderPage();

      const link = await screen.findByRole("link", { name: "Commit 0123456" });
      expect(link).toHaveAttribute(
        "href",
        "https://github.com/acme/widgets/commit/0123456789abcdef0123456789abcdef01234567",
      );
    });

    it("shows no link for an item a person wrote", async () => {
      items = [item({ id: "m-1", status: "approved", source: "human", source_ref: null })];
      renderPage();

      const row = await screen.findByRole("listitem", { name: /content of m-1/ });
      expect(within(row).queryByRole("link")).not.toBeInTheDocument();
      expect(within(row).getByText("human")).toBeInTheDocument();
    });

    it("does not build a link from a reference that is not a number, a sha or a task id", async () => {
      items = [
        item({ id: "m-1", status: "approved", source: "task", source_ref: "../../etc?x=1" }),
      ];
      renderPage();

      const row = await screen.findByRole("listitem", { name: /content of m-1/ });
      expect(within(row).queryByRole("link")).not.toBeInTheDocument();
      expect(within(row).getByText(/\.\.\/\.\.\/etc\?x=1/)).toBeInTheDocument();
    });
  });

  describe("repo map", () => {
    const map = "src/\n  app.py\n  util.py";

    it("renders as monospace text in a collapsed panel", async () => {
      items = [item({ id: "map-1", kind: "repo_map", status: "approved", content: map })];
      renderPage();

      const summary = await screen.findByText("Repo map");
      const panel = summary.closest("details");
      expect(panel).not.toBeNull();
      expect(panel).not.toHaveAttribute("open");
      const text = within(panel!).getByText((_, element) => element?.tagName === "PRE");
      expect(text).toHaveTextContent("src/ app.py util.py");
      expect(text.className).toContain("font-mono");
    });

    it("opens when the operator clicks the summary", async () => {
      const user = userEvent.setup();
      items = [item({ id: "map-1", kind: "repo_map", status: "approved", content: map })];
      renderPage();

      const summary = await screen.findByText("Repo map");
      await user.click(summary);

      expect(summary.closest("details")).toHaveAttribute("open");
    });

    it("does not put other kinds in a panel", async () => {
      renderPage();
      await screen.findByText("run make check");

      expect(screen.queryByText("Repo map")).not.toBeInTheDocument();
    });
  });

  describe("states", () => {
    it("says so when a tab has no items", async () => {
      items = [item({ id: "m-1", status: "approved" })];
      const user = userEvent.setup();
      renderPage();
      await screen.findByText("content of m-1");

      await user.click(screen.getByRole("tab", { name: /^Candidates/ }));

      expect(screen.getByText("No candidates for this repo.")).toBeInTheDocument();
    });

    it("says so when there are no repos", async () => {
      fetchStub.mockResolvedValue(json([]));
      renderPage();

      expect(await screen.findByText("No repos yet.")).toBeInTheDocument();
    });

    it("shows an error when the items cannot be loaded", async () => {
      fetchStub.mockImplementation((input, init) =>
        String(input).startsWith("/api/memory")
          ? Promise.resolve(json({ detail: "database unavailable" }, 503))
          : api(input, init),
      );
      renderPage();

      expect(await screen.findByRole("alert")).toHaveTextContent("database unavailable");
    });

    it("shows an error when the repos cannot be loaded", async () => {
      fetchStub.mockResolvedValue(json({ detail: "database unavailable" }, 503));
      renderPage();

      expect(await screen.findByRole("alert")).toHaveTextContent("database unavailable");
    });
  });
});
