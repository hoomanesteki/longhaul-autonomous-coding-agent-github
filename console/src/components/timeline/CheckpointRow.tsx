import {
  formatTime,
  shortSha,
  type TimelineCheckpoint,
} from "../../util/timeline";

/** A checkpoint that no turn made: the bundle of the history before a hand-over. */
export default function CheckpointRow({
  checkpoint,
}: {
  checkpoint: TimelineCheckpoint;
}) {
  return (
    <div
      data-testid="timeline-row"
      className="flex flex-wrap items-baseline gap-x-3 rounded border border-teal-300 bg-teal-50 px-3 py-1.5 text-sm"
    >
      <span className="font-mono text-slate-700">{`Checkpoint ${shortSha(checkpoint.sha) ?? ""}`}</span>
      <span className="text-xs text-slate-600">
        {checkpoint.checkpoint_kind}
      </span>
      <span className="ml-auto text-xs text-slate-500">
        {formatTime(checkpoint.ts)}
      </span>
    </div>
  );
}
