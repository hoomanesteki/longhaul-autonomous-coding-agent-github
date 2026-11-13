# ADR-0003: Own agent loop on the Messages API

- Status: Accepted
- Date: 2026-10-30

## Context

Longhaul runs tasks that last hours and must survive crashes, restarts and a sleeping laptop. That makes one model turn and one tool call the unit of durability. Each of them has to be its own Temporal activity, with its own retry policy, timeout, heartbeat and checkpoint, so a failure replays one step and not the whole run.

Three ways to get an agent loop were on the table: the Claude Agent SDK, a LangGraph graph, or our own code on the Messages API. We have shipped with both of the first two. They are good tools for interactive and graph-shaped agents. The facts that matter here:

- The Agent SDK owns its loop and its session transcript. We cannot checkpoint or retry between its internal steps.
- LangGraph with a Postgres checkpointer can checkpoint per node. We would still need a job runner, durable timers (24 hour hint waits), schedules, signals and a history UI. Temporal gives us all of those (ADR-0004).
- CI has no API keys, so the loop must run against a scripted stand-in for the model as easily as against the real one.

## Decision

Write our own agent loop on the Messages API. A thin `LLMProvider` protocol keeps the loop vendor-neutral: one `complete()` call is one turn, and the loop decides when to run tools, compact context or stop. The Anthropic provider is built on the official `anthropic` SDK. A scripted provider serves tests, and an OpenAI-compatible provider is a second backend.

## Consequences

What gets easier:

- Retries, timeouts, heartbeats and checkpoints sit exactly where the failures are, because every turn and every tool call is its own activity.
- The loop is testable without keys: `ScriptedProvider` (and `ReplayProvider` once it lands in Phase 2) stands in for the model in CI.
- A second vendor needs one new provider, not a new loop.
- Every provider call passes through our code, so every call can write a cost ledger row (invariant I6).

What gets harder:

- We own what a framework would otherwise own: stop-reason handling, pairing every tool call with a result, refusals, prompt caching, token counting, context management and error mapping.
- We track the API ourselves. The appendix shows how much moves: thinking is on by default, forced tool choice is rejected, edits to history invalidate thinking blocks, and the SDK has a new major version.

What to watch:

- The appendix is the contract with the API. Items marked "pending" have not been run against the live API yet. `spikes/messages_api.py` runs them (checks S1 to S10), and LH-104 builds on the results, so run it before LH-104 pins behavior.
- The API facts are dated: the model table cited is dated 2026-10-06 and the preserved-thinking notes quote the live page as of 2026-10-14. Check prices, model lists, beta names and enforcement rules against the live pages before code depends on them.

## Alternatives

- Claude Agent SDK as the core loop. Rejected: it owns the loop and the session transcript, so we cannot checkpoint or retry between its internal steps, which is the point of the durable design.
- LangGraph with a Postgres checkpointer as the core. Rejected: it can checkpoint per node, but we would still need a job runner, durable timers, schedules, signals and a history UI, and Temporal already provides them.

## Decisions from the API research

These decisions came out of the LH-102 research on the Claude API docs. The live calls are still pending, so each decision names the check that could change it. Models: planner, coder and verifier use `claude-sonnet-5-5`. The compactor and `summarize_for_human` use `claude-haiku-5-5`. Both think by default (adaptive thinking) and have a 1M token window. The labels D1, D3, D7, D8 and D10 are the ones used in the research notes, so they are not renumbered here.

### D1. Thinking blocks are stripped at ingestion (v1)

- The Anthropic provider drops every `thinking` and `redacted_thinking` block from a response before it becomes an `LLMResponse`, and counts them in `LLMResponse.dropped_thinking_blocks`.
- Removing all thinking blocks is on the documented keep list, so it is valid on every account, including accounts that are enforced on the history-editing check. Longhaul's elision and compaction edit history, which would invalidate replayed thinking blocks. Stripping avoids that.
- Cost: the model loses its earlier reasoning on every turn. On Anthropic's multi-turn coding benchmark, dropping thinking on every turn after the first raised output tokens by 4.6 percent at low effort, 9.7 percent at medium and 10.3 percent at high (67.1 percent at max, which we do not use). The docs call these estimates with wide error bars and do not name the model, so read the cost as roughly 5 to 10 percent more output tokens at our effort levels (appendix, section 7).
- The steady state the docs describe is an append-only context that keeps thinking and strips it once per compaction. That is the follow-up, planned with Phase 4.
- Contracts v2 gets no thinking block types. Live check S1 confirms that a loop with every thinking block stripped, the newest turn included, is accepted. If it is not, contracts gain thinking blocks at that point.

### D3. Call options (contracts v2)

- `LLMProvider.complete()` and `count_tokens()` gain `options: CallOptions | None = None`.
- `CallOptions` holds `effort` (`low`, `medium`, `high`, `xhigh` or `max`), `force_tool` (a tool name) and `output_schema` (a JSON schema). All three default to `None`.
- `claude-sonnet-5-5` rejects a forced `tool_choice` (`any` or `tool`) with a 400. The Anthropic provider therefore never sends a forced tool choice to a model that rejects it. It raises `ProviderError(INVALID_REQUEST)` that names the option, before any HTTP call.
- Callers that need schema-valid JSON use `output_schema` (structured outputs), or a `strict` tool with `tool_choice` set to auto plus an instruction in the prompt.
- `ScriptedProvider` accepts `options` and ignores them (it may record them for tests). The OpenAI provider maps `force_tool` to a function `tool_choice` and `output_schema` to a `json_schema` `response_format`, and ignores `effort`.

