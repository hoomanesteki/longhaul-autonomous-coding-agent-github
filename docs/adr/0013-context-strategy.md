# ADR-0013: Two-tier context strategy

- Status: Accepted
- Date: 2026-11-04

## Context

A task can run for 150 turns, and every request carries the whole transcript again. A noisy test run prints ten thousand lines, and a model that reads three of them in a row has used a good part of a 200,000 token window on text it will never look at again. The provider rejects a request that is over its limit with a 400, and a 400 is never retried (ADR-0003), so a task that fills its window ends. Cost grows with it, because each turn is billed for the prefix it re-sends.

Four things shape the answer:

- The workflow is deterministic and cannot read a blob or count tokens. The transcript is a blob (ADR-0005), so anything that edits it runs in an activity.
- The Messages API answers 400 to a `tool_use` with no `tool_result` and to a `tool_result` with no `tool_use`. A cut in the middle of a tool round ends the task.
- The prompt cache holds the prefix of the request. Every edit to an old message gives up the cache from that message on (ADR-0003, D7).
- Reports need the whole story of a task, and the model does not.

LH-401 to LH-403 built the parts: a token meter and budget, output handles for long tool results, and structured compaction. LH-404 wires them into the workflow. The settings are in the `context` section of the config: `window_tokens` (200,000), `compact_at` (0.60), `hard_limit` (0.85), `keep_recent_turns` (6), `elide_tool_outputs_after_turns` (4) and `tool_output_max_tokens` (4,000).

## Decision

**Tier 1: elide old tool outputs.** Before each model call, inside `llm_turn`, a tool result older than `elide_tool_outputs_after_turns` turns becomes `[elided: <tool> output, <n> lines, handle <h>]`. The handle is the one `tool_exec` stored the full text under, so `read_output` can still return it. The results of the last K turns stay whole. The function is `context.evict.elide_old_outputs`. It needs no model, and the stub depends only on the tool name, the text and the call id, so it is the same bytes on every later call. One result ages out per turn, which changes the request at one place, K turns back, and nowhere before it. Results of control tools and results shorter than their stub are left alone.

**Tier 2: compact into progress notes.** Past `compact_at` of the window, the older turns become `ProgressNotes` (LH-403), and the view is the first message with the notes, then the last `keep_recent_turns` turns. The cut is always a turn boundary where every `tool_use` has its `tool_result`. The `compact` activity does this with the compactor model (10 minutes, 5 attempts, as in plan section 3.7).

**Two blobs, a log and a view.** The view is what the model sees, and it is the transcript ref the workflow already carries. The log is the whole transcript with nothing elided and nothing compacted, for reports. `CarryState` has one transcript ref and does not change, so the log is not a blob that a ref points at. It is a set of small blobs at `tasks/<task_id>/log/<turn>/reply.json` and `.../results.json`, named by the task and the turn alone. `llm_turn` writes the reply part (the hints it showed, then the reply) and `append_results` writes the results part. That needs no state in the workflow, survives Continue-As-New, and cannot be duplicated by a retried activity, which writes the same key with the same content. `context.log.read_log` joins the parts back into one transcript.

**The projection is made where the tokens are.** The workflow cannot count, so `llm_turn` does, with a `TokenMeter` for the task that learns characters per token from the replies, and returns the result in `LlmTurnOutput`:

- `request_tokens` is the projection of the request it sent. Close to the thresholds (from 0.05 under `compact_at` up to `hard_limit`) the provider's `count_tokens` replaces the estimate.
- `context_decision` is `ok`, `compact` or `force` for the request after this one. That request has one more result elided, and holds the results of the calls just made. They are not known yet, so each call reserves `tool_output_max_tokens`, except `finish`.

The workflow acts on the decision when the turn is over and its results are in the view, so a hand-over to a new run carries the compacted view. A `compact` decision calls the activity with `keep_recent_turns`. A `force` decision keeps the last two turns.

**A request over the hard limit is not sent.** Before the call, `llm_turn` projects the request it is about to send. At `hard_limit` of the window or above it fails with `context_too_long`, the same error the provider raises when it rejects a prompt. This is the guarantee that no request is over `hard_limit * window`: the projection after the previous turn is early warning, and this check is the limit.

**F6: compact down to two turns and ask once more.** The workflow treats `context_too_long`, from the guard or from the provider, by compacting with `forced=True` (the last two turns and the notes) and asking the same turn again, once. It does not ask again when the compaction folded nothing, because the view would be the same. A second overflow ends the run with "context limit", as it did before this story.

**Every compactor call has its own ledger row (I6).** `compact()` reports each billed call to an `on_billed` callback as soon as it is known, and the activity writes a row for it. That covers a reply that came back, a forced call that was refused after it was billed, and a call that failed with a usage, whether `compact()` then succeeds or not. The error that `compact()` raises still carries the summed usage, but a sum is not what the ledger needs. Haiku bills a call at a higher rate when its prompt is over 100,000 tokens, so two calls that are each under the line can add up to a sum that is over it, and a row priced from the sum would charge both at the dearer rate.

