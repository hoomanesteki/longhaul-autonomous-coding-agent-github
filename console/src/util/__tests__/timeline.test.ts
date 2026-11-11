import { describe, expect, it } from "vitest";

import type { components } from "../../api/schema";
import {
  argsSummary,
  cacheRatio,
  costSeries,
  describeEvent,
  eventSummary,
  eventTone,
  formatDuration,
  groupTimeline,
  shortSha,
} from "../timeline";

type Item = components["schemas"]["TimelinePage"]["items"][number];

function turn(index: number, attempt = 1, cost = 0.01): Item {
  return {
    kind: "turn",
    ts: "2026-11-01T10:00:00Z",
    turn_index: index,
    attempt,
    role: "coder",
    input_tokens: 100,
    output_tokens: 20,
    cache_read_tokens: 0,
    cache_write_tokens: 0,
    cost_usd: cost,
    stop_reason: "tool_use",
    latency_ms: 900,
    ended_at: null,
  };
}

function call(id: string, turnIndex: number): Item {
  return {
    kind: "tool_call",
    ts: "2026-11-01T10:00:01Z",
    id,
    turn_index: turnIndex,
    name: "run_command",
    ok: true,
    exit_code: 0,
    duration_ms: 1200,
    has_output: true,
    post_sha: null,
  };
}

function event(id: number, type: string): Item {
  return { kind: "event", ts: "2026-11-01T10:00:02Z", id, type, payload: {} };
}

describe("groupTimeline", () => {
  it("puts each tool call under its turn and keeps events where they happened", () => {
    const rows = groupTimeline([
      turn(0),
      call("a", 0),
      event(1, "hint"),
      turn(1),
      call("b", 1),
      call("c", 1),
    ]);

    expect(rows.map((row) => row.key)).toEqual(["turn:0", "event:1", "turn:1"]);
    const second = rows[2];
    expect(second?.kind === "turn" && second.calls.map((c) => c.id)).toEqual(["b", "c"]);
  });

  it("keeps every attempt of a turn in one group", () => {
    const rows = groupTimeline([turn(4, 1), call("a", 4), turn(4, 2)]);

    expect(rows).toHaveLength(1);
    const only = rows[0];
    expect(only?.kind === "turn" && only.attempts.map((t) => t.attempt)).toEqual([1, 2]);
  });

  it("starts a group for a tool call whose turn is not in the list", () => {
    const rows = groupTimeline([call("a", 9)]);

    expect(rows).toHaveLength(1);
    const only = rows[0];
    expect(only?.kind === "turn" && only.attempts).toEqual([]);
    expect(only?.kind === "turn" && only.calls).toHaveLength(1);
  });

  it("gives a group that grows between polls the same key", () => {
    const first = groupTimeline([turn(0)]);
    const second = groupTimeline([turn(0), call("a", 0)]);

    expect(second[0]?.key).toBe(first[0]?.key);
  });
});

describe("eventTone", () => {
  it.each([
    ["compaction", "compaction"],
    ["paused", "pause"],
    ["stuck", "pause"],
    ["resumed", "pause"],
    ["hint", "hint"],
    ["budget_added", "hint"],
    ["verdict", "verdict"],
    ["sandbox_recreated", "recovery"],
    ["continue_as_new", "recovery"],
    ["error", "error"],
    ["step_updated", "other"],
  ])("colours %s as %s", (type, tone) => {
    expect(eventTone(type)).toBe(tone);
  });
});

describe("eventSummary", () => {
  it("lists the short values of a payload and leaves out the long ones", () => {
    const text = eventSummary({ reason: "stuck", count: 3, trace: "x".repeat(500), nested: { a: 1 } });

    expect(text).toContain("reason: stuck");
    expect(text).toContain("count: 3");
    expect(text).not.toContain("xxxx");
    expect(text).not.toContain("nested");
  });

  it("is empty for an empty payload", () => {
    expect(eventSummary({})).toBe("");
  });
});

