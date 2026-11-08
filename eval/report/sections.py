"""The sections of docs/results.md, as Markdown text (LH-905).

Each function takes the runs and returns one section. The tables follow plan section 8.6, with
two additions that the plan asks for in the build list: a Wilson interval beside every resolve
rate, and a list of the failures in the chaos suite. The numbers are computed here from the
result objects. Nothing is typed in by hand, so the page can be rebuilt from the same files and
give the same bytes.

A section whose runs were not given still appears, with one line that says what is missing. A
reader then sees that the question was not answered instead of wondering whether the section was
forgotten.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence

from eval import results
from eval.chaos import plan as chaos_plan
from eval.chaos.results import RunResult
from eval.chaos.results import Summary as ChaosSummary
from eval.report import stats
from eval.report.runs import HEADLINE_CONFIGS, ChaosRun, EvalRun, Layout, Pairing

SIGNIFICANCE = 0.05
REASON_LIMIT = 160
CHAOS_LABELS = {
    "worker": "Worker SIGKILL",
    "sandbox": "Sandbox kill",
    "temporal": "Temporal restart",
    "postgres": "Postgres restart",
}
# (with, without): the results of one task in the two runs of an ablation.
Pair = tuple[results.TaskResult, results.TaskResult]
SET_TITLES = {"swebench": "SWE-bench Verified subset", "custom": "Custom set"}


# ---------- how numbers are written ----------
def percent(share: float) -> str:
    return f"{100 * share:.1f}%"


def interval(bounds: tuple[float, float]) -> str:
    return f"{percent(bounds[0])} to {percent(bounds[1])}"


def money(value: float | None) -> str:
    """Dollars. Under a dollar the third decimal matters, as in $0.047 for one short call."""
    if value is None:
        return "n/a"
    return f"${value:.2f}" if value >= 1 else f"${value:.3f}"


def minutes(seconds: float | None) -> str:
    return "n/a" if seconds is None else f"{seconds / 60:.1f} min"


def seconds(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f} s"


def p_value(p: float) -> str:
    return "<0.001" if p < 0.001 else f"{p:.3f}"


def p_text(p: float) -> str:
    """The p-value as a clause: "p = 0.031", or "p < 0.001" for the smallest."""
    return "p < 0.001" if p < 0.001 else f"p = {p:.3f}"


def whole(value: float) -> str:
    return f"{round(value):,}"


def table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(lines)


def cell(text: str) -> str:
    """Text for one table cell: one line, and no bar that would end the cell early."""
    return " ".join(text.split()).replace("|", "\\|")


# ---------- numbers of one run ----------
def tokens_of(result: results.TaskResult) -> int:
    return results.tokens_per_task(result.tokens)


def input_tokens_of(result: results.TaskResult) -> int:
    """Everything the model read: new input plus what the prompt cache served."""
    return result.tokens.input + result.tokens.cache_read


def cost_per_resolved(run: EvalRun) -> float | None:
    """Cost of every attempted task over the resolved ones (plan section 8.5)."""
    return sum(r.cost_usd for r in run.results) / run.resolved if run.resolved else None


def median_wall_s(run: EvalRun) -> float | None:
    times = [r.wall_time_s for r in run.results if r.resolved]
    return statistics.median(times) if times else None


def rate_cells(run: EvalRun) -> list[str]:
    """Resolved, rate and Wilson interval of a run."""
    low_high = stats.wilson(run.resolved, run.attempted)
    return [
        f"{run.resolved} / {run.attempted}",
        percent(run.resolved / run.attempted),
        interval(low_high),
    ]


def labelled(by_config: dict[str, list[EvalRun]]) -> list[tuple[str, EvalRun]]:
    """(row label, run) in the order of the tables. A second run of a config says so."""
    rows: list[tuple[str, EvalRun]] = []
    for config in HEADLINE_CONFIGS:
        for index, run in enumerate(by_config.get(config, [])):
            rows.append((config if index == 0 else f"{config} (run {index + 1})", run))
    return rows


def ungraded_note(rows: Sequence[tuple[str, EvalRun]]) -> str:
    notes = [f"{label}: {run.ungraded}" for label, run in rows if run.ungraded]
    if not notes:
        return ""
    return (
        "Tasks that were not graded count as attempted and not resolved. "
        + ", ".join(notes)
        + ".\n\n"
    )


def none_given(what: str) -> str:
    return f"No run {what} was given, so this section has no numbers.\n"


# ---------- method ----------
def method_section(layout: Layout) -> str:
    model = f"`{layout.headline_model}`" if layout.headline_model else "none"
    return f"""## Method

