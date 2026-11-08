import { useInfiniteQuery } from "@tanstack/react-query";

import { getToolOutput } from "../../api/client";
import { formatCount } from "../../util/timeline";

// A page is this many lines. The API cuts the output into lines, so a long build log costs one
// request for its first screen and one more for each screen after it.
const PAGE_LINES = 200;

/** The output of one tool call, a page of lines at a time. Nothing is fetched until it is rendered. */
export default function ToolOutput({ taskId, callId }: { taskId: string; callId: string }) {
  const output = useInfiniteQuery({
    queryKey: ["tool-output", taskId, callId],
    queryFn: ({ pageParam }) =>
      getToolOutput(taskId, callId, { start: pageParam, end: pageParam + PAGE_LINES - 1 }),
    initialPageParam: 1,
    getNextPageParam: (last) => last.next_start ?? undefined,
  });

  if (output.isError) {
    return (
      <p role="alert" className="mt-1 text-xs text-red-700">
        Could not load the output: {output.error.message}
      </p>
    );
  }
  if (output.isPending) return <p className="mt-1 text-xs text-slate-600">Loading.</p>;

  const pages = output.data.pages;
  const first = pages[0];
  const last = pages[pages.length - 1];
  const text = pages.map((page) => page.text).join("\n");
  return (
    <div className="mt-1">
      <pre className="max-h-80 overflow-auto rounded bg-slate-100 p-2 text-xs break-words whitespace-pre-wrap">
        {text}
      </pre>
      <div className="mt-1 flex items-center gap-3 text-xs text-slate-600">
        {first !== undefined && last !== undefined && (
          <span>
            Lines {formatCount(first.start_line)} to {formatCount(last.end_line)} of{" "}
            {formatCount(last.total_lines)}
          </span>
        )}
        {output.hasNextPage && (
          <button
            type="button"
            className="text-blue-700 underline disabled:opacity-50"
            disabled={output.isFetchingNextPage}
            onClick={() => void output.fetchNextPage()}
          >
            Show more lines
          </button>
        )}
      </div>
    </div>
  );
}
