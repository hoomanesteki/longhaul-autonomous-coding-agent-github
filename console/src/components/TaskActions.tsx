import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import type { FormEvent } from "react";

import { addBudget, approvePlan, cancelTask, hint, rejectPlan } from "../api/client";
import type { components } from "../api/schema";
import { offerFor, parseAmounts } from "../util/actions";

type Workflow = components["schemas"]["TaskDetail"]["workflow"];

/** One action to send. `done` and `failed` are the words of the toast. */
type Job = {
  send: () => Promise<unknown>;
  done: string;
  failed: string;
  /** Called after the API accepted the action, to clear the form it came from. */
  reset?: () => void;
};

type Toast = { kind: "ok" | "error"; text: string };

// Long enough to read, short enough not to hide the page for good.
const TOAST_MS = 6_000;

const BUTTON =
  "rounded border border-slate-300 bg-white px-3 py-1 text-sm font-medium text-slate-900 " +
  "hover:bg-slate-100 disabled:cursor-not-allowed disabled:text-slate-400 disabled:hover:bg-white";
const DANGER = BUTTON.replace("text-slate-900", "text-red-800");
const FIELD = "w-full rounded border border-slate-300 px-2 py-1 text-sm";

/**
 * What a person can do to a task: decide its plan, give a hint, add budget, or cancel it.
 *
 * Only the actions that can work in the task's phase are shown (util/actions.ts). Each one calls
 * the API, says what happened in a toast, and then has the task and its timeline fetched again,
 * so the page shows the effect without waiting for the next poll.
 */
export default function TaskActions({
  taskId,
  phase,
  workflow,
}: {
  taskId: string;
  phase: string;
  workflow: Workflow;
}) {
  const queryClient = useQueryClient();
  const [toast, setToast] = useState<Toast | null>(null);

  const mutation = useMutation({
    mutationFn: (job: Job) => job.send(),
    onSuccess: async (_reply, job) => {
      job.reset?.();
      setToast({ kind: "ok", text: job.done });
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["task", taskId] }),
        queryClient.invalidateQueries({ queryKey: ["timeline", taskId] }),
      ]);
    },
    onError: (error, job) => {
      setToast({ kind: "error", text: `Could not ${job.failed}: ${error.message}` });
    },
  });

  useEffect(() => {
    if (toast === null) return;
    const timer = setTimeout(() => setToast(null), TOAST_MS);
    return () => clearTimeout(timer);
  }, [toast]);

  const offer = offerFor(phase, workflow);
  const busy = mutation.isPending;
  const run = (job: Job) => mutation.mutate(job);

  return (
    <section aria-label="Actions" className="mt-4">
      <h2 className="text-sm font-semibold">Actions</h2>
      {offer.kind === "ended" && (
        <p className="mt-1 text-sm text-slate-600">This task has ended. There is nothing to do.</p>
      )}
      {offer.kind === "no_workflow" && (
        <p className="mt-1 text-sm text-slate-600">
          This task has no running workflow, so there is nothing to act on.
        </p>
      )}
      {offer.kind === "open" && (
        <div className="mt-2 grid gap-4 sm:grid-cols-2">
          {offer.plan && <PlanForm taskId={taskId} busy={busy} run={run} />}
          <HintForm taskId={taskId} busy={busy} run={run} />
          <BudgetForm taskId={taskId} busy={busy} run={run} />
          <CancelForm taskId={taskId} busy={busy} run={run} />
        </div>
      )}
      {toast !== null && (
        <div
          role={toast.kind === "error" ? "alert" : "status"}
          className={`fixed right-4 bottom-4 max-w-sm rounded border px-3 py-2 text-sm shadow ${
            toast.kind === "error"
              ? "border-red-300 bg-red-50 text-red-900"
              : "border-green-300 bg-green-50 text-green-900"
          }`}
        >
          {toast.text}
        </div>
      )}
    </section>
  );
}

type FormProps = { taskId: string; busy: boolean; run: (job: Job) => void };

