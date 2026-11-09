import { formatCost } from "../../util/tasks";
import type { CostPoint } from "../../util/timeline";

const WIDTH = 160;
const HEIGHT = 32;
const PAD = 2;
const LABEL = "Cost per turn";

function scale(value: number, max: number, size: number): number {
  return max === 0 ? 0 : (value / max) * size;
}

/**
 * The running cost of a task, one point for each turn. It has no axes: it shows whether the cost
 * climbs steadily or jumps, and the total sits in the accessible name for the exact figure.
 *
 * The label is visible, and the accessible name starts with the same words. A task that has cost
 * nothing draws no line: at the bottom of the box it would look like a stray mark and not like
 * "free", so a short note says it instead.
 */
export default function Sparkline({ points }: { points: readonly CostPoint[] }) {
  const last = points.at(-1);
  if (last === undefined) return null;
  const free = last.total === 0;
  const inner = { w: WIDTH - 2 * PAD, h: HEIGHT - 2 * PAD };
  const coordinates = points.map((point, i) => {
    const x = PAD + (points.length === 1 ? inner.w : scale(i, points.length - 1, inner.w));
    const y = HEIGHT - PAD - scale(point.total, last.total, inner.h);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  return (
    <div className="flex items-center gap-2">
      <span className="text-xs text-slate-600">{LABEL}</span>
      {free ? (
        <span className="text-xs text-slate-600">no cost yet</span>
      ) : (
        <svg
          role="img"
          aria-label={`${LABEL}, ${formatCost(last.total)} in total`}
          viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
          width={WIDTH}
          height={HEIGHT}
          className="text-blue-700"
        >
          <polyline
            points={coordinates.join(" ")}
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinejoin="round"
          />
        </svg>
      )}
    </div>
  );
}