### D7. The system prompt stays a string

The top-level system prompt holds only the static blocks 1 to 4 and is frozen for the life of a transcript. The plan and the progress notes are text blocks of the first user message, after the task statement, and are edited in place when they change (ADR-0013). So `system: str` stays in the contract, and the system cache breakpoint sits at the end of that string.

### D8. Effort and output limits per role

- `config/longhaul.example.yaml` model ids: `claude-sonnet-5-5` for planner, coder and verifier, and `claude-haiku-5-5` for the compactor.
- A new config section `effort` sets `planner: medium`, `coder: medium`, `verifier: medium` and `compactor: low`. It is passed on every call through `CallOptions.effort` and stays fixed for the life of a transcript (changing effort between requests invalidates the messages cache, appendix section 3).
- `loop.max_tokens_per_turn` stays 8192 for normal calls. A new `loop.max_tokens_retry` is 64000.
- When a turn stops on `max_tokens`, the loop never appends or runs it. It retries that turn once at the higher cap (the provider streams when `max_tokens` is above 16000). A second `max_tokens` stop ends the task with outcome ERROR and the summary "output limit".

### D10. SDK and dependencies

- Use `anthropic>=2.3,<3`. The 2.x line sends API version `2026-10-21` by default and is built on `httpx2`. The latest release when this was written is 2.3.0, published on 2026-10-23 (PyPI).
- LH-104 locked `anthropic` 2.3.0 (recorded in ADR-0002). It sends API version 2026-10-21, and a unit test fails if an SDK upgrade changes that.
- Tests inject `httpx2.MockTransport` through `anthropic.DefaultAsyncHttpxClient(transport=...)`. Tools that patch `httpx` stop seeing SDK traffic.
- No sampling parameters (`temperature`, `top_p`, `top_k`) are ever sent. On API version `2026-10-21` any of them is a 400 on every model.
- Invariant I1 and CONTRIBUTING.md rule 4 list `httpx2` and `httpcore2` among the imports that workflow code may not use.

## Appendix: API facts (LH-102)

Drafted on 2026-10-30 from the Claude API documentation. Every fact names its page and section, written as `API docs, <page>: <section>`; the pages are listed under Sources below. "Confirmed live: pending" marks a fact that needs a call with a real key. The spike items S1 to S10 are listed in the log at the end. After a run, replace "pending" with the result and the date.

- Model scope: planner, coder and verifier use `claude-sonnet-5-5`. The compactor and `summarize_for_human` use `claude-haiku-5-5`. A fact that names no model applies to both.
- SDK: `anthropic` 2.3.0 is locked (LH-104, ADR-0002). It sends `anthropic-version` 2026-10-21, pinned by `test_the_sdk_sends_the_api_version_adr_0003_records`.
- The pages change. The model table cited here is dated 2026-10-06, and the preserved-thinking notes quote the live page as of 2026-10-14. Check prices, model lists, beta names and enforcement rules against the live pages before code depends on them.

Sources (pages of the Claude API documentation on docs.claude.com):

- Messages API: https://docs.claude.com/en/api/messages
- Tool use: https://docs.claude.com/en/docs/agents-and-tools/tool-use/overview
- Prompt caching: https://docs.claude.com/en/docs/build-with-claude/prompt-caching
- Also cited by page name: Errors, Models overview, Models migration guide, Token counting, Extended thinking, Streaming, Python SDK, Cost optimization, API version migration guide and Platform availability.

### Part 1. The five facts LH-102 asks for

#### 1. Tool definitions and block shapes

| Fact | Source | Confirmed live |
|---|---|---|
| A tool definition is `{"name", "description", "input_schema"}`. `input_schema` is a JSON Schema object. This maps one to one to `ToolSpec`. | API docs, Tool use: User-defined tools, Tool definition structure | pending (S1) |
| A tool definition may also carry `"strict": true` (arguments always match the schema, and every object in the schema must set `additionalProperties: false`) and `"cache_control"`. | API docs, Tool use: Structured outputs; API docs, Prompt caching: API reference | pending (S4, S8) |
| A tool call is `{"type": "tool_use", "id": "toolu_...", "name": "...", "input": {...}}`. In a non-streaming response `input` is already a parsed object. Python reads `block.type`, `block.id`, `block.name` and `block.input`. | API docs, Messages API: Tool use example; API docs, Tool use: Handling tool results | pending (S1) |
| A result is `{"type": "tool_result", "tool_use_id": "<id>", "content": "<text>", "is_error": true}`. Every example sends `content` as a string. No example shows `"is_error": false` or empty content. | API docs, Tool use: Handling tool results; Client-side tools (Bash and text editor) | pending (S2) |
| When one response has several `tool_use` blocks, run them all and send every result back in one `user` message, one `tool_result` per call. | API docs, Tool use: Handling tool results | not needed |
| Text the user adds during a task goes in a `text` block after the last `tool_result` of the same `user` message. Never put it inside a `tool_result`. Sonnet 5.5 and Haiku 5.5 may ignore user text found inside a tool result, or in a system message right after one, as a possible prompt injection. | API docs, Models migration guide: Claude Sonnet 5.5, Behavioral shifts; Claude Haiku 5.5, Behavioral shifts | pending (S2) |
| Two `user` messages in a row: the docs disagree. The Python SDK page says they are allowed and merged into one turn. The Errors page lists them as a 400. Keep roles alternating until S2 settles it. | API docs, Python SDK: Multi-turn conversations, Rules; API docs, Errors: 400 Bad Request; Common mistakes and fixes | pending (S2) |
| A request must end with a `user` message. An assistant message at the end is a prefill. It returns 400 on Sonnet 4.6 and later (the Sonnet 5.5 checklist carries the rule forward) and on Haiku 5.5. | API docs, Models migration guide: The 4.6 family, item 3; Claude Haiku 5.5, Breaking change 3 | not needed |
| Sonnet 5.5 sometimes calls a tool by a name that differs only in case, or uses a near-miss parameter name. Answer with `is_error: true` and the exact expected name; the model usually corrects itself. | API docs, Models migration guide: Claude Sonnet 5.5, Behavioral shifts | not needed |