describe("argsSummary", () => {
  it("shows the field that says what the call does", () => {
    expect(argsSummary({ command: "pytest -q" })).toBe("pytest -q");
    expect(argsSummary({ path: "src/app.py", content: "..." })).toBe("src/app.py");
  });

  it("falls back to the JSON, cut to one line", () => {
    const text = argsSummary({ a: "x".repeat(300) });

    expect(text.length).toBeLessThanOrEqual(81);
    expect(text.endsWith("…")).toBe(true);
    expect(text).not.toContain("\n");
  });

  it("takes the first line of a multi-line command", () => {
    expect(argsSummary({ command: "cd src\nmake" })).toBe("cd src");
  });
});

describe("small formats", () => {
  it("shortens a sha to seven characters", () => {
    expect(shortSha("0123456789abcdef")).toBe("0123456");
    expect(shortSha(null)).toBeNull();
  });

  it("writes a duration in the largest unit that reads well", () => {
    expect(formatDuration(0)).toBe("0 ms");
    expect(formatDuration(840)).toBe("840 ms");
    expect(formatDuration(1200)).toBe("1.2 s");
    expect(formatDuration(75_000)).toBe("1 m 15 s");
  });

  it("works out the cache ratio as cache reads over all input", () => {
    expect(cacheRatio(100, 300)).toBeCloseTo(0.75);
    expect(cacheRatio(0, 0)).toBeNull();
  });
});

describe("costSeries", () => {
  it("adds up the cost per turn, counting every attempt, in turn order", () => {
    const series = costSeries(groupTimeline([turn(0, 1, 0.1), turn(1, 1, 0.2), turn(1, 2, 0.3)]));

    expect(series.map((point) => point.turn)).toEqual([0, 1]);
    expect(series[0]?.total).toBeCloseTo(0.1);
    expect(series[1]?.total).toBeCloseTo(0.6);
  });
});

