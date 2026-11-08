import { useEffect, useState } from "react";

/**
 * The current time in milliseconds, refreshed every `everyMs`. A page that shows an age needs this
 * because a poll that returns unchanged data does not re-render the page.
 */
export function useNow(everyMs: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), everyMs);
    return () => clearInterval(timer);
  }, [everyMs]);
  return now;
}
