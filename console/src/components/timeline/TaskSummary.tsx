import type { components } from "../../api/schema";
import { formatCost } from "../../util/tasks";
import { cacheRatio, formatCount, formatPercent } from "../../util/timeline";
import PhaseBadge from "../PhaseBadge";

type Detail = components["schemas"]["TaskDetail"];
type Step = components["schemas"]["PlanView"]["plan"]["steps"][number];

const STEP_TONES: Record<Step["status"], string> = {
  todo: "bg-slate-100 text-slate-700",
  doing: "bg-blue-100 text-blue-900",
  done: "bg-green-100 text-green-900",
  skipped: "bg-slate-200 text-slate-600",
};

// Why there is no live status. The numbers below it are then the last the database was given.
const NO_LIVE: Record<Detail["workflow"], string> = {
  running: "The workflow does not answer its status query. A worker may not be running.",
  closed: "The workflow has ended. The numbers are the last the database was given.",
  not_found: "Temporal has no workflow for this task. The numbers are the last the database was given.",
  unreachable:
    "Live status is not available: Temporal does not answer. The numbers are the last the database was given.",
};

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-xs text-slate-600">{label}</dt>
      <dd className="text-sm font-medium tabular-nums">{children}</dd>
    </div>
  );
}

/**
 * The facts of a task and its plan.
 *
 * The `tasks` row can lag behind the task, so the phase, cost and turns come from the workflow's
 * own answer when there is one.
 */
export default function TaskSummary({ detail }: { detail: Detail }) {
  const { task, live, plan } = detail;
  const ratio = cacheRatio(task.input_tokens_total, task.cache_read_total);
  return (
    <>
      <section aria-label="Task summary" className="mt-3">
        <dl className="flex flex-wrap gap-x-8 gap-y-2">
          <Fact label="Phase">
            <PhaseBadge phase={live?.phase ?? task.phase} />
          </Fact>
          <Fact label="Cost">{formatCost(live?.cost_usd ?? task.cost_usd)}</Fact>
          <Fact label="Turns">{live?.turn_index ?? task.turns}</Fact>
          <Fact label="Tokens">
            {`${formatCount(task.input_tokens_total)} in, ${formatCount(task.output_tokens_total)} out`}
          </Fact>
          <Fact label="Cache ratio">{formatPercent(ratio)}</Fact>
          {task.outcome !== null && <Fact label="Outcome">{task.outcome}</Fact>}
          {task.pr_url !== null && (
            <Fact label="Pull request">
              <a href={task.pr_url} target="_blank" rel="noreferrer" className="text-blue-700 underline">
                Pull request
              </a>
            </Fact>
          )}
        </dl>
        {live?.paused_reason != null && (
          <p className="mt-2 rounded bg-amber-50 px-3 py-1.5 text-sm text-amber-900">
            {live.paused_reason}
          </p>
        )}
        {live === null && <p className="mt-2 text-sm text-slate-600">{NO_LIVE[detail.workflow]}</p>}
      </section>

      <section aria-label="Plan" className="mt-4">
        <h2 className="text-sm font-semibold">Plan</h2>
        {plan === null ? (
          <p className="mt-1 text-sm text-slate-600">No plan yet.</p>
        ) : (
          <>
            <p className="mt-1 text-sm">{plan.plan.summary}</p>
            <ul className="mt-1 space-y-1 text-sm">
              {plan.plan.steps.map((step) => (
                <li key={step.id} className="flex items-baseline gap-2">
                  <span className={`rounded px-1.5 py-0.5 text-xs ${STEP_TONES[step.status]}`}>
                    {step.status}
                  </span>
                  <span>{step.description}</span>
                </li>
              ))}
            </ul>
          </>
        )}
      </section>
    </>
  );
}