describe("describeEvent", () => {
  const SHA = "0123456789abcdef0123456789abcdef01234567";

  it("writes a step update as the step, its status, the note and the short commit", () => {
    expect(
      describeEvent("step_updated", {
        step_id: "s1",
        status: "done",
        note: "Added the parser test",
        head_sha: SHA,
        turn_index: 4,
      }),
    ).toBe("Step s1 done: Added the parser test (0123456)");
    expect(describeEvent("step_updated", { step_id: "s2", status: "in_progress", note: "", head_sha: null })).toBe(
      "Step s2 in progress",
    );
  });

  it("writes a verdict with its round and the tests, or with the first reason it failed", () => {
    expect(describeEvent("verdict", { round: 1, passed: true, tests_summary: "pytest -q" })).toBe(
      "Verifier passed (round 1): pytest -q",
    );
    expect(describeEvent("verdict", { round: 1, passed: true, tests_summary: null })).toBe(
      "Verifier passed (round 1)",
    );
    expect(
      describeEvent("verdict", { round: 2, passed: false, issues: ["test_add still fails", "no docs"] }),
    ).toBe("Verifier failed (round 2): test_add still fails, and 1 more");
    expect(describeEvent("verdict", { round: 3, passed: false, issues: [] })).toBe("Verifier failed (round 3)");
  });

  it("writes an outcome with its turns and summary, and leaves the cost out", () => {
    expect(
      describeEvent("outcome", {
        kind: "pr_opened",
        repo: "acme/widgets",
        turns: 9,
        cost_usd: 0,
        summary: "Fixed the parser",
      }),
    ).toBe("Pull request opened after 9 turns: Fixed the parser");
    expect(describeEvent("outcome", { kind: "stuck", turns: 1, summary: "" })).toBe("Stopped as stuck after 1 turn");
    expect(describeEvent("outcome", { kind: "budget_exhausted", turns: 30 })).toBe(
      "Stopped, the budget ran out after 30 turns",
    );
    expect(describeEvent("outcome", { kind: "cancelled", turns: 2, summary: "cancelled by alice" })).toBe(
      "Cancelled after 2 turns: cancelled by alice",
    );
    expect(describeEvent("outcome", { kind: "error", turns: 2, summary: "boom" })).toBe(
      "Ended with an error after 2 turns: boom",
    );
  });

  it("cuts a long summary and leaves the rest to the details", () => {
    const line = describeEvent("outcome", { kind: "pr_opened", turns: 3, summary: "x".repeat(400) });

    expect(line).not.toBeNull();
    expect(line!.length).toBeLessThan(200);
    expect(line!.endsWith("…")).toBe(true);
  });

  it("writes a pause with its reason and a resume with how it ended", () => {
    expect(describeEvent("paused", { kind: "hint", reason: "repeated_failure", pause: 1 })).toBe(
      "Paused: repeated failure",
    );
    expect(describeEvent("paused", { kind: "hint", reason: "agent_asked", question: "Which API?" })).toBe(
      "Paused: agent asked, Which API?",
    );
    expect(describeEvent("resumed", { kind: "hint", via: "hint", by: "alice" })).toBe(
      "Resumed with a hint from alice",
    );
    expect(describeEvent("resumed", { kind: "budget", via: "budget", reason: "max_usd" })).toBe(
      "Resumed with a budget top-up (max usd)",
    );
    expect(describeEvent("resumed", { kind: "budget", via: "midnight" })).toBe(
      "Resumed at midnight UTC, when the daily limit reset",
    );
    expect(describeEvent("resumed", { pause: 2, via: null })).toBe("Resumed");
  });

  it("writes the amount of a budget top-up", () => {
    expect(describeEvent("budget_added", { pause: 1, usd: 2, turns: 5, by: "alice" })).toBe(
      "Budget added: $2.00 and 5 turns, by alice",
    );
    expect(describeEvent("budget_added", { pause: 1, usd: 0, turns: 1 })).toBe("Budget added: 1 turn");
    expect(describeEvent("budget_added", { pause: 1, usd: 0, turns: 0 })).toBe("Budget added: nothing");
  });

  it("writes a compaction as the tokens it saved", () => {
    expect(
      describeEvent("compaction", {
        tokens_before: 40_000,
        tokens_after: 28_000,
        folded_turns: 8,
        forced: false,
      }),
    ).toBe("Compaction saved 12,000 tokens (40,000 to 28,000), 8 turns folded");
    expect(
      describeEvent("compaction", { tokens_before: 9000, tokens_after: 4000, folded_turns: 1, forced: true }),
    ).toBe("Compaction saved 5,000 tokens (9,000 to 4,000), 1 turn folded, forced");
  });

  it("gives up, so the caller shows the key and value pairs, for a kind or a payload it does not know", () => {
    expect(describeEvent("sandbox_recreated", { sandbox_id: "abc" })).toBeNull();
    expect(describeEvent("verdict", { passed: "yes" })).toBeNull();
    expect(describeEvent("step_updated", {})).toBeNull();
    expect(describeEvent("compaction", { dropped_turns: 6 })).toBeNull();
    expect(describeEvent("outcome", {})).toBeNull();
  });
});

describe("groupTimeline checkpoints", () => {
  it("puts a checkpoint under the turn that made it", () => {
    const rows = groupTimeline([
      turn(2),
      call("a", 2),
      {
        kind: "checkpoint",
        ts: "2026-11-01T10:00:03Z",
        id: 7,
        sha: "0123456789abcdef0123456789abcdef01234567",
        checkpoint_kind: "step",
        turn_index: 2,
        step_id: "s1",
      },
    ]);

    expect(rows).toHaveLength(1);
    const only = rows[0];
    expect(only?.kind === "turn" && only.checkpoints.map((c) => c.sha.slice(0, 7))).toEqual(["0123456"]);
  });
});
