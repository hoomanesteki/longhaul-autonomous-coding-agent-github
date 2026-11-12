# Longhaul case study

This is the outline and the text for the Longhaul page of the portfolio site. It follows the format of the other project cards: a short problem line, one hero metric, one line about the project, and "See the project". Every number comes from a committed file. Where a number does not exist yet, the page says so and names the file it will come from.

## Card

| Part | Text |
|---|---|
| Problem line | Agents crash and loop quietly. |
| Hero metric | 20 of 20 chaos runs ended with a pull request and the same diff as a run with no kill, with 0 duplicate GitHub writes. The model answers were scripted, so this measures durability only. |
| Tag | Long-running agents, open source |
| Title | Longhaul |
| Line | Works an issue for hours. Survives crashes. Asks when stuck. |
| Filters | AI engineering, Software, Machine learning |
| Link | See the project |

The hero metric comes from `eval/results/chaos-2026-11-07/summary.json`. If a run with a live model replaces it before release, the card changes to that run and says which model it used.

## The problem

Teams have a long tail of small, well-specified issues, and coding agents can fix many of them. In practice someone sits in a terminal and watches the agent, because it fails in five predictable ways:

1. Runs die. A laptop sleeps, a container is killed, the API returns 529 for ten minutes, and the agent starts over and pays for the same tokens twice.
2. Context fills up. The agent forgets what it tried and loops.
3. Nobody knows it is stuck. It edits the same three lines for forty minutes while the cost climbs.
4. Side effects repeat. A retry posts the same comment twice or opens a second pull request.
5. Cost is a surprise. There is no budget and no ledger, so the bill is the first signal.

I wanted to know what it takes to make an agent safe to leave alone, and to measure each part of the answer.

## What I built

Longhaul takes a GitHub issue with a label and ends with a pull request or a report of where it got stuck. The system context is in [the README](../README.md#how-it-works) and the other diagrams are in [architecture.md](architecture.md). Four ideas carry it:

- **Durable turns.** Every model turn and every tool call is a Temporal activity with its own timeout, retry policy and heartbeat. The workflow only decides what runs next.
- **Git checkpoints.** After every change the workspace is committed to a bare repo, so a retried tool call starts from a known commit and never from half-applied edits.
- **Two-tier context.** Old tool output becomes a one-line stub with a handle. Past a threshold, old turns become structured progress notes. Both cut at a turn where every tool call has its result.
- **Plan, verify, stuck.** A planner posts a plan a person can approve. A verifier runs the tests before a pull request is ready. A stuck detector pauses the task and asks one question, and hard budgets stop the spend.

Around them sit a hardened Docker sandbox with no credentials inside, a cost ledger with a row for every provider call, a memory that only a merged pull request or a person can write to, OpenTelemetry traces, Prometheus metrics, and a React console to approve a plan, send a hint or add budget.

## Proof

The chaos suite is the one result that exists today. It runs four recorded tasks 20 times, with one to three kills each, of four kinds: the worker, the sandbox, Temporal and Postgres. It counts what each run leaves on a fake GitHub.

| Kill type | Kills landed / planned | Completed | Diff equal | Duplicate side effects | Resume p50 / p95 |
|---|---|---|---|---|---|
| Worker SIGKILL | 10 / 10 | 8 / 8 | 8 / 8 | 0 | 16.6 s / 21.8 s |
| Sandbox kill | 11 / 12 | 10 / 10 | 10 / 10 | 0 | 0.4 s / 22.1 s |
| Temporal restart | 9 / 12 | 7 / 7 | 7 / 7 | 0 | 2.0 s / 10.5 s |
| Postgres restart | 9 / 11 | 6 / 6 | 6 / 6 | 0 | 1.4 s / 34.6 s |
| All | 39 / 45 | 20 / 20 | 20 / 20 | 0 | 2.0 s / 22.1 s |

Two limits go with this table. The model answers were scripted recordings, so it says nothing about how well a model codes. And 18 of the 20 runs count as survived, because in two of them none of the planned kills landed before the run ended.

The comparison with a naive loop and a single-shot baseline on a 40-task SWE-bench Verified subset, the resolve rate with its interval, the dollars per resolved task, and the two ablations (compaction and memory) are pending. They need an API key, so the runs are not made yet. Each of those numbers comes from `docs/results.md` ([results.md](results.md)) after the runs it lists, and this section is rewritten from that page then. Until then I make no claim about them.

## What did not work

These come from the gate reports and the chaos suite, and they are the cases where my first design was wrong.

- **A short heartbeat timeout made live work look dead.** I first gave the chaos runs a 5 second heartbeat timeout so a killed worker would be noticed fast. On a busy machine a healthy activity missed a beat, Temporal started a retry, and the retry ran beside the original. The suite uses 20 seconds, and the cost is that a worker kill shows a median resume latency of 16.6 seconds, most of it waiting.
- **The Temporal dev server stopped answering.** The Temporal dev server keeps its state in one SQLite connection. On a loaded machine a query ran out of time, and from then on every call failed. It showed up in the first try of the full chaos suite. I added a watchdog that restarts the container after 60 seconds of failures and counts each restart in the summary.
- **Budgets covered only the tasks the poller started.** The eval runner and `longhaul run` started tasks with no check, so one ran past its dollar cap. I found it in the review of the Phase 7 gate and fixed it so every way of starting a task enforces its budget.
- **Dropping the model's reasoning has a cost.** I strip thinking blocks when I read a response, because compaction rewrites history and a rewrite would invalidate any thinking blocks sent back with it. The model loses its earlier reasoning on each turn. ADR-0003 records it, and the effect on the resolve rate is something the pending runs will show or not.

## Decisions I would defend

- **Temporal over an SDK loop or a graph library** (ADR-0003, ADR-0004). The unit of durability here is one model turn and one tool call, each with its own retry, timeout and heartbeat. An SDK that owns its loop and transcript cannot be checkpointed between its internal steps. A graph library with a checkpointer would still leave me to build a job runner, durable timers for a 24 hour wait for a hint, schedules, signals and a history view. Temporal gives all of those. The price is the determinism rules in workflow code, which a test enforces.
- **Git micro-checkpoints** (ADR-0006). A commit after every mutating call gives each call a `pre_sha` and a `post_sha`. Before any retry the workspace is reset to `pre_sha`, so a command that is unsafe to run twice cannot leave its mark twice. The chaos test fails when that one line is removed, which I checked at the Phase 2 gate.
- **A strict memory write policy** (ADR-0010). Only approved notes reach a prompt, and approval comes from the default branch, a merged pull request or a person. Work that nobody merged can only make candidates. That stops one bad task from teaching the next one.

## Links

- Repository: https://github.com/hoomanesteki/longhaul-autonomous-coding-agent-github
- Docs site: https://hoomanesteki.github.io/longhaul-autonomous-coding-agent-github/ (not live yet: it goes up once GitHub Pages is turned on, as the runbook's Publishing section says)
- Results and method: [results.md](results.md) and [the chaos method](../eval/chaos/README.md)
- Demo video: not recorded yet. The script is [demo-script.md](demo-script.md).