function PlanForm({ taskId, busy, run }: FormProps) {
  const [feedback, setFeedback] = useState("");
  const text = feedback.trim();
  return (
    <form
      aria-label="Decide the plan"
      className="space-y-2"
      onSubmit={(event: FormEvent) => {
        event.preventDefault();
        run({
          send: () => rejectPlan(taskId, { feedback: text }),
          done: "Plan rejected.",
          failed: "reject the plan",
          reset: () => setFeedback(""),
        });
      }}
    >
      <label className="block text-sm">
        Feedback on the plan
        <textarea
          className={`${FIELD} mt-1`}
          rows={3}
          value={feedback}
          onChange={(event) => setFeedback(event.target.value)}
        />
      </label>
      <div className="flex gap-2">
        <button
          type="button"
          className={BUTTON}
          disabled={busy}
          onClick={() =>
            run({
              send: () => approvePlan(taskId),
              done: "Plan approved.",
              failed: "approve the plan",
            })
          }
        >
          Approve plan
        </button>
        <button type="submit" className={BUTTON} disabled={busy || text === ""}>
          Reject plan
        </button>
      </div>
    </form>
  );
}

function HintForm({ taskId, busy, run }: FormProps) {
  const [text, setText] = useState("");
  return (
    <form
      aria-label="Send a hint"
      className="space-y-2"
      onSubmit={(event: FormEvent) => {
        event.preventDefault();
        run({
          send: () => hint(taskId, { text: text.trim() }),
          done: "Hint sent.",
          failed: "send the hint",
          reset: () => setText(""),
        });
      }}
    >
      <label className="block text-sm">
        Hint
        <textarea
          className={`${FIELD} mt-1`}
          rows={3}
          value={text}
          onChange={(event) => setText(event.target.value)}
        />
      </label>
      <button type="submit" className={BUTTON} disabled={busy || text.trim() === ""}>
        Send hint
      </button>
    </form>
  );
}

function BudgetForm({ taskId, busy, run }: FormProps) {
  const [usd, setUsd] = useState("");
  const [turns, setTurns] = useState("");
  const amounts = parseAmounts(usd, turns);
  return (
    <form
      aria-label="Add budget"
      className="space-y-2"
      onSubmit={(event: FormEvent) => {
        event.preventDefault();
        if (amounts === null) return;
        run({
          send: () => addBudget(taskId, amounts),
          done: "Budget added.",
          failed: "add the budget",
          reset: () => {
            setUsd("");
            setTurns("");
          },
        });
      }}
    >
      <div className="flex gap-2">
        <label className="block flex-1 text-sm">
          Dollars
          <input
            type="number"
            min="0"
            step="0.01"
            className={`${FIELD} mt-1`}
            value={usd}
            onChange={(event) => setUsd(event.target.value)}
          />
        </label>
        <label className="block flex-1 text-sm">
          Turns
          <input
            type="number"
            min="0"
            step="1"
            className={`${FIELD} mt-1`}
            value={turns}
            onChange={(event) => setTurns(event.target.value)}
          />
        </label>
      </div>
      <button type="submit" className={BUTTON} disabled={busy || amounts === null}>
        Add budget
      </button>
    </form>
  );
}

function CancelForm({ taskId, busy, run }: FormProps) {
  const [typed, setTyped] = useState("");
  return (
    <form
      aria-label="Cancel the task"
      className="space-y-2"
      onSubmit={(event: FormEvent) => {
        event.preventDefault();
        // The id is typed in full on purpose: a cancel cannot be taken back.
        if (typed !== taskId) return;
        run({
          // No body: the API words the reason itself.
          send: () => cancelTask(taskId, null),
          done: "Cancel sent.",
          failed: "cancel the task",
          reset: () => setTyped(""),
        });
      }}
    >
      <label className="block text-sm">
        Type {taskId} to confirm
        <input
          type="text"
          autoComplete="off"
          className={`${FIELD} mt-1`}
          value={typed}
          onChange={(event) => setTyped(event.target.value)}
        />
      </label>
      <button type="submit" className={DANGER} disabled={busy || typed !== taskId}>
        Cancel task
      </button>
    </form>
  );
}
