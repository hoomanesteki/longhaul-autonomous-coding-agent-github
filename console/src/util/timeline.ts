/**
 * What the task page shows of a timeline, worked out from the rows of the API.
 *
 * The API sends events, model calls, tool calls and checkpoints (the commits a turn saved) as one
 * flat list in the order they happened. The page wants one block per turn, with its tool calls and
 * checkpoints inside, so the grouping and the small formats live here as plain functions and are
 * tested without a browser.
 */

import type { components } from "../api/schema";

type Schemas = components["schemas"];
export type TimelineItem = Schemas["TimelinePage"]["items"][number];
export type TimelineTurn = Schemas["TimelineTurn"];
export type TimelineToolCall = Schemas["TimelineToolCall"];
export type TimelineEvent = Schemas["TimelineEvent"];
export type TimelineCheckpoint = Schemas["TimelineCheckpoint"];

export type TurnRow = {
  kind: "turn";
  key: string;
  turnIndex: number;
  /** One entry for each time the turn was asked, oldest first. Empty if only its tool calls are known. */
  attempts: TimelineTurn[];
  calls: TimelineToolCall[];
  /** The commits saved while the turn ran, oldest first. */
  checkpoints: TimelineCheckpoint[];
};
export type EventRow = { kind: "event"; key: string; event: TimelineEvent };
/** A hand-over bundle, in the place it was made. It belongs to no turn, see `groupTimeline`. */
export type CheckpointRow = { kind: "checkpoint"; key: string; checkpoint: TimelineCheckpoint };
export type Row = TurnRow | EventRow | CheckpointRow;

/**
 * One row for each turn and each event, in the order they first appear.
 *
 * A tool call belongs to its turn even when an event was written between the two, so it joins the
 * group of that turn instead of taking a row of its own. The key of a group depends only on the
 * turn, so a group that grows between two polls keeps its place and its open state.
 */
export function groupTimeline(items: readonly TimelineItem[]): Row[] {
  const rows: Row[] = [];
  const turns = new Map<number, TurnRow>();

  function groupOf(turnIndex: number): TurnRow {
    let group = turns.get(turnIndex);
    if (group === undefined) {
      group = { kind: "turn", key: `turn:${turnIndex}`, turnIndex, attempts: [], calls: [], checkpoints: [] };
      turns.set(turnIndex, group);
      rows.push(group);
    }
    return group;
  }

  for (const item of items) {
    if (item.kind === "event") rows.push({ kind: "event", key: `event:${item.id}`, event: item });
    else if (item.kind === "turn") groupOf(item.turn_index).attempts.push(item);
    // The hand-over records a bundle with the number of turns done, which is the index of the next
    // turn, run by the next workflow run. Grouping it by that index would open an empty block for a
    // turn that has not started and put it before the hand-over event, so it keeps its own row.
    else if (item.kind === "checkpoint" && item.checkpoint_kind === "bundle")
      rows.push({ kind: "checkpoint", key: `checkpoint:${item.id}`, checkpoint: item });
    else if (item.kind === "checkpoint") groupOf(item.turn_index).checkpoints.push(item);
    else groupOf(item.turn_index).calls.push(item);
  }
  return rows;
}

// ---------- events ----------
export type EventTone =
  | "compaction"
  | "pause"
  | "hint"
  | "verdict"
  | "recovery"
  | "error"
  | "other";

const TONES: Record<string, EventTone> = {
  compaction: "compaction",
  paused: "pause",
  stuck: "pause",
  resumed: "pause",
  hint: "hint",
  budget_added: "hint",
  verdict: "verdict",
  sandbox_recreated: "recovery",
  continue_as_new: "recovery",
  error: "error",
};

/** The colour family of an event type. A type the console does not know is plain. */
export function eventTone(type: string): EventTone {
  return TONES[type] ?? "other";
}

const MAX_VALUE = 60;
const MAX_FIELDS = 5;