#### 2. Error classes and status codes

| Case | HTTP | `error.type` | Python class (`anthropic.`) | Docs: retryable | Longhaul kind | Confirmed live |
|---|---|---|---|---|---|---|
| Bad request | 400 | `invalid_request_error` | `BadRequestError` | no | `invalid_request` | pending (S3) |
| Bad or missing key | 401 | `authentication_error` | `AuthenticationError` | no | `auth` | pending (S3) |
| Billing problem | 402 | `billing_error` | none named, caught as `APIStatusError` | no | `auth` | offline |
| Not allowed for this credential | 403 | `permission_error` | `PermissionDeniedError` | no | `auth` | offline |
| Unknown model, or one the org cannot use | 404 | `not_found_error` | `NotFoundError` | no | `invalid_request` | pending (S3) |
| Request too large (causes include "too many tokens in input") | 413 | `request_too_large` | none named, `APIStatusError` | no | `context_too_long` | offline |
| Unprocessable | 422 | not listed | `UnprocessableEntityError` | not listed | `invalid_request` | offline |
| Rate limited | 429 | `rate_limit_error` | `RateLimitError` | yes | `rate_limit` | offline |
| Server error | 500 | `api_error` | `InternalServerError` | yes | `server` | offline |
| Overloaded | 529 | `overloaded_error` | `InternalServerError` (every status of 500 or more) | yes | `overloaded`, split on `status_code == 529` or `e.type == "overloaded_error"` | offline |
| Request timeout, conflict | 408, 409 | not listed | none named, `APIStatusError` | the SDK retries them | `timeout` (408), `server` (409) | offline |
| Client-side timeout | none | none | `APITimeoutError` | the SDK retries it | `timeout` | pending (S3) |
| Network failure | none | none | `APIConnectionError` | the SDK retries it | `server` | offline |

Sources: API docs, Errors: Error code summary; Typed exceptions in SDKs, Exception class names by language; API docs, Python SDK: Error handling; Client configuration, Retries. "Offline" means a unit test with `httpx2.MockTransport` and the documented error body. No key is needed.

- Error body: `{"type": "error", "error": {"type": "...", "message": "..."}, "request_id": "req_..."}`. Source: API docs, Errors: 400 Bad Request. Confirmed live: pending (S3 saves real bodies as fixtures).
- Every `APIStatusError` subclass has `.type` (the `error.type` string), `.status_code`, `.message` and `.response` (an `httpx2` response). The docs say to classify with the typed classes, not by matching message text. Source: API docs, Errors: Error `.type` field; API docs, Python SDK: Upgrading, Step 3.
- Hierarchy: `APIError` is the base. `APIStatusError` (Python only) is the base for HTTP status errors. The docs call `APIConnectionError` a sibling in Python without saying of what, and do not say whether `APITimeoutError` subclasses it. Catch `APITimeoutError` first. Source: API docs, Errors: Catch most-specific first, in a chain. Check offline with `issubclass` on the pinned SDK.
- Context too long: no doc gives the status, type or message for a prompt over the context window. Both 5.5 models have a 1M-token window (API docs, Models overview: Current models (recommended)), so Longhaul's 200,000-token budget sits far below it. A response that fills the window while generating ends with `stop_reason: "model_context_window_exceeded"` (section 9). Source: API docs, Errors: 413 Request Too Large. Confirmed live: pending (S3, optional item f).
- 429 headers: `retry-after` (seconds to wait), `x-ratelimit-limit-*`, `x-ratelimit-remaining-*`. The docs show `retry-after` only on 429. Source: API docs, Errors: 429 Rate Limited. Confirmed live: pending (record any 429 seen during the spike).
- SDK retries: connection errors, 408, 409, 429 and 500 or above, 2 retries by default with backoff. `max_retries=0` turns them off. Source: API docs, Python SDK: Client configuration, Retries.
- SDK timeout: 10 minutes by default. On timeout the SDK raises `APITimeoutError`. `anthropic.Timeout` is `httpx2.Timeout`. Source: API docs, Python SDK: Client configuration, Timeouts.
- The SDK raises `ValueError` for a non-streaming request it estimates will run past about 10 minutes, unless `timeout` is set explicitly. The `max_tokens` threshold is not stated. Source: API docs, Streaming: Best practices, item 6.
- `anthropic` 1.x is built on `httpx2`, not `httpx`. An object from the `httpx` package is rejected, and an `httpx` client passed as `http_client=` raises `TypeError` at construction. Tests inject `DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler))`. Tools that patch `httpx` (respx, pytest-httpx, vcrpy) silently stop seeing SDK traffic. Source: API docs, Python SDK: Upgrading, Step 3, httpx to httpx2; Custom HTTP client (proxy, base URL).
- Every response object has `_request_id`, public despite the underscore. Source: API docs, Python SDK: Response helpers.
- Setting both `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` makes the SDK send both headers, and the API returns 401. Source: API docs, Errors: 401 Unauthorized.
- Request shapes that return 400 `invalid_request_error` on Longhaul's models, never retryable: a forced `tool_choice` on Sonnet 5.5 (also on count_tokens); `thinking: {"type": "disabled"}` on Sonnet 5.5, and on Haiku 5.5 at effort `xhigh` or `max`; `budget_tokens` on both; `between_tools` on any model except Sonnet 5.5; a non-default `temperature` or `top_p`, or any `top_k` (any of the three on API version `2026-10-21`); an assistant prefill; a replayed thinking block after a history edit on an enforced account (section 7). Source: API docs, Errors: 400 validation errors.

