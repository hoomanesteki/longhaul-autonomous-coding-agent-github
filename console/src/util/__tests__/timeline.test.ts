import { describe, expect, it } from "vitest";

import type { components } from "../../api/schema";
import {
  argsSummary,
  cacheRatio,
  costSeries,
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
