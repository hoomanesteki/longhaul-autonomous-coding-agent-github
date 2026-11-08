/**
 * What the task page shows of a timeline, worked out from the rows of the API.
 *
 * The API sends events, model calls and tool calls as one flat list in the order they happened.
 * The page wants one block per turn, with its tool calls inside, so the grouping and the small
 * formats live here as plain functions and are tested without a browser.
 */

import type { components } from "../api/schema";

type Schemas = components["schemas"];
export type TimelineItem = Schemas["TimelinePage"]["items"][number];
export type TimelineTurn = Schemas["TimelineTurn"];
export type TimelineToolCall = Schemas["TimelineToolCall"];
export type TimelineEvent = Schemas["TimelineEvent"];

export type TurnRow = {
  kind: "turn";
  key: string;
  turnIndex: number;
  /** One entry for each time the turn was asked, oldest first. Empty if only its tool calls are known. */
  attempts: TimelineTurn[];
  calls: TimelineToolCall[];
};
export type EventRow = { kind: "event"; key: string; event: TimelineEvent };
export type Row = TurnRow | EventRow;

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
      group = { kind: "turn", key: `turn:${turnIndex}`, turnIndex, attempts: [], calls: [] };
      turns.set(turnIndex, group);
      rows.push(group);
    }
    return group;
  }

  for (const item of items) {
    if (item.kind === "event") rows.push({ kind: "event", key: `event:${item.id}`, event: item });
    else if (item.kind === "turn") groupOf(item.turn_index).attempts.push(item);
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
  return sha === null ? null : sha.slice(0, 7);
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