#### 3. Prompt caching

| Fact | Source | Confirmed live |
|---|---|---|
| Marker: `"cache_control": {"type": "ephemeral"}` (5 minutes, the default) or `{"type": "ephemeral", "ttl": "1h"}`. | API docs, Prompt caching: API reference | pending (S4) |
| A marker can go on system text blocks, on tool definitions, and on message blocks of type `text`, `image`, `tool_use`, `tool_result` and `document`. A top-level `cache_control` on the request is automatic caching: it marks the last cacheable block, moves forward each turn and uses one slot. | API docs, Prompt caching: API reference; Automatic vs explicit breakpoints | pending (S4) |
| At most 4 breakpoints per request, the automatic one included. | API docs, Prompt caching: API reference | pending (S4) |
| Render order is tools, then system, then messages. The cache matches the exact bytes up to each breakpoint. A change at position N invalidates every breakpoint at or after N. A marker on the last system block caches tools and system together. | API docs, Prompt caching: The one invariant everything follows from | not needed |
| A tool definition change (add, remove, reorder) or a model switch invalidates every cache. A system prompt change invalidates the system and messages caches. A `tool_choice` change invalidates the messages cache only. A thinking or effort change invalidates the messages cache, and on some models (not named) the tools and system caches too. An edit to an earlier message invalidates the messages cache from that point. | API docs, Prompt caching: Invalidation hierarchy | not needed |
| Minimum cacheable prefix: 512 tokens on both 5.5 models. The doc itself says to check this before relying on it. A shorter prefix silently does not cache: `cache_creation_input_tokens` stays 0 and no error is returned. | API docs, Prompt caching: API reference, minimum prefix table | pending (S4) |
| Each breakpoint looks back at most 20 positions for an earlier entry. A run of `tool_use` blocks, or of `tool_result` blocks, counts as one position. | API docs, Prompt caching: 20-block lookback window | not needed |
| An entry lives 5 minutes (or 1 hour) from the start of the request that wrote or last read it. A read refreshes the timer at no cost. | API docs, Prompt caching: Choosing the TTL | not needed |
| Price multipliers on both 5.5 models: a write costs 1.25x the input price (5 minutes) or 2x (1 hour); a read costs 0.1x. | API docs, Prompt caching: API reference, Economics | pending (Pricing page) |
| Usage fields: `usage.cache_creation_input_tokens` (written by this request), `usage.cache_read_input_tokens` (read by this request), and `usage.cache_creation.ephemeral_5m_input_tokens` / `ephemeral_1h_input_tokens` (the write split by TTL). | API docs, Prompt caching: Verifying cache hits | pending (S4) |
| Documented layouts: in a multi-turn conversation the moving marker goes on the last block of the newest message. For agent loops the docs pair one explicit marker at the end of the static system prefix with top-level automatic caching for the growing tail. | API docs, Prompt caching: Placement patterns, Multi-turn conversations; Automatic vs explicit breakpoints | pending (S4) |
| Caches are kept per workspace on the Claude API. | API docs, Prompt caching: API reference | not needed |

#### 4. Token counting

| Fact | Source | Confirmed live |
|---|---|---|
| `client.messages.count_tokens(model=..., system=..., messages=[...])` exists. It calls `POST /v1/messages/count_tokens` and returns an object whose `.input_tokens` is the size of the whole prompt. The endpoint is stateless. | API docs, Token counting: opening section; Count a file or string; API docs, Python SDK: Cost optimization strategies, 3. Use token counting before requests | pending (S5) |
| Counts are model-specific: pass the model id you will call. Do not estimate with `tiktoken`, which undercounts Claude by about 15 to 20 percent on text and more on code. | API docs, Token counting: opening section | not needed |
| It rejects unknown fields. Remove the create-only fields (`max_tokens`, `stream`, `temperature`, `top_p`, `top_k`, `stop_sequences`, `metadata`, `service_tier`) and send the rest of the real request, including `tools`, `tool_choice`, `thinking` and `output_config`. | API docs, Extended thinking: Preserved thinking, drop_block probe example, comment on `COUNT_TOKENS_EXCLUDED` | pending (S5) |
| It applies the same rules as `messages.create`: a forced `tool_choice` is a 400 on Sonnet 5.5, and the history-editing check returns the same 400. | API docs, Models migration guide: Claude Sonnet 5.5, Breaking change 2; Claude Fable 5.1 from Claude Fable 5, Breaking change 3 | pending (S8) |
| It costs nothing and samples nothing. No doc states its rate limits. | API docs, Extended thinking: Preserved thinking, Step 2, 2.2 What the response tells you | pending (S5) |
| Both 5.5 models count about 30 percent more tokens than their predecessors (Haiku 4.5, Sonnet 4.6) for the same text. Sonnet 5.5 uses Sonnet 5's tokenizer. The docs do not say whether Sonnet 5.5 and Haiku 5.5 share one. | API docs, Models migration guide: Claude Haiku 5.5, The same text counts as about 30% more tokens; Claude Sonnet 5.5, opening paragraph | pending (S5) |