**The notes reach the model in the first message.** `compact.apply` writes them after the task text of the first user message, and the system prompt does not change (D7). `LlmTurnInput.notes` is the workflow's own copy and is handed back to the compact activity. LH-405 lays out the cache breakpoints around this.

**Hints survive a compaction.** `show_hints` shows a hint again when its text is not in the transcript. The compact activity moves the hint texts of the folded turns into the first message, where they stay through later compactions, so a compaction does not make every hint come back.

**Switching the tiers off (LH-406).** `context.mode` is `full`, `no_compaction` (tier 1 only) or `none`, and a run can set its own with `longhaul run --context-mode`. The evaluation needs the two ablations to measure what each tier buys, and a switch in the product beats a second code path in the harness. The guard in front of the call is not a tier, so it stays on in every mode. Without tier 2 there is nothing to answer it with, so the run ends with `error` and the summary `context limit` and does not force a compaction. A forced compaction in `no_compaction` would make the ablation measure something other than tier 1.

The mode reaches the two places that act on it. `llm_turn` and `compact` hold the config, so they resolve it there, and the mode of a run travels in the workflow memo and in their inputs, like the script of a scripted task. The workflow reads the memo only to skip a `compact` call that would do nothing. A run that asks for no mode has no memo entry, and then `compact` answers `unchanged` in a mode without compaction, which the workflow already reads as nothing to fold.

The totals of a task (`tasks.input_tokens_total`, `output_tokens_total`, `cache_read_total`, `compactions`, `max_fraction`) are written by activities and are never counters. The token totals are the sum of the ledger rows, the compactions are the number of different `compaction` events, and the share of the window is a maximum. Each is the same after an activity runs twice. `input_tokens_total` includes cache writes, so that the three token totals do not overlap and the plan's formulas (tokens per task, cache ratio) can add and divide them.

## Consequences

**Easier**

- A task can run far past its window. In the test with a window of 8,000 tokens, a task of 60 turns is several windows long by its log, and the largest request is about 4,700 tokens against a hard limit of 6,800, with four compactions.
- The report has the whole transcript, and the model has only what fits. Neither depends on the other.
- A forced compaction and a provider rejection take one path.
- The compaction event carries the tokens before and after, so the numbers for Phase 9 come from the run itself.

**Harder**

- Each turn changes the request at one place, K turns back, so the cache re-writes the last K turns every turn. A compaction re-writes everything after the first message. Both are the price of a bounded window, and LH-406 and LH-405 measure them.
- Tier 1 shows the model a stub where it had the text. It has to call `read_output` to get the text back, and the prompt tells it how.
- The log is read by probing keys, one `get` for each part, and has no hash to check the bytes against, since nothing keeps the refs. Each part is parsed as messages, so damage is an error, and a report is the only reader.
- The reserve for the results to come makes the proactive decision conservative. A turn with several calls compacts a little early. The guard in front of the call stays the limit either way.
- A worker that has not seen a task yet counts with the default 3.5 characters per token until its first reply. A wrong count near the hard limit ends in the provider's own rejection, which F6 handles.
- The in-process loop (`run_in_process`) does not compact. It ends a run at the first overflow, as before. The parity test compares the two for runs that stay under the thresholds.

**To watch**

- `tool_output_max_tokens` as the reserve is a cap, and most results are shorter. If compactions come too early in a real run, the reserve is the number to look at.
- A compactor that fails after its retries fails the workflow, like any activity. Phase 7 decides how a provider outage pauses a task (F5), and compaction follows.
- If the API starts to require thinking blocks to be replayed, tier 1 and keep-tail compaction need the follow-up in ADR-0003.

## Alternatives

- **Compact inside `llm_turn`.** One activity less, but a model call with a ten minute budget would sit inside another, the workflow would not see the compaction as an event, and it could not be retried on its own. A separate activity is the unit of durability here (ADR-0003).
- **Count tokens in the workflow.** Not possible: the transcript is a blob, and workflow code reads no blob. Passing the text through the history would break the 256 KB cap (ADR-0005).
- **One log blob that grows.** Every step would read and write the whole log, and a retried step could not tell whether its append had already happened. A part per step is written once.
- **A log ref in `CarryState`.** It needs a contracts change, and a reviewer sign-off, for something that a key derived from the task id does as well.
- **Elide at request time only.** The stored view would keep the full text, so the next request would elide it again, and the view would grow without bound. Storing the elided view keeps it bounded and makes the stub a fact of the transcript.
- **Summarize in free text (simple compaction).** Free text drifts and drops specifics. Fields keep file paths, failing tests and what was tried, and make the merge with an earlier round mechanical (LH-403).
- **Sum the compactor calls into one ledger row.** Simple, and wrong whenever the calls are on different sides of a price line.
- **Drop the exact count and trust the estimate.** The estimate is within about 10 percent after a few replies, which is enough for the proactive decision, but the guard at the hard limit is where the invariant is enforced, so near it the provider is asked.
