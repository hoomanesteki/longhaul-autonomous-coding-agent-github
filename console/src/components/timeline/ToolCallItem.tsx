import { formatDuration, shortSha, type TimelineToolCall } from "../../util/timeline";
import ToolOutput from "./ToolOutput";

/** One tool call of a turn: what it was, whether it worked, how long it took and what it committed. */
export default function ToolCallItem({
  taskId,
  call,
  args,
  open,
  onToggle,
}: {
  taskId: string;
  call: TimelineToolCall;
  /** A one-line summary of the arguments, once the turn's reply has been loaded. */
  args: string | undefined;
  open: boolean;
  onToggle: () => void;
}) {
  const sha = shortSha(call.post_sha);
  return (
    <li className="py-1">
      <div className="flex flex-wrap items-baseline gap-x-3 text-sm">
        <span className="font-mono font-medium">{call.name}</span>
        {args !== undefined && <span className="min-w-0 truncate font-mono text-slate-700">{args}</span>}
        <span className={call.ok ? "text-green-800" : "font-medium text-red-700"}>
          {call.ok ? "ok" : "error"}
        </span>
        {!call.ok && call.exit_code !== null && (
          <span className="text-slate-600">{`exit ${call.exit_code}`}</span>
        )}
        <span className="text-slate-600">{formatDuration(call.duration_ms)}</span>
        {sha !== null && <span className="font-mono text-slate-600">{sha}</span>}
        {call.has_output && (
          <button
            type="button"
            aria-expanded={open}
            className="text-xs text-blue-700 underline"
            onClick={onToggle}
          >
            {open ? "Hide output" : "Show output"}
          </button>
        )}
      </div>
      {open && <ToolOutput taskId={taskId} callId={call.id} />}
    </li>
  );
}
