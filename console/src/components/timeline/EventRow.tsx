import {
  eventSummary,
  eventTone,
  formatTime,
  type EventTone,
  type TimelineEvent,
} from "../../util/timeline";

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

/** One event of the task: a compaction, a pause, a hint, a verdict, a recovery. */
export default function EventRow({ event }: { event: TimelineEvent }) {
  const tone = eventTone(event.type);
  const summary = eventSummary(event.payload);
  return (
    <div
      data-testid="timeline-row"
      data-tone={tone}
      className={`flex flex-wrap items-baseline gap-x-3 rounded border px-3 py-1.5 text-sm ${TONES[tone]}`}
    >
      <span className="font-medium">{event.type}</span>
      {summary !== "" && <span className="text-slate-700">{summary}</span>}
      <span className="ml-auto text-xs text-slate-500">{formatTime(event.ts)}</span>
    </div>
  );
}
