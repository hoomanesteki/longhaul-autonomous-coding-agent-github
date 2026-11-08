import { useState } from "react";

import type { components } from "../../api/schema";
import Clip from "./Clip";

type Schemas = components["schemas"];
type Message = Schemas["Message"];
type Block = Message["content"][number];

// The request of a turn is the whole transcript so far. Its end is what the model was just asked,
// so that is what shows first, and the earlier messages come ten at a time.
const FIRST_MESSAGES = 4;
const MORE_MESSAGES = 10;

function BlockView({ block }: { block: Block }) {
  if (block.type === "text") return <Clip text={block.text} />;
  if (block.type === "tool_use") {
    return (
      <div>
        <p className="text-xs text-slate-600">Tool call: {block.name}</p>
        <Clip text={JSON.stringify(block.input, null, 2)} />
      </div>
    );
  }
  return (
    <div>
      <p className="text-xs text-slate-600">{block.is_error ? "Tool result (error)" : "Tool result"}</p>
      <Clip text={block.content} />
    </div>
  );
}

function MessageView({ message }: { message: Message }) {
  return (
    <div className="space-y-1">
      <p className="text-xs font-medium text-slate-600">{message.role}</p>
      {message.content.map((block, i) => (
        <BlockView key={i} block={block} />
      ))}
    </div>
  );
}

/** The reply of one model call and the end of the transcript it was given. */
export default function TurnDetails({ detail }: { detail: Schemas["TurnDetail"] }) {
  const [shown, setShown] = useState(FIRST_MESSAGES);
  const request = detail.request ?? [];
  const visible = request.slice(Math.max(0, request.length - shown));
  const hidden = request.length - visible.length;
  return (
    <div className="mt-2 space-y-3 border-l-2 border-slate-200 pl-3">
      <section aria-label="Reply" className="space-y-1">
        <h3 className="text-xs font-semibold uppercase text-slate-600">Reply</h3>
        {detail.response === null ? (
          <p className="text-sm text-slate-600">This call has no stored reply.</p>
        ) : (
          detail.response.content.map((block, i) => <BlockView key={i} block={block} />)
        )}
      </section>
      <section aria-label="Request" className="space-y-2">
        <h3 className="text-xs font-semibold uppercase text-slate-600">Request</h3>
        {detail.request === null && (
          <p className="text-sm text-slate-600">This call has no stored request.</p>
        )}
        {hidden > 0 && (
          <button
            type="button"
            className="text-xs text-blue-700 underline"
            onClick={() => setShown(shown + MORE_MESSAGES)}
          >
            Show {Math.min(hidden, MORE_MESSAGES)} earlier messages
          </button>
        )}
        {visible.map((message, i) => (
          <MessageView key={hidden + i} message={message} />
        ))}
      </section>
    </div>
  );
}
