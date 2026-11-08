/**
 * What the task table shows of a task, worked out from a row of the API.
 *
 * These are plain functions so that the task list now, and the task page and its actions later,
 * agree on what "needs you" means and how money and age are written.
 */

import type { components } from "../api/schema";

export type TaskSummary = components["schemas"]["TaskSummary"];

/**
 * The phases in which the task waits for a person, and what the person has to do. Everything
 * else is the agent's turn. A phase that is not here never raises the "needs you" flag.
 */
const WAITING_FOR: Record<string, string> = {
  paused: "Paused",
  awaiting_plan_approval: "Approve the plan",
};

/** What the task waits for from a person, or null when it waits for nothing. */
export function needsYou(task: Pick<TaskSummary, "phase">): string | null {
  return WAITING_FOR[task.phase] ?? null;
}

/** Cents, except for a cost too small to show in cents, which would read as free. */
export function formatCost(usd: number): string {
  if (usd > 0 && usd < 0.01) return "<$0.01";
  return `$${usd.toFixed(2)}`;
}

const UNITS: [label: string, seconds: number][] = [
  ["d", 86_400],
  ["h", 3_600],
  ["m", 60],
];

/** How long ago, in the largest whole unit: "now", "5m", "3h", "2d". */
export function formatAge(iso: string, now: number = Date.now()): string {
  const seconds = Math.max(0, Math.floor((now - Date.parse(iso)) / 1000));
  for (const [label, size] of UNITS) {
    if (seconds >= size) return `${Math.floor(seconds / size)}${label}`;
  }
  return "now";
}

/** The issue on GitHub, or null for a task that did not start from an issue. */
export function issueUrl(task: Pick<TaskSummary, "repo" | "issue_number">): string | null {
  return task.issue_number === null
    ? null
    : `https://github.com/${task.repo}/issues/${task.issue_number}`;
}
