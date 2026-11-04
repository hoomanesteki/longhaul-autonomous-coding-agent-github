# ADR-0008: Poll GitHub on a schedule, do not take webhooks

- Status: Accepted
- Date: 2026-11-04

## Context

A maintainer labels an issue `longhaul`, and within a minute a task should exist for it. Longhaul has to learn about the label somehow.

The facts that decide it:

- GitHub can push the event as a webhook. That needs an endpoint on the public internet, a tunnel for a laptop, a shared secret to verify each delivery, and a plan for deliveries that arrive while the receiver is down. GitHub does not resend a failed delivery by itself.
- Longhaul is background work. A task takes minutes to hours, so a start that is up to a minute late costs nothing a person would notice.
- The worker already holds the GitHub token and a Temporal client (ADR-0004, ADR-0012). Reading a list of issues needs nothing else.
- The rate limit of a token is 5,000 requests an hour. One list call per repo per minute is 60 an hour for each repo.
- Starting a task must be safe to repeat. The poll runs 1,440 times a day, sees the same labeled issue every time until its task ends, and may die at any point of a run.

## Decision

**Poll every minute with a Temporal Schedule, and take no webhooks in v1.** The Schedule `longhaul-poller` starts `PollerWorkflow` every `poller.interval_seconds` (60 in the example config). The workflow runs one activity, `poll_once`, and returns the counts it gets back. It stays that small on purpose: the repos, the label, the database and the calls to GitHub and Temporal are all in the activity, so the workflow never needs a patch (failure mode F13).

**The Schedule.** `longhaul poller install` creates it, with these settings:

| Setting | Value | Why |
|---|---|---|
| Interval | `poller.interval_seconds` | One number in the config. |
| Overlap policy | `SKIP` | A tick that comes while a poll is open is dropped. A poll is a full scan, so a dropped tick loses nothing, and two polls never race over the same issue. The spike measured it: with a 1 s interval and a 2.5 s poll, 12 s gave 4 runs and 8 skipped ticks, and never two polls at once. |
| Catch-up window | 2 minutes | After an outage the Schedule runs the ticks of the last two minutes and drops the rest. One poll is all a recovery needs. |
| Run timeout | 15 minutes | All attempts of `poll_once` at their longest come to under 10 minutes. A run that lasts longer has lost its worker, and `SKIP` would drop every tick for as long as it stayed open. |

Install can be run again at any time. It creates the Schedule when it is missing. When it exists, install compares the action, the interval, the overlap policy and the catch-up window with what it wants, and sends an update only if they differ. The pause and the note of the Schedule are not compared, so a Schedule that an operator paused stays paused when install changes its interval. Run twice with the same config, it changes nothing, and `last_updated_at` does not move. `longhaul poller pause`, `resume` and `trigger` act on the Schedule. A trigger works on a paused Schedule, and it does nothing while a poll is open, like any tick.

**What a poll does.** For each repo of the config, in order:

1. Skip it unless it is allowlisted (`is_allowlisted`) and is not a `local:` path. The table `repo_settings` is brought in line with the config first (`sync_repo_settings`).
2. List the open issues that carry the label `poller.label`, oldest first. Pull requests are left out.
3. For each issue whose task id has no row in `tasks`, build the `TaskSpec` (`issue_to_spec`) and start `TaskWorkflow` with `start_task`. Its comments are filtered by the write permission of their authors, and a permission that GitHub will not tell counts as none.
4. Write the `tasks` row after the start.

**Three guards against a second task.** All three are needed, and each covers a case the others do not:

- The `tasks` row. It outlives the workflow. Temporal forgets a closed workflow when its retention runs out, one day on the dev server, and the label usually stays on the issue for ever. Without the row, the poll would run a finished task again the next day.
- `REJECT_DUPLICATE` on the workflow id, which is the task id (ADR-0004 appendix section 6). It covers the window between a start and the row, and two pollers in different processes. `WorkflowAlreadyStartedError` counts as done, and the row is written then.
- The overlap policy, which keeps two polls from running at once.

**The row is written after the start.** The other order loses a task when the poll dies in between: the row would say the task is known, the next poll would skip it, and no workflow would exist. In this order the worst case is a workflow with no row, and the next poll repairs it. A row, once it exists, is never put back to `queued`: a poll that meets it again updates only the title and the settings, which come from the issue.

**The poller reads from GitHub and writes one thing.** It sets no label and posts no "Picked up" comment. The task owns those side effects, and its first activity writes them with idempotency keys (LH-306, invariant I4). The one write of the poller is the reply to a command from someone who may not steer a task (see the next section). `start_task` opens a new trace for each task, so a task does not join the trace of the poll that started it.

**Commands in the comments (LH-308).** After the new tasks are started, the poll reads the new comments of every open labeled issue that has a task, and carries out the slash commands in them (`/longhaul approve`, `reject`, `hint`, `budget`, `cancel`, `status` and `retry`, parsed by `github/commands.py`). The rules, in `durable/activities/steering.py`:

