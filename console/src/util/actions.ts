/**
 * Which actions a task offers, worked out from its phase and the state of its workflow.
 *
 * The API refuses an action the workflow cannot take (409 for a task that has ended), but a
 * button that only fails is noise, so the page offers an action only when it can work. The
 * plan decision is the one action that belongs to a single phase: the workflow reads it while
 * it waits for the plan, and a decision at any other time would sit in the workflow unread.
 */

import type { components } from "../api/schema";
import { isEnded } from "./tasks";

type Workflow = components["schemas"]["TaskDetail"]["workflow"];

export type Offer =
  /** The task can be steered. `plan` is true while it waits for a decision on its plan. */
  | { kind: "open"; plan: boolean }
  /** The phase is final. */
  | { kind: "ended" }
  /** The row says the task is going, but Temporal has no open workflow for it. */
  | { kind: "no_workflow" };

export function offerFor(phase: string, workflow: Workflow): Offer {
  if (isEnded(phase)) return { kind: "ended" };
  // The row can lag behind the task, so a closed or missing workflow wins over its phase.
  if (workflow === "closed" || workflow === "not_found") return { kind: "no_workflow" };
  // "unreachable" is left open: Temporal may be back by the time the button is clicked, and the
  // API answers 503 with the reason if it is not.
  return { kind: "open", plan: phase === "awaiting_plan_approval" };
}

/** Longest amounts the API takes (BudgetRequest). A larger one is a typo, not a top-up. */
const MAX_USD = 10_000;
const MAX_TURNS = 100_000;

export type Amounts = { usd: number; turns: number };

/**
 * The budget a person typed, or null when the API would refuse it. A field left empty is zero,
 * and two zeros add nothing, so they are refused too.
 */
export function parseAmounts(usd: string, turns: string): Amounts | null {
  const dollars = usd.trim() === "" ? 0 : Number(usd);
  const count = turns.trim() === "" ? 0 : Number(turns);
  if (!Number.isFinite(dollars) || dollars < 0 || dollars > MAX_USD) return null;
  if (!Number.isInteger(count) || count < 0 || count > MAX_TURNS) return null;
  if (dollars === 0 && count === 0) return null;
  return { usd: dollars, turns: count };
}
