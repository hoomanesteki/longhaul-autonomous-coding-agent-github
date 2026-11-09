import { useWindowVirtualizer } from "@tanstack/react-virtual";
import { useCallback, useLayoutEffect, useRef, useState } from "react";

import type { Row } from "../../util/timeline";
import EventRow from "./EventRow";
import TurnBlock, { type OpenState } from "./TurnBlock";

// A turn with one tool call is about this tall. Rows are measured once they are on screen, so the
// guess only decides how long the scroll bar is before they are.
const ESTIMATE_PX = 96;
const OVERSCAN = 6;

/**
 * The rows of a timeline, as many as fit on the screen and a few more on each side.
 *
 * A task of 300 turns has a thousand rows, and every one of them is a card with buttons. Only the
 * ones in view are in the page, and the rest are a blank space of the height they would take. The
 * page scrolls as a whole, so the summary above the timeline scrolls away with it.
 */
export default function Timeline({
  taskId,
  rows,
  coderModel,
}: {
  taskId: string;
  rows: readonly Row[];
  coderModel: string | null;
}) {
  const listRef = useRef<HTMLDivElement>(null);
  const [margin, setMargin] = useState(0);
  const [opened, setOpened] = useState<ReadonlySet<string>>(new Set());

  // The list starts below the summary, whose height changes as the task loads and as it is polled.
  // Without the offset the virtualizer would count the summary as part of the list. The body
  // changes size whenever the summary or the window does, so that is what is watched.
  useLayoutEffect(() => {
    const measure = () => {
      const top = (listRef.current?.getBoundingClientRect().top ?? 0) + window.scrollY;
      setMargin((current) => (Math.abs(top - current) >= 1 ? top : current));
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(document.body);
    return () => observer.disconnect();
  }, []);

  // The open state sits here and not in each row, because a row that scrolls out of view is
  // unmounted and would forget that it was open.
  const toggle = useCallback((key: string) => {
    setOpened((current) => {
      const next = new Set(current);
      if (!next.delete(key)) next.add(key);
      return next;
    });
  }, []);
  const open: OpenState = { has: (key) => opened.has(key), toggle };

  const virtualizer = useWindowVirtualizer({
    count: rows.length,
    estimateSize: () => ESTIMATE_PX,
    overscan: OVERSCAN,
    scrollMargin: margin,
    getItemKey: (index) => rows[index]?.key ?? index,
    // An element that is not laid out reports no height, and a row of height zero would let every
    // row into the window. The estimate stands in until the row has a real size.
    measureElement: (element) => Math.round(element.getBoundingClientRect().height) || ESTIMATE_PX,
  });

  return (
    <div ref={listRef} style={{ height: virtualizer.getTotalSize(), position: "relative" }}>
      {virtualizer.getVirtualItems().map((item) => {
        const row = rows[item.index];
        if (row === undefined) return null;
        return (
          <div
            key={item.key}
            data-index={item.index}
            ref={virtualizer.measureElement}
            className="absolute top-0 left-0 w-full pb-2"
            style={{ transform: `translateY(${item.start - margin}px)` }}
          >
            {row.kind === "event" ? (
              <EventRow event={row.event} />
            ) : (
              <TurnBlock taskId={taskId} row={row} coderModel={coderModel} open={open} />
            )}
          </div>
        );
      })}
    </div>
  );
}
