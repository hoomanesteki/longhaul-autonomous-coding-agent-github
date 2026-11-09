import { describe, expect, it } from "vitest";

import { formatAge, formatCost, issueUrl, needsYou } from "../tasks";

describe("needsYou", () => {
  it("names what a waiting task needs", () => {
    expect(needsYou({ phase: "paused" })).toBe("Paused");
    expect(needsYou({ phase: "awaiting_plan_approval" })).toBe("Approve the plan");
  });

  it("is null for every other phase", () => {
    for (const phase of ["queued", "coding", "verifying", "delivering", "done", "failed"]) {
      expect(needsYou({ phase })).toBeNull();
    }
  });
});

describe("formatCost", () => {
  it("writes cents", () => {
    expect(formatCost(0)).toBe("$0.00");
    expect(formatCost(1.5)).toBe("$1.50");
    expect(formatCost(12.345)).toBe("$12.35");
  });

  it("does not show a small cost as free", () => {
    expect(formatCost(0.004)).toBe("<$0.01");
  });
});

describe("formatAge", () => {
  const now = Date.parse("2026-11-08T12:00:00Z");
  const ago = (seconds: number) => new Date(now - seconds * 1000).toISOString();

  it("uses the largest whole unit", () => {
    expect(formatAge(ago(30), now)).toBe("now");
    expect(formatAge(ago(60), now)).toBe("1m");
    expect(formatAge(ago(59 * 60), now)).toBe("59m");
    expect(formatAge(ago(3600), now)).toBe("1h");
    expect(formatAge(ago(86_400 * 2 + 5), now)).toBe("2d");
  });

  it("treats a time in the future as now", () => {
    expect(formatAge(ago(-120), now)).toBe("now");
  });
});

describe("issueUrl", () => {
  it("links the issue on GitHub", () => {
    expect(issueUrl({ repo: "acme/widgets", issue_number: 7 })).toBe(
      "https://github.com/acme/widgets/issues/7",
    );
  });

  it("is null without an issue number", () => {
    expect(issueUrl({ repo: "acme/widgets", issue_number: null })).toBeNull();
  });
});
