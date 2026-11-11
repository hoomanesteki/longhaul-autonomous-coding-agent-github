import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";

import type { TimelineEvent } from "../../util/timeline";
import EventRow from "../timeline/EventRow";

const SHA = "0123456789abcdef0123456789abcdef01234567";

function event(type: string, payload: Record<string, unknown>): TimelineEvent {
  return { kind: "event", ts: "2026-11-01T10:00:00Z", id: 1, type, payload };
}

/** The row with the open state that the timeline keeps for it. */
function Row({ value }: { value: TimelineEvent }) {
  const [opened, setOpened] = useState<ReadonlySet<string>>(new Set());
  const open = {
    has: (key: string) => opened.has(key),
    toggle: (key: string) =>
      setOpened((current) => {
        const next = new Set(current);
        if (!next.delete(key)) next.add(key);
        return next;
      }),
  };
  return <EventRow event={value} open={open} />;
}

function renderRow(value: TimelineEvent) {
  render(<Row value={value} />);
  return screen.getByTestId("timeline-row");
}

describe("EventRow", () => {
  it.each([
    [
      "step_updated",
      { step_id: "s1", status: "done", note: "Added the test", head_sha: SHA, turn_index: 3 },
      "Step s1 done: Added the test (0123456)",
    ],
    [
      "verdict",
      { round: 1, passed: true, tests_summary: "pytest -q" },
      "Verifier passed (round 1): pytest -q",
    ],
    [
      "verdict",
      { round: 2, passed: false, issues: ["test_add still fails"] },
      "Verifier failed (round 2): test_add still fails",
    ],
    [
      "outcome",
      { kind: "pr_opened", repo: "acme/widgets", turns: 9, cost_usd: 0, summary: "Fixed it" },
      "Pull request opened after 9 turns: Fixed it",
    ],
    ["paused", { kind: "hint", reason: "no_progress", pause: 1 }, "Paused: no progress"],
    ["resumed", { kind: "hint", via: "hint", by: "alice" }, "Resumed with a hint from alice"],
    ["budget_added", { pause: 1, usd: 2.5, turns: 0, by: "alice" }, "Budget added: $2.50, by alice"],
    [
      "compaction",
      { tokens_before: 40000, tokens_after: 28000, folded_turns: 8, forced: false },
      "Compaction saved 12,000 tokens (40,000 to 28,000), 8 turns folded",
    ],
  ])("shows %s as one readable line", (type, payload, line) => {
    const row = renderRow(event(type, payload));

    expect(row).toHaveTextContent(line);
    // The raw payload is not in the line, not the full commit and not a key and value pair.
    expect(screen.queryByText(SHA)).not.toBeInTheDocument();
    expect(row).not.toHaveTextContent("cost_usd");
    expect(row).not.toHaveTextContent("turn_index:");
  });

  it("keeps the key and value fallback for a kind it does not know", () => {
    const row = renderRow(event("sandbox_recreated", { sandbox_id: "abc", attempt: 2 }));

    expect(row).toHaveTextContent("sandbox_recreated");
    expect(row).toHaveTextContent("sandbox_id: abc, attempt: 2");
  });

  it("keeps the fallback for a known kind whose payload is not what it expects", () => {
    const row = renderRow(event("compaction", { dropped_turns: 6 }));

    expect(row).toHaveTextContent("dropped_turns: 6");
  });

  it("keeps the whole payload one click away, written as text", async () => {
    const payload = { step_id: "s1", status: "done", head_sha: SHA, note: "<img src=x onerror=alert(1)>" };
    const row = renderRow(event("step_updated", payload));
    const toggle = screen.getByRole("button", { name: "Details" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(row).not.toHaveTextContent(SHA);

    await userEvent.click(toggle);

    expect(screen.getByRole("button", { name: "Hide details" })).toHaveAttribute("aria-expanded", "true");
    const shown = row.querySelector("pre");
    expect(shown?.textContent).toBe(JSON.stringify(payload, null, 2));
    expect(shown?.textContent).toContain(SHA);
    // The note is text in the page and not a tag.
    expect(row.querySelector("img")).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: "Hide details" }));
    expect(row.querySelector("pre")).toBeNull();
  });

  it("has no details toggle for an event with no payload", () => {
    renderRow(event("sandbox_recreated", {}));

    expect(screen.queryByRole("button", { name: "Details" })).not.toBeInTheDocument();
  });
});
