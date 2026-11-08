import { useId } from "react";
import { Link } from "react-router-dom";

import type { components } from "../api/schema";
import { sourceLink } from "./memorySource";
import { formatTime } from "./time";

type Item = components["schemas"]["MemoryItemView"];

function Source({ item }: { item: Item }) {
  const link = sourceLink(item);
  const linkClass = "text-blue-700 underline";
  if (link?.kind === "console") {
    return (
      <Link to={link.to} className={linkClass}>
        {link.label}
      </Link>
    );
  }
  if (link?.kind === "github") {
    return (
      <a href={link.href} className={linkClass} target="_blank" rel="noreferrer">
        {link.label}
      </a>
    );
  }
  // No link: say where it came from in words, with the reference as it is stored.
  return (
    <span>
      {item.source}
      {item.source_ref !== null && item.source_ref !== "" ? ` ${item.source_ref}` : ""}
    </span>
  );
}

/** The content of an item. A repo map is long and meant to be read as a tree, so it folds away. */
function Content({ item, id }: { item: Item; id: string }) {
  if (item.kind === "repo_map") {
    return (
      <details className="mt-2 rounded border border-slate-200">
        <summary id={id} className="cursor-pointer px-3 py-2 text-sm font-medium">Repo map</summary>
        <pre className="max-h-96 overflow-auto border-t border-slate-200 p-3 font-mono text-xs whitespace-pre">
          {item.content}
        </pre>
      </details>
    );
  }
  return (
    <p id={id} className="mt-2 text-sm whitespace-pre-wrap">
      {item.content}
    </p>
  );
}

type Props = {
  item: Item;
  /** Set for a candidate: the two decisions an operator can make on it. */
  onDecide?: (decision: "approve" | "reject") => void;
  busy?: boolean;
};

/** One memory item: what it says, where it came from, how much it was used, and the decision. */
export default function MemoryItemRow({ item, onDecide, busy = false }: Props) {
  const contentId = useId();
  return (
    <li aria-labelledby={contentId} className="rounded border border-slate-200 p-3">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-600">
        <span className="rounded bg-slate-100 px-2 py-0.5 font-medium text-slate-800">
          {item.kind}
        </span>
        <span>
          Source: <Source item={item} />
        </span>
        <span>
          Uses: <span>{item.uses}</span>
        </span>
        <span>
          Last used: <span>{formatTime(item.last_used_at)}</span>
        </span>
      </div>
      <Content item={item} id={contentId} />
      {onDecide !== undefined && (
        <div className="mt-3 flex gap-2">
          <button
            type="button"
            disabled={busy}
            onClick={() => onDecide("approve")}
            className="rounded bg-slate-900 px-3 py-1 text-sm text-white disabled:opacity-50"
          >
            Approve
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => onDecide("reject")}
            className="rounded border border-slate-300 px-3 py-1 text-sm disabled:opacity-50"
          >
            Reject
          </button>
        </div>
      )}
    </li>
  );
}
