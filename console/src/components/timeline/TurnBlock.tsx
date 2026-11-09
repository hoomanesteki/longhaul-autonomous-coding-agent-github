import { useQueries } from "@tanstack/react-query";

import { getTurn } from "../../api/client";
import {
  argsSummary,
  cacheRatio,
  formatCount,
  formatDuration,
  formatPercent,
  formatTime,
  formatTurnCost,
  type TurnRow,
} from "../../util/timeline";
import ToolCallItem from "./ToolCallItem";
import TurnDetails from "./TurnDetails";

export type OpenState = { has: (key: string) => boolean; toggle: (key: string) => void };

/**
 * One turn: a line for each time the model was asked, and the tool calls that came of it.
 *
 * A turn's request and reply are fetched when its Details button is opened and not before, so
 * a task of hundreds of turns costs one request per turn that someone reads. The reply also
 * carries the arguments of the tool calls, which the timeline rows do not.
 */
export default function TurnBlock({
  taskId,
  row,
  coderModel,
  open,
}: {
  taskId: string;
  row: TurnRow;
  coderModel: string | null;
  open: OpenState;
}) {
  const detailKey = (attempt: number) => `details:${row.turnIndex}:${attempt}`;
  const opened = row.attempts.filter((attempt) => open.has(detailKey(attempt.attempt)));
  const loaded = useQueries({
    queries: opened.map((attempt) => ({
      queryKey: ["turn", taskId, row.turnIndex, attempt.attempt],
      queryFn: () => getTurn(taskId, row.turnIndex, { attempt: attempt.attempt }),
      // A turn is written once its call has finished, so what it holds does not change.
      staleTime: Infinity,
    })),
  });
  const details = new Map(opened.map((attempt, i) => [attempt.attempt, loaded[i]]));

  const args = new Map<string, string>();
  for (const query of loaded) {
    for (const call of query.data?.response?.tool_calls ?? []) {
      args.set(call.id, argsSummary(call.input));
    }
  }

  const several = row.attempts.length > 1;
  const first = row.attempts[0];
  return (
    <article data-testid="timeline-row" className="rounded border border-slate-200 bg-white px-3 py-2">
      <div className="flex items-baseline gap-3">
        <h2 className="text-sm font-semibold">{`Turn ${row.turnIndex}`}</h2>
        {first !== undefined && (
          <span className="ml-auto text-xs text-slate-500">{formatTime(first.ts)}</span>
        )}
      </div>

      {row.attempts.map((attempt) => {
        const key = detailKey(attempt.attempt);
        const query = details.get(attempt.attempt);
        const model = query?.data?.response?.model ?? (attempt.role === "coder" ? coderModel : null);
        const ratio = cacheRatio(attempt.input_tokens, attempt.cache_read_tokens);
        return (
          <div key={attempt.attempt} className="mt-1">
            <div className="flex flex-wrap items-baseline gap-x-3 text-sm">
              {several && <span className="text-slate-600">{`attempt ${attempt.attempt}`}</span>}
              <span className="font-medium">{attempt.role}</span>
              {model !== null && <span className="text-slate-700">{model}</span>}
              <span>{`${formatCount(attempt.input_tokens)} in, ${formatCount(attempt.output_tokens)} out`}</span>
              <span className="text-slate-600">{`cache ${formatPercent(ratio)}`}</span>
              <span className="tabular-nums">{formatTurnCost(attempt.cost_usd)}</span>
              {attempt.stop_reason !== null && (
                <span className="text-slate-600">{`stop: ${attempt.stop_reason}`}</span>
              )}
              {attempt.latency_ms !== null && (
                <span className="text-slate-600">{formatDuration(attempt.latency_ms)}</span>
              )}
              <button
                type="button"
                aria-expanded={open.has(key)}
                className="text-xs text-blue-700 underline"
                onClick={() => open.toggle(key)}
              >
                {open.has(key) ? "Hide details" : "Details"}
              </button>
            </div>
            {query?.isPending && <p className="mt-1 text-xs text-slate-600">Loading.</p>}
            {query?.isError && (
              <p role="alert" className="mt-1 text-xs text-red-700">
                Could not load the turn: {query.error.message}
              </p>
            )}
            {query?.data !== undefined && <TurnDetails detail={query.data} />}
          </div>
        );
      })}

      {row.calls.length > 0 && (
        <ul className="mt-1 divide-y divide-slate-100 border-t border-slate-100">
          {row.calls.map((call) => (
            <ToolCallItem
              key={call.id}
              taskId={taskId}
              call={call}
              args={args.get(call.id)}
              open={open.has(`output:${call.id}`)}
              onToggle={() => open.toggle(`output:${call.id}`)}
            />
          ))}
        </ul>
      )}
    </article>
  );
}
