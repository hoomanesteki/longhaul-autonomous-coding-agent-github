import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";

import { listRepos, listTasks } from "../api/client";
import PhaseBadge from "../components/PhaseBadge";
import { formatAge, formatCost, issueUrl, needsYou } from "../util/tasks";

const POLL_MS = 10_000;

// The statuses a task row can have. A status the API returns that is not in the list is added to
// the dropdown, so a new one can still be picked.
const STATUSES = ["active", "done", "failed", "cancelled"];

const CELL = "py-2 pr-4 align-top";
const HEAD = "py-2 pr-4 font-medium";

/** Every task in one table, with the filters that decide which ones, and the ones that need a person. */
export default function TasksPage() {
  const [params, setParams] = useSearchParams();
  // The filters live in the address, so a filtered list can be bookmarked and survives a reload.
  const status = params.get("status") ?? "";
  const repo = params.get("repo") ?? "";
  const onlyNeedsYou = params.get("needs_you") === "1";

  function setFilter(name: string, value: string) {
    setParams(
      (current) => {
        const next = new URLSearchParams(current);
        if (value === "") next.delete(name);
        else next.set(name, value);
        return next;
      },
      { replace: true },
    );
  }

  const tasks = useInfiniteQuery({
    queryKey: ["tasks", { status, repo }],
    queryFn: ({ pageParam }) =>
      listTasks({
        status: status === "" ? undefined : status,
        repo: repo === "" ? undefined : repo,
        cursor: pageParam,
      }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    refetchInterval: POLL_MS,
  });
  // The repo list only fills a dropdown, so a failure there leaves the dropdown empty and nothing more.
  const repos = useQuery({ queryKey: ["repos"], queryFn: listRepos });

  const loaded = tasks.data?.pages.flatMap((page) => page.items) ?? [];
  const rows = onlyNeedsYou ? loaded.filter((task) => needsYou(task) !== null) : loaded;
  const statuses = [...new Set([...STATUSES, ...loaded.map((task) => task.status), status])]
    .filter((value) => value !== "")
    .sort();
  const repoNames = [...new Set([...(repos.data?.map((r) => r.repo) ?? []), repo])]
    .filter((value) => value !== "")
    .sort();
  const filtered = status !== "" || repo !== "";

  return (
    <section>
      <h1 className="text-lg font-semibold">Tasks</h1>

      <div className="mt-3 flex flex-wrap items-end gap-x-4 gap-y-2 text-sm">
        <label className="flex flex-col gap-1">
          <span className="text-slate-600">Status</span>
          <select
            className="rounded border border-slate-300 px-2 py-1"
            value={status}
            onChange={(event) => setFilter("status", event.target.value)}
          >
            <option value="">All</option>
            {statuses.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-slate-600">Repo</span>
          <select
            className="max-w-full rounded border border-slate-300 px-2 py-1"
            value={repo}
            onChange={(event) => setFilter("repo", event.target.value)}
          >
            <option value="">All</option>
            {repoNames.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-2 py-1">
          <input
            type="checkbox"
            checked={onlyNeedsYou}
            onChange={(event) => setFilter("needs_you", event.target.checked ? "1" : "")}
          />
          Needs you
        </label>
      </div>

      {tasks.isError && (
        <div role="alert" className="mt-3 text-sm text-red-700">
          Could not load the tasks: {tasks.error.message}{" "}
          <button
            type="button"
            className="underline"
            onClick={() => void tasks.refetch()}
          >
            Try again
          </button>
        </div>
      )}
      {tasks.isPending && <p className="mt-3 text-sm text-slate-600">Loading.</p>}
      {tasks.isSuccess && loaded.length === 0 && (
        <p className="mt-3 text-sm text-slate-600">
          {filtered ? "No task matches these filters." : "No tasks yet."}
        </p>
      )}
      {tasks.isSuccess && loaded.length > 0 && rows.length === 0 && (
        <p className="mt-3 text-sm text-slate-600">
          {tasks.hasNextPage
            ? "Nothing that needs you in the tasks loaded so far."
            : "Nothing needs you right now."}
        </p>
      )}

      {rows.length > 0 && (
        // The table keeps a width that fits its columns and its wrapper scrolls, so a narrow screen
        // scrolls the table and not the page.
        <div className="mt-3 overflow-x-auto">
          <table className="w-full min-w-[56rem] text-left text-sm">
            <thead className="border-b border-slate-300 text-slate-600">
              <tr>
                <th className={HEAD}>Repo</th>
                <th className={HEAD}>Issue</th>
                <th className={HEAD}>Title</th>
                <th className={HEAD}>Phase</th>
                <th className={HEAD}>Outcome</th>
                <th className={`${HEAD} text-right`}>Turns</th>
                <th className={`${HEAD} text-right`}>Cost</th>
                <th className={HEAD}>Age</th>
                <th className="py-2 font-medium">Needs you</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((task) => {
                const waiting = needsYou(task);
                const issue = issueUrl(task);
                return (
                  <tr
                    key={task.id}
                    className={`border-b border-slate-100 ${waiting === null ? "" : "bg-amber-50"}`}
                  >
                    <td className={CELL}>{task.repo}</td>
                    <td className={CELL}>
                      {issue !== null && (
                        <a
                          href={issue}
                          target="_blank"
                          rel="noreferrer"
                          className="text-blue-700 underline"
                        >
                          #{task.issue_number}
                        </a>
                      )}
                    </td>
                    <td className={CELL}>
                      <Link to={`/tasks/${encodeURIComponent(task.id)}`} className="hover:underline">
                        {task.title}
                      </Link>
                    </td>
                    <td className={CELL}>
                      <PhaseBadge phase={task.phase} />
                    </td>
                    <td className={CELL}>{task.outcome}</td>
                    <td className={`${CELL} text-right tabular-nums`}>{task.turns}</td>
                    <td className={`${CELL} text-right tabular-nums`}>{formatCost(task.cost_usd)}</td>
                    <td className={CELL}>{formatAge(task.created_at)}</td>
                    <td className="py-2 align-top font-medium text-amber-900">{waiting}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {tasks.hasNextPage && (
        <button
          type="button"
          className="mt-3 rounded border border-slate-300 px-3 py-1 text-sm hover:bg-slate-100 disabled:opacity-50"
          disabled={tasks.isFetchingNextPage}
          onClick={() => void tasks.fetchNextPage()}
        >
          Load more
        </button>
      )}
    </section>
  );
}
