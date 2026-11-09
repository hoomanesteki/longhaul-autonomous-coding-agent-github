const TONES: Record<string, string> = {
  paused: "bg-amber-100 text-amber-900",
  awaiting_plan_approval: "bg-amber-100 text-amber-900",
  done: "bg-green-100 text-green-900",
  failed: "bg-red-100 text-red-900",
  cancelled: "bg-slate-200 text-slate-700",
};

/** The phase of a task as a small label. Phases that wait for a person or have ended get a colour. */
export default function PhaseBadge({ phase }: { phase: string }) {
  const tone = TONES[phase] ?? "bg-blue-100 text-blue-900";
  return (
    <span className={`inline-block rounded px-2 py-0.5 text-xs font-medium ${tone}`}>{phase}</span>
  );
}
