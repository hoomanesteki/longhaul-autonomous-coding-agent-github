import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import ScrollBox from "../ScrollBox";

/** jsdom does no layout, so the sizes of the scroll box are set by hand. */
function size(box: HTMLElement, { scrollWidth, clientWidth, scrollLeft }: Record<string, number>) {
  Object.defineProperty(box, "scrollWidth", { configurable: true, value: scrollWidth });
  Object.defineProperty(box, "clientWidth", { configurable: true, value: clientWidth });
  Object.defineProperty(box, "scrollLeft", { configurable: true, value: scrollLeft, writable: true });
}

function renderBox() {
  render(
    <ScrollBox label="Tasks table">
      <p>wide content</p>
    </ScrollBox>,
  );
  return screen.getByRole("region", { name: "Tasks table" });
}

describe("ScrollBox", () => {
  it("shows no edge when the content fits", () => {
    const box = renderBox();
    size(box, { scrollWidth: 300, clientWidth: 300, scrollLeft: 0 });
    act(() => {
      fireEvent.scroll(box);
    });

    expect(screen.queryByTestId("edge-right")).not.toBeInTheDocument();
    expect(screen.queryByTestId("edge-left")).not.toBeInTheDocument();
  });

  it("shows an edge on the side that has more to scroll to", () => {
    const box = renderBox();

    size(box, { scrollWidth: 900, clientWidth: 375, scrollLeft: 0 });
    act(() => {
      fireEvent.scroll(box);
    });
    expect(screen.getByTestId("edge-right")).toBeInTheDocument();
    expect(screen.queryByTestId("edge-left")).not.toBeInTheDocument();

    size(box, { scrollWidth: 900, clientWidth: 375, scrollLeft: 200 });
    act(() => {
      fireEvent.scroll(box);
    });
    expect(screen.getByTestId("edge-right")).toBeInTheDocument();
    expect(screen.getByTestId("edge-left")).toBeInTheDocument();

    size(box, { scrollWidth: 900, clientWidth: 375, scrollLeft: 525 });
    act(() => {
      fireEvent.scroll(box);
    });
    expect(screen.queryByTestId("edge-right")).not.toBeInTheDocument();
    expect(screen.getByTestId("edge-left")).toBeInTheDocument();
  });

  it("can be reached by keyboard, so a person without a pointer can scroll it", () => {
    expect(renderBox()).toHaveAttribute("tabindex", "0");
  });
});