#### 5. Whether `usage.input_tokens` excludes cached tokens

Yes, per the docs. `input_tokens` is only the uncached remainder. The total prompt size is `input_tokens + cache_creation_input_tokens + cache_read_input_tokens`. Source: API docs, Prompt caching: Verifying cache hits. Confirmed live: pending (S4 checks that the sum matches `count_tokens` for the same body).

| `Usage` field | API field |
|---|---|
| `input_tokens` | `usage.input_tokens` (uncached part only) |
| `cache_read_tokens` | `usage.cache_read_input_tokens` |
| `cache_write_tokens` | `usage.cache_creation_input_tokens` |
| `output_tokens` | `usage.output_tokens` |

The four fields do not overlap, so the LH-701 formula adds them with no subtraction. Anything that measures prompt size (the LH-401 calibration, the Haiku 5.5 rate card) must use the sum of the three input fields.

Output side: thinking is billed as output tokens even when its text is omitted, and `max_tokens` covers thinking plus the reply. `usage.output_tokens_details.thinking_tokens` meters thinking where the response carries it. No doc says in one sentence that `usage.output_tokens` includes thinking. Source: API docs, Models migration guide: Claude Sonnet 5, Silent default change (thinking.display defaults to omitted); Claude Fable 5.1 from Claude Fable 5, New API features, item 3; API docs, Cost optimization: Ask for the app's own usage logs first. Confirmed live: pending (S1, S6).

### Part 2. Other facts that matter for Longhaul

#### 6. Thinking blocks

- Both models think by default. With `thinking` omitted they run adaptive thinking. The default effort is `high` on Sonnet 5.5 and `medium` on Haiku 5.5. Source: API docs, Models migration guide: Claude Sonnet 5.5, Choosing an effort level; Claude Haiku 5.5, Thinking is on by default; Claude Haiku 5.5, Choosing an effort level. Confirmed live: pending (S1).
- A response can start with one or more `thinking` blocks before its `text` and `tool_use` blocks, or have none on a short turn. Read blocks by `type`, never by position. Under the default `display: "omitted"` a thinking block has an empty `thinking` string and a `signature`. Source: same sections; API docs, Models migration guide: Claude Sonnet 5.5, Breaking change 1, step 3. Confirmed live: pending (S1).
- On Sonnet 5.5, notes of more than a sentence or two between tool calls come back as progress-update `thinking` blocks, empty under the default display. `display: "updates"` (beta `thinking-display-updates-2026-08-18`, adaptive thinking only) returns their text. Source: API docs, Models migration guide: Claude Sonnet 5.5, Text between tool calls comes back in thinking blocks.
- `redacted_thinking` is a second block type the docs say to keep. They do not say whether Sonnet 5.5 or Haiku 5.5 return it. Source: API docs, Models migration guide: Claude Fable 5.1 from Claude Fable 5, Breaking change 3, recovery 1.
- Thinking settings: Sonnet 5.5 returns 400 for `{"type": "disabled"}`. Its lowest setting is `{"type": "between_tools"}`, accepted only by Sonnet 5.5, only at effort `high` or below, with no other field inside `thinking`; its progress notes still come back as thinking blocks, with text. Haiku 5.5 accepts `disabled` at effort `low`, `medium` or `high`. The docs prefer a lower effort to switching thinking off. Source: API docs, Models migration guide: Claude Sonnet 5.5, Breaking change 1; Claude Haiku 5.5, Thinking is on by default. Confirmed live: pending (S3).
- Thinking counts toward `max_tokens`. A response can stop with `stop_reason: "max_tokens"` after a thinking block and before any text (stated for Haiku 5.5). For long agentic coding turns the Sonnet 5.5 guide suggests 64,000. The docs say to stream above about 16K output tokens, and to treat a `max_tokens` stop as a failed attempt rather than retry at the same cap. Source: API docs, Models migration guide: Claude Sonnet 5.5, Breaking change 1, step 5; Claude Haiku 5.5, What carries over, and what to re-check; API docs, Cost optimization: 2.4 Output tokens. Confirmed live: pending (S6).
- Thinking is billed whatever the display setting. Source: API docs, Models migration guide: Claude Sonnet 5, Silent default change (thinking.display defaults to omitted).
- The docs say to pass thinking blocks back unchanged with the rest of the assistant turn. Section 7 covers what happens when a harness does not. Source: API docs, Models migration guide: Claude Sonnet 5.5, Breaking change 1, step 4.
- Model and account binding: Haiku 5.5 cannot read Sonnet 5.5 thinking blocks. Sonnet 5.5 reads Haiku 5.5 blocks on the Claude API and Google Cloud. The API drops a block the model cannot read, and does not bill it. A block works only in the account that produced it. Source: API docs, Models migration guide: Claude Haiku 5.5, Thinking blocks stay with the account that produced them; Claude Sonnet 5.5, Breaking change 3.
- Longhaul today: `render._block_from_dict` raises `ValueError` on a `thinking` block, and the `Block` union has no thinking type. The provider must filter by type before it converts a response.

