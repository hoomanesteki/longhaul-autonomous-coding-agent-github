import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";

import { approveMemory, listMemory, listRepos, rejectMemory } from "../api/client";
import type { components } from "../api/schema";
import MemoryItemRow from "../components/MemoryItemRow";

type Item = components["schemas"]["MemoryItemView"];
type Decision = "approve" | "reject";

/** The API's own ceiling for one page of items. A tab with more shows a note instead of a gap. */
const LIMIT = 1_000;

const TABS = [
  { key: "approved", label: "Approved", status: "approved", empty: "No approved items" },
  { key: "candidates", label: "Candidates", status: "candidate", empty: "No candidates" },
  { key: "rejected", label: "Rejected", status: "rejected", empty: "No rejected items" },
] as const;

type TabKey = (typeof TABS)[number]["key"];

function tabOf(value: string | null): (typeof TABS)[number] {
  return TABS.find((tab) => tab.key === value) ?? TABS[0];
}

const memoryKey = (repo: string | undefined, status?: Item["status"]) =>
  status === undefined ? (["memory", repo] as const) : (["memory", repo, status] as const);

/**
 * What the agent knows about a repo, and the candidates waiting for a decision.
 *
 * Each tab asks the API for its own status. The API lists oldest first and cuts at the limit, so
 * one list of all statuses would push the newest items, the candidates, out of a busy repo.
 * The repo and the tab live in the address, so a reload or a shared link opens the same view.
 */
export default function MemoryPage() {
  const [params, setParams] = useSearchParams();
  const queryClient = useQueryClient();
  const repos = useQuery({ queryKey: ["repos"], queryFn: listRepos });

  const repo = params.get("repo") ?? repos.data?.[0]?.repo;
  const tab = tabOf(params.get("tab"));

  const lists = useQueries({
    queries: TABS.map((entry) => ({
      queryKey: memoryKey(repo, entry.status),
      queryFn: () => listMemory({ repo, status: entry.status, limit: LIMIT }),
      enabled: repo !== undefined,
    })),
  });
  const items = lists[TABS.findIndex((entry) => entry.key === tab.key)]!;

  const decide = useMutation({
    mutationFn: ({ id, decision }: { id: string; decision: Decision }) =>
      decision === "approve" ? approveMemory(id) : rejectMemory(id),
    onSuccess: (updated) => {
      // Show the item in its new tab at once; the refetch below confirms it. Every tab of the
      // repo is refetched, because the one the item left and the one it joined both changed.
      for (const entry of TABS) {
        queryClient.setQueryData<Item[]>(memoryKey(updated.repo, entry.status), (old) => {
          if (old === undefined) return old;
          const rest = old.filter((item) => item.id !== updated.id);
          return entry.status === updated.status ? [...rest, updated] : rest;
        });
      }
      void queryClient.invalidateQueries({ queryKey: memoryKey(updated.repo) });
    },
  });

  function show(next: { repo?: string; tab?: TabKey }) {
    const search = new URLSearchParams(params);
    if (next.repo !== undefined) {
      search.set("repo", next.repo);
      // A failed decision belongs to the repo it was made on.
      decide.reset();
    }
    if (next.tab !== undefined) search.set("tab", next.tab);
    setParams(search, { replace: true });
  }

  const repoNames = (repos.data ?? []).map((entry) => entry.repo);
  // A repo named in the address stays selectable even if the repo list has not caught up.
  if (repo !== undefined && !repoNames.includes(repo)) repoNames.push(repo);

  const shown = items.data ?? [];

  return (
    <section>
      <h1 className="text-lg font-semibold">Memory</h1>

      {repos.error !== null && (
        <p role="alert" className="mt-2 text-sm text-red-700">
          Could not load the repos: {repos.error.message}
        </p>
      )}
      {repos.data?.length === 0 && <p className="mt-2 text-sm text-slate-600">No repos yet.</p>}

      {repo !== undefined && (
        <>
          <div className="mt-3 flex items-center gap-2 text-sm">
            <label htmlFor="memory-repo" className="text-slate-600">
              Repo
            </label>
            <select
              id="memory-repo"
              value={repo}
              onChange={(event) => show({ repo: event.target.value })}
              className="rounded border border-slate-300 px-2 py-1"
            >
              {repoNames.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
          </div>

          <div role="tablist" aria-label="Memory status" className="mt-4 flex gap-1 border-b">
            {TABS.map((entry) => {
              const count = lists[TABS.indexOf(entry)]!.data?.length;
              const selected = entry.key === tab.key;
              return (
                <button
                  key={entry.key}
                  id={`memory-tab-${entry.key}`}
                  type="button"
                  role="tab"
                  aria-selected={selected}
                  aria-controls="memory-panel"
                  onClick={() => show({ tab: entry.key })}
                  className={`-mb-px border-b-2 px-3 py-2 text-sm ${
                    selected
                      ? "border-slate-900 font-medium text-slate-900"
                      : "border-transparent text-slate-600 hover:text-slate-900"
                  }`}
                >
                  {count === undefined ? entry.label : `${entry.label} (${count})`}
                </button>
              );
            })}
          </div>

          <div
            id="memory-panel"
            role="tabpanel"
            aria-labelledby={`memory-tab-${tab.key}`}
            className="mt-3"
          >
            {items.isPending && <p className="text-sm text-slate-600">Loading.</p>}
            {items.error !== null && (
              <p role="alert" className="text-sm text-red-700">
                Could not load the memory: {items.error.message}
              </p>
            )}
            {decide.error !== null && (
              <p role="alert" className="mb-3 text-sm text-red-700">
                Could not save the decision: {decide.error.message}
              </p>
            )}
            {items.data !== undefined && shown.length === 0 && (
              <p className="text-sm text-slate-600">{tab.empty} for this repo.</p>
            )}
            {shown.length > 0 && (
              <ul className="flex flex-col gap-3">
                {shown.map((item) => (
                  <MemoryItemRow
                    key={item.id}
                    item={item}
                    busy={decide.isPending && decide.variables.id === item.id}
                    onDecide={
                      item.status === "candidate"
                        ? (decision) => decide.mutate({ id: item.id, decision })
                        : undefined
                    }
                  />
                ))}
              </ul>
            )}
            {shown.length >= LIMIT && (
              <p className="mt-3 text-sm text-slate-600">
                Showing the first {LIMIT} items of this tab.
              </p>
            )}
          </div>
        </>
      )}
    </section>
  );
}
