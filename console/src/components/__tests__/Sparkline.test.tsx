import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { CostPoint } from "../../util/timeline";
import Sparkline from "../timeline/Sparkline";

function points(...totals: number[]): CostPoint[] {
  return totals.map((total, turn) => ({ turn, total }));
}

describe("Sparkline", () => {
  it("has a visible label and the same words as its accessible name", () => {
    render(<Sparkline points={points(0.1, 0.3, 0.6)} />);

    expect(screen.getByText("Cost per turn")).toBeVisible();
    const chart = screen.getByRole("img", { name: /^Cost per turn/ });
    expect(chart).toHaveAccessibleName("Cost per turn, $0.60 in total");
    expect(chart.querySelector("polyline")?.getAttribute("points")?.trim().split(/\s+/)).toHaveLength(3);
  });

  it("draws nothing when no turn has cost anything, and says so", () => {
    render(<Sparkline points={points(0, 0, 0)} />);

    // A flat line at the bottom of the box reads as a stray mark, not as "free".
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    expect(document.querySelector("polyline")).toBeNull();
    expect(screen.getByText("Cost per turn")).toBeVisible();
    expect(screen.getByText("no cost yet")).toBeVisible();
  });

  it("draws a task whose first turns were free and later ones were not", () => {
    render(<Sparkline points={points(0, 0, 0.02)} />);

    expect(screen.getByRole("img", { name: "Cost per turn, $0.02 in total" })).toBeInTheDocument();
    expect(screen.queryByText("no cost yet")).not.toBeInTheDocument();
  });

  it("draws a single turn as a line that spans the box", () => {
    render(<Sparkline points={points(0.05)} />);

    expect(screen.getByRole("img", { name: /^Cost per turn/ })).toBeInTheDocument();
  });

  it("shows nothing for a task with no turns", () => {
    const { container } = render(<Sparkline points={[]} />);

    expect(container).toBeEmptyDOMElement();
  });
});