#### 7. History edits (the preserved-thinking check)

- A thinking block's signature binds the top-level `system`, the set of `tools` (full definitions: name, description and schema) and every earlier message. Earlier thinking blocks are chained to it rather than part of that prefix, which is why blocks can be removed from the front of the history but not from the middle. Source: API docs, Models migration guide: Claude Fable 5.1 from Claude Fable 5, Breaking change 3; API docs, Extended thinking: Preserved thinking, Switching models mid-conversation.
- Models that run the check: `claude-sonnet-5-5`, `claude-haiku-5-5`, `claude-opus-5-5` and `claude-fable-5-1`. It is enforced by default, as a 400, for accounts created on or after 2026-08-31 00:00 UTC, on every platform. Older accounts only record mismatches, unless a request sets `thinking.block_binding.prefix_mismatch_behavior`; any value opts that request in. Source: API docs, Models migration guide: Claude Sonnet 5.5, Breaking change 3; Claude Haiku 5.5, Breaking change 5. Confirmed live: pending (S7).
- Edits that invalidate every later thinking block: changing or removing an earlier turn, including trimming old tool results; adding text to an earlier turn and removing it later; rebuilding `system` or `tools` between requests; removing a thinking block from the middle. Source: API docs, Models migration guide: Claude Fable 5.1 from Claude Fable 5, Breaking change 3.
- Not edits: appending; removing thinking blocks from the start, from the end, or all of them; reordering tools; adding, moving or removing `cache_control`; changing `max_tokens`, `output_config` (including effort), `tool_choice` or the thinking configuration; server-side compaction and context editing. Source: API docs, Extended thinking: Preserved thinking, Keep list.
- The failure is a 400 `invalid_request_error`, decided before any output, for example `messages.5.content.0: Invalid signature in thinking block. The block is bound to a different conversation. ...`. The same body fails every time, and `count_tokens` returns the same 400. Recovery without a beta: strip every `thinking` and `redacted_thinking` block once, record the strip, retry once. With beta `thinking-binding-controls-2026-08-01`, `prefix_mismatch_behavior: "drop_block"` drops the first mismatched block and every later one and lists each in a top-level `input_transformations` array. `block_binding` works only with adaptive thinking. Source: API docs, Models migration guide: Claude Fable 5.1 from Claude Fable 5, Breaking change 3; API docs, Errors: 400 validation errors.
- Compaction: the recommended client-side shape is simple compaction, one summary message plus the new user turn and nothing older. Keep-tail compaction fails on the kept turns' thinking. Pruning old tool results on the client is tier 4, with no workaround. Never compact in the middle of a tool round. Source: API docs, Models migration guide: Claude Fable 5.1 from Claude Fable 5, Breaking change 3, append-only table; API docs, Extended thinking: Preserved thinking, Severity tiers.
- The docs disagree about stripping. The keep list allows removing all thinking, and asks that an assistant turn whose `tool_use` still waits for its `tool_result` keep its thinking. The model-binding text says that removing blocks yourself "can trigger ordering/signature 400s". Source: API docs, Extended thinking: Preserved thinking, Keep list; API docs, Models migration guide: Claude Fable 5.1 from Claude Fable 5, Breaking change 2. Confirmed live: pending (S1).
- Cost of lost thinking: on Anthropic's multi-turn coding benchmark (the model is not named), dropping thinking on every turn after the first raised output tokens by 4.6% at low effort, 9.7% at medium, 10.3% at high and 67.1% at max. The docs call these estimates with wide error bars. Source: API docs, Extended thinking: Preserved thinking, Failure modes to avoid.
- Test for enforcement: replay a thinking block after an edit, without the beta header. A 400 that names the header means the account is enforced by default; a 200 means it is not. Source: API docs, Extended thinking: Preserved thinking, Step 0, item 1. Confirmed live: pending (S7).
- Longhaul today: the transcript holds no thinking, so nothing is replayed and the check has nothing to compare. Tier 1 elision, keep-tail compaction and system prompt rebuilds stay valid only while that holds.

#### 8. Forced tool choice and structured outputs

- Sonnet 5.5 rejects `tool_choice` `{"type": "any"}` and `{"type": "tool", "name": ...}` with 400 `tool_choice: type "tool" and "any" are not supported for this model.`, on Messages, Batches and count_tokens. `auto` and `none` work. Source: API docs, Tool use: Tool choice options; API docs, Models migration guide: Claude Sonnet 5.5, Breaking change 2. Confirmed live: pending (S3, S8).
- Haiku 5.5 accepts a forced `tool_choice`. The response then starts with the tool call and has no thinking block. Source: API docs, Models migration guide: Claude Haiku 5.5, Thinking is on by default. Confirmed live: pending (S8).
- Documented replacements: `auto`, a prompt that names the tool, `strict: true` on the tool, and a check that the call happened (retry if not). Or structured outputs, `output_config: {"format": {"type": "json_schema", "schema": {...}}}`, when the forced call only existed to get JSON. Source: same sections.
- Structured outputs and strict tools work on both 5.5 models. Every object needs `additionalProperties: false`. Not supported: recursive schemas, `minimum`, `maximum`, `multipleOf`, `minLength`, `maxLength` and complex array constraints. The Python SDK removes unsupported keywords and checks them client-side; the docs do not say whether that also happens on a raw `messages.create` call. Output is guaranteed to match only on a normal finish: after a refusal it may not match, and on a `max_tokens` stop it may be incomplete. A new schema compiles once and stays cached for 24 hours. Use `output_config.format`; the top-level `output_format` parameter is deprecated. Source: API docs, Tool use: Structured outputs; JSON schema limitations; Important notes; API docs, Models migration guide: All models, item 6. Confirmed live: pending (S8).
- `disable_parallel_tool_use: true` with `auto` means at most one tool call per response. Source: API docs, Tool use: Tool choice options.

