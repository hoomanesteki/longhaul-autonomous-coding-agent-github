import { useCallback, useLayoutEffect, useRef, useState, type ReactNode } from "react";

/**
 * A box that scrolls sideways and shows, with a shadow on the edge, that there is more to see.
 *
 * On a phone a wide table scrolls inside its box, and a scroll bar that fades out gives no sign of
 * it. The shadows are drawn over the content and not as a background of the box, because a row
 * with its own background would cover the second. A shadow shows only on the side that has more.
 */
export default function ScrollBox({ label, children }: { label: string; children: ReactNode }) {
  const box = useRef<HTMLDivElement>(null);
  const [edges, setEdges] = useState({ left: false, right: false });

  const measure = useCallback(() => {
    const el = box.current;
    if (el === null) return;
    const left = el.scrollLeft > 1;
    const right = el.scrollLeft + el.clientWidth < el.scrollWidth - 1;
    setEdges((current) => (current.left === left && current.right === right ? current : { left, right }));
  }, []);

  // The width of the content changes with the rows, and the box with the window.
  useLayoutEffect(() => {
    measure();
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  });

  return (
    <div className="relative">
      <div
        ref={box}
        role="region"
        aria-label={label}
        // A scroll box has to take focus for a keyboard to scroll it.
        tabIndex={0}
        className="overflow-x-auto"
        onScroll={measure}
      >
        {children}
      </div>
      {edges.left && (
        <span
          data-testid="edge-left"
          aria-hidden="true"
          className="pointer-events-none absolute inset-y-0 left-0 w-3 bg-gradient-to-r from-slate-400/40 to-transparent"
        />
      )}
      {edges.right && (
        <span
          data-testid="edge-right"
          aria-hidden="true"
          className="pointer-events-none absolute inset-y-0 right-0 w-3 bg-gradient-to-l from-slate-400/40 to-transparent"
        />
      )}
    </div>
  );
}
