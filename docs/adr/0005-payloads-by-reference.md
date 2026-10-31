# ADR-0005: Payloads by reference

- Status: Accepted
- Date: 2026-10-30

## Context

Temporal writes every activity input and output into the workflow's event history, and replays that whole history whenever a worker has to rebuild the workflow's state: after a crash, a restart, or a cache eviction. Anything that crosses an activity boundary is paid for again on every replay.

A coding agent moves large things across that boundary. The transcript grows with every turn. Tool arguments carry whole files. Tool outputs carry test logs. Diffs and git bundles can be megabytes.

The numbers show why this matters:

- A transcript of 150 KB sent into and out of an activity adds 300 KB to the history on each turn. Over the 150 turns a task may run, that is about 45 MB, close to the server's default history limit of 50 MB, before any other event is counted.
- The server's defaults also reject a single payload of about 2 MB, and terminate a workflow whose history passes 51,200 events or 50 MB.
- Long before those limits, replay time grows with history size, so recovery after a worker crash gets slower exactly when a task has run longest.

## Decision

**Pass references, not data.** Transcripts, tool arguments and outputs, diffs and bundles are written to the blob store. Workflows and activities pass a `BlobRef` (key, sha256, size) and small summaries. Anything bigger than 16 KB that crosses an activity boundary becomes a `BlobRef` (rule 5 in `CLAUDE.md`).

**Cap every payload at 256 KB.** No activity input or output may serialize to more than 256,000 bytes of JSON (invariant I2). That is about eight times below the server's payload limit, which leaves room for summaries to grow. `assert_small` in `longhaul.durable.payload_guard` measures the JSON size of a pydantic model or plain value and raises `PayloadTooLargeError` with the size and the limit. Activity wrappers call it on inputs and outputs, so a mistake fails in the activity that made it, with a message that says what to do.

**Continue-As-New every 40 turns.** Small payloads still add up, because every turn adds activities and each activity adds several history events. The workflow continues as new every 40 turns, or earlier when Temporal suggests it. The new run starts from `CarryState`, which holds refs and counters, not data. The value is a starting point: LH-207 measures real event counts, and the setting `loop.continue_as_new_every_turns` changes if the measurement says so.

**Verify every read.** `put` hashes the bytes with sha256 and returns the `BlobRef`. `get` checks the size and the hash before it returns any bytes, and raises `BlobIntegrityError` if either differs. `S3BlobStore` also sends the hash with the upload, so the server rejects a write that was damaged in transit instead of leaving a ref that can never be read. The size is checked from the response headers before the body is read, so a replaced object much larger than the ref says is never loaded into memory.

**Keys.** `blob_key(task_id, kind, turn_index, name)` builds `tasks/<task_id>/<kind>/<turn:04d>/<name>`, for example `tasks/lh-x/turns/0007/response.json`. Each part may hold letters, digits, dot, underscore and hyphen, must start with a letter or digit, and may not contain `..`. Every key passed to `put` or `get` is also checked: at most 1,024 bytes (the S3 limit), no leading or trailing slash, no empty or `.` segment, no `..`, and no control characters or backslash.

A key can be written again. A retried activity writes the same key, and the ref it returns names the content of the attempt that completed. A ref from an earlier attempt then fails verification instead of silently returning newer content.

**Client library: boto3 through `asyncio.to_thread`.** `S3BlobStore` holds one boto3 client, which is thread-safe, and runs each call, including the hashing, on a worker thread. The reasons:

- The dependency set does not resolve with aioboto3. The project requires `boto3>=1.43.118`. On the date above, the newest aioboto3 on the package index (15.5.0) pins aiobotocore 2.25.1, which requires boto3 below 1.40.62, and `uv pip compile` reports the pair as unsatisfiable. Choosing aioboto3 would hold boto3 back by about three minor versions and tie every later boto3 upgrade to aiobotocore's release schedule.
- The repo already runs a blocking SDK this way. The Docker sandbox manager calls the Docker SDK through `asyncio.to_thread`.
- The load does not need native async I/O. Payloads are kilobytes to a few megabytes, and a turn makes a handful of calls next to model latency measured in seconds.
- It needs no packages beyond boto3, which the project already depended on for its tests.

boto3 moves from the dev group to the runtime dependencies, and `types-boto3[s3]` joins the dev group so that mypy checks the calls.