/** The short values of an event payload as `key: value` pairs. Long text and nested data are left out. */
export function eventSummary(payload: Record<string, unknown>): string {
  const parts: string[] = [];
  for (const [name, value] of Object.entries(payload)) {
    const plain = typeof value === "string" || typeof value === "number" || typeof value === "boolean";
    if (plain && String(value).length <= MAX_VALUE) parts.push(`${name}: ${String(value)}`);
    if (parts.length === MAX_FIELDS) break;
  }
  return parts.join(", ");
}

// ---------- readable events ----------
const MAX_LINE_TEXT = 140;

function text(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

function count(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function words(name: string): string {
  return name.replaceAll("_", " ");
}

function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

/** Text from a person or a model, cut to what one line holds. The details have all of it. */
function clip(value: string): string {
  const line = value.split("\n")[0] ?? "";
  return line.length > MAX_LINE_TEXT ? `${line.slice(0, MAX_LINE_TEXT)}…` : line;
}

const OUTCOMES: Record<string, string> = {
  pr_opened: "Pull request opened",
  stuck: "Stopped as stuck",
  budget_exhausted: "Stopped, the budget ran out",
  cancelled: "Cancelled",
  error: "Ended with an error",
};

function stepLine(p: Record<string, unknown>): string | null {
  const id = text(p.step_id);
  const status = text(p.status);
  if (id === null || status === null) return null;
  const note = text(p.note);
  const sha = shortSha(text(p.head_sha));
  return `Step ${id} ${words(status)}${note === null ? "" : `: ${clip(note)}`}${sha === null ? "" : ` (${sha})`}`;
}

function verdictLine(p: Record<string, unknown>): string | null {
  if (typeof p.passed !== "boolean") return null;
  const round = count(p.round);
  const head = `Verifier ${p.passed ? "passed" : "failed"}${round === null ? "" : ` (round ${round})`}`;
  if (p.passed) {
    const tests = text(p.tests_summary);
    return tests === null ? head : `${head}: ${clip(tests)}`;
  }
  const issues = Array.isArray(p.issues) ? p.issues.filter((i): i is string => typeof i === "string") : [];
  const [first, ...rest] = issues;
  if (first === undefined) return head;
  return `${head}: ${clip(first)}${rest.length > 0 ? `, and ${rest.length} more` : ""}`;
}

function outcomeLine(p: Record<string, unknown>): string | null {
  const kind = text(p.kind);
  if (kind === null) return null;
  const turns = count(p.turns);
  const summary = text(p.summary);
  const head = OUTCOMES[kind] ?? `Ended (${words(kind)})`;
  return `${head}${turns === null ? "" : ` after ${plural(turns, "turn")}`}${summary === null ? "" : `: ${clip(summary)}`}`;
}

function pausedLine(p: Record<string, unknown>): string | null {
  const reason = text(p.reason);
  if (reason === null) return null;
  const question = text(p.question);
  return `Paused: ${words(reason)}${question === null ? "" : `, ${clip(question)}`}`;
}

const ANSWERS: Record<string, string> = { hint: "a hint", budget: "a budget top-up" };

function resumedLine(p: Record<string, unknown>): string {
  const via = text(p.via);
  if (via === "midnight") return "Resumed at midnight UTC, when the daily limit reset";
  const answer = via === null ? null : (ANSWERS[via] ?? null);
  if (answer === null) return "Resumed";
  const by = text(p.by);
  const reason = text(p.reason);
  return `Resumed with ${answer}${by === null ? "" : ` from ${by}`}${reason === null ? "" : ` (${words(reason)})`}`;
}

function budgetLine(p: Record<string, unknown>): string | null {
  const usd = count(p.usd);
  const turns = count(p.turns);
  if (usd === null && turns === null) return null;
  const parts: string[] = [];
  if (usd !== null && usd !== 0) parts.push(`$${usd.toFixed(2)}`);
  if (turns !== null && turns !== 0) parts.push(plural(turns, "turn"));
  const by = text(p.by);
  return `Budget added: ${parts.join(" and ") || "nothing"}${by === null ? "" : `, by ${by}`}`;
}

function compactionLine(p: Record<string, unknown>): string | null {
  const before = count(p.tokens_before);
  const after = count(p.tokens_after);
  if (before === null || after === null) return null;
  const folded = count(p.folded_turns);
  return (
    `Compaction saved ${formatCount(before - after)} tokens (${formatCount(before)} to ${formatCount(after)})` +
    `${folded === null ? "" : `, ${plural(folded, "turn")} folded`}${p.forced === true ? ", forced" : ""}`
  );
}

const LINES: Record<string, (payload: Record<string, unknown>) => string | null> = {
  step_updated: stepLine,
  verdict: verdictLine,
  outcome: outcomeLine,
  paused: pausedLine,
  resumed: resumedLine,
  budget_added: budgetLine,
  compaction: compactionLine,
};

/**
 * One readable line for an event of a kind the console knows, or null.
 *
 * The caller falls back to `eventSummary` on null: for a kind that is not here, and for a known
 * kind whose payload lacks what the line needs (an older run, or a writer that changed), so a row
 * never reads "undefined" or hides what the payload holds.
 */
export function describeEvent(type: string, payload: Record<string, unknown>): string | null {
  return LINES[type]?.(payload) ?? null;
}

// ---------- tool calls ----------
// The argument that says what a call does, in the order the tools use them.
const KEY_ARGS = ["command", "path", "file_path", "query", "pattern", "url"];
const MAX_ARGS = 80;

function oneLine(text: string): string {
  const first = text.split("\n")[0] ?? "";
  return first.length > MAX_ARGS ? `${first.slice(0, MAX_ARGS)}…` : first;
}

/** What a tool call was asked to do, in one line: its main argument, or else its arguments as JSON. */
export function argsSummary(input: Record<string, unknown>): string {
  for (const name of KEY_ARGS) {
    const value = input[name];
    if (typeof value === "string" && value !== "") return oneLine(value);
  }
  return oneLine(JSON.stringify(input));
}

/** The first seven characters of a commit, the length git shows. */
export function shortSha(sha: string | null): string | null {
  return sha === null || sha === "" ? null : sha.slice(0, 7);
}

// ---------- numbers ----------
/** A duration in the largest unit that reads well: "840 ms", "1.2 s", "1 m 15 s". */
export function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
  const seconds = Math.round(ms / 1000);
  return `${Math.floor(seconds / 60)} m ${seconds % 60} s`;
}

/** A count with thousands separators, written the same in every locale. */
export function formatCount(value: number): string {
  return value.toLocaleString("en-US");
}

/** The cost of one turn. Four decimals, because a turn costs a cent or less and cents would read as zero. */
export function formatTurnCost(usd: number): string {
  return `$${usd.toFixed(4)}`;
}

/** Cache reads over all the input the model read, or null before any input was counted. */
export function cacheRatio(inputTokens: number, cacheReadTokens: number): number | null {
  const total = inputTokens + cacheReadTokens;
  return total === 0 ? null : cacheReadTokens / total;
}

export function formatPercent(ratio: number | null): string {
  return ratio === null ? "n/a" : `${Math.round(ratio * 100)}%`;
}

export type CostPoint = { turn: number; total: number };

/** The running cost at the end of each turn, counting every attempt, in turn order. */
export function costSeries(rows: readonly Row[]): CostPoint[] {
  const turns = rows
    .filter((row): row is TurnRow => row.kind === "turn")
    .map((row) => ({
      turn: row.turnIndex,
      cost: row.attempts.reduce((sum, attempt) => sum + attempt.cost_usd, 0),
    }))
    .sort((a, b) => a.turn - b.turn);
  let total = 0;
  return turns.map(({ turn, cost }) => {
    total += cost;
    return { turn, total };
  });
}

/** The time of day of a row, in the viewer's time zone. */
export function formatTime(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour12: false });
}
