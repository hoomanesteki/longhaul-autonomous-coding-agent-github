/**
 * Where a memory item came from, as a link the operator can follow.
 *
 * `source_ref` is free text in the table: a pull request number, a task id or a commit sha,
 * depending on the source (ADR-0010). The text of a note started with a model, so the reference
 * is never trusted to build a URL: a link is made only when the reference has the exact shape
 * of one of the three, and anything else is shown as plain text.
 */

import type { components } from "../api/schema";

type Item = components["schemas"]["MemoryItemView"];

export type SourceLink =
  /** A page of the console, reached through the router. */
  | { kind: "console"; label: string; to: string }
  /** A page on GitHub. */
  | { kind: "github"; label: string; href: string };

const REPO = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;
const PULL_NUMBER = /^\d+$/;
const TASK_ID = /^lh-[A-Za-z0-9._-]+$/;
const SHA = /^[0-9a-f]{7,64}$/;
const SHORT_SHA = 7;

/** The link for an item's source, or null when it has none or the reference has no known shape. */
export function sourceLink(item: Pick<Item, "repo" | "source" | "source_ref">): SourceLink | null {
  const ref = item.source_ref;
  if (ref === null || ref === "") return null;
  const github = REPO.test(item.repo) ? `https://github.com/${item.repo}` : null;

  if (item.source === "task" || item.source === "merged_pr") {
    if (PULL_NUMBER.test(ref)) {
      return github === null
        ? null
        : { kind: "github", label: `PR #${ref}`, href: `${github}/pull/${ref}` };
    }
    // Lessons of a task that opened no pull request carry the task id.
    if (TASK_ID.test(ref)) {
      return { kind: "console", label: `Task ${ref}`, to: `/tasks/${encodeURIComponent(ref)}` };
    }
  }
  if (item.source === "default_branch" && SHA.test(ref) && github !== null) {
    return {
      kind: "github",
      label: `Commit ${ref.slice(0, SHORT_SHA)}`,
      href: `${github}/commit/${ref}`,
    };
  }
  return null;
}