## Consequences

**Easier**

- History stays small, so replay after a crash stays fast and Continue-As-New has headroom.
- The console and the run report read full content from blobs by key, and the Temporal UI stays readable.
- Every blob read is verified, so a damaged, replaced or truncated object is an error and never bad input to the model.
- Unit tests use `MemoryBlobStore`, which applies the same key rules and the same checks as the S3 store.

**Harder**

- Every large payload costs a blob round trip. A blob store outage fails activities, which Temporal retries.
- The data is not in the history. A replay test or a chaos run needs the blob contents as well as the history.
- Blobs need a retention policy. This ADR does not define one, and nothing deletes blobs yet.
- Calls on the default thread pool cannot be cancelled once started. A cancelled activity leaves its S3 call running until it finishes or times out. Connect and read timeouts, set on the client, bound that.
- Retries overwrite keys, so activities must return the ref from the attempt that completed and must not reuse refs from an earlier attempt.

**To watch**

- The 16 KB threshold and the 256 KB cap are enforced by `assert_small` in the activity wrappers (LH-204). `PayloadTooLargeError` should map to a non-retryable activity error there, since retrying cannot make a payload smaller. I2 also calls for a guard in the payload converter, which is later work.
- The upload checksum needs a backend that supports the `x-amz-checksum-sha256` header. S3 and the MinIO build in the compose stack do. A different S3-compatible store has to be tried before it is used.
- The server limits quoted above are defaults. Check them against the Temporal version in use when it changes.

## Alternatives

- **aioboto3.** It gives native async I/O and real cancellation, but it does not resolve with the boto3 the project already pins (see Decision), and the gain is not needed at this load.
- **Pass payloads inline and raise the server limits.** Dynamic config can raise the payload limit, but history size and replay time keep growing, so the problem only moves later.
- **A Temporal payload codec that compresses.** Compression shrinks payloads by a constant factor and leaves the linear growth. It also hides content from the Temporal UI. It could be added later as a second layer.
- **Postgres large objects or `bytea`.** One service fewer, but large writes would sit in the database that holds task state and the cost ledger, and the AWS deployment would not use the same API as the local stack.
- **A shared volume or local disk.** Activities can run on any worker, so this ties a task to one host and defeats recovery after a host is lost (failure mode F4).
- **Content-addressed keys, where the key is the hash.** This removes duplicates, but keys become opaque to people debugging a run. Readable keys plus a hash in the ref give the same integrity guarantee.

## Appendix: how LH-204 applies it

The LLM and tool activities were the first code to put the rule into practice. These are the choices it made, with the numbers in `longhaul.durable.acts`.

- **Tool arguments.** A call's arguments travel inline up to 16,000 bytes. A whole model reply may keep 96,000 bytes of arguments inline, because twelve calls of 10 KB are each under the limit and together fill half of the 256 KB cap. A call over either limit is stored and named by `args_ref`. The full list of calls is stored too, as `tool_calls_ref`.
- **Tool output.** `tool_exec` stores the text the model reads and returns a `ToolResult` with a preview of 2,000 characters in `output` and the blob in `output_ref`. The preview is for the Temporal UI. `append_results` takes one `ToolResultSummary` per result that names the blob, so a turn of fifty calls with 15 KB of output each adds a few KB to the history and not 750 KB. A result that no tool made, such as the answer to a `finish` without a summary, carries its short text inline.
- **The guard.** Every activity input and output goes through `support.small`, which calls `assert_small` and turns `PayloadTooLargeError` into a non-retryable `ApplicationError`. The retry policies also list the error type, as a second guard.
- **Blob keys of a model call.** A turn can be asked more than once (the retry after `max_tokens`, a continued `pause_turn`, a Temporal retry), so the response and the transcript are stored under `response-<attempt>` and `transcript-<attempt>`. A `turns` row keeps the key of its response, and a later call does not overwrite it. The attempt number is one more than the rows of the turn, so a key is written again only by the retry of a call that left no row, and then no ref to the earlier content exists.
- **The task statement.** `LlmTurnInput` carries the `TaskSpec`, because the activity builds the system prompt from it on every call. The issue body and the comments have no size limit yet. LH-303 adds one, as the deferred contract change in `docs/architecture.md` says.