Every number on this page is computed from result files by `eval/report/build_report.py`. The
files are written by `eval/run.py` and `eval/chaos/chaos.py`, and nothing here is edited by hand.
The headline model is {model}.

A comparison uses one model, so a run on another model appears only under "Runs used" unless it
is the partner of an ablation.

* Resolve rate: resolved over attempted. Every started task counts, and so does a task that ended
  in an error, a timeout or without a grade.
* 95% interval: the Wilson score interval on the resolve rate.
* Paired test: McNemar's exact test, two-sided, on the tasks that both runs attempted. The
  discordant pairs are b, the tasks only the first config solved, and c, the tasks only the
  second solved. A gap whose p is above 0.05 is called inconclusive.
* Dollars per resolved: the cost of every attempted task over the number resolved.
* Median wall time: over the resolved tasks. Tokens per task: input, output and cache reads. Cache
  ratio: cache reads over input plus cache reads, per task, then the median.
* Intervention needed: the task paused at least once, so a person would have had to answer.
  Silent failure: the task ended without a pull request, a stuck report or a budget comment.
* Compaction savings: per task, one minus the input tokens with compaction over the input tokens
  without it, then the median and the quartiles. Input tokens are new input plus cache reads.
"""


# ---------- tables 1 and 2 ----------
def swebench_section(layout: Layout) -> str:
    rows = labelled(layout.swebench)
    if not rows:
        return f"## Table 1: {SET_TITLES['swebench']}\n\n" + none_given("on the SWE-bench subset")
    size = max(run.attempted for _, run in rows)
    body = []
    for label, run in rows:
        single = run.config == "single_shot"
        body.append(
            [
                label,
                *rate_cells(run),
                money(cost_per_resolved(run)),
                minutes(median_wall_s(run)),
                whole(statistics.median(tokens_of(r) for r in run.results)),
                "n/a" if single else percent(statistics.median(r.cache_ratio for r in run.results)),
            ]
        )
    header = [
        "Config",
        "Resolved",
        "Rate",
        "95% CI",
        "$ / resolved",
        "Median wall time",
        "Median tokens / task",
        "Cache ratio",
    ]
    return (
        f"## Table 1: {SET_TITLES['swebench']} (n={size})\n\n"
        f"Model: `{rows[0][1].model}`.\n\n"
        + table(header, body)
        + "\n\n"
        + ungraded_note(rows)
        + mcnemar_text(layout.swebench)
    )


def mcnemar_text(by_config: dict[str, list[EvalRun]]) -> str:
    """The paired test of the first full run against the first naive run, as one paragraph."""
    if not by_config.get("full") or not by_config.get("naive"):
        return "No McNemar test: it needs a full run and a naive run on the same set.\n"
    full, naive = by_config["full"][0], by_config["naive"][0]
    shared = [t for t in full.task_ids if t in set(naive.task_ids)]
    b = sum(1 for t in shared if full.solved(t) and not naive.solved(t))
    c = sum(1 for t in shared if naive.solved(t) and not full.solved(t))
    p = stats.mcnemar_exact(b, c)
    verdict = (
        "The difference is significant at 0.05."
        if p < SIGNIFICANCE
        else "The gap is inconclusive at 0.05; read it with the intervals above."
    )
    return (
        f"McNemar full vs naive (exact binomial test, two-sided, {len(shared)} paired tasks): "
        f"discordant pairs b = {b}, c = {c}, {p_text(p)}. {verdict}\n"
    )


def custom_section(layout: Layout) -> str:
    rows = labelled(layout.custom)
    title = f"## Table 2: {SET_TITLES['custom']}"
    if not rows:
        return f"{title}\n\n" + none_given("on the custom set")
    size = max(run.attempted for _, run in rows)
    body = []
    for label, run in rows:
        single = run.config == "single_shot"
        needed = sum(1 for r in run.results if r.intervention_needed)
        body.append(
            [
                label,
                *rate_cells(run),
                money(cost_per_resolved(run)),
                "n/a" if single else f"{percent(needed / run.attempted)} ({needed})",
                "n/a" if single else str(run.silent_failures),
            ]
        )
    header = [
        "Config",
        "Resolved",
        "Rate",
        "95% CI",
        "$ / resolved",
        "Intervention-needed",
        "Silent failures",
    ]
    return (
        f"{title} (n={size})\n\n"
        f"Model: `{rows[0][1].model}`.\n\n"
        + table(header, body)
        + "\n\n"
        + ungraded_note(rows)
        + mcnemar_text(layout.custom)
    )


def chart_line(name: str, alt: str, refs: dict[str, str] | None) -> str:
    """The image of a chart, or the reason there is none."""
    if refs is None:
        return (
            "The charts need matplotlib, which is in the eval extra. Run `uv sync --extra eval` "
            "and build the page again.\n"
        )
    return f"![{alt}]({refs[name]})\n"


def rates_chart_section(refs: dict[str, str] | None) -> str:
    return "Resolve rate with its 95% interval, for each config and set:\n\n" + chart_line(
        "resolve-rates.png", "Resolve rates with 95% intervals", refs
    )


# ---------- tokens ----------
def tokens_section(layout: Layout, refs: dict[str, str] | None) -> str:
    rows = [
        (SET_TITLES[run.task_set], label, run)
        for set_runs in (layout.swebench, layout.custom)
        for label, run in labelled(set_runs)
    ]
    if not rows:
        return "## Tokens per task\n\n" + none_given("on a headline set")
    body = []
    for set_title, label, run in rows:
        values = [float(tokens_of(r)) for r in run.results]
        median, low, high = stats.median_iqr(values)
        body.append(
            [
                set_title,
                label,
                whole(median),
                f"{whole(low)} to {whole(high)}",
                money(statistics.median(r.cost_usd for r in run.results)),
            ]
        )
    return (
        "## Tokens per task\n\n"
        + chart_line("tokens-per-task.png", "Tokens per task, box plot by config", refs)
        + "\n"
        + table(["Set", "Config", "Median tokens", "Quartiles", "Median cost / task"], body)
        + "\n"
    )


# ---------- table 3 ----------
def pair_rows(pairing: Pairing) -> list[Pair]:
    with_by = {r.task_id: r for r in pairing.with_run.results}
    without_by = {r.task_id: r for r in pairing.without_run.results}
    return [(with_by[t], without_by[t]) for t in pairing.task_ids]


def resolved_row(name: str, set_cell: str, pairs: Sequence[Pair]) -> list[str]:
    won = sum(1 for with_, _ in pairs if with_.resolved)
    lost = sum(1 for _, without in pairs if without.resolved)
    return [
        name,
        set_cell,
        "Resolved",
        f"{won} / {len(pairs)}",
        f"{lost} / {len(pairs)}",
        f"{won - lost:+d}",
    ]


def savings(pairs: Sequence[Pair]) -> list[float]:
    """Per task, 1 - input tokens with / input tokens without. A task with no input is skipped."""
    return [
        1 - input_tokens_of(with_) / input_tokens_of(without)
        for with_, without in pairs
        if input_tokens_of(without)
    ]


def cost_per_resolved_of(pairs: Sequence[Pair], side: int) -> float | None:
    chosen = [p[side] for p in pairs]
    done = sum(1 for r in chosen if r.resolved)
    return sum(r.cost_usd for r in chosen) / done if done else None


def ablation_section(layout: Layout) -> str:
    title = "## Table 3: Ablations"
    if not layout.compaction and not layout.memory:
        return f"{title}\n\n" + none_given("with a compaction or memory ablation")
    body: list[list[str]] = []
    notes: list[str] = []
    for pairing in layout.compaction:
        pairs = pair_rows(pairing)
        set_cell = f"{SET_TITLES[pairing.with_run.task_set]} ({len(pairs)} shared tasks)"
        body.append(resolved_row("Compaction", set_cell, pairs))
        gains = savings(pairs)
        with_median = statistics.median(input_tokens_of(w) for w, _ in pairs)
        without_median = statistics.median(input_tokens_of(o) for _, o in pairs)
        saved = stats.median_iqr(gains) if gains else None
        body.append(
            [
                "Compaction",
                set_cell,
                "Median input tokens / task",
                whole(with_median),
                whole(without_median),
                f"{percent(saved[0])} saved" if saved else "n/a",
            ]
        )
        if saved:
            notes.append(
                f"Compaction savings per task ({pairing.with_run.run_id} against "
                f"{pairing.without_run.run_id}): median {percent(saved[0])}, quartiles "
                f"{percent(saved[1])} to {percent(saved[2])}."
            )
    for pairing in layout.memory:
        pairs = pair_rows(pairing)
        set_cell = f"{SET_TITLES[pairing.with_run.task_set]} ({len(pairs)} shared tasks)"
        body.append(resolved_row("Memory", set_cell, pairs))
        turns_with = statistics.median(w.turns for w, _ in pairs)
        turns_without = statistics.median(o.turns for _, o in pairs)
        body.append(
            [
                "Memory",
                set_cell,
                "Median turns / task",
                whole(turns_with),
                whole(turns_without),
                f"{turns_with - turns_without:+g}",
            ]
        )
        body.append(
            [
                "Memory",
                set_cell,
                "$ / resolved",
                money(cost_per_resolved_of(pairs, 0)),
                money(cost_per_resolved_of(pairs, 1)),
                "",
            ]
        )
    header = ["Comparison", "Set", "Metric", "With", "Without", "Paired delta"]
    text = f"{title}\n\n" + table(header, body) + "\n"
    if layout.memory:
        text += (
            "\nThe result files do not record the turn of the first green test, so the memory "
            "comparison uses turns per task.\n"
        )
    if notes:
        text += "\n" + "\n\n".join(notes) + "\n"
    return text


# ---------- table 4 ----------
def runs_hit(found: Sequence[RunResult], kind: str) -> list[RunResult]:
    return [r for r in found if any(k.type == kind and k.landed for k in r.kills)]


def latency_cell(stat_median: float | None, stat_p95: float | None) -> str:
    return f"{seconds(stat_median)} / {seconds(stat_p95)}"


def chaos_table(summary: ChaosSummary) -> str:
    body = []
    for kind in chaos_plan.KILL_TYPES:
        hit = runs_hit(summary.results, kind)
        latency = summary.resume_latency_by_type[kind]
        body.append(
            [
                CHAOS_LABELS[kind],
                f"{summary.kills_landed[kind]} / {summary.kills_planned[kind]}",
                str(len(hit)),
                f"{sum(r.completed for r in hit)} / {len(hit)}",
                f"{sum(r.diff_equals_reference for r in hit)} / {len(hit)}",
                str(sum(r.duplicate_side_effects for r in hit)),
                latency_cell(latency.median_s, latency.p95_s),
            ]
        )
    body.append(
        [
            "**All**",
            f"{sum(summary.kills_landed.values())} / {sum(summary.kills_planned.values())}",
            str(summary.runs),
            f"{summary.completed} / {summary.runs}",
            f"{summary.diff_matches} / {summary.completed}",
            str(summary.duplicate_side_effects),
            latency_cell(summary.resume_latency.median_s, summary.resume_latency.p95_s),
        ]
    )
    header = [
        "Kill type",
        "Kills landed / planned",
        "Runs hit",
        "Completed",
        "Diff equal",
        "Duplicate side effects",
        "Resume p50 / p95",
    ]
    return table(header, body)


def chaos_failures(summary: ChaosSummary) -> str:
    """Every run that did not survive, with what went wrong. The plan reports failures too."""
    failed = [r for r in summary.results if not r.survived]
    if not failed:
        return "Every run survived.\n"
    lines = []
    for r in failed:
        why = "; ".join(r.problems) or _unsurvived_reason(r)
        lines.append(f"* Run {r.run} ({r.task_id}): {cell(why)}")
    return "Runs that did not survive:\n\n" + "\n".join(lines) + "\n"


def _unsurvived_reason(run: RunResult) -> str:
    if run.kills_planned and not any(k.landed for k in run.kills):
        return "no planned kill landed before the run ended"
    return "side effects differ from the expected ones"


def chaos_section(chaos: Sequence[ChaosRun]) -> str:
    title = "## Table 4: Chaos"
    if not chaos:
        return f"{title}\n\n" + none_given("of the chaos suite")
    parts = [title]
    for run in chaos:
        s = run.summary
        total_cost = sum(r.cost_usd for r in s.results)
        overhead = s.extra_cost_usd / total_cost if total_cost else 0.0
        parts.append(
            f"Suite `{run.run_id}`: {s.runs} runs, seed {s.seed}, git sha `{s.git_sha[:12]}`. "
            f"Model answers: {_recordings(s)}.\n\n"
            + chaos_table(s)
            + f"\n\nSurvived (completed, same diff, no duplicate or missing side effect, at least "
            f"one kill landed): {s.survived} / {s.runs}. Missing side effects: "
            f"{s.missing_side_effects}.\n\n"
            f"Retry cost overhead across chaos runs: {percent(overhead)} "
            f"({money(s.extra_cost_usd)} of {money(total_cost)}, from "
            f"{s.extra_ledger_rows} repeated ledger rows).\n\n" + chaos_failures(s)
        )
    return "\n".join(parts)


def _recordings(summary: ChaosSummary) -> str:
    kinds = sorted(set(summary.recordings.values()))
    return ", ".join(kinds) if kinds else "not recorded"


# ---------- table 5 ----------
def reason_for(result: results.TaskResult) -> str:
    """One line on why a task is not resolved: the stuck report, else the grader, else the end."""
    if result.stuck_reasons:
        reason = "stuck: " + "; ".join(result.stuck_reasons)
    else:
        reason = result.grading.strip() or result.outcome_summary
    flat = cell(reason)
    return flat if len(flat) <= REASON_LIMIT else flat[: REASON_LIMIT - 3].rstrip() + "..."


def unresolved_section(layout: Layout) -> str:
    title = "## Table 5: Unresolved tasks"
    full_runs = [
        (label, run)
        for by_config in (layout.swebench, layout.custom)
        for label, run in labelled({"full": by_config.get("full", [])})
    ]
    if not full_runs:
        return f"{title}\n\n" + none_given("of the full config on a headline set")
    body = []
    for _, run in full_runs:
        for r in run.results:
            if r.resolved is True:
                continue
            outcome = r.outcome if r.resolved is False else f"{r.outcome}, not graded"
            body.append([r.task_id, run.run_id, outcome, reason_for(r)])
    if not body:
        return f"{title}\n\nEvery task of the full config was resolved.\n"
    return (
        f"{title}\n\nTasks of the full config that were not resolved, with the reason the "
        "result file gives.\n\n" + table(["Task", "Run", "Outcome", "Reason"], body) + "\n"
    )


# ---------- runs used ----------
def runs_section(
    eval_runs: Sequence[EvalRun], chaos: Sequence[ChaosRun], order: Sequence[str], git_sha: str
) -> str:
    rows: dict[str, list[str]] = {}
    for run in eval_runs:
        variant = [
            f"memory {run.memory}" if run.memory != "on" else "",
            f"context {run.context_mode}" if run.context_mode not in (None, "full") else "",
        ]
        rows[run.run_id] = [
            f"`{run.run_id}`",
            run.task_set,
            ", ".join([run.config, *[v for v in variant if v]]),
            f"`{run.model}`",
            "scripted" if run.scripted else "model",
            f"{run.resolved} / {run.attempted}",
            f"`{run.first.config_hash[:12]}`",
            f"`{run.first.git_sha[:12]}`",
        ]
    for suite in chaos:
        rows[suite.run_id] = [
            f"`{suite.run_id}`",
            "chaos",
            "full",
            _recordings(suite.summary),
            "scripted" if suite.scripted else "model",
            f"{suite.summary.completed} / {suite.summary.runs}",
            "n/a",
            f"`{suite.summary.git_sha[:12]}`",
        ]
    header = ["Run", "Set", "Config", "Model", "Answers", "Resolved", "Config hash", "Git sha"]
    ids = ", ".join(order)
    return (
        "## Runs used\n\n"
        + table(header, [rows[run_id] for run_id in order])
        + f"\n\nBuilt from runs: {ids}.\n\nGit sha of the report build: `{git_sha}`.\n"
    )