#### 9. Stop reasons

| `stop_reason` | Meaning | Longhaul handling | Source | Confirmed live |
|---|---|---|---|---|
| `end_turn` | Finished normally. | End of turn. | API docs, Python SDK: Stop reasons | pending (S1) |
| `tool_use` | Wants tools run. | Run every call and return all results in one user message. | same | pending (S1) |
| `max_tokens` | Output cap reached; thinking counts toward it. | Run no `tool_use` from this turn, because its input can be a valid but partial object. Treat it as a failed attempt. | same; API docs, Tool use: Eager input streaming, item 2; API docs, Cost optimization: 2.4 Output tokens | pending (S6) |
| `stop_sequence` | A custom stop sequence was hit. | Longhaul sends none. | same | not needed |
| `pause_turn` | A server-side tool loop paused. | Longhaul declares only client tools, so treat it as unexpected and fail visibly. | same; API docs, Tool use: Tool runner vs manual loop | not needed |
| `refusal` | Declined, as an HTTP 200 with `stop_details`. | Check it before reading content. Run no tools. Never resend the same prompt. Surface the category. | API docs, Python SDK: Structured stop details; API docs, Models migration guide: Claude Fable 5.1, refusal stop reason section | not testable on demand |
| `model_context_window_exceeded` | The context window filled during generation (models 4.5 and later; the 5.5 models are not named). | Same path as context too long: compact, then retry once. | API docs, Models migration guide: Additional changes when coming from 3.x, 4.0 or 4.1 to 4.6, item 4 | not testable cheaply |

- `stop_details` has `category` and `explanation`. It can be null even on a refusal, and `explanation` may be missing, so branch on `stop_reason`. Source: API docs, Models migration guide: Claude Fable 5.1, refusal stop reason section.
- Categories: Sonnet 5.5 can decline as `cyber`, `bio`, `frontier_llm`, `reasoning_extraction` or `general_harms`. Haiku 5.5 uses the same set without `reasoning_extraction`. A prompt that asks the model to write out its reasoning invites `reasoning_extraction`. Source: API docs, Models migration guide: Claude Sonnet 5.5, Safeguards and fallback; Claude Haiku 5.5, Safeguards.
- A refusal can come before any output (empty content) or after partial output (discard it). Whether a refusal before output is billed depends on its category; it counts against rate limits either way. Haiku 5.5 has no server-side fallback, so do not send `fallbacks`. Source: same sections.
- The docs' sample manual loop branches only on `end_turn` and `pause_turn` and sends every other value down the tool path. Longhaul's loop must not copy it. Source: API docs, Tool use: Manual agentic loop.

#### 10. Model ids, limits and prices

| Use in Longhaul | Model id | Context | Max output | Input | Output | Cache write, 5 min | Cache write, 1 h | Cache read | Default effort |
|---|---|---|---|---|---|---|---|---|---|
| planner, coder, verifier | `claude-sonnet-5-5` | 1M | 128K | 2.00 | 10.00 | 2.50 | 4.00 | 0.20 | `high` |
| compactor, `summarize_for_human` (prompt of 100K tokens or fewer) | `claude-haiku-5-5` | 1M | 128K | 0.10 | 0.50 | 0.125 | 0.20 | 0.01 | `medium` |
| same, prompt over 100K tokens | `claude-haiku-5-5` | 1M | 128K | 0.50 | 2.50 | 0.625 | 1.00 | 0.05 | `medium` |

Prices are USD per million tokens on the first-party Claude API. The Haiku 5.5 cache prices are derived from the stated multipliers (1.25x, 2x and 0.1x of the active card); the docs print only the multipliers. The docs do not say which tokens count toward the 100K split. Sources: API docs, Models overview: Current models (recommended); API docs, Models migration guide: Claude Sonnet 5.5, opening paragraph; Claude Haiku 5.5, opening paragraph. Confirmed live: pending (S9 for limits; the live Pricing page for prices).

