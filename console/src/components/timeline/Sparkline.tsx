import { formatCost } from "../../util/tasks";
import type { CostPoint } from "../../util/timeline";

const WIDTH = 160;
const HEIGHT = 32;
const PAD = 2;

function scale(value: number, max: number, size: number): number {
  return max === 0 ? 0 : (value / max) * size;
}

/**
 * The running cost of a task, one point for each turn. It has no axes: it shows whether the cost
 * climbs steadily or jumps, and the total sits in the label for the exact figure.
 */
export default function Sparkline({ points }: { points: readonly CostPoint[] }) {
  const last = points.at(-1);
  if (last === undefined) return null;
  const inner = { w: WIDTH - 2 * PAD, h: HEIGHT - 2 * PAD };
  const coordinates = points.map((point, i) => {
    const x = PAD + (points.length === 1 ? inner.w : scale(i, points.length - 1, inner.w));
    const y = HEIGHT - PAD - scale(point.total, last.total, inner.h);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  return (
    <svg
      role="img"
      aria-label={`Cost over turns, ${formatCost(last.total)} in total`}
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
  );
}