- **Who.** The author needs admin, maintain or write permission, read from the collaborator permission endpoint each time a poll meets a command from them. Unknown means no. A person without it gets one reply for each task, posted with an idempotency key that holds their login, "Only maintainers can steer this task", and the command is dropped. This is the one place where the poller writes, because no task owns the answer to a stranger. Comments of bots and comments with our marker are never commands.
- **Which comments.** Each `tasks` row keeps a cursor: the time and the id of the newest comment handled (`last_comment_at` and `last_comment_id`, migration 0012). The time is for the `since` of the request, and the id decides, because GitHub stamps a comment to the second and two comments of one second could not be told apart by time. A new task starts at the newest comment its spec was built from, so a comment from before the label is never a command.
- **Once.** The cursor moves after the signal was sent, comment by comment. A poll that fails halfway and is retried by Temporal sends the commands it had not sent. A worker that dies between the signal and the write of the cursor sends that one command again. Of the six signals only `budget` and `hint` do harm when repeated, a second addition and a second hint, and the next status shows both. Signalling twice with the same Temporal request id would close the window, but the SDK does not expose the id and the raw call would bypass the data converter and the interceptors, so it is not worth the price.
- **Ended tasks.** A signal to a closed workflow fails with NOT_FOUND, and the command is dropped. `retry` is the exception: on a task that has ended it starts the attempt `<task id>-a2` (`-a3` and so on) from the issue as it is now, writing the row after the start as for any task. On a task that still runs it does nothing. Later commands go to the latest attempt.
- **Failures.** An issue that fails does not stop the next one. The poll fails after the last issue, like a repo that cannot be read.

`status` is the signal `report_status`. The task posts the answer itself, through `post_comment` with the key `status:<n>`, where n counts the requests in the workflow state and in the memo across a Continue-As-New.

**Failures.** A repo that GitHub cannot read is logged, and the poll goes on with the next repo. At the end the activity fails, so a broken repo shows as a failed poll in the Temporal UI for as long as it is broken, without stopping the repos that work.

| Failure | Error type | Retried |
|---|---|---|
| 5xx, network, rate limit | `GitHubRetryable`, with the wait GitHub asked for | Yes, 3 attempts, backoff 5 s to 30 s. A longer wait than 30 s is cut to 30 s, so all attempts fit in the 15 minutes of a run. The next tick is a retry too, so the backoff is short. |
| A repo that does not exist, a token without access, an issue without a usable number | `GitHubFatal` | No. |
| No `LONGHAUL_GITHUB_TOKEN` on the worker, or an allowlist that `intake.py` refuses | `PollerConfigError` | No. |
| Temporal unreachable | `RPCError` | Yes. |

When both kinds fail in one poll, the poll is retried: the retry finishes the repos that can still be read.

**Where it runs.** `poll_once` is an activity on the worker, with the GitHub client and the task queue of that worker. A worker without a token still starts, and registers the activity. A poll on it fails with the name of the setting. A task starts on the queue of the worker that polled.

## Consequences

Easier:

- No public endpoint, no tunnel, no webhook secret and no signature check. Longhaul works from a laptop behind a firewall.
- A missed event cannot lose an issue. The label is state, and the next poll reads it.
- The server owns the clock. Every poll is a workflow in the Temporal UI, with its counts, its failure and its trace. Pause, resume and trigger come with the Schedule.
- Another repo is one more entry in the config and one more list call per minute.

Harder:

- A task starts up to one interval after the label, 60 s with the example config. The latency is the price of the design.
- Every tick lists every labeled open issue of every repo. That is one call per repo per minute, 60 an hour for each repo, and a page of 100 issues is one call. Ten repos use about 12 percent of a token's hourly limit. A new issue costs two calls more: its comments, and the permission of each commenter.
- The poller does not see an issue that gets the label and loses it within one interval. That is intended: nobody asked for a task.
- Closed runs of the poller stay in the namespace until retention removes them, 1,440 a day. They are small, one activity each.

Watch from now on:

- The number of repos against the rate limit. If it gets close, send `If-None-Match` with the last ETag of each list: GitHub answers 304 and does not count it against the limit.
- Comment commands (LH-308) make the poll read the new comments of every open labeled issue that has a task, one call for each. With 20 such issues that is 1,200 calls an hour, about a quarter of the limit of a token, besides the list calls. A command also costs one permission call for its author. The `If-None-Match` idea above would help here too, by asking for the issue list once and reading comments only on the issues whose `updated_at` moved.
- The `tasks` table is the long term record of what was started. Its status and phase are written by the stories that follow the task; the poller writes `active` and `queued` once.

## Alternatives

- **Webhooks.** Lowest latency, and no use of the rate limit. They need a public endpoint or a tunnel, a secret, and a way to recover the events that arrive while the receiver is down, which would mean polling anyway. Rejected for v1. If latency ever matters, a webhook can call the same `start_task` path, and the guards above make the two safe together.
- **A loop inside the worker.** An `asyncio` task that sleeps and polls. It is not durable, two workers would poll twice, and it leaves no history to look at.
- **A long-running workflow that sleeps between polls.** It works, but its history grows by a few events a minute and needs Continue-As-New, and somebody has to start it again if it ever ends. A Schedule has no such state, and the server restarts it.
- **The Events API.** It shows what changed since the last look and costs less per poll. GitHub says its events can lag by anything from 30 seconds to six hours, so it is neither fast nor complete enough to replace a scan. Rejected for v1.
- **`BUFFER_ONE` instead of `SKIP`.** It would start a second poll right after the first ends. That poll reads what the first one just read.
