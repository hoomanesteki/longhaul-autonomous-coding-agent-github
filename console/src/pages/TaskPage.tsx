import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { useEffect, useMemo } from "react";
import { Link, useParams } from "react-router-dom";

import { ApiError, getTask, getTimeline } from "../api/client";
import Sparkline from "../components/timeline/Sparkline";
import TaskSummary from "../components/timeline/TaskSummary";
import Timeline from "../components/timeline/Timeline";
import { issueUrl } from "../util/tasks";
import { costSeries, groupTimeline } from "../util/timeline";

const POLL_MS = 10_000;
// The API's largest page. A timeline is read as a whole, so the fewer requests it takes the better.
const TIMELINE_PAGE = 500;

// Phases a task does not leave. A task in one of them is not polled any more.
const ENDED = new Set(["done", "failed", "cancelled"]);

/** One task: where it stands, its plan, and every turn, tool call and event in order. */
export default function TaskPage() {
  const { taskId = "" } = useParams();
  const detail = useQuery({
    queryKey: ["task", taskId],
    queryFn: () => getTask(taskId),
    refetchInterval: (query) => {
      const data = query.state.data;
      return data !== undefined && ENDED.has(data.live?.phase ?? data.task.phase) ? false : POLL_MS;
    },
  });
  const ended =
    detail.data !== undefined && ENDED.has(detail.data.live?.phase ?? detail.data.task.phase);

  const timeline = useInfiniteQuery({
    queryKey: ["timeline", taskId],
    queryFn: ({ pageParam }) => getTimeline(taskId, { cursor: pageParam, limit: TIMELINE_PAGE }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    refetchInterval: ended ? false : POLL_MS,
  });

  // The page groups and totals the whole timeline, so it takes every page and not just the first.
  const { hasNextPage, isFetchingNextPage, fetchNextPage } = timeline;
  useEffect(() => {
    if (hasNextPage && !isFetchingNextPage) void fetchNextPage();
  }, [hasNextPage, isFetchingNextPage, fetchNextPage]);

  const rows = useMemo(
    () => groupTimeline(timeline.data?.pages.flatMap((page) => page.items) ?? []),
    [timeline.data],
  );
  const series = useMemo(() => costSeries(rows), [rows]);

  if (detail.isError) {
    const missing = detail.error instanceof ApiError && detail.error.status === 404;
    return (
      <section>
        <Link to="/tasks" className="text-sm text-blue-700 underline">
          All tasks
        </Link>
        <p role={missing ? undefined : "alert"} className="mt-3 text-sm text-slate-700">
          {missing ? "This task does not exist." : `Could not load the task: ${detail.error.message}`}
        </p>
      </section>
    );
  }
  if (detail.isPending) return <p className="text-sm text-slate-600">Loading.</p>;

  const { task } = detail.data;
  const issue = issueUrl(task);
  return (
    <section>
      <Link to="/tasks" className="text-sm text-blue-700 underline">
        All tasks
      </Link>
      <h1 className="mt-2 text-lg font-semibold">{task.title}</h1>
      <p className="text-sm text-slate-600">
        {task.repo}
        {issue !== null && (
          <>
            {" "}
            <a href={issue} target="_blank" rel="noreferrer" className="text-blue-700 underline">
              #{task.issue_number}
            </a>
          </>
        )}
      </p>

      <TaskSummary detail={detail.data} />

      <div className="mt-6 flex flex-wrap items-center gap-x-6 gap-y-2">
        <h2 className="text-sm font-semibold">Timeline</h2>
        <Sparkline points={series} />
      </div>

      {timeline.isError && (
        <div role="alert" className="mt-3 text-sm text-red-700">
          Could not load the timeline: {timeline.error.message}{" "}
          <button type="button" className="underline" onClick={() => void timeline.refetch()}>
            Try again
          </button>
        </div>
      )}
      {timeline.isPending && <p className="mt-3 text-sm text-slate-600">Loading.</p>}
      {timeline.isSuccess && rows.length === 0 && (
        <p className="mt-3 text-sm text-slate-600">Nothing recorded yet.</p>
      )}
      {rows.length > 0 && (
        <div className="mt-3">
          <Timeline taskId={taskId} rows={rows} coderModel={task.model_coder} />
        </div>
      )}
    </section>
  );
}
