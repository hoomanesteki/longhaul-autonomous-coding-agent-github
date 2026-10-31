# Architecture

Longhaul runs a coding agent as a Temporal workflow. The workflow is pure and
only schedules activities. Activities do the I/O: calling the model, running
tools in a sandbox, writing checkpoints and talking to GitHub.

All pieces share one module of types, `src/longhaul/contracts.py`. It has no
I/O and imports only the standard library and pydantic. Vendor shapes, such as
the Anthropic Messages API format, live in `src/longhaul/agent/render.py`.

## Contracts changelog

Every change to `contracts.py` gets an entry here, with the new
`contracts_version` and the reason. Changes also need a note in the PR.

| Version | Change |
|---|---|
| 1 | First version (LH-101). Records, messages and blocks, provider errors, task and plan types, carry state, and the protocols for providers, sandboxes, blobs, checkpoints, memory and GitHub. `Budgets` and `RepoSettings` moved here from `settings.py`. |
| 2 | Reviewer sign-off (LH-198). Stop reasons `refusal`, `pause_turn` and `model_context_window_exceeded`, with `LLMResponse.raw_stop_reason` and `LLMResponse.stop_details` (`StopDetails`). `CallOptions` (`effort`, `force_tool`, `output_schema`) and an `options` argument on `LLMProvider.complete()` and `count_tokens()`. `ToolSpec.strict`. `LLMResponse.request_id` and `LLMResponse.dropped_thinking_blocks`. `ProviderError.usage`, so a call that was billed and then failed can still be ledgered. `CarryState.started_at` must carry a timezone and is stored in UTC. Block `type` tags are written in every dump, also with `exclude_unset` and `exclude_defaults`. |

### Version 2 decisions

These are the choices behind the version 2 row. Two of them are things the contracts do not do: they carry no thinking blocks, and the system prompt stays a string.

- **Thinking blocks are not in the contracts.** The Anthropic provider (LH-104) drops `thinking` and `redacted_thinking` blocks when it parses a response, and counts them in `dropped_thinking_blocks`. Elision and compaction rewrite history, and a rewrite would invalidate any thinking blocks that were sent back with it. The cost is that the model loses its earlier reasoning on each turn. ADR-0003 records it. The live check S1 is meant to confirm that the API accepts the stripped loop. If it does not, the contracts gain thinking blocks then.
- **A forced tool choice is a request, not a promise.** `claude-sonnet-5-5` rejects a forced `tool_choice` with a 400. So `CallOptions.force_tool` may be refused: the Anthropic provider (LH-104) raises `INVALID_REQUEST` before any HTTP call, and the OpenAI provider maps it to a function `tool_choice`. A caller that needs JSON that parses uses `output_schema`, or a `strict` tool with `tool_choice` left on auto and a prompt instruction.
- **The system prompt stays one string.** It holds the static blocks 1 to 4 of the prompt layout and does not change for the life of a transcript, so its cache breakpoint sits at the end. Changes to the plan and the notes are added as messages.
- **Effort and output limits live in config.** `effort` per role and `loop.max_tokens_retry` are in `config/longhaul.example.yaml`. Effort reaches a provider through `CallOptions.effort` and stays fixed for the life of a transcript. The loop (LH-110) never stores or runs a turn that stopped on `max_tokens`. It retries that turn once at `max_tokens_retry`, and a second stop on `max_tokens` ends the task. The Anthropic provider streams the reply when `max_tokens` is above 16000, which includes the retry.

### Deferred contract changes

The review of `contracts.py` raised more than version 2 holds. Each of these waits for the story that first needs it, so the contracts change once and for a reason that is on the table.

| Change | Story | Why it waits |
|---|---|---|
| `CarryState` keeps the ref of the append-only log blob and the window of recent stuck signals | LH-404, LH-505 | The log and the detector do not exist yet. The window needs `TurnSignal` moved into the contracts from `roles/stuck.py`. |
| `CheckpointStore` attaches the checkpoint volume to a sandbox, and `SandboxManager.create` can mount it | LH-205 | Settled in LH-205 (deferred item contracts-8) without a contracts change. `DockerSandboxManager.create` takes an optional `mounts` argument, a map of host folder to container path, and `GitCheckpointStore` has `mounts(task_id)` and `attach(sandbox, task_id)`. The `SandboxManager` and `CheckpointStore` protocols and `contracts_version` stay as they are, because only the Docker manager can mount a host folder and the worker builds both objects. Revisit if a second manager needs mounts. The reasons are in ADR-0006. |
| `MemoryItem.source_ref`, and a lookup by it | LH-606 | Promotion on a merged PR is the first code that reads it. |
| A size limit on `TaskSpec.body` and `comments`, with a blob ref for the full text | LH-303 | Issue intake is the first code that writes long text. |
| `Message.role == "system"` | LH-405 | Decided together with the prompt layout. |
| `Usage.thinking_tokens` and 1 hour cache writes | LH-701 | Optional. The cost ledger decides whether it needs them. |
