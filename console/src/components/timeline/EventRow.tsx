import {
  describeEvent,
  eventSummary,
  eventTone,
  formatTime,
  type EventTone,
  type TimelineEvent,
} from "../../util/timeline";
import type { OpenState } from "./TurnBlock";

// Events that need a person's eye (a pause, a stuck loop) are warm, the agent's own housekeeping
// (compaction, recovery) is cool, and a verdict or an error stands out from both.
const TONES: Record<EventTone, string> = {
  compaction: "border-violet-300 bg-violet-50",
  pause: "border-amber-300 bg-amber-50",
  hint: "border-sky-300 bg-sky-50",
  verdict: "border-green-300 bg-green-50",
  recovery: "border-teal-300 bg-teal-50",
  error: "border-red-300 bg-red-50",
  other: "border-slate-200 bg-slate-50",
};

/**
 * One event of the task: a compaction, a pause, a hint, a verdict, a recovery.
 *
 * A kind the console knows is one sentence, and any other is its key and value pairs. The whole
 * payload is one click away either way, shown as text.
 */
export default function EventRow({ event, open }: { event: TimelineEvent; open: OpenState }) {
  const tone = eventTone(event.type);
  const summary = describeEvent(event.type, event.payload) ?? eventSummary(event.payload);
  const detailKey = `event:${event.id}`;
  const shown = open.has(detailKey);
  return (
    <div
      data-testid="timeline-row"
      data-tone={tone}
      className={`rounded border px-3 py-1.5 text-sm ${TONES[tone]}`}
    >
      <div className="flex flex-wrap items-baseline gap-x-3">
        <span className="font-medium">{event.type}</span>
        {summary !== "" && <span className="min-w-0 break-words text-slate-700">{summary}</span>}
        {Object.keys(event.payload).length > 0 && (
          <button
            type="button"
            aria-expanded={shown}
            className="text-xs text-blue-700 underline"
            onClick={() => open.toggle(detailKey)}
          >
            {shown ? "Hide details" : "Details"}
          </button>
        )}
        <span className="ml-auto text-xs text-slate-500">{formatTime(event.ts)}</span>
      </div>
      {shown && (
        <pre className="mt-1 max-h-64 overflow-auto rounded bg-white/70 p-2 text-xs whitespace-pre-wrap break-words">
          {JSON.stringify(event.payload, null, 2)}
        </pre>
      )}
    </div>
  );
}