- Use the ids exactly as written, with no date suffix. Source: same sections.
- Do not fall back to `claude-haiku-4-5`: it has a 200K window, 64K output, costs $1 / $5 at any prompt length, and errors on the effort parameter. Source: API docs, Models overview: Current models (recommended); API docs, Models migration guide: Claude Haiku 5.5, Choosing an effort level.
- Effort is sent as `output_config: {"effort": ...}` with `low`, `medium`, `high`, `xhigh` or `max`. Sending the default is the same as omitting it. Changing top-level effort between requests invalidates the messages cache. Source: API docs, Models migration guide: Claude Sonnet 5.5, Choosing an effort level; Claude Haiku 5.5, Choosing an effort level; API docs, Prompt caching: Invalidation hierarchy.
- Send no `temperature`, `top_p` or `top_k`. Both models reject non-default values, and on API version `2026-10-21` any of the three is a 400 on every model. Python `anthropic` 2.0.0 is the first release that sends `2026-10-21` by default, and it has breaking changes of its own (a per-role `MessageParam`, one credential per client). Source: API docs, API version migration guide: 1. Sampling parameters are removed; 5. The new SDK and CLI releases have breaking changes of their own; Set the version.
- `client.models.retrieve(id)` returns `max_input_tokens`, `max_tokens` and a `capabilities` dict read with bracket access. Source: API docs, Models overview: Programmatic model discovery. Confirmed live: pending (S9).
- Mid-conversation `role: "system"` messages: Sonnet 5.5 supports them with no beta header. For Haiku 5.5 the Haiku section of the Models migration guide says yes, while the Prompt caching and Platform availability pages list Sonnet 5.5 but not Haiku 5.5. Rules: text only, placed after a `user` message, last in `messages` or followed by an assistant turn, never `messages[0]`. Source: API docs, Prompt caching: Placement patterns, Mid-conversation system messages; API docs, Models migration guide: Claude Haiku 5.5, Breaking change 5. Confirmed live: pending (S10).

### Part 3. Checked offline on anthropic 2.3.0 (no key)

Run on 2026-10-30 in the project environment with `anthropic` 2.3.0 and `httpx2` 2.16.0, using `httpx2.MockTransport`. No API call was made. The spike repeats the class, transport, streaming-limit and credential checks in S3, so a later SDK version can be compared. They do not replace the live items: the live log below still reads "pending".

- Headers: the client sends `anthropic-version: 2026-10-21` and the key as `Authorization: Bearer`. The default timeout is 600 seconds for read, write and pool, and 5 seconds to connect.
- Status to class: 400 `BadRequestError`, 401 `AuthenticationError`, 403 `PermissionDeniedError`, 404 `NotFoundError`, 409 `ConflictError`, 413 `RequestTooLargeError`, 422 `UnprocessableEntityError`, 429 `RateLimitError`, 529 `OverloadedError`, any other status of 500 or more `InternalServerError`. Anything else, 402 and 408 included, is a plain `APIStatusError`.
- This differs from the table in section 2 in three rows. `OverloadedError` (529) is a sibling of `InternalServerError`, not a subclass, so a handler for `InternalServerError` alone misses overload. 409 and 413 have classes of their own. Every class here subclasses `APIStatusError`, so classifying by `e.status_code` or `e.type`, as section 2 recommends, still works.
- `APITimeoutError` subclasses `APIConnectionError`. A connect timeout raised `APITimeoutError`, and a refused connection raised `APIConnectionError`.
- `e.message` holds `Error code: <status> - <body>`. The API's own text is `e.body["error"]["message"]`. `e.request_id` is read from the `request-id` header, and `retry-after` on a 429 is readable from `e.response.headers`.
- A non-streaming `messages.create` raises `ValueError` before any request when `max_tokens` is above 21,333 (checked with the default timeout; 21,333 itself is sent). Passing an explicit `timeout` skips the check. D8's retry at 64000 therefore needs streaming, whatever threshold we choose ourselves.
- With both `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` set, 2.3.0 prints a warning and uses the API key. Section 2 describes an older behavior: both headers sent and a 401.
- `anthropic.Timeout` is `httpx2.Timeout`, and `anthropic` 2.3.0 requires `httpx2>=2.0.0,<3` and no `httpx` (PyPI metadata, read 2026-10-30).

### Live confirmation log

| Item | What it confirms | Result | Date | SDK version and anthropic-version |
|---|---|---|---|---|
| S1 | A tool loop with all thinking stripped is accepted on both models; block shapes; thinking present by default | pending | | |
| S2 | `is_error: false`, empty `tool_result` content, text after `tool_result`, two user messages in a row | pending | | |
| S3 | Exception classes, `.type`, real error bodies; the error for a prompt over the window | pending | | |
| S4 | Cache marker placement, cache usage fields, `input_tokens` excludes cached tokens, 512-token minimum | pending | | |
| S5 | `count_tokens` fields, return value, rate-limit headers | pending | | |
| S6 | Thinking tokens per turn and `max_tokens` behaviour at 8192 | pending | | |
| S7 | Owner account enforced on the history-editing check (yes or no), and the check is wired | pending | | |
| S8 | Forced tool call on Haiku 5.5, structured outputs and strict tools with Pydantic-generated schemas | pending | | |
| S9 | Model ids, context windows, output caps, effort capability | pending | | |
| S10 | Mid-conversation system message on both models | pending | | |

Running the spike (with a real key):

```bash
export ANTHROPIC_API_KEY=sk-ant-...
uv run --with "anthropic>=2.3,<3" python spikes/messages_api.py --live
```

`--with` adds the SDK to a throwaway environment, so `pyproject.toml` and `uv.lock` do not change. Without `--live` the script only prints its plan. It should cost about 0.3 to 0.6 USD and stops at a hard cap of 1.00 USD (`--max-usd`). It prints the usage of every call, writes error bodies, usage samples and raw responses under `spikes/out/run-<time>/` (gitignored), and ends with one table row per check, ready to paste into the table above. Copy one of the responses that starts with a thinking block from the `fixtures/` folder to `tests/fixtures/anthropic/` for LH-104. Item S7 also tells you whether your account is enforced on the history-editing check.
