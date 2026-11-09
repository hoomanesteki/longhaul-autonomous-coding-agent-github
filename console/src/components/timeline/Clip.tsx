import { useState } from "react";

import { formatCount } from "../../util/timeline";

const LIMIT = 2_000;

/** Text in a box that scrolls. Past `LIMIT` characters it shows the start and a button for the rest. */
export default function Clip({ text }: { text: string }) {
  const [all, setAll] = useState(false);
  const cut = !all && text.length > LIMIT;
  return (
    <div>
      <pre className="max-h-80 overflow-auto rounded bg-slate-100 p-2 text-xs break-words whitespace-pre-wrap">
        {cut ? text.slice(0, LIMIT) : text}
      </pre>
      {cut && (
        <button
          type="button"
          className="mt-1 text-xs text-blue-700 underline"
          onClick={() => setAll(true)}
        >
          Show all {formatCount(text.length)} characters
        </button>
      )}
    </div>
  );
}
